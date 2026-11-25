"""Unit and mock-bridge integration tests for scene_batch workflow."""
from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import sys
from unittest import mock
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS_ROOT = PLUGIN_DIR / "skills" / "tonerelay-scene-batch" / "scripts"
if str(SKILL_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS_ROOT))

from scene_batch.batch_sync import (
    apply_anchor_settings,
    build_sync_plan,
    execute_sync,
    extract_recipe_diff,
    read_anchors_from_lightroom,
)
from scene_batch.contact_audit import run_audit
from scene_batch.crop_math import compute_lr_crop, exif_to_lr_orientation, lr_raw_to_display_box
from scene_batch.lrops_client import (
    LrOpsBridgeClient,
    LrOpsError,
    discover_tonerelay_config,
    is_protected_name,
    load_tonerelay_config,
    validate_wb_patch,
)
from scene_batch.scene_segment import (
    cluster_segments,
    compute_lab_metrics,
    pick_anchors_and_outliers,
    resegment_from_json,
    rgb_to_lab,
    run_segmentation,
    segment_timeline,
)


def _make_synthetic_jpeg(path: Path, base_rgb: tuple[int, int, int], patch_rgb: tuple[int, int, int] | None = None) -> None:
    arr = np.full((120, 160, 3), base_rgb, dtype=np.uint8)
    # Add neutral grey wall area (30%) and foreground patch if requested
    if patch_rgb is not None:
        arr[40:110, 40:120] = patch_rgb
    # Add subtle texture for non-zero sharpness
    arr[::4, ::4] = np.clip(arr[::4, ::4].astype(np.int16) + 8, 0, 255).astype(np.uint8)
    Image.fromarray(arr).save(path, format="JPEG", quality=92)


class _MockBridgeState:
    def __init__(self, tmp_dir: Path):
        self.tmp_dir = tmp_dir
        self.catalog_identity = "catalog-md5-v1:testcat"
        self.access_mode = "full"
        self.photos: dict[str, dict] = {}
        self.settings: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.outputs: dict[str, bytes] = {}
        self.mutations: list[str] = []
        self.fail_batch_at: int | None = None

    def add_master(self, uuid_str: str, file_name: str, flag: str = "pick", settings: dict | None = None, *, width: int = 7008, height: int = 4672, lr_orientation: str = "AB"):
        self.photos[uuid_str] = {
            "uuid": uuid_str,
            "file_name": file_name,
            "copy_name": "",
            "is_virtual_copy": False,
            "master_uuid": uuid_str,
            "edit_protected": False,
            "flag": flag,
            "rating": 0,
            "width": width,
            "height": height,
        }
        self.settings[uuid_str] = dict(settings or {
            "WhiteBalance": "As Shot",
            "Exposure2012": 0.0,
            "Contrast2012": 0,
            "Highlights2012": 0,
            "Shadows2012": 0,
            "orientation": lr_orientation,
        })

    def add_vc(self, uuid_str: str, master_uuid: str, copy_name: str = "TRLR Scene", settings: dict | None = None):
        master = self.photos[master_uuid]
        self.photos[uuid_str] = {
            "uuid": uuid_str,
            "file_name": master["file_name"],
            "copy_name": copy_name,
            "is_virtual_copy": True,
            "master_uuid": master_uuid,
            "edit_protected": is_protected_name(copy_name),
            "flag": master["flag"],
            "rating": master["rating"],
            "width": master["width"],
            "height": master["height"],
        }
        self.settings[uuid_str] = dict(settings or self.settings[master_uuid])


def _make_handler(state: _MockBridgeState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _send_json(self, code: int, obj: dict):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/health":
                self._send_json(200, {"ok": True, "version": "0.8.0.0"})
                return
            if self.path.startswith("/jobs/"):
                jid = self.path.split("/jobs/", 1)[1]
                job = state.jobs.get(jid)
                if not job:
                    self._send_json(404, {"error": "not found"})
                else:
                    self._send_json(200, job)
                return
            if self.path.startswith("/files/") and self.path.endswith("/output"):
                ref = self.path.split("/")[2]
                data = state.outputs.get(ref)
                if data is None:
                    self._send_json(404, {"error": "no output"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send_json(404, {"error": "unknown route"})

        def do_POST(self):
            if self.path != "/jobs":
                self._send_json(404, {"error": "unknown route"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            cmd = body["command"]
            params = body.get("params") or {}
            jid = str(uuid.uuid4())
            result = self._exec_cmd(cmd, params, jid)
            state.jobs[jid] = {"job_id": jid, "command": cmd, "status": "done", "result": result}
            self._send_json(200, {"job_id": jid})

        def _exec_single(self, cmd: str, params: dict, jid: str) -> dict:
            if cmd == "probe_client":
                return {
                    "ok": True,
                    "data": {
                        "catalog_open": True,
                        "catalog_authorized": True,
                        "access_mode": state.access_mode,
                        "catalog_identity": state.catalog_identity,
                    },
                }
            if cmd == "list_folder_photos":
                return {"ok": True, "data": {"photos": list(state.photos.values()), "complete": True}}
            if cmd == "get_develop_snapshot":
                uid = params["photo_uuid"]
                return {"ok": True, "data": {"photo": state.photos[uid], "settings": dict(state.settings[uid])}}
            if cmd == "create_virtual_copy":
                src = params["photo_uuid"]
                name = params.get("name", "TRLR Scene")
                new_uid = f"vc-{uuid.uuid4().hex[:8]}"
                state.add_vc(new_uid, src, copy_name=name)
                state.mutations.append(f"create_vc:{src}->{new_uid}")
                return {"ok": True, "data": {"source": state.photos[src], "copy": state.photos[new_uid]}}
            if cmd == "apply_candidate":
                uid = params["photo_uuid"]
                p = state.photos[uid]
                if not p["is_virtual_copy"]:
                    return {"ok": False, "error": "apply_candidate refuses master"}
                if is_protected_name(p["copy_name"]):
                    return {"ok": True, "data": {"photo": p, "status": "conflict", "code": "copy_protected", "applied": False}}
                state.settings[uid].update(params["settings"])
                if "Temperature" in params["settings"] or "Tint" in params["settings"]:
                    state.settings[uid]["WhiteBalance"] = "Custom"
                state.mutations.append(f"apply:{uid}")
                return {"ok": True, "data": {"photo": p, "before": {}, "after": state.settings[uid]}}
            if cmd == "export_preview":
                uid = params["photo_uuid"]
                buf = io.BytesIO()
                # Deterministic high-frequency image (~180-220 KB at q=95) so max_file_kb tests real compression
                rng = np.random.default_rng(42)
                noisy = rng.integers(30, 225, size=(480, 720, 3), dtype=np.uint8)
                Image.fromarray(noisy).save(buf, format="JPEG", quality=95)
                state.outputs[jid] = buf.getvalue()
                state.mutations.append(f"export:{uid}")
                return {"ok": True, "data": {"photo": state.photos[uid], "output_ref": jid, "color_space": "sRGB"}}
            return {"ok": False, "error": f"Unknown cmd {cmd}"}

        def _exec_cmd(self, cmd: str, params: dict, jid: str) -> dict:
            if cmd != "command_batch":
                return self._exec_single(cmd, params, jid)
            entries = params.get("commands") or []
            results = []
            for idx, entry in enumerate(entries, start=1):
                if state.fail_batch_at == idx:
                    state.fail_batch_at = None
                    return {
                        "ok": False,
                        "error": f"Simulated failure at {idx}",
                        "failed_at_index": idx,
                        "results_so_far": results,
                    }
                res = self._exec_single(entry["command"], entry.get("params") or {}, f"{jid}-{idx}")
                if not res.get("ok"):
                    return {
                        "ok": False,
                        "error": res.get("error"),
                        "failed_at_index": idx,
                        "results_so_far": results,
                    }
                results.append({"command": entry["command"], "ok": True, "data": res["data"]})
            return {"ok": True, "data": {"results": results}}

    return Handler


class SceneBatchWorkflowTests(unittest.TestCase):
    def test_rgb_to_lab_and_red_patch_immunity(self):
        white = rgb_to_lab(np.full((4, 4, 3), 255, dtype=np.uint8))
        self.assertAlmostEqual(float(white[0, 0, 0]), 100.0, delta=0.5)
        self.assertAlmostEqual(float(white[0, 0, 1]), 0.0, delta=0.5)
        self.assertAlmostEqual(float(white[0, 0, 2]), 0.0, delta=0.5)

        # 60% neutral wall + 40% bright red clothing patch should still read neutral near 0
        arr = np.full((100, 100, 3), (185, 185, 185), dtype=np.uint8)
        arr[0:40, :] = (230, 30, 30)
        m = compute_lab_metrics(arr)
        self.assertLess(abs(m["neutral_a"]), 2.0)
        self.assertLess(abs(m["neutral_b"]), 2.0)

    def test_timeline_segmentation_clustering_and_outlier(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "photos"
            out = root / "seg"
            inp.mkdir()

            # 40-photo shoot: RoomA (10, cool), RoomB (8, warm), RoomA (10, cool), Kitchen (3, green), RoomB (9, warm)
            layout = (
                [("cool", (165, 180, 210))] * 10
                + [("warm", (210, 185, 145))] * 8
                + [("cool", (165, 180, 210))] * 10
                + [("green", (160, 200, 165))] * 3
                + [("warm", (210, 185, 145))] * 9
            )
            for idx, (kind, rgb) in enumerate(layout, start=1):
                col = (252, 250, 255) if idx == 4 else rgb  # intentional overexposed outlier in RoomA
                _make_synthetic_jpeg(inp / f"DSC{1000 + idx:04d}.jpg", col)

            res = run_segmentation(inp, out, min_segment=3, time_gap_sec=180.0, wb_jump_threshold=5.0, cluster_threshold=6.0)
            self.assertEqual(res["photo_count"], 40)
            # 5 timeline segments clustered into 3 lighting scenes (cool, warm, green)
            self.assertGreaterEqual(res["segment_count"], 4)
            self.assertEqual(res["scene_count"], 3)
            self.assertTrue(Path(res["color_barcode"]).is_file())
            self.assertTrue(Path(res["scene_overview"]).is_file())

            scenes_doc = json.loads(Path(res["scenes_json"]).read_text(encoding="utf-8"))
            flagged = {p["stem"]: p["outlier_reasons"] for p in scenes_doc["photos"] if p["is_outlier"]}
            self.assertIn("DSC1004", flagged)
            self.assertTrue(any(r in flagged["DSC1004"] for r in ("overexposed", "highlight_clip")))

            # Delete original JPEGs and prove resegment_from_json works with zero photo-folder disk reads
            for jpg in inp.glob("*.jpg"):
                jpg.unlink()
            reseg_out = root / "reseg"
            reseg = resegment_from_json(
                Path(res["scenes_json"]), reseg_out,
                min_segment=3, time_gap_sec=180.0, wb_jump_threshold=5.0, cluster_threshold=6.0,
            )
            self.assertEqual(reseg["photo_count"], 40)
            self.assertEqual(reseg["scene_count"], 3)
            self.assertTrue(Path(reseg["color_barcode"]).is_file())
            self.assertIsNone(reseg["scene_overview"])

    def test_wb_guard_and_protected_names(self):
        self.assertTrue(is_protected_name("交付版-勿改"))
        self.assertTrue(is_protected_name("Read-Only Copy"))
        self.assertFalse(is_protected_name("TRLR Scene S01"))

        with self.assertRaises(ValueError):
            validate_wb_patch({"Temperature": 4500}, {"WhiteBalance": "As Shot"})
        validate_wb_patch({"Temperature": 4500, "Tint": 8}, {"WhiteBalance": "As Shot"})

    def test_crop_math_orientations_and_rotated_corners(self):
        import math
        self.assertEqual(exif_to_lr_orientation(1), "AB")
        self.assertEqual(exif_to_lr_orientation(6), "BC")
        self.assertEqual(exif_to_lr_orientation(3), "CD")
        self.assertEqual(exif_to_lr_orientation(8), "DA")

        # Portrait DA (EXIF 8): trimming top 20% of displayed image must map to CropRight == 0.8
        crop_da = compute_lr_crop({"box": [0.0, 0.2, 1.0, 1.0], "aspect": "free"}, disp_width=2000, disp_height=3000, lr_orientation="DA")
        self.assertAlmostEqual(crop_da["CropRight"], 0.8, places=5)
        self.assertAlmostEqual(crop_da["CropLeft"], 0.0, places=5)
        self.assertAlmostEqual(crop_da["CropTop"], 0.0, places=5)
        self.assertAlmostEqual(crop_da["CropBottom"], 1.0, places=5)

        # Rotated corners must remain strictly inside frame for AB, DA, BC, CD across negative/positive angles & edge centers
        for ori, (w, h) in (("AB", (7008, 4672)), ("DA", (4672, 7008)), ("BC", (4672, 7008)), ("CD", (7008, 4672))):
            for angle in (-12.0, -3.5, 0.0, 4.0, 11.5):
                spec = {"angle": angle, "zoom": 1.35, "center_x": 0.88, "center_y": 0.14, "aspect": "original"}
                c = compute_lr_crop(spec, disp_width=w, disp_height=h, lr_orientation=ori)
                u0, v0, u1, v1 = lr_raw_to_display_box(c["CropLeft"], c["CropTop"], c["CropRight"], c["CropBottom"], ori)
                cx = 0.5 * (u0 + u1) * w
                cy = 0.5 * (v0 + v1) * h
                hw = 0.5 * (u1 - u0) * w
                hh = 0.5 * (v1 - v0) * h
                rad = math.radians(angle)
                ca, sa = math.cos(rad), math.sin(rad)
                for sx in (-1.0, 1.0):
                    for sy in (-1.0, 1.0):
                        px = cx + (sx * hw * ca - sy * hh * sa)
                        py = cy + (sx * hw * sa + sy * hh * ca)
                        self.assertGreaterEqual(px, -1e-3, msg=(ori, angle, px, py))
                        self.assertLessEqual(px, w + 1e-3, msg=(ori, angle, px, py))
                        self.assertGreaterEqual(py, -1e-3, msg=(ori, angle, px, py))
                        self.assertLessEqual(py, h + 1e-3, msg=(ori, angle, px, py))

    def test_mock_bridge_end_to_end_and_resume(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raw"
            seg_dir = root / "seg"
            exp_dir = root / "exports"
            aud_dir = root / "audit"
            raw_dir.mkdir()

            state = _MockBridgeState(root)
            for i in range(1, 7):
                stem = f"DSC200{i}"
                rgb = (165, 180, 210) if i <= 3 else (210, 185, 145)
                _make_synthetic_jpeg(raw_dir / f"{stem}.jpg", rgb)
                if i == 5:
                    state.add_master(f"m-{i}", f"{stem}.jpg", flag="pick", width=4672, height=7008, lr_orientation="DA")
                else:
                    state.add_master(f"m-{i}", f"{stem}.jpg", flag="pick")

            # Existing anchor VC for S01 (DSC2001) and S02 (DSC2004), plus one protected VC on DSC2006
            state.add_vc("vc-anchor-1", "m-1", copy_name="TRLR Anchor 1", settings={
                "WhiteBalance": "Custom", "Temperature": 4600, "Tint": 6,
                "Exposure2012": 0.45, "Clarity2012": 8, "CropTop": 0.1,
            })
            state.add_vc("vc-anchor-2", "m-4", copy_name="TRLR Anchor 2", settings={
                "WhiteBalance": "Custom", "Temperature": 3800, "Tint": -4,
                "Exposure2012": 0.35, "Clarity2012": 8,
            })
            state.add_vc("vc-prot-6", "m-6", copy_name="交付版-保护", settings={"Exposure2012": 0.0})

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = LrOpsBridgeClient(
                    base=f"http://127.0.0.1:{port}",
                    token=None,
                    catalog_identity=state.catalog_identity,
                    access_mode="full",
                )
                # Wrong catalog identity must fail
                bad_client = LrOpsBridgeClient(
                    base=f"http://127.0.0.1:{port}",
                    token=None,
                    catalog_identity="catalog-md5-v1:wrong",
                    access_mode="full",
                )
                with self.assertRaises(LrOpsError):
                    bad_client.probe()

                seg_res = run_segmentation(raw_dir, seg_dir, min_segment=2, wb_jump_threshold=4.0)
                scenes_doc = json.loads(Path(seg_res["scenes_json"]).read_text(encoding="utf-8"))
                self.assertEqual(scenes_doc["scene_count"], 2)

                recipes = read_anchors_from_lightroom(
                    client,
                    scenes_doc,
                    {"S01": "vc-anchor-1", "S02": "vc-anchor-2"},
                    include_crop=False,
                )
                # CropTop excluded by default, Temperature & Tint preserved together
                s01_recipe = recipes["scenes"]["S01"]["settings"]
                self.assertNotIn("CropTop", s01_recipe)
                self.assertEqual(s01_recipe["Temperature"], 4600)
                self.assertEqual(s01_recipe["Tint"], 6)

                catalog_photos = client.list_folder_photos(["f1"], include_virtual_copies=True)
                plan = build_sync_plan(
                    scenes_doc,
                    recipes,
                    catalog_photos,
                    flag_filter="pick",
                    overrides={
                        "DSC2002": {
                            "Exposure2012_delta": -0.25,
                            "crop": {"angle": -1.5, "zoom": 1.3, "center_x": 0.45, "center_y": 0.4, "aspect": "original"},
                        },
                        "DSC2005": {
                            "crop": {"angle": 2.0, "zoom": 1.25, "center_x": 0.5, "center_y": 0.35, "aspect": "original"},
                        },
                    },
                )
                # DSC2006 skipped because only VC is protected
                self.assertEqual(plan["item_count"], 5)
                self.assertEqual(plan["skipped_count"], 1)
                self.assertEqual(plan["skipped"][0]["stem"], "DSC2006")

                state_path = root / "sync_state.json"
                # Dry-run produces zero mutations
                dry = execute_sync(client, plan, state_path, output_dir=exp_dir, dry_run=True)
                self.assertTrue(dry["dry_run"])
                self.assertEqual(len(state.mutations), 0)

                # Simulate partial batch failure at index 2 on first run -> test crash resume
                state.fail_batch_at = 2
                with self.assertRaises(LrOpsError):
                    execute_sync(client, plan, state_path, output_dir=exp_dir, batch_size=3)

                saved = json.loads(state_path.read_text(encoding="utf-8"))
                # All 3 missing VCs were persisted and entry 1 applied before index 2 failed
                vc_creations_after_crash = [m for m in state.mutations if m.startswith("create_vc:")]
                self.assertEqual(len(vc_creations_after_crash), 3)
                self.assertIn("applied_hash", saved["items"]["DSC2001"])

                # Resume without fail -> must NOT create duplicate VCs, finishes remaining apply & all 5 exports
                res = execute_sync(client, plan, state_path, output_dir=exp_dir, batch_size=3)
                self.assertEqual(res["vcs_created"], 0)
                self.assertEqual(res["exported_count"], 5)
                self.assertEqual(res["mismatches_count"], 0)
                s2002 = state.settings[saved["items"]["DSC2002"]["vc_uuid"]]
                self.assertAlmostEqual(s2002["Exposure2012"], 0.20, places=2)
                self.assertAlmostEqual(s2002["CropAngle"], -1.5, places=2)
                self.assertGreater(s2002["CropLeft"], 0.0)
                s2005 = state.settings[saved["items"]["DSC2005"]["vc_uuid"]]
                self.assertAlmostEqual(s2005["CropAngle"], 2.0, places=2)
                self.assertLess(s2005["CropRight"], 1.0)

                # Second run with no changes must be a complete no-op
                noop = execute_sync(client, plan, state_path, output_dir=exp_dir, batch_size=3)
                self.assertEqual(noop["vcs_created"], 0)
                self.assertEqual(noop["applied_count"], 0)
                self.assertEqual(noop["exported_count"], 0)

                # Changing max_file_kb with --export-only re-exports all 5 strictly under byte cap without re-applying
                capped = execute_sync(
                    client, plan, state_path, output_dir=exp_dir,
                    export_only=True, max_file_kb=120, min_quality=55,
                )
                self.assertEqual(capped["applied_count"], 0)
                self.assertEqual(capped["exported_count"], 5)
                for p_jpg in exp_dir.glob("*.jpg"):
                    self.assertLessEqual(p_jpg.stat().st_size, 120 * 1000)

                # Run contact audit with --grid on the 5 exported files (single-pass sequential workers=1)
                aud = run_audit(exp_dir, aud_dir, scenes_path=Path(seg_res["scenes_json"]), cols=3, rows=2, workers=1, grid=True)
                self.assertEqual(aud["photo_count"], 5)
                self.assertTrue(Path(aud["audit_metrics"]).is_file())
                metrics_json = json.loads(Path(aud["audit_metrics"]).read_text(encoding="utf-8"))
                self.assertEqual(metrics_json["photos"][0]["dimensions"], [720, 480])
                self.assertNotIn("_thumb", metrics_json["photos"][0])
                self.assertEqual(len(aud["audit_sheets"]), 1)
                self.assertTrue(Path(aud["audit_sheets"][0]).is_file())
            finally:
                server.shutdown()
                server.server_close()

    def test_discover_and_load_tonerelay_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit_cfg = root / "explicit.json"
            explicit_cfg.write_text(json.dumps({
                "targets": {
                    "active": "main",
                    "targets": [{"id": "main", "base": "http://127.0.0.1:18888/", "token": "tok-exp", "access_mode": "apply_write", "catalog_identity": "cat-exp"}],
                }
            }), encoding="utf-8")

            env_cfg = root / "env_config.json"
            env_cfg.write_text(json.dumps({
                "targets": {
                    "active": "env1",
                    "targets": [{"id": "env1", "base": "http://127.0.0.1:19999", "token": "tok-env", "access_mode": "apply_write", "catalog_identity": "cat-env"}],
                }
            }), encoding="utf-8")

            data_root = root / "app_data"
            (data_root / "workbench").mkdir(parents=True)
            (data_root / "workbench" / "config.json").write_text(json.dumps({
                "targets": {
                    "active": "dr",
                    "targets": [{"id": "dr", "base": "http://127.0.0.1:17788", "token": "tok-dr"}],
                }
            }), encoding="utf-8")

            # Explicit config wins over env vars
            with mock.patch.dict(os.environ, {"TONERELAY_CONFIG": str(env_cfg)}, clear=False):
                self.assertEqual(discover_tonerelay_config(explicit_cfg), explicit_cfg)
                loaded = load_tonerelay_config(explicit_cfg)
                self.assertEqual(loaded["base"], "http://127.0.0.1:18888")
                self.assertEqual(loaded["token"], "tok-exp")
                self.assertEqual(loaded["catalog_identity"], "cat-exp")

                # Explicit non-existent path must NOT leak env config
                missing = root / "does_not_exist.json"
                self.assertIsNone(discover_tonerelay_config(missing))
                with mock.patch.dict(os.environ, {"LROPS_BASE": "http://127.0.0.1:17799/", "LR_XMP_MIMIC_LOCAL_TOKEN": "tok-fb"}, clear=False):
                    fb = load_tonerelay_config(missing)
                    self.assertEqual(fb["base"], "http://127.0.0.1:17799")
                    self.assertEqual(fb["token"], "tok-fb")
                    self.assertIsNone(fb["catalog_identity"])

            # TONERELAY_CONFIG env discovery
            with mock.patch.dict(os.environ, {"TONERELAY_CONFIG": str(env_cfg)}, clear=True):
                self.assertEqual(discover_tonerelay_config(), env_cfg)
                loaded_env = load_tonerelay_config()
                self.assertEqual(loaded_env["token"], "tok-env")

            # TONERELAY_DATA_ROOT env discovery
            with mock.patch.dict(os.environ, {"TONERELAY_DATA_ROOT": str(data_root)}, clear=True):
                self.assertEqual(discover_tonerelay_config(), data_root / "workbench" / "config.json")
                loaded_dr = load_tonerelay_config()
                self.assertEqual(loaded_dr["base"], "http://127.0.0.1:17788")
                self.assertEqual(loaded_dr["token"], "tok-dr")

            # Empty environment without HOME/USERPROFILE must fall back cleanly instead of raising
            empty_cwd = root / "empty_cwd"
            empty_cwd.mkdir()
            prev_cwd = os.getcwd()
            try:
                os.chdir(empty_cwd)
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertIsNone(discover_tonerelay_config())
                    fb_empty = load_tonerelay_config()
                    self.assertEqual(fb_empty["base"], "http://127.0.0.1:17777")
                    self.assertIsNone(fb_empty["token"])
            finally:
                os.chdir(prev_cwd)

    def test_codex_plugin_bundles_both_skills_and_strips_pycache(self):
        plugin_script = PLUGIN_DIR / "scripts" / "tonerelay_codex_plugin.py"
        spec = importlib.util.spec_from_file_location("codex_plugin_for_scene_batch_test", plugin_script)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "marketplace"
            runtime = root / "runtime"
            python_exe = runtime / "active" / ".venv" / "Scripts" / "python.exe"
            python_exe.parent.mkdir(parents=True)
            python_exe.write_bytes(b"interpreter")

            manifest = source / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"name": plugin.NAME, "skills": "./skills/", "mcpServers": "./.mcp.json"}),
                encoding="utf-8",
            )
            (source / ".mcp.json").write_text('{"command":"/bin/sh"}', encoding="utf-8")
            (source / "scripts").mkdir()
            (source / "scripts" / "control.py").write_text("print('control')", encoding="utf-8")

            shutil.copytree(PLUGIN_DIR / "skills" / plugin.NAME, source / "skills" / plugin.NAME)
            shutil.copytree(
                PLUGIN_DIR / "skills" / "tonerelay-scene-batch",
                source / "skills" / "tonerelay-scene-batch",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            pycache = source / "skills" / "tonerelay-scene-batch" / "scripts" / "scene_batch" / "__pycache__"
            pycache.mkdir(parents=True, exist_ok=True)
            (pycache / "dummy.pyc").write_bytes(b"cache")

            with mock.patch.object(plugin.bootstrap, "IS_WINDOWS", True):
                report = plugin.prepare(source, destination, runtime)

            self.assertEqual(report["status"], "prepared")
            package = destination / "plugins" / plugin.NAME
            self.assertTrue((package / "skills" / plugin.NAME / "SKILL.md").is_file())
            self.assertTrue((package / "skills" / "tonerelay-scene-batch" / "SKILL.md").is_file())
            self.assertTrue((package / "skills" / "tonerelay-scene-batch" / "agents" / "openai.yaml").is_file())
            self.assertTrue((package / "skills" / "tonerelay-scene-batch" / "references" / "workflow.md").is_file())
            self.assertTrue((package / "skills" / "tonerelay-scene-batch" / "scripts" / "scene_batch" / "cli.py").is_file())
            self.assertFalse((package / "skills" / "tonerelay-scene-batch" / "scripts" / "scene_batch" / "__pycache__").exists())

    def test_reset_curves_preferred_anchor_apply_anchor_and_progress(self):
        linear = [0, 0, 255, 255]
        master = {
            "Exposure2012": 0.0,
            "ToneCurvePV2012": linear,
            "ToneCurvePV2012Red": linear,
            "ToneCurvePV2012Green": linear,
            "ToneCurvePV2012Blue": linear,
        }
        anchor = dict(master, Exposure2012=0.25, WhiteBalance="Custom", Temperature=4500, Tint=10)
        recipe_reset = extract_recipe_diff(anchor, master, reset_curves=True)
        self.assertEqual(recipe_reset["ToneCurvePV2012Red"], linear)
        self.assertEqual(recipe_reset["ToneCurvePV2012Blue"], linear)
        recipe_omit = extract_recipe_diff(anchor, master, reset_curves=False)
        self.assertNotIn("ToneCurvePV2012Red", recipe_omit)

        photos = [
            {"stem": f"P{i:02d}", "neutral_a": 0.0, "neutral_b": 0.0, "neutral_l": 50.0,
             "median_l": 50.0, "clip_high_pct": 0.0, "clip_low_pct": 0.0, "sharpness": 100.0 if i == 0 else 50.0}
            for i in range(4)
        ]
        scenes = [{"scene_id": "S01", "stems": [p["stem"] for p in photos]}]
        pick_anchors_and_outliers(scenes, photos, preferred_stems={"P02", "P03"})
        self.assertIn(scenes[0]["anchor_stem"], {"P02", "P03"})
        self.assertTrue(scenes[0]["anchor_in_preferred"])
        self.assertEqual(scenes[0]["preferred_count"], 2)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img_dir = root / "in"
            seg_dir = root / "seg"
            exp_dir = root / "exp"
            img_dir.mkdir()
            for idx in range(1, 3):
                arr = np.full((120, 180, 3), 140, dtype=np.uint8)
                Image.fromarray(arr).save(img_dir / f"DSC300{idx}.jpg", quality=92)

            state = _MockBridgeState(root)
            state.add_master("m-1", "DSC3001.jpg", flag="pick", width=180, height=120)
            state.add_master("m-2", "DSC3002.jpg", flag="pick", width=180, height=120)
            state.settings["m-1"]["ToneCurvePV2012Red"] = linear
            state.settings["m-2"]["ToneCurvePV2012Red"] = linear
            state.add_vc(
                "vc-1",
                "m-1",
                copy_name="TRLR Anchor",
                settings={"WhiteBalance": "As Shot", "Exposure2012": 0.0, "ToneCurvePV2012Red": [0, 0, 128, 138, 255, 255]},
            )
            state.settings["vc-1"].pop("Temperature", None)
            state.settings["vc-1"].pop("Tint", None)

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = LrOpsBridgeClient(
                    base=f"http://127.0.0.1:{port}",
                    token=None,
                    catalog_identity=state.catalog_identity,
                    access_mode="full",
                )
                render_jpg = root / "anchor_preview.jpg"
                app_res = apply_anchor_settings(
                    client,
                    {"Temperature": 4800, "Tint": 12, "Exposure2012": 0.2, "ToneCurvePV2012Red": linear},
                    vc_uuid="vc-1",
                    render_path=render_jpg,
                    render_max_edge=600,
                )
                self.assertTrue(app_res["ok"])
                self.assertEqual(app_res["white_balance"], "Custom")
                self.assertEqual(app_res["mismatches_count"], 0)
                self.assertTrue(render_jpg.is_file())

                seg = run_segmentation(img_dir, seg_dir, window=1, min_segment=1, workers=1, preferred_stems={"DSC3001"})
                scenes_doc = json.loads(Path(seg["scenes_json"]).read_text(encoding="utf-8"))
                self.assertEqual(scenes_doc["scenes"][0]["anchor_stem"], "DSC3001")

                recipes = read_anchors_from_lightroom(client, scenes_doc, {"S01": "vc-1"})
                self.assertEqual(recipes["scenes"]["S01"]["settings"]["ToneCurvePV2012Red"], linear)

                plan = build_sync_plan(scenes_doc, recipes, list(state.photos.values()), flag_filter="pick")
                progress_buf = io.StringIO()
                state_path = root / "sync_state.json"
                sync_res = execute_sync(client, plan, state_path, output_dir=exp_dir, progress_stream=progress_buf)
                self.assertEqual(sync_res["exported_count"], 2)
                self.assertIn("[scene_batch sync] phase=apply", progress_buf.getvalue())
                self.assertIn("[scene_batch sync] phase=export", progress_buf.getvalue())
                saved = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["progress"]["phase"], "done")
                self.assertEqual(saved["progress"]["done"], 2)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
