"""Session-layer execution, state, and measurement protocol tests.

用真实 Store（临时目录）+ 假 lr-ops 桥 + 真实 Workbench 驱动 McpServer，
不需要 Lightroom。假桥刻意模拟两个 session-b 实测故障：
  * export_preview 在 long_edge > 2048 时失败（超大原图场景）；
  * apply_candidate 在缺省 WB 时把 IncrementalTemperature/Tint 钳位到 0
    （连续轮次白平衡漂移场景）——WB 守护必须让它不发生。
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.runtime import Workbench  # noqa: E402
from workbench.lrops import LrOpsError  # noqa: E402
from workbench.store import Store  # noqa: E402
from workbench_mcp.server import McpServer, TOOLS  # noqa: E402
from workbench_mcp.xmp_full import serialize_full_settings  # noqa: E402

CATALOG_IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"


def jpeg_bytes(rgb, size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="JPEG")
    return buf.getvalue()


class FakeLrOps:
    """带故障注入的桥替身。"""

    def __init__(self):
        self.settings = {"IncrementalTemperature": 5, "IncrementalTint": -3}
        self.baseline = dict(self.settings)
        self.copy_count = 0
        self.outputs = {}
        self._next = 0
        self.export_calls = []      # 每次 export_preview 的 long_edge
        self.export_edge_limit = None  # 超过即失败，模拟超大原图导出
        self.clamp_wb_when_absent = True  # 模拟 WB 钳位故障
        # Keep the default fixtures small, then let scaling regressions opt in
        # to a genuine 2048px source without changing every existing assertion.
        self.export_size = (64, 48)
        self.thumbnail_size = (64, 48)

    def reachable(self):
        return True

    def _handle(self, prefix):
        self._next += 1
        return f"{prefix}-{self._next}"

    def fetch_output(self, output_ref, destination):
        name, content = self.outputs[output_ref]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def run(self, command, params=None, timeout=None):
        params = params or {}
        assert params.get("expected_access_mode") == "full"
        if command == "probe_client":
            assert "expected_catalog_identity" not in params
            return {"access_mode": "full", "catalog_identity": CATALOG_IDENTITY,
                    "catalog_id": "test.lrcat", "sandbox": False,
                    "catalog_authorized": True,
                    "capabilities": {"expected_catalog_identity": True,
                                     "expected_access_mode": True,
                                     "develop_snapshot": True}}
        assert params.get("expected_catalog_identity") == CATALOG_IDENTITY
        if command == "capture_baseline":
            if params["photo_uuid"].startswith("copy-"):
                self.baseline = dict(self.settings)
            return {"baseline": dict(self.settings)}
        if command == "create_virtual_copy":
            self.copy_count += 1
            return {"source": {"uuid": params["photo_uuid"]},
                    "copy": {"uuid": f"copy-{self.copy_count}"}}
        if command == "apply_candidate":
            incoming = dict(params["settings"])
            wb_keys = ("Temperature", "Tint", "IncrementalTemperature", "IncrementalTint")
            before = dict(self.settings)
            self.settings.update(incoming)
            if self.clamp_wb_when_absent and not set(incoming) & set(wb_keys):
                # 故障复刻：缺省 WB 时被钳位归零
                self.settings["IncrementalTemperature"] = 0
                self.settings["IncrementalTint"] = 0
            return {"before": before, "after": dict(self.settings)}
        if command == "get_develop_state":
            return {"settings": {k: v for k, v in self.settings.items()
                                 if isinstance(v, (int, float))}}
        if command == "get_develop_snapshot":
            return {"settings": {
                **self.settings,
                "GrainAmount": 25, "GrainSize": 30,
                "ToneCurvePV2012": [0, 0, 64, 48, 255, 255],
                "SaturationAdjustmentOrange": -12,
                "ConvertToGrayscale": False,
                "WhiteBalance": "Custom",
                "ProcessVersion": "6.7",
                "HasSettings": True,  # DENY: 不应进入 XMP
                "MaskGroupBasedCorrections": [{"what": "Mask", "enabled": True}],
            }}
        if command == "reset_to_baseline":
            self.settings = dict(self.baseline)
            return {"settings": dict(self.settings)}
        if command == "export_preview":
            edge = params.get("long_edge", 2048)
            self.export_calls.append(edge)
            if self.export_edge_limit is not None and edge > self.export_edge_limit:
                raise LrOpsError("Export finished but the expected file is missing: /x.jpg")
            ref = self._handle("out")
            rgb = {"src": (60, 50, 40), "ref": (180, 170, 150)}.get(
                params["photo_uuid"], (120, 80, 40))
            self.outputs[ref] = (params["photo_uuid"] + ".jpg",
                                 jpeg_bytes(rgb, size=self.export_size))
            return {"output_ref": ref, "long_edge": edge, "color_space": "sRGB"}
        if command == "request_jpeg_thumbnail":
            ref = self._handle("thumb")
            self.outputs[ref] = (params["photo_uuid"] + ".jpg",
                                 jpeg_bytes((90, 60, 30), size=self.thumbnail_size))
            return {"output_ref": ref, "method": "requestJpegThumbnail",
                    "purpose": "fast", "freshness": "unverified"}
        raise AssertionError(f"unexpected command {command}")


class SessionToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.fake = FakeLrOps()
        self.wb = Workbench(self.store, self.fake)
        self.server = McpServer(self.wb)
        self.session = self.wb.create_session("src", "ref", "style_match")

    def call(self, tool, **arguments):
        resp = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": arguments}})
        result = resp["result"]
        if result["isError"]:
            raise AssertionError(result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def call_error(self, tool, **arguments):
        resp = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": arguments}})
        assert resp["result"]["isError"], resp
        return resp["result"]["content"][0]["text"]

    # ---- 工具登记 ------------------------------------------------------------

    def test_new_tools_registered(self):
        names = {t["name"] for t in TOOLS}
        for expected in ("trlr_export_image", "trlr_image_measure",
                         "trlr_get_full_settings", "trlr_round_diff",
                         "trlr_replay_round", "trlr_export_session_context"):
            self.assertIn(expected, names)
        self.assertNotIn("trlr_image_stats", names)
        self.assertNotIn("trlr_image_inspect", names)
        self.assertNotIn("trlr_reference_match", names)

    def test_presentation_budget_caps_new_session_slots_and_later_round(self):
        updated = self.call("trlr_set_presentation_config", edit_render_max_edge=256)
        self.assertEqual(updated["edit_render_max_edge"], 256)
        start = len(self.fake.export_calls)

        session = self.call("trlr_create_session", source_uuid="src", reference_uuid="ref",
                            mode="color_match")
        self.assertEqual(self.fake.export_calls[start:], [256, 256])

        self.call("trlr_apply_settings", session_id=session["session_id"],
                  settings={"Exposure2012": 0.5})
        self.assertEqual(self.fake.export_calls[-1], 256)

    # ---- WB 守护 ---------------------------------------------------------------

    def test_apply_without_wb_keeps_wb_for_10_rounds(self):
        """不带 WB 连跑 10 轮，回读 WB 无漂移。"""
        for _ in range(10):
            record = self.call("trlr_apply_settings",
                               session_id=self.session["session_id"],
                               settings={"Exposure2012": 0.3})
        self.assertEqual(self.fake.settings["IncrementalTemperature"], 5)
        self.assertEqual(self.fake.settings["IncrementalTint"], -3)
        self.assertEqual(record["wb_written"],
                         {"IncrementalTemperature": 5, "IncrementalTint": -3})
        self.assertEqual(record["wb_guard"]["wb_fill"], "filled_from_current")

    def test_apply_with_explicit_wb_not_touched(self):
        record = self.call("trlr_apply_settings",
                           session_id=self.session["session_id"],
                           settings={"IncrementalTemperature": 12})
        self.assertEqual(record["wb_guard"]["wb_fill"], "caller_explicit")
        self.assertEqual(record["wb_written"]["IncrementalTemperature"], 12)
        self.assertEqual(record["wb_written"]["IncrementalTint"], -3)  # 守护已补齐

    def test_first_round_fills_wb_from_source(self):
        fresh = self.wb.create_session("src", "ref", "color_match")
        record = self.call("trlr_apply_settings",
                           session_id=fresh["session_id"],
                           settings={"Contrast2012": 10})
        self.assertEqual(record["wb_guard"]["wb_fill"], "filled_from_source")
        self.assertEqual(self.fake.settings["IncrementalTemperature"], 5)

    # ---- 导出 ------------------------------------------------------------------

    def test_export_live_fallback_records_actual_edge(self):
        self.fake.export_edge_limit = 2048  # 7008px 场景：>2048 必失败
        result = self.call("trlr_export_image", photo_uuid="u-big", max_edge=4096)
        self.assertEqual(result["actual_edge"], 2048)
        self.assertEqual(result["basis"], "live_export")
        self.assertIn("attempts", result)
        self.assertFalse(result["attempts"][0]["ok"])
        self.assertTrue(result["attempts"][-1]["ok"])
        self.assertEqual(self.fake.export_calls[-2:], [4096, 2048])  # 前面是建会话的槽位渲染
        self.assertEqual(len(result["sha256"]), 64)
        self.assertTrue(Path(result["export_path"]).exists())
        self.assertEqual(result["artifact_ref"]["sha256"], result["sha256"])

    def test_live_export_download_destination_is_unlocked_and_removed(self):
        destinations = []
        fetch = self.fake.fetch_output

        def check_destination(ref, destination):
            destinations.append(Path(destination))
            self.assertFalse(destination.exists())
            return fetch(ref, destination)

        with patch.object(self.fake, "fetch_output", side_effect=check_destination):
            self.call("trlr_export_image", photo_uuid="u-catalog", max_edge=1024)
        self.assertEqual(len(destinations), 1)
        self.assertFalse(destinations[0].parent.exists())

    def test_live_export_partial_downloads_are_removed_on_failure(self):
        destinations = []

        def fail_download(ref, destination):
            destinations.append(Path(destination))
            destination.write_bytes(b"partial transfer")
            raise LrOpsError("injected transfer failure")

        with patch.object(self.fake, "fetch_output", side_effect=fail_download):
            self.assertIn("injected transfer failure", self.call_error(
                "trlr_export_image", photo_uuid="u-catalog", max_edge=1024))
        self.assertEqual(len(destinations), 2)
        self.assertTrue(all(not path.parent.exists() for path in destinations))

    def test_export_defaults_to_full_resolution(self):
        """不传 max_edge 默认全尺寸：向桥请求 long_edge=0，服务端不再缩放。"""
        self.fake.export_size = (2048, 1536)
        result = self.call("trlr_export_image", photo_uuid="u-catalog")
        self.assertIsNone(result["requested_max_edge"])
        self.assertEqual(result["basis"], "live_export")
        self.assertEqual(self.fake.export_calls[-1], 0)
        self.assertEqual(result["dimensions"], [2048, 1536])
        self.assertEqual(result["actual_edge"], 2048)
        with Image.open(result["export_path"]) as image:
            self.assertEqual(image.size, (2048, 1536))

    def test_export_round_reuses_stored_preview(self):
        self.call("trlr_apply_settings", session_id=self.session["session_id"],
                  settings={"Exposure2012": 0.5})
        calls_before = len(self.fake.export_calls)
        result = self.call("trlr_export_image",
                           session_id=self.session["session_id"], round=1,
                           max_edge=512, format="jpeg")
        self.assertEqual(result["basis"], "stored_round_preview")
        self.assertEqual(len(self.fake.export_calls), calls_before)  # 没惊动 LR
        self.assertEqual(result["format"], "jpeg")
        self.assertTrue(result["export_path"].endswith(".jpg"))

    def test_export_jpeg_1024_scales_2048_source_and_keeps_catalog_round_none(self):
        """A catalog export with the default ``round=None`` uses built-in round."""
        self.fake.export_size = (2048, 1536)
        result = self.call("trlr_export_image", photo_uuid="u-catalog",
                           max_edge=1024, format="jpeg")
        self.assertEqual(result["basis"], "live_export")
        self.assertEqual(result["format"], "jpeg")
        self.assertEqual(result["dimensions"], [1024, 768])
        self.assertEqual(result["actual_edge"], 1024)
        self.assertTrue(result["export_path"].endswith(".jpg"))
        with Image.open(result["export_path"]) as image:
            self.assertEqual(image.size, (1024, 768))

    def test_export_requires_exactly_one_address(self):
        self.assertIn("exactly one", self.call_error("trlr_export_image"))
        self.assertIn("exactly one", self.call_error(
            "trlr_export_image", photo_uuid="u", session_id="s"))

    # ---- 统一图像测量 ------------------------------------------------------------

    def test_measure_on_round_preview(self):
        self.call("trlr_apply_settings", session_id=self.session["session_id"],
                  settings={"Exposure2012": 0.5})
        result = self.call("trlr_image_measure",
                           source={"session_id": self.session["session_id"], "round": 1},
                           grid=True)
        metrics = result["metrics"]
        # 合成图是 (120,80,40) 纯色：R−B=80，色相约 30°（橙）
        self.assertAlmostEqual(metrics["midtone_r_minus_b"], 80, delta=2)
        self.assertAlmostEqual(metrics["shadow_20pct"]["hue_mean_deg"], 30, delta=4)
        self.assertIn("p0_5", metrics["relative_luminance_quantiles"])
        self.assertEqual(len(metrics["grid_8x8_relative_luminance"]), 8)
        self.assertEqual(result["source"]["kind"], "session_round")
        self.assertEqual(result["schema_version"], "image-measurement-v1")
        self.assertNotIn("interpretation", result)

    def test_measure_via_photo_uuid_uses_fast_preview(self):
        self.fake.thumbnail_size = (2048, 1536)
        result = self.call("trlr_image_measure", source={"photo_uuid": "u-catalog"})
        self.assertEqual(result["source"]["kind"], "catalog_photo_fast_preview")
        self.assertEqual(result["source"]["freshness"], "unverified")
        self.assertEqual(result["analysis_edge"], 1024)
        self.assertAlmostEqual(result["metrics"]["midtone_r_minus_b"], 60, delta=2)

    def test_measure_via_whitelisted_folder(self):
        class FakeFolders:
            def preview(self, **kwargs):
                assert kwargs["catalog_write"] is False
                return {"key": "k1", "method": "direct"}

            def preview_bytes(self, key):
                assert key == "k1"
                return jpeg_bytes((40, 90, 160)), "image/jpeg"

        self.wb._folders = FakeFolders()
        result = self.call("trlr_image_measure",
                           source={"root": "abcdef012345", "name": "IMG_01.JPG"})
        self.assertEqual(result["source"]["kind"], "whitelisted_folder")
        self.assertEqual(result["source"]["method"], "direct")
        self.assertAlmostEqual(result["metrics"]["midtone_r_minus_b"], -120, delta=2)

    def test_folder_measurement_uses_the_catalog_bound_client(self):
        captured = {}

        class FakeFolders:
            def preview(self, **kwargs):
                captured.update(kwargs)
                return {"key": "k1", "method": "direct"}

            def preview_bytes(self, key):
                return jpeg_bytes((40, 90, 160)), "image/jpeg"

        self.wb._folders = FakeFolders()
        self.call(
            "trlr_image_measure",
            source={
                "root": "abcdef012345",
                "name": "IMG_01.CR2",
                "catalog_write": "allow",
            },
        )
        self.assertTrue(captured["catalog_write"])
        self.assertIsNot(captured["client"], self.fake)
        self.assertIs(captured["client"].client, self.fake)

    def test_measure_via_published_episode_artifact(self):
        episode_id = str(uuid.uuid4())
        directory = self.store.root / "episodes" / episode_id
        directory.mkdir(parents=True)
        content = jpeg_bytes((30, 90, 180))
        (directory / "v0.jpg").write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        (directory / "episode.json").write_text(json.dumps({
            "reference_sha256": digest,
            "versions": [{"sha256": digest}],
        }))
        result = self.call("trlr_image_measure",
                           source={"episode_id": episode_id, "name": "v0.jpg"},
                           points=[[0.5, 0.5]], sample_radius=0)
        self.assertEqual(result["source"]["kind"], "episode_artifact")
        self.assertEqual(result["source"]["sha256"], digest)
        self.assertEqual(result["samples"][0]["pixel"], [32, 24])

    def test_measure_rejects_ambiguous_or_extra_source_fields(self):
        self.assertIn("exactly one of round or slot", self.call_error(
            "trlr_image_measure", source={"session_id": self.session["session_id"]}))
        self.assertIn("exactly one address type", self.call_error(
            "trlr_image_measure",
            source={"photo_uuid": "u", "session_id": self.session["session_id"],
                    "slot": "source"}))
        self.assertIn("unexpected name", self.call_error(
            "trlr_image_measure", source={"photo_uuid": "u", "name": "v0.jpg"}))

    # ---- 全量回读 ----------------------------------------------------------------

    def test_full_settings_json_and_xmp(self):
        self.call("trlr_apply_settings", session_id=self.session["session_id"],
                  settings={"Exposure2012": 0.4})
        result = self.call("trlr_get_full_settings",
                           session_id=self.session["session_id"], format="both")
        self.assertEqual(result["settings"]["GrainAmount"], 25)
        # XMP 可解析、含曲线/颗粒/HSL，蒙版被显式列出而非静默丢弃
        root = ET.fromstring(result["xmp"])
        text = result["xmp"]
        self.assertIn('GrainAmount="25"', text)
        self.assertIn("ToneCurvePV2012", text)
        self.assertIn("0, 0", text)  # 曲线点对
        self.assertIn('SaturationAdjustmentOrange="-12"', text)
        self.assertIn('ConvertToGrayscale="False"', text)
        self.assertNotIn("HasSettings", text)
        self.assertIn("MaskGroupBasedCorrections", result["skipped_structured_keys"])
        self.assertIsNotNone(root)

    def test_full_settings_requires_working_copy(self):
        fresh = self.wb.create_session("src", "ref", "color_match")
        self.assertIn("no working copy", self.call_error(
            "trlr_get_full_settings", session_id=fresh["session_id"]))

    # ---- 轮次 diff ---------------------------------------------------------------

    def test_round_diff_params_and_stats(self):
        sid = self.session["session_id"]
        self.fake.export_size = (2048, 1536)
        self.call("trlr_apply_settings", session_id=sid, settings={"Exposure2012": 0.3})
        self.call("trlr_apply_settings", session_id=sid,
                  settings={"Exposure2012": 0.8, "Saturation": -10})
        result = self.call("trlr_round_diff", session_id=sid, round_a=1, round_b=2)
        changed = result["submitted_settings_diff"]["changed"]
        self.assertEqual(changed["Exposure2012"], [0.3, 0.8])
        self.assertEqual(result["submitted_settings_diff"]["added"], {"Saturation": -10})
        self.assertIn("metric_delta_b_minus_a", result)
        self.assertIn("saturation_mean", result["metric_delta_b_minus_a"])

    def test_measure_scales_2048_session_slots(self):
        self.fake.export_size = (2048, 1536)
        fresh = self.wb.create_session("src", "ref", "color_match")
        source_measurement = self.call(
            "trlr_image_measure",
            source={"session_id": fresh["session_id"], "slot": "source"})
        reference_measurement = self.call(
            "trlr_image_measure",
            source={"session_id": fresh["session_id"], "slot": "reference"})
        self.assertEqual(source_measurement["analysis_edge"], 1024)
        self.assertEqual(source_measurement["dimensions"], [1024, 768])
        self.assertEqual(reference_measurement["dimensions"], [1024, 768])

    def test_compare_session_source_to_round_at_native_resolution(self):
        sid = self.session["session_id"]
        self.call("trlr_apply_settings", session_id=sid,
                  settings={"Exposure2012": 0.5})
        result = self.call(
            "trlr_image_compare",
            left={"session_id": sid, "slot": "source"},
            right={"session_id": sid, "round": 1},
            mode="aligned",
        )
        self.assertEqual(result["schema_version"], "image-comparison-v1")
        self.assertEqual(result["comparison_basis"], {
            "color_space": "sRGB",
            "resolution": "decoded_native",
            "resampling": "none",
        })
        self.assertEqual(result["left"]["kind"], "session_slot")
        self.assertEqual(result["right"]["kind"], "session_round")
        for side in ("left", "right"):
            self.assertEqual(result[side]["freshness"], "verified")
            self.assertEqual(result[side]["integrity"], "sha256_verified")
            self.assertEqual(len(result[side]["sha256"]), 64)
            self.assertEqual(result[side]["dimensions"], [64, 48])
        self.assertEqual(result["aligned_error"]["alignment"],
                         "caller_asserted_not_verified")
        self.assertNotIn("recommendation", result)

    def test_compare_rejects_tampered_session_preview(self):
        sid = self.session["session_id"]
        public_record = self.call(
            "trlr_apply_settings", session_id=sid,
            settings={"Exposure2012": 0.5})
        self.assertNotIn("preview_path", public_record)
        round_record = self.store.get_session(sid)["rounds"][0]
        Path(round_record["preview_path"]).write_bytes(jpeg_bytes((1, 2, 3)))
        error = self.call_error(
            "trlr_image_compare",
            left={"session_id": sid, "slot": "source"},
            right={"session_id": sid, "round": 1},
        )
        self.assertIn("session preview hash mismatch", error)

    def test_compare_rejects_round_preview_outside_session_directory(self):
        sid = self.session["session_id"]
        public_record = self.call(
            "trlr_apply_settings", session_id=sid,
            settings={"Exposure2012": 0.5})
        self.assertNotIn("preview_path", public_record)
        round_record = self.store.get_session(sid)["rounds"][0]
        outside = Path(self.tmp.name) / "outside.jpg"
        outside.write_bytes(Path(round_record["preview_path"]).read_bytes())
        state = self.store.get_session(sid)
        state["rounds"][0]["preview_path"] = str(outside)
        self.store._write(state)
        error = self.call_error(
            "trlr_image_compare",
            left={"session_id": sid, "slot": "source"},
            right={"session_id": sid, "round": 1},
        )
        self.assertIn("escapes its preview directory", error)

    def test_compare_rejects_session_preview_symlink(self):
        sid = self.session["session_id"]
        public_record = self.call(
            "trlr_apply_settings", session_id=sid,
            settings={"Exposure2012": 0.5})
        self.assertNotIn("preview_path", public_record)
        round_record = self.store.get_session(sid)["rounds"][0]
        preview = Path(round_record["preview_path"])
        target = Path(self.tmp.name) / "round-copy.jpg"
        target.write_bytes(preview.read_bytes())
        preview.unlink()
        preview.symlink_to(target)
        error = self.call_error(
            "trlr_image_compare",
            left={"session_id": sid, "slot": "source"},
            right={"session_id": sid, "round": 1},
        )
        self.assertIn("symlinks are not accepted", error)

    def test_compare_rejects_oversized_session_preview_before_decode(self):
        sid = self.session["session_id"]
        preview = self.store.preview_dir(sid) / "source.jpg"
        preview.write_bytes(b"123456789")
        stable = {"session_id": sid, "slot": "reference"}
        with patch("workbench_mcp.session_tools.MAX_BYTES", 8):
            error = self.call_error(
                "trlr_image_compare",
                left={"session_id": sid, "slot": "source"},
                right=stable,
            )
        self.assertIn("exceeds 32 MiB analysis limit", error)

    def test_compare_marks_legacy_session_preview_unverified(self):
        sid = self.session["session_id"]
        state = self.store.get_session(sid)
        state["slot_previews"]["source"].pop("sha256")
        self.store._write(state)
        result = self.call(
            "trlr_image_compare",
            left={"session_id": sid, "slot": "source"},
            right={"session_id": sid, "slot": "reference"},
        )
        self.assertEqual(result["left"]["freshness"], "unverified")
        self.assertEqual(result["left"]["integrity"], "legacy_no_sha256")
        self.assertEqual(result["right"]["freshness"], "verified")

    def test_compare_rejects_sources_that_can_refresh_or_render(self):
        sid = self.session["session_id"]
        stable = {"session_id": sid, "slot": "source"}
        self.assertIn("stored session", self.call_error(
            "trlr_image_compare", left={"photo_uuid": "u"}, right=stable))
        self.assertIn("stored session", self.call_error(
            "trlr_image_compare",
            left={"root": "abcdef012345", "name": "IMG_01.JPG"},
            right=stable))
        self.assertIn("canonical UUID", self.call_error(
            "trlr_image_compare",
            left={"session_id": "../escape", "slot": "source"},
            right=stable))

    def test_get_preview_uses_persisted_hash_and_rejects_tampering(self):
        sid = self.session["session_id"]
        result = self.call("trlr_get_preview", session_id=sid, slot="source")
        expected = self.store.get_session(sid)["slot_previews"]["source"]["sha256"]
        self.assertEqual(result["integrity"], "sha256_verified")
        self.assertEqual(result["artifact_ref"]["sha256"], expected)
        self.assertEqual(result["artifact_ref"]["freshness"], "verified")
        self.assertEqual(result["artifact_ref"]["evidence_status"], "verified_render")

        path = self.store.preview_dir(sid) / "source.jpg"
        path.write_bytes(jpeg_bytes((1, 2, 3)))
        self.assertIn("session preview hash mismatch", self.call_error(
            "trlr_get_preview", session_id=sid, slot="source"))

    def test_get_preview_uses_one_verified_snapshot_without_rereading_state(self):
        sid = self.session["session_id"]
        round_record = self.call(
            "trlr_apply_settings", session_id=sid,
            settings={"Exposure2012": 0.5})
        expected_sha = round_record["preview_sha256"]
        outside = Path(self.tmp.name) / "outside-preview.jpg"
        outside.write_bytes(jpeg_bytes((250, 1, 1)))
        original_get = self.store.get_session
        calls = 0

        def changing_state(session_id):
            nonlocal calls
            calls += 1
            state = original_get(session_id)
            if calls > 1:
                state["rounds"][0]["preview_path"] = str(outside)
            return state

        with patch.object(self.store, "get_session", side_effect=changing_state):
            result = self.call("trlr_get_preview", session_id=sid, round=1)
        self.assertEqual(calls, 1)
        self.assertEqual(result["artifact_ref"]["sha256"], expected_sha)
        self.assertEqual(result["integrity"], "sha256_verified")

    def test_get_preview_rejects_symlinked_preview_root(self):
        sid = self.session["session_id"]
        preview_root = self.store.preview_dir(sid)
        moved = Path(self.tmp.name) / "outside-previews"
        preview_root.rename(moved)
        preview_root.symlink_to(moved, target_is_directory=True)
        self.assertIn("preview root symlinks are not accepted", self.call_error(
            "trlr_get_preview", session_id=sid, slot="source"))

    def test_get_preview_rejects_symlinked_session_directory(self):
        sid = self.session["session_id"]
        session_root = self.store.preview_dir(sid).parent
        moved = Path(self.tmp.name) / "outside-session"
        session_root.rename(moved)
        session_root.symlink_to(moved, target_is_directory=True)
        self.assertIn("session directory symlinks are not accepted", self.call_error(
            "trlr_get_preview", session_id=sid, slot="source"))

    def test_get_preview_allows_store_root_symlink(self):
        actual_root = Path(self.tmp.name) / "actual-store"
        linked_root = Path(self.tmp.name) / "linked-store"
        actual_root.mkdir()
        # Populate the persisted fixture via its physical root. This is a read
        # contract test, independent of OS policy for creating directories
        # through a symlink in the system temporary directory.
        physical_wb = Workbench(Store(actual_root), FakeLrOps())
        session = physical_wb.create_session("src", "ref", "color_match")
        linked_root.symlink_to(actual_root, target_is_directory=True)
        store = Store(linked_root)
        wb = Workbench(store, FakeLrOps())
        server = McpServer(wb)
        response = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "trlr_get_preview", "arguments": {
                "session_id": session["session_id"], "slot": "source"}},
        })
        self.assertFalse(response["result"]["isError"])
        metadata = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(metadata["integrity"], "sha256_verified")

    def test_get_preview_marks_legacy_slot_unverified(self):
        sid = self.session["session_id"]
        state = self.store.get_session(sid)
        state["slot_previews"]["source"].pop("sha256")
        self.store._write(state)
        result = self.call("trlr_get_preview", session_id=sid, slot="source")
        self.assertEqual(result["integrity"], "legacy_no_sha256")
        self.assertEqual(result["artifact_ref"]["freshness"], "unverified")
        self.assertEqual(result["artifact_ref"]["evidence_status"], "unverified")

    def test_get_preview_requires_exactly_one_bounded_address(self):
        sid = self.session["session_id"]
        self.assertIn("exactly one", self.call_error(
            "trlr_get_preview", session_id=sid))
        self.assertIn("exactly one", self.call_error(
            "trlr_get_preview", session_id=sid, round=1, slot="source"))
        self.assertIn("exactly one", self.call_error(
            "trlr_get_preview", session_id=sid, round=None, slot="source"))
        self.assertIn("exactly one", self.call_error(
            "trlr_get_preview", session_id=sid, round=1, slot=None))
        self.assertIn("round must be an integer", self.call_error(
            "trlr_get_preview", session_id=sid, round=None))
        self.assertIn("slot must be source or reference", self.call_error(
            "trlr_get_preview", session_id=sid, slot=None))
        self.assertIn("64 to 2048", self.call_error(
            "trlr_get_preview", session_id=sid, slot="source", max_edge=32))

    def test_compare_and_preview_normalize_json_integer_values(self):
        sid = self.session["session_id"]
        self.call("trlr_apply_settings", session_id=sid,
                  settings={"Exposure2012": 0.5})
        preview = self.call(
            "trlr_get_preview", session_id=sid, round=1.0, max_edge=64.0)
        self.assertEqual(preview["integrity"], "sha256_verified")
        comparison = self.call(
            "trlr_image_compare",
            left={"session_id": sid, "round": 1.0},
            right={"session_id": sid, "slot": "source"},
            max_edge=64.0,
        )
        self.assertEqual(comparison["left"]["round"], 1)
        for value in (True, float("nan"), 64.5):
            with self.subTest(value=value):
                self.assertIn("max_edge must be an integer", self.call_error(
                    "trlr_image_compare",
                    left={"session_id": sid, "round": 1},
                    right={"session_id": sid, "slot": "source"},
                    max_edge=value,
                ))

    def test_round_diff_unknown_round(self):
        self.call("trlr_apply_settings", session_id=self.session["session_id"],
                  settings={"Exposure2012": 0.3})
        self.assertIn("unknown round", self.call_error(
            "trlr_round_diff", session_id=self.session["session_id"],
            round_a=1, round_b=9))

    # ---- 全量重放 ------------------------------------------------------------------

    def test_replay_from_round_uses_after_readback(self):
        sid = self.session["session_id"]
        self.call("trlr_apply_settings", session_id=sid,
                  settings={"Exposure2012": 0.3, "Contrast2012": 10})
        self.call("trlr_apply_settings", session_id=sid, settings={"Saturation": -20})
        result = self.call("trlr_replay_round", session_id=sid, source_round=2)
        self.assertEqual(result["basis"], "after_readback")
        applied = result["settings_applied"]
        self.assertEqual(applied["Exposure2012"], 0.3)
        self.assertEqual(applied["Saturation"], -20)
        self.assertEqual(applied["IncrementalTemperature"], 5)  # WB 随 after 全量带回
        self.assertEqual(result["round"]["round"], 3)
        # reset 真的发生过：重放后 fake 的状态 == 重放目标
        self.assertEqual(self.fake.settings["Saturation"], -20)

    def test_replay_explicit_settings_with_patch(self):
        sid = self.session["session_id"]
        self.call("trlr_apply_settings", session_id=sid, settings={"Exposure2012": 0.3})
        result = self.call("trlr_replay_round", session_id=sid,
                           settings={"Exposure2012": 1.0, "Contrast2012": 5},
                           patch={"Contrast2012": 15})
        self.assertEqual(result["basis"], "explicit+patch")
        self.assertEqual(result["settings_applied"]["Contrast2012"], 15)
        # WB 守护对显式参数集同样生效
        self.assertEqual(result["settings_applied"]["IncrementalTemperature"], 5)

    def test_replay_requires_exactly_one_target(self):
        sid = self.session["session_id"]
        self.assertIn("exactly one", self.call_error("trlr_replay_round", session_id=sid))

    # ---- 会话归档 -------------------------------------------------------------------

    def test_export_session_context(self):
        sid = self.session["session_id"]
        self.call("trlr_apply_settings", session_id=sid, settings={"Exposure2012": 0.3})
        self.call("trlr_apply_settings", session_id=sid, settings={"Saturation": -10})
        out_dir = str(Path(self.tmp.name) / "experiments" / "case-a")
        result = self.call("trlr_export_session_context", session_id=sid,
                           output_dir=out_dir)
        path = Path(result["context_path"])
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn(sid, text)
        self.assertIn("working_copy_uuid", text)
        self.assertIn("round 2", text)
        self.assertIn("相对上一轮参数 diff", text)
        self.assertIn("## 思路", text)
        self.assertIn("## 翻车记录", text)
        self.assertIn("## 修复与结论", text)
        self.assertIn("sha256", text)
        self.assertEqual(result["rounds"], 2)
        # 默认不覆盖
        self.assertIn("already exists", self.call_error(
            "trlr_export_session_context", session_id=sid, output_dir=out_dir))
        again = self.call("trlr_export_session_context", session_id=sid,
                          output_dir=out_dir, overwrite=True)
        self.assertTrue(Path(again["context_path"]).exists())

class UnitTests(unittest.TestCase):
    """不经过 RPC 的纯函数核对。"""

    def test_xmp_serializer_skips_safely(self):
        result = serialize_full_settings({
            "Exposure2012": 0.4, "Temperature": 5200,
            "ToneCurvePV2012": [0, 0, 255, 255],
            "OddArray": [1, 2, 3],
            "MaskGroupBasedCorrections": [{"what": "Mask"}],
            "HasSettings": True,
            "bad key!": 1,
        })
        root = ET.fromstring(result["xmp"])  # 必须是合法 XML
        self.assertIsNotNone(root)
        self.assertIn('Exposure2012="0.4"', result["xmp"])
        self.assertIn('Temperature="5200"', result["xmp"])
        self.assertIn("OddArray", result["skipped_shape_keys"])
        self.assertIn("MaskGroupBasedCorrections", result["skipped_structured_keys"])
        self.assertIn("HasSettings", result["skipped_metadata_keys"])
        self.assertIn("bad key!", result["skipped_shape_keys"])


if __name__ == "__main__":
    unittest.main()
