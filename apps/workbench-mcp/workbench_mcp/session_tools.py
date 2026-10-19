"""Session-layer execution, state, and deterministic measurement helpers.

这一层只调用既有 Workbench / Store / LrOps 接口，不改桥行为。
它保留 Session 兼容工具需要的导出、测量、全量回读、白平衡保持、轮次对比、
重放和上下文导出能力。
"""

from __future__ import annotations

import hashlib
import builtins
import io
import json
import os
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from workbench.artifacts import build_artifact_ref
from workbench.image_tools import (MAX_BYTES, compare_images, decode_srgb,
                                   diff_metrics, measure_image)
from workbench.color_patches import (compare_region_signatures,
                                     digital_references, measure_patches,
                                     region_overlay)
from workbench.lrops import LrOpsError
from workbench.srgb_profile import SRGB_ICC
from workbench.store import StoreError
from workbench.studio.settings import WB_KEYS

from .xmp_full import serialize_full_settings

EXPORT_FORMATS = ("jpeg",)
# 大图导出失败时的有界重试阶梯。
EXPORT_EDGE_LADDER = (2048, 1024, 512)
ANALYSIS_MAX_EDGE = 1024


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _check_edge(value, lo=64, hi=4096, name="max_edge"):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer from {lo} to {hi}")
    return value


@dataclass(frozen=True)
class SessionPreviewPayload:
    """One exact persisted byte snapshot plus its decoded image and provenance."""

    content: bytes
    image: Image.Image
    provenance: dict
    filename: str


class SessionTools:
    def __init__(self, wb):
        self.wb = wb

    # ---- 会话解析 ------------------------------------------------------------

    def _session(self, session_id: str) -> dict:
        if not isinstance(session_id, str):
            raise StoreError("session_id must be a canonical UUID")
        try:
            canonical = str(uuid.UUID(session_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise StoreError("session_id must be a canonical UUID") from exc
        if canonical != session_id:
            raise StoreError("session_id must be a canonical UUID")
        return self.wb.store.get_session(session_id)

    def _working_copy(self, session: dict) -> str:
        copy_uuid = session.get("working_copy_uuid")
        if not copy_uuid:
            raise StoreError("session has no working copy yet; run trlr_apply_settings "
                             "once first, or address a stored round instead")
        return copy_uuid

    def _round_record(self, session: dict, number) -> dict:
        if type(number) is not int:
            raise ValueError("round must be an integer")
        for record in session.get("rounds", []):
            if record.get("round") == number:
                return record
        raise StoreError(f"unknown round {number}")

    # ---- WB 钳位守护 -----------------------------------------------------------

    def wb_guard_fill(self, session: dict, settings: dict) -> tuple[dict, dict]:
        """缺省时以当前值为基准显式补齐 WB 两字段。

        session-b 实测：不显式带 WB 时个别轮次被桥钳位（画面翻绿）。session
        已有工作副本时先回读当前 WB，把当前值写进本次 apply，让「不带 WB」
        从「交给运气」变成「显式保持」。返回 (补齐后的 settings, 回显信息)。
        """
        if set(settings) & set(WB_KEYS):
            return dict(settings), {"wb_fill": "caller_explicit"}
        copy_uuid = session.get("working_copy_uuid")
        # 首轮还没有工作副本：副本从源片继承全部状态，以源片回读为基准补齐，
        # 否则「建副本后的第一次 apply」正是钳位故障的入口（B-1 round 2 翻绿）。
        read_uuid = copy_uuid or session.get("source_uuid")
        if not read_uuid:
            return dict(settings), {"wb_fill": "unavailable_no_photo"}
        state = self.wb.run("get_develop_state", {"photo_uuid": read_uuid})
        current = state.get("settings") or {}
        filled = dict(settings)
        basis = "current" if copy_uuid else "source"
        if "Temperature" in current and "Tint" in current:
            filled.setdefault("Temperature", current["Temperature"])
            filled.setdefault("Tint", current["Tint"])
            fill = {"Temperature": filled["Temperature"], "Tint": filled["Tint"]}
        elif "IncrementalTemperature" in current or "IncrementalTint" in current:
            filled.setdefault("IncrementalTemperature", current.get("IncrementalTemperature", 0))
            filled.setdefault("IncrementalTint", current.get("IncrementalTint", 0))
            fill = {"IncrementalTemperature": filled["IncrementalTemperature"],
                    "IncrementalTint": filled["IncrementalTint"]}
        else:
            return filled, {"wb_fill": "unavailable_no_wb_in_readback"}
        return filled, {"wb_fill": f"filled_from_{basis}", "wb_filled": fill}

    @staticmethod
    def _wb_echo(record: dict) -> dict:
        after = record.get("after") or {}
        return {key: after[key] for key in WB_KEYS if key in after}

    def apply_settings(self, session_id: str, settings: dict) -> dict:
        """Apply settings with WB preservation and actual-value readback."""
        if not isinstance(settings, dict) or not settings:
            raise ValueError("settings must be a nonempty object")
        session = self._session(session_id)
        filled, guard = self.wb_guard_fill(session, settings)
        record = self.wb.run_round(session_id, filled)
        record["wb_guard"] = guard
        record["wb_written"] = self._wb_echo(record)
        return record

    # ---- 导出 ------------------------------------------------------------------

    def _live_export(self, photo_uuid: str, max_edge: int | None) -> tuple[bytes, int, list]:
        """按重试阶梯渲染导出；返回 (字节, 实际 edge, 尝试记录)。

        max_edge=None 表示全尺寸导出（long_edge=0，Lightroom 端不做尺寸约束）。
        """
        ladder = [0 if max_edge is None else max_edge]
        for fallback in EXPORT_EDGE_LADDER:
            if ladder[-1] == 0 or fallback < ladder[-1]:
                ladder.append(fallback)
        attempts = []
        for edge in ladder:
            try:
                data = self.wb.run("export_preview",
                                   {"photo_uuid": photo_uuid, "long_edge": edge})
                ref = data.get("output_ref")
                if not ref:
                    raise LrOpsError("导出未返回文件句柄；请重启 lr-ops 桥以加载新协议")
                # The bridge must open its destination itself. A still-open
                # delete-on-close NamedTemporaryFile prevents that on Windows.
                with tempfile.TemporaryDirectory(prefix="trlr-export-") as directory:
                    destination = Path(directory) / "preview.jpg"
                    self.wb.lrops.fetch_output(ref, destination)
                    content = destination.read_bytes()
                if not content:
                    raise LrOpsError("导出返回空文件")
                attempts.append({"edge": edge, "ok": True})
                return content, edge, attempts
            except (LrOpsError, OSError) as exc:
                attempts.append({"edge": edge, "ok": False, "error": str(exc)})
        raise LrOpsError("导出在全部重试阶梯上失败: "
                         + json.dumps(attempts, ensure_ascii=False))

    def export_image(self, photo_uuid=None, session_id=None, round=None,
                     max_edge=None, format="jpeg", quality=90) -> dict:
        """服务端渲染 + 可选缩放导出，返回 artifact_ref + sha256（图不进上下文）。

        max_edge 缺省为 None，即按原始分辨率全尺寸导出；只有用户明确要求
        指定尺寸的成品时才传 max_edge。看图时的跨工具回退由 Runtime Operator
        提示词规定，本工具仍只返回导出元数据与文件引用。
        """
        # 桥的 live 导出只产 JPEG；不提供把有损源重编码成 PNG 的选项。
        if max_edge is not None:
            _check_edge(max_edge, hi=16384)
        if format not in EXPORT_FORMATS:
            raise ValueError(f"format must be one of {EXPORT_FORMATS}")
        if type(quality) is not int or not 1 <= quality <= 100:
            raise ValueError("quality must be an integer from 1 to 100")
        if (photo_uuid is None) == (session_id is None):
            raise ValueError("pass exactly one of photo_uuid or session_id")

        session = None
        round_no = None
        if session_id is not None:
            session = self._session(session_id)
        if session is not None and round is not None:
            # 已有轮次的 2048px 存档预览就是已验证渲染，直接服务端缩放，
            # 不再惊动 Lightroom。
            record = self._round_record(session, int(round))
            round_no = int(round)
            path = Path(record["preview_path"])
            if not path.exists():
                raise StoreError(f"preview file missing: {path}")
            content, actual_edge, attempts = path.read_bytes(), None, []
            basis = "stored_round_preview"
            target_uuid = session.get("working_copy_uuid")
        else:
            target_uuid = (self._working_copy(session) if session is not None
                           else photo_uuid)
            content, actual_edge, attempts = self._live_export(target_uuid, max_edge)
            basis = "live_export"

        image = Image.open(io.BytesIO(content)).convert("RGB")
        if max_edge is not None and max(image.size) > max_edge:
            scale = max_edge / max(image.size)
            image = image.resize((max(1, builtins.round(image.width * scale)),
                                  max(1, builtins.round(image.height * scale))), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, icc_profile=SRGB_ICC)
        ext = "jpg"
        payload = out.getvalue()
        digest = _sha256(payload)

        if session is not None:
            export_dir = self.wb.store.preview_dir(session["session_id"]).parent / "exports"
            prefix = f"session-{session['session_id'][:8]}"
        else:
            export_dir = self.wb.store.root / "exports"
            prefix = "photo"
        export_dir.mkdir(parents=True, exist_ok=True)
        final = export_dir / f"{prefix}-{digest[:16]}.{ext}"
        final.write_bytes(payload)

        source = ({"kind": "session_export", "session_id": session["session_id"],
                   "round": round_no, "photo_uuid": target_uuid}
                  if session is not None else
                  {"kind": "photo_export", "photo_uuid": target_uuid})
        artifact = build_artifact_ref(
            final, artifact_id=(f"session:{session['session_id']}:export:{digest[:16]}"
                                if session is not None else
                                f"photo:{target_uuid}:export:{digest[:16]}"),
            source=source, purpose="verified" if basis == "live_export" else "analysis",
            method="LrExportSession" if basis == "live_export" else "stored_preview_rescale",
            freshness="verified" if basis == "live_export" else "derived",
            evidence_status="verified_render" if basis == "live_export" else "derived",
            color_space="sRGB")
        result = {
            "basis": basis,
            "requested_max_edge": max_edge,
            "actual_edge": (actual_edge if actual_edge else max(image.size)),
            "dimensions": [image.width, image.height],
            "format": format,
            "quality": quality,
            "bytes": len(payload),
            "sha256": digest,
            "export_path": str(final),
            "artifact_ref": artifact,
        }
        if session is not None:
            result["session_id"] = session["session_id"]
            if round_no is not None:
                result["round"] = round_no
        else:
            result["photo_uuid"] = target_uuid
        if len(attempts) > 1 or any(not a["ok"] for a in attempts):
            result["attempts"] = attempts
            result["note"] = ("大图导出失败后已自动降 edge 重试成功，"
                              "实际 edge 见 actual_edge")
        return result

    # ---- 统一图像测量 ----------------------------------------------------------

    @staticmethod
    def _validate_source_keys(source: dict, required: set[str], optional=None):
        optional = optional or set()
        allowed = required | optional
        missing = required - set(source)
        extras = set(source) - allowed
        if missing or extras:
            details = []
            if missing:
                details.append("missing " + ", ".join(sorted(missing)))
            if extras:
                details.append("unexpected " + ", ".join(sorted(extras)))
            raise ValueError("invalid image source: " + "; ".join(details))

    @staticmethod
    def _open_rgb(path_or_bytes):
        return decode_srgb(path_or_bytes)

    def _open_session_preview(self, session_id: str, path: Path, expected_sha256=None):
        """Decode exact persisted bytes and report whether their identity was verified."""
        store_root = Path(self.wb.store.root)
        resolved_store_root = store_root.resolve()
        sessions_root = store_root / "sessions"
        session_root = sessions_root / session_id
        preview_root = session_root / "previews"
        for label, directory in (("sessions root", sessions_root),
                                 ("session directory", session_root),
                                 ("preview root", preview_root)):
            if directory.is_symlink():
                raise StoreError(f"{label} symlinks are not accepted")
        if path.is_symlink():
            raise StoreError("session preview symlinks are not accepted")
        candidate = Path(os.path.abspath(path))
        lexical_root = Path(os.path.abspath(preview_root))
        resolved_root = resolved_store_root / "sessions" / session_id / "previews"
        if candidate.parent not in (lexical_root, resolved_root):
            raise StoreError("session preview path escapes its preview directory")

        # Anchor every directory below the configured Store root with openat and
        # O_NOFOLLOW. Store-root ancestors may be platform symlinks (for example
        # macOS /var -> /private/var), but sessions, the session directory,
        # previews and the final file may not be swapped for symlinks.
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        file_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            file_flags |= os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            file_flags |= os.O_CLOEXEC

        directory_fds = []
        try:
            if os.name == "nt":
                from .windows_files import read_preview
                content = read_preview(resolved_store_root, session_id,
                                       candidate.name, MAX_BYTES)
            else:
                root_fd = os.open(resolved_store_root, directory_flags)
                directory_fds.append(root_fd)
                parent_fd = root_fd
                for component in ("sessions", session_id, "previews"):
                    parent_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                    directory_fds.append(parent_fd)
                file_fd = os.open(candidate.name, file_flags, dir_fd=parent_fd)
                try:
                    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                        raise StoreError("session preview is not a regular file")
                    with os.fdopen(file_fd, "rb") as handle:
                        file_fd = None
                        content = handle.read(MAX_BYTES + 1)
                finally:
                    if file_fd is not None:
                        os.close(file_fd)
        except FileNotFoundError as exc:
            raise StoreError(f"preview file missing: {path}") from exc
        except StoreError:
            raise
        except OSError as exc:
            raise StoreError("session preview could not be opened safely") from exc
        finally:
            for descriptor in reversed(directory_fds):
                os.close(descriptor)
        if len(content) > MAX_BYTES:
            raise StoreError("session preview exceeds 32 MiB analysis limit")
        digest = _sha256(content)
        if expected_sha256 is None:
            freshness = "unverified"
            integrity = "legacy_no_sha256"
        else:
            if not isinstance(expected_sha256, str) or digest != expected_sha256:
                raise StoreError("session preview hash mismatch")
            freshness = "verified"
            integrity = "sha256_verified"
        image, color_policy = self._open_rgb(content)
        return image, content, {
            "sha256": digest,
            "dimensions": list(image.size),
            "freshness": freshness,
            "integrity": integrity,
            "color_space": "sRGB",
            "color_policy": (color_policy
                             if color_policy == "embedded_icc_to_srgb"
                             else "assumed_srgb_from_lr_export_contract"),
        }

    def load_session_preview(self, source) -> SessionPreviewPayload:
        """Load one Session slot/round once; callers reuse this exact byte snapshot."""
        if not isinstance(source, dict) or not source:
            raise ValueError("session source must be a nonempty object")
        has_round, has_slot = "round" in source, "slot" in source
        if has_round == has_slot:
            raise ValueError("session source requires exactly one of round or slot")
        required = {"session_id", "round"} if has_round else {"session_id", "slot"}
        self._validate_source_keys(source, required)
        session_id = source["session_id"]
        session = self._session(session_id)
        if has_round:
            number = source["round"]
            if type(number) is not int:
                raise ValueError("round must be an integer")
            record = self._round_record(session, number)
            path = Path(record["preview_path"])
            provenance = {"kind": "session_round", "session_id": session_id,
                          "round": number}
            expected_sha256 = record.get("preview_sha256")
        else:
            slot = source["slot"]
            if slot not in ("source", "reference"):
                raise ValueError("slot must be source or reference")
            slot_record = (session.get("slot_previews") or {}).get(slot) or {}
            # Slot names have one canonical server-derived path. Stored
            # metadata supplies only the expected byte identity.
            path = self.wb.store.preview_dir(session_id) / f"{slot}.jpg"
            provenance = {"kind": "session_slot", "session_id": session_id,
                          "slot": slot}
            expected_sha256 = slot_record.get("sha256")
        image, content, preview_meta = self._open_session_preview(
            session_id, path, expected_sha256)
        provenance.update(preview_meta)
        return SessionPreviewPayload(
            content=content, image=image, provenance=provenance,
            filename=path.name)

    def _source_image(self, source):
        """Resolve exactly one supported address into an RGB image and provenance."""
        if not isinstance(source, dict) or not source:
            raise ValueError("source must be a nonempty object")
        selectors = [key for key in ("photo_uuid", "session_id", "root", "episode_id")
                     if key in source]
        if len(selectors) != 1:
            raise ValueError("source must use exactly one address type: photo_uuid, "
                             "session_id, root, or episode_id")

        selector = selectors[0]
        if selector == "photo_uuid":
            self._validate_source_keys(source, {"photo_uuid"})
            photo_uuid = source["photo_uuid"]
            if not isinstance(photo_uuid, str) or not photo_uuid:
                raise ValueError("photo_uuid must be a nonempty string")
            record = self.wb.preview_photo(photo_uuid, size=2048, force=False)
            image, color_policy = self._open_rgb(record["preview_path"])
            desc = {
                "kind": "catalog_photo_fast_preview",
                "photo_uuid": photo_uuid,
                "freshness": record.get("freshness", "unverified"),
                "method": record.get("method", "requestJpegThumbnail"),
                "color_space": "sRGB",
                "color_policy": (color_policy if color_policy == "embedded_icc_to_srgb"
                                 else "assumed_srgb_from_preview_contract"),
            }
        elif selector == "session_id":
            payload = self.load_session_preview(source)
            image, desc = payload.image, payload.provenance
        elif selector == "root":
            self._validate_source_keys(source, {"root", "name"},
                                       {"dir", "catalog_write"})
            catalog_write = source.get("catalog_write", "never")
            if catalog_write not in ("never", "allow"):
                raise ValueError("catalog_write must be never or allow")
            relative_dir = source.get("dir", "")
            if not isinstance(relative_dir, str):
                raise ValueError("dir must be a string")
            record = self.wb.folders.preview(
                root=source["root"], rel=relative_dir, name=source["name"],
                client=self.wb.catalog_client(),
                edge=self.wb.presentation.get()["edit_render_max_edge"], render="auto",
                catalog_write=(catalog_write == "allow"))
            served = self.wb.folders.preview_bytes(record["key"])
            if served is None:
                raise StoreError("预览字节不可用；请重新扫描该目录。")
            image, color_policy = self._open_rgb(served[0])
            desc = {"kind": "whitelisted_folder", "root": source["root"],
                    "name": source["name"], "dir": relative_dir,
                    "method": record.get("method"), "color_space": "sRGB",
                    "color_policy": (color_policy
                                     if color_policy == "embedded_icc_to_srgb"
                                     else "assumed_srgb_from_folder_preview_contract")}
        else:
            self._validate_source_keys(source, {"episode_id", "name"})
            image, desc = self.wb.image_tools.load(source)
        return image, desc

    def image_measure(self, source, roi=None, points=None, sample_radius=2,
                      grid=False, analysis_edge=ANALYSIS_MAX_EDGE) -> dict:
        """Return deterministic measurements for exactly one explicitly addressed image."""
        _check_edge(analysis_edge, name="analysis_edge")
        image, provenance = self._source_image(source)
        return measure_image(image, provenance, roi=roi, points=points,
                             sample_radius=sample_radius, grid=grid,
                             analysis_edge=analysis_edge)

    def color_patch_measure(self, source, patches) -> dict:
        """Sample caller-marked patches at the available decoded resolution."""
        image, provenance = self._source_image(source)
        return measure_patches(image, provenance, patches)

    @staticmethod
    def digital_color_references(patches) -> dict:
        """Convert supplied reference values to a small numeric sRGB table."""
        return digital_references(patches)

    def color_region_compare(self, baseline, candidate, regions,
                             include_overlay=False) -> dict:
        """Reuse baseline-selected sample tiles on the same-crop candidate."""
        self._validate_compare_source(baseline)
        self._validate_compare_source(candidate)
        left_image, left_provenance = self._source_image(baseline)
        right_image, right_provenance = self._source_image(candidate)
        result = compare_region_signatures(left_image, left_provenance,
                                           right_image, right_provenance, regions)
        if include_overlay:
            result["overlay_jpeg"] = region_overlay(left_image, result["regions"])
        return result

    @staticmethod
    def _validate_compare_source(source):
        """Accept only immutable/persisted renders; never trigger a fresh preview."""
        if not isinstance(source, dict) or not source:
            raise ValueError("comparison source must be a nonempty object")
        selectors = [key for key in ("session_id", "episode_id") if key in source]
        if len(selectors) != 1:
            raise ValueError("comparison source must be a stored session round/slot "
                             "or a published episode render")
        if selectors[0] == "session_id":
            has_round, has_slot = "round" in source, "slot" in source
            if has_round == has_slot:
                raise ValueError("session comparison source requires exactly one of round or slot")
            required = {"session_id", "round"} if has_round else {"session_id", "slot"}
            SessionTools._validate_source_keys(source, required)
        else:
            SessionTools._validate_source_keys(source, {"episode_id", "name"})

    def image_compare(self, left, right, mode="distribution", roi=None,
                      max_edge=512) -> dict:
        """Compare two persisted native renders without selecting a preferred side."""
        # Validate both addresses before loading either side so invalid input can
        # never cause a catalog thumbnail request or folder render as a side effect.
        self._validate_compare_source(left)
        self._validate_compare_source(right)
        left_image, left_provenance = self._source_image(left)
        right_image, right_provenance = self._source_image(right)
        return compare_images(
            left_image, left_provenance, right_image, right_provenance,
            mode=mode, roi=roi, max_edge=max_edge)

    # ---- 全量回读 ----------------------------------------------------------------

    def full_settings(self, session_id: str, format="both") -> dict:
        if format not in ("json", "xmp", "both"):
            raise ValueError("format must be json, xmp or both")
        session = self._session(session_id)
        copy_uuid = self._working_copy(session)
        state = self.wb.run("get_develop_snapshot", {"photo_uuid": copy_uuid})
        settings = state.get("settings") or {}
        result = {"session_id": session_id, "working_copy_uuid": copy_uuid,
                  "field_count": len(settings)}
        if format in ("json", "both"):
            result["settings"] = settings
        if format in ("xmp", "both"):
            assembled = serialize_full_settings(settings)
            result["xmp"] = assembled["xmp"]
            result["written_keys"] = assembled["written_keys"]
            for key in ("skipped_metadata_keys", "skipped_structured_keys",
                        "skipped_shape_keys"):
                if assembled[key]:
                    result[key] = assembled[key]
        return result

    # ---- 轮次 diff ---------------------------------------------------------------

    @staticmethod
    def _settings_diff(a: dict, b: dict) -> dict:
        a, b = a or {}, b or {}
        changed = {key: [a[key], b[key]] for key in a.keys() & b.keys()
                   if a[key] != b[key]}
        return {"changed": changed,
                "added": {key: b[key] for key in b.keys() - a.keys()},
                "removed": {key: a[key] for key in a.keys() - b.keys()}}

    def round_diff(self, session_id: str, round_a, round_b, stats=True) -> dict:
        session = self._session(session_id)
        ra = self._round_record(session, int(round_a))
        rb = self._round_record(session, int(round_b))
        result = {
            "session_id": session_id,
            "round_a": ra["round"], "round_b": rb["round"],
            "submitted_settings_diff": self._settings_diff(
                ra.get("settings"), rb.get("settings")),
        }
        if ra.get("after") and rb.get("after"):
            result["after_readback_diff"] = self._settings_diff(ra["after"], rb["after"])
        if stats:
            sa = self.image_measure(
                {"session_id": session_id, "round": ra["round"]})["metrics"]
            sb = self.image_measure(
                {"session_id": session_id, "round": rb["round"]})["metrics"]
            result["metrics_a"] = sa
            result["metrics_b"] = sb
            result["metric_delta_b_minus_a"] = diff_metrics(sa, sb)
        return result

    # ---- 全量重放 ------------------------------------------------------------------

    def replay_round(self, session_id: str, settings=None, source_round=None,
                     patch=None) -> dict:
        """reset 后一次性全量应用目标参数集，产新 round（翻绿教训的正解）。"""
        if (settings is None) == (source_round is None):
            raise ValueError("pass exactly one of settings or source_round")
        if patch is not None and (not isinstance(patch, dict) or not patch):
            raise ValueError("patch must be a nonempty object")
        session = self._session(session_id)
        if settings is not None:
            if not isinstance(settings, dict) or not settings:
                raise ValueError("settings must be a nonempty object")
            target, basis = dict(settings), "explicit"
        else:
            record = self._round_record(session, int(source_round))
            if record.get("after"):
                # after 是 LR 真实写入的全量回读（含 WB），是最诚实的重放基准。
                target, basis = dict(record["after"]), "after_readback"
            else:
                target = {}
                for item in session.get("rounds", []):
                    if item.get("round", 0) > int(source_round):
                        break
                    target.update(item.get("settings") or {})
                basis = "merged_submitted"
        if patch:
            target.update(patch)
            basis += "+patch"
        self.wb.reset(session_id)
        record = self.apply_settings(session_id, target)
        guard = record.get("wb_guard") or {}
        applied = {**target, **(guard.get("wb_filled") or {})}
        return {"basis": basis, "settings_applied": applied, "round": record}

    # ---- 会话归档 -------------------------------------------------------------------

    def export_session_context(self, session_id: str, output_dir: str,
                               overwrite=False) -> dict:
        """生成 Session 兼容归档的 context.md 骨架；叙述三段留空给 Agent 补。"""
        session = self._session(session_id)
        if not isinstance(output_dir, str) or not output_dir.strip():
            raise ValueError("output_dir is required")
        out_dir = Path(output_dir).expanduser()
        if not out_dir.is_absolute():
            raise ValueError("output_dir must be an absolute path")
        target = out_dir / "context.md"
        if target.exists() and not overwrite:
            raise StoreError(f"context.md already exists: {target}（传 overwrite=true 覆盖）")

        rounds = session.get("rounds", [])
        artifacts = []
        preview_dir = self.wb.store.preview_dir(session_id)
        for path in sorted(preview_dir.glob("*.jpg")) + sorted((preview_dir.parent / "exports").glob("*")):
            if path.is_file():
                artifacts.append({"file": path.name,
                                  "sha256": _sha256(path.read_bytes()),
                                  "bytes": path.stat().st_size})

        lines = [
            f"# 调色会话归档 · {session_id}",
            "",
            "<!-- 由 trlr_export_session_context 生成；「思路 / 翻车记录 / 修复与结论」三段由 agent 补写 -->",
            "",
            "## 基本信息",
            "",
            f"- session_id: `{session_id}`",
            f"- mode: `{session.get('mode')}`",
            f"- source_uuid: `{session.get('source_uuid')}`",
            f"- reference_uuid: `{session.get('reference_uuid')}`",
            f"- 虚拟副本 working_copy_uuid: `{session.get('working_copy_uuid')}`",
            f"- 创建时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session.get('created_at', 0)))}",
            f"- 轮次总数: {len(rounds)}",
            "",
            "## 轮次时间线",
            "",
        ]
        previous = None
        for record in rounds:
            stamp = time.strftime("%H:%M:%S", time.localtime(record.get("created_at", 0)))
            lines.append(f"### round {record.get('round')} · {stamp}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(record.get("settings") or {},
                                    ensure_ascii=False, indent=2, sort_keys=True))
            lines.append("```")
            if previous is not None:
                diff = self._settings_diff(previous.get("settings"), record.get("settings"))
                lines.append("")
                lines.append(f"相对上一轮参数 diff: `{json.dumps(diff, ensure_ascii=False)}`")
            if record.get("preview_sha256"):
                lines.append(f"- preview_sha256: `{record['preview_sha256']}`")
            lines.append("")
            previous = record
        final_settings = (previous or {}).get("after") or (previous or {}).get("settings") or {}
        lines += [
            "## 最终设置",
            "",
            f"（基准：{'最后一轮 after 回读' if (previous or {}).get('after') else '最后一轮提交值'}）",
            "",
            "```json",
            json.dumps(final_settings, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## Artifact 哈希清单",
            "",
        ]
        if artifacts:
            for item in artifacts:
                lines.append(f"- `{item['file']}` sha256 `{item['sha256']}` ({item['bytes']} B)")
        else:
            lines.append("- （无预览文件）")
        lines += [
            "",
            "## 思路",
            "",
            "<!-- TODO: 本段叙述留空，由 agent 补写 -->",
            "",
            "## 翻车记录",
            "",
            "<!-- TODO -->",
            "",
            "## 修复与结论",
            "",
            "<!-- TODO -->",
            "",
        ]
        out_dir.mkdir(parents=True, exist_ok=True)
        content = "\n".join(lines)
        target.write_text(content, encoding="utf-8")
        return {"context_path": str(target), "sha256": _sha256(content.encode("utf-8")),
                "rounds": len(rounds), "artifacts": len(artifacts),
                "narrative_sections": ["思路", "翻车记录", "修复与结论"]}
