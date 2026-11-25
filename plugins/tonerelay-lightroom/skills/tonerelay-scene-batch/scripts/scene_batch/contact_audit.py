"""Post-export Lab metrics, outlier isolation, and paginated contact-sheet renderer."""
from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .raw_io import STANDARD_IMG_EXTS, load_image_and_dimensions, load_preview_image
from .scene_segment import classify_photo_anomaly, compute_lab_metrics, load_font


def _analyze_one_export(path: Path, scene_id: str, cell_width: int, cell_img_h: int) -> dict:
    img, w, h = load_image_and_dimensions(path, max_edge=max(cell_width, 256))
    analysis_im = img.copy()
    if max(analysis_im.size) > 160:
        analysis_im.thumbnail((160, 160), Image.Resampling.BILINEAR)
    metrics = compute_lab_metrics(np.asarray(analysis_im, dtype=np.uint8))

    thumb = img
    thumb.thumbnail((cell_width, cell_img_h), Image.Resampling.LANCZOS)
    return {
        "stem": path.stem.upper() if path.stem.upper().startswith("DSC") else path.stem,
        "file_name": path.name,
        "path": str(path),
        "scene_id": scene_id,
        "dimensions": [w, h],
        "bytes": path.stat().st_size,
        "neutral_a": metrics["neutral_a"],
        "neutral_b": metrics["neutral_b"],
        "neutral_l": metrics["neutral_l"],
        "mean_l": metrics["mean_l"],
        "median_l": metrics["median_l"],
        "p05_l": metrics["p05_l"],
        "p95_l": metrics["p95_l"],
        "clip_high_pct": metrics["clip_high_pct"],
        "clip_low_pct": metrics["clip_low_pct"],
        "_thumb": thumb,
    }


def _short_badge(reasons: list[str]) -> str:
    mapping = {
        "overexposed": "EXP+",
        "underexposed": "EXP-",
        "highlight_clip": "CLIP",
        "shadow_crush": "SHAD",
        "wb_shift_cool": "WB-COOL",
        "wb_shift_warm": "WB-WARM",
        "wb_shift_green": "WB-GRN",
        "wb_shift_magenta": "WB-MAG",
        "wb_shift": "WB",
    }
    tags = [mapping.get(r, r.upper()) for r in reasons]
    return ",".join(tags[:2])


def _apply_grid_overlay(im: Image.Image, font: ImageFont.ImageFont) -> Image.Image:
    """Draw a subtle rule-of-thirds, center horizon cross, and coordinate ticks for crop/tilt checks."""
    w, h = im.size
    if w < 40 or h < 40:
        return im
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for frac in (1.0 / 3.0, 2.0 / 3.0):
        gx = int(round(w * frac))
        gy = int(round(h * frac))
        od.line([(gx, 0), (gx, h)], fill=(255, 255, 255, 85), width=1)
        od.line([(0, gy), (w, gy)], fill=(255, 255, 255, 85), width=1)
    cx = w // 2
    cy = h // 2
    od.line([(cx, 0), (cx, h)], fill=(0, 225, 255, 115), width=1)
    od.line([(0, cy), (w, cy)], fill=(0, 225, 255, 115), width=1)
    for frac, lbl in ((0.2, ".2"), (0.5, ".5"), (0.8, ".8")):
        tx = int(round(w * frac))
        ty = int(round(h * frac))
        od.line([(tx, 0), (tx, 6)], fill=(0, 225, 255, 180), width=1)
        od.line([(0, ty), (6, ty)], fill=(0, 225, 255, 180), width=1)
        od.text((tx + 2, 2), lbl, fill=(0, 225, 255, 200), font=font)
        od.text((3, max(2, ty - 11)), lbl, fill=(0, 225, 255, 200), font=font)
    return Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")


def _render_sheet(
    items: list[dict],
    output_path: Path,
    *,
    title: str,
    cols: int,
    rows: int,
    cell_width: int,
    grid: bool = False,
) -> Path:
    pad = 12
    cell_img_h = int(round(cell_width * 0.68))
    label_h = 34
    cell_h = cell_img_h + label_h
    title_h = 38

    width = pad + cols * (cell_width + pad)
    actual_rows = max(1, math.ceil(len(items) / cols))
    height = title_h + actual_rows * (cell_h + pad) + pad

    canvas = Image.new("RGB", (width, height), (22, 24, 28))
    draw = ImageDraw.Draw(canvas)
    font_title = load_font(16)
    font_cell = load_font(12)
    font_badge = load_font(11)

    draw.text((pad, 10), title, fill=(242, 244, 247), font=font_title)

    for idx, item in enumerate(items):
        r = idx // cols
        c = idx % cols
        x0 = pad + c * (cell_width + pad)
        y0 = title_h + r * (cell_h + pad)

        im = item.get("_thumb")
        if im is None:
            try:
                im = load_preview_image(Path(item["path"]), max_edge=cell_width)
                im.thumbnail((cell_width, cell_img_h), Image.Resampling.LANCZOS)
            except OSError:
                im = Image.new("RGB", (cell_width, cell_img_h), (65, 35, 35))

        px = x0 + (cell_width - im.width) // 2
        py = y0 + (cell_img_h - im.height) // 2
        render_im = _apply_grid_overlay(im, font_badge) if grid else im
        canvas.paste(render_im, (px, py))

        is_out = item.get("is_outlier", False)
        score = item.get("anomaly_score", 0.0)
        border_col = (239, 68, 68) if score >= 1.5 else ((245, 158, 11) if is_out else (75, 82, 92))
        draw.rectangle([x0, y0, x0 + cell_width, y0 + cell_img_h], outline=border_col, width=3 if is_out else 1)

        if is_out:
            badge = _short_badge(item.get("reasons", []))
            bw = max(54, len(badge) * 7 + 12)
            draw.rectangle([x0 + 4, y0 + 4, x0 + 4 + bw, y0 + 22], fill=border_col)
            draw.text((x0 + 8, y0 + 6), badge, fill=(255, 255, 255), font=font_badge)

        line1 = f"{item['stem']} [{item['scene_id']}] L={item['median_l']:.0f} clip={item['clip_high_pct']:.1f}%"
        line2 = f"a={item['neutral_a']:+.1f} b={item['neutral_b']:+.1f}"
        if item.get("reasons"):
            line2 += " | " + ",".join(item["reasons"][:2])
        draw.text((x0 + 2, y0 + cell_img_h + 3), line1, fill=(235, 238, 242), font=font_cell)
        draw.text((x0 + 2, y0 + cell_img_h + 18), line2, fill=(180, 188, 198), font=font_badge)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="JPEG", quality=90)
    return output_path


def run_audit(
    input_dir: Path,
    output_dir: Path,
    *,
    scenes_path: Path | None = None,
    cols: int = 5,
    rows: int = 4,
    cell_width: int = 420,
    wb_outlier_delta: float = 6.5,
    exp_l_delta: float = 16.0,
    clip_high_threshold: float = 7.5,
    workers: int = min(4, os.cpu_count() or 1),
    grid: bool = False,
) -> dict:
    """Measure exported images in a single pass, flag outliers, and render paginated contact sheets."""
    t0 = time.monotonic()
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Export directory not found: {input_dir}")

    stem_to_scene: dict[str, str] = {}
    if scenes_path and Path(scenes_path).is_file():
        sdoc = json.loads(Path(scenes_path).read_text(encoding="utf-8"))
        for p in sdoc.get("photos", []):
            stem_to_scene[p["stem"].upper()] = p.get("scene_id", "S01")

    files = sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in STANDARD_IMG_EXTS
    )
    if not files:
        raise ValueError(f"No exported image files found in {input_dir}")

    cell_img_h = int(round(cell_width * 0.68))
    if workers <= 1:
        records = [
            _analyze_one_export(p, stem_to_scene.get(p.stem.upper(), "S01"), cell_width, cell_img_h)
            for p in files
        ]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(files))) as pool:
            records = list(
                pool.map(
                    lambda p: _analyze_one_export(p, stem_to_scene.get(p.stem.upper(), "S01"), cell_width, cell_img_h),
                    files,
                )
            )

    by_scene: dict[str, list[dict]] = {}
    for rec in records:
        by_scene.setdefault(rec["scene_id"], []).append(rec)

    outliers: list[dict] = []
    for sid, group in sorted(by_scene.items()):
        med_a = float(np.median([x["neutral_a"] for x in group]))
        med_b = float(np.median([x["neutral_b"] for x in group]))
        med_l = float(np.median([x["median_l"] for x in group]))
        for rec in group:
            reasons, score = classify_photo_anomaly(
                rec, med_a, med_b, med_l,
                wb_outlier_delta=wb_outlier_delta,
                exp_l_delta=exp_l_delta,
                clip_high_threshold=clip_high_threshold,
            )
            rec["is_outlier"] = bool(reasons)
            rec["reasons"] = reasons
            rec["anomaly_score"] = score
            if reasons:
                outliers.append(rec)

    outliers.sort(key=lambda x: x["anomaly_score"], reverse=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    per_sheet = max(1, cols * rows)
    outlier_sheets: list[str] = []
    if outliers:
        num_osheets = math.ceil(len(outliers) / per_sheet)
        for idx in range(num_osheets):
            chunk = outliers[idx * per_sheet:(idx + 1) * per_sheet]
            name = "outliers_sheet.jpg" if num_osheets == 1 else f"outliers_sheet_{idx + 1:02d}.jpg"
            p = _render_sheet(
                chunk,
                output_dir / name,
                title=f"Flagged Outliers ({idx + 1}/{num_osheets} | {len(outliers)} total flagged of {len(records)})",
                cols=cols,
                rows=rows,
                cell_width=cell_width,
                grid=grid,
            )
            outlier_sheets.append(str(p))

    audit_sheets: list[str] = []
    num_sheets = math.ceil(len(records) / per_sheet)
    for idx in range(num_sheets):
        chunk = records[idx * per_sheet:(idx + 1) * per_sheet]
        p = _render_sheet(
            chunk,
            output_dir / f"audit_sheet_{idx + 1:02d}.jpg",
            title=f"Export Audit Sheet {idx + 1}/{num_sheets} ({chunk[0]['stem']}..{chunk[-1]['stem']})",
            cols=cols,
            rows=rows,
            cell_width=cell_width,
            grid=grid,
        )
        audit_sheets.append(str(p))

    clean_records = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
    metrics_doc = {
        "schema_version": "audit-metrics-v1",
        "input_dir": str(input_dir),
        "photo_count": len(clean_records),
        "outlier_count": len(outliers),
        "outliers": [
            {
                "stem": x["stem"],
                "scene_id": x["scene_id"],
                "reasons": x["reasons"],
                "anomaly_score": x["anomaly_score"],
                "median_l": x["median_l"],
                "clip_high_pct": x["clip_high_pct"],
                "neutral_a": x["neutral_a"],
                "neutral_b": x["neutral_b"],
            }
            for x in outliers
        ],
        "photos": clean_records,
    }
    metrics_path = output_dir / "audit_metrics.json"
    metrics_path.write_text(json.dumps(metrics_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "elapsed_sec": round(time.monotonic() - t0, 2),
        "photo_count": len(records),
        "outlier_count": len(outliers),
        "audit_metrics": str(metrics_path),
        "outlier_sheets": outlier_sheets,
        "audit_sheets": audit_sheets,
        "top_outliers": metrics_doc["outliers"][:15],
    }
