"""Independent image export and deterministic measurement adapters."""
from __future__ import annotations

import hashlib
import builtins
import io
import json
import tempfile
from pathlib import Path

from PIL import Image
from workbench.artifacts import build_artifact_ref
from workbench.image_tools import decode_srgb, measure_image
from workbench.color_patches import (compare_region_signatures,
                                     digital_references, measure_patches,
                                     region_overlay)
from workbench.lrops import LrOpsError
from workbench.srgb_profile import SRGB_ICC
from workbench.state import StoreError

EXPORT_FORMATS = ("jpeg",)
EXPORT_EDGE_LADDER = (2048, 1024, 512)
ANALYSIS_MAX_EDGE = 1024


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _check_edge(value, lo=64, hi=4096, name="max_edge"):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer from {lo} to {hi}")
    return value


class MediaTools:
    def __init__(self, wb):
        self.wb = wb

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

    def export_image(self, photo_uuid,
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
        if not isinstance(photo_uuid, str) or not photo_uuid:
            raise ValueError("photo_uuid must be a nonempty string")
        target_uuid = photo_uuid
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

        export_dir = self.wb.store.root / "exports"
        prefix = "photo"
        export_dir.mkdir(parents=True, exist_ok=True)
        final = export_dir / f"{prefix}-{digest[:16]}.{ext}"
        final.write_bytes(payload)

        source = {"kind": "photo_export", "photo_uuid": target_uuid}
        artifact = build_artifact_ref(
            final, artifact_id=f"photo:{target_uuid}:export:{digest[:16]}",
            source=source, purpose="verified",
            method="LrExportSession",
            freshness="verified",
            evidence_status="verified_render",
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

    def _source_image(self, source):
        """Resolve exactly one supported address into an RGB image and provenance."""
        if not isinstance(source, dict) or not source:
            raise ValueError("source must be a nonempty object")
        selectors = [key for key in ("photo_uuid", "root", "episode_id")
                     if key in source]
        if len(selectors) != 1:
            raise ValueError("source must use exactly one address type: photo_uuid, "
                             "root, or episode_id")

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
        if "episode_id" not in source:
            raise ValueError("comparison source must be a published episode render")
        MediaTools._validate_source_keys(source, {"episode_id", "name"})

    def image_compare(self, left, right, mode="distribution", roi=None,
                      max_edge=512) -> dict:
        """Compare two persisted native renders without selecting a preferred side."""
        # The shared comparison loader validates both addresses and resolves
        # version shortcuts without exposing live catalog or folder sources.
        return self.wb.image_tools.compare(
            left, right,
            mode=mode, roi=roi, max_edge=max_edge)

