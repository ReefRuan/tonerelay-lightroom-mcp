"""Zero-token fixed-WB scene segmentation, lighting clustering, outlier flagging, and visual maps."""
from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .raw_io import SUPPORTED_EXTS, compute_sharpness, load_preview_image, read_exif_and_preview_meta

_SCENE_PALETTE = [
    (66, 133, 244),
    (234, 67, 53),
    (251, 188, 5),
    (52, 168, 83),
    (171, 71, 188),
    (0, 172, 193),
    (255, 112, 67),
    (124, 179, 66),
    (92, 107, 192),
    (240, 98, 146),
    (141, 110, 99),
    (38, 166, 154),
]

_FONT_CACHE: dict[int, ImageFont.ImageFont | ImageFont.FreeTypeFont] = {}


def load_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Load a crisp local sans/mono font across platforms with safe default fallback."""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for candidate in (
        r"C:\Windows\Fonts\consola.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, size=size)
                _FONT_CACHE[size] = font
                return font
            except OSError:
                pass
    font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def rgb_to_lab(rgb_u8: np.ndarray) -> np.ndarray:
    """Convert uint8 sRGB image (H, W, 3) to float32 CIE L*a*b* (D65)."""
    srgb = np.asarray(rgb_u8, dtype=np.float32) / 255.0
    lin = np.where(srgb <= 0.04045, srgb / 12.92, np.power((srgb + 0.055) / 1.055, 2.4))

    r, g, b = lin[..., 0], lin[..., 1], lin[..., 2]
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) / 1.00000
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    eps = 216.0 / 24389.0
    kappa = 24389.0 / 27.0

    def _f(t: np.ndarray) -> np.ndarray:
        return np.where(t > eps, np.cbrt(t), (kappa * t + 16.0) / 116.0)

    fx, fy, fz = _f(x), _f(y), _f(z)
    l_star = 116.0 * fy - 16.0
    a_star = 500.0 * (fx - fy)
    b_star = 200.0 * (fy - fz)
    return np.stack((l_star, a_star, b_star), axis=-1)


def _neutral_cast_rgb_swatch(neutral_a: float, neutral_b: float, l_star: float = 75.0) -> list[int]:
    """Render an approximate sRGB neutral swatch (at given L*) for barcode visualization."""
    fy = (l_star + 16.0) / 116.0
    fx = fy + neutral_a / 500.0
    fz = fy - neutral_b / 200.0
    eps3 = 6.0 / 29.0
    x = (fx ** 3 if fx > eps3 else 3 * (eps3 ** 2) * (fx - 4.0 / 29.0)) * 0.95047
    y = (fy ** 3 if fy > eps3 else 3 * (eps3 ** 2) * (fy - 4.0 / 29.0)) * 1.00000
    z = (fz ** 3 if fz > eps3 else 3 * (eps3 ** 2) * (fz - 4.0 / 29.0)) * 1.08883

    r_lin = 3.2404542 * x - 1.5371385 * y - 0.4985314 * z
    g_lin = -0.9692660 * x + 1.8760108 * y + 0.0415560 * z
    b_lin = 0.0556434 * x - 0.2040259 * y + 1.0572252 * z
    out = []
    for c in (r_lin, g_lin, b_lin):
        c = max(0.0, min(1.0, c))
        s = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055
        out.append(int(round(max(0.0, min(1.0, s)) * 255.0)))
    return out


def compute_lab_metrics(rgb_u8: np.ndarray) -> dict:
    """Compute neutral illuminant color cast, exposure percentiles, clipping, and Lab histograms."""
    lab = rgb_to_lab(rgb_u8)
    l_ch = lab[..., 0].ravel()
    a_ch = lab[..., 1].ravel()
    b_ch = lab[..., 2].ravel()

    chroma = np.hypot(a_ch, b_ch)
    mid_mask = (l_ch >= 18.0) & (l_ch <= 94.0)
    if int(np.count_nonzero(mid_mask)) >= 16:
        mid_c = chroma[mid_mask]
        cutoff = float(np.percentile(mid_c, 35.0))
        neutral_mask = mid_mask & (chroma <= max(cutoff, 2.0))
    else:
        cutoff = float(np.percentile(chroma, 35.0))
        neutral_mask = chroma <= max(cutoff, 2.0)

    if int(np.count_nonzero(neutral_mask)) == 0:
        neutral_mask = np.ones_like(l_ch, dtype=bool)

    neutral_a = float(np.median(a_ch[neutral_mask]))
    neutral_b = float(np.median(b_ch[neutral_mask]))
    neutral_l = float(np.median(l_ch[neutral_mask]))

    max_rgb = rgb_u8.max(axis=-1).ravel()
    clip_high_pct = float(np.mean(max_rgb >= 250) * 100.0)
    clip_low_pct = float(np.mean(max_rgb <= 10) * 100.0)

    hl, _ = np.histogram(l_ch, bins=8, range=(0.0, 100.0))
    ha, _ = np.histogram(a_ch, bins=12, range=(-40.0, 40.0))
    hb, _ = np.histogram(b_ch, bins=12, range=(-40.0, 40.0))
    n_pix = max(1, l_ch.size)

    return {
        "neutral_a": round(neutral_a, 3),
        "neutral_b": round(neutral_b, 3),
        "neutral_l": round(neutral_l, 3),
        "mean_l": round(float(np.mean(l_ch)), 3),
        "median_l": round(float(np.median(l_ch)), 3),
        "p05_l": round(float(np.percentile(l_ch, 5.0)), 3),
        "p95_l": round(float(np.percentile(l_ch, 95.0)), 3),
        "clip_high_pct": round(clip_high_pct, 3),
        "clip_low_pct": round(clip_low_pct, 3),
        "hist_l": [round(float(v) / n_pix, 5) for v in hl],
        "hist_a": [round(float(v) / n_pix, 5) for v in ha],
        "hist_b": [round(float(v) / n_pix, 5) for v in hb],
    }


def extract_photo_features(path: Path, analysis_edge: int = 160) -> dict:
    """Extract fast EXIF, Lab neutral cast, histograms, sharpness, and barcode strip for one photo."""
    path = Path(path)
    meta = read_exif_and_preview_meta(path)
    img = load_preview_image(path, meta=meta, max_edge=analysis_edge)
    rgb = np.asarray(img, dtype=np.uint8)
    metrics = compute_lab_metrics(rgb)
    sharpness = round(compute_sharpness(rgb), 2)

    # 24 vertical bands of image mean RGB + 8 solid neutral swatch bands = 32-band column
    strip_img = img.resize((1, 24), Image.Resampling.BILINEAR)
    top_bands = np.asarray(strip_img, dtype=np.uint8).reshape(24, 3)
    swatch = np.array(_neutral_cast_rgb_swatch(metrics["neutral_a"], metrics["neutral_b"]), dtype=np.uint8)
    bottom_bands = np.repeat(swatch[None, :], 8, axis=0)
    barcode_col = np.vstack([top_bands, bottom_bands])

    return {
        "file_name": meta["file_name"],
        "stem": meta["stem"],
        "path": meta["path"],
        "dt": meta.get("dt"),
        "timestamp": meta.get("timestamp"),
        "iso": meta.get("iso"),
        "bv": round(meta["bv"], 3) if isinstance(meta.get("bv"), (int, float)) else None,
        "ev": round(meta["ev"], 3) if isinstance(meta.get("ev"), (int, float)) else None,
        "fnum": round(meta["fnum"], 2) if isinstance(meta.get("fnum"), (int, float)) else None,
        "shutter": meta.get("shutter"),
        "orientation": meta.get("orientation", 1),
        "sharpness": sharpness,
        **metrics,
        "_barcode_col": barcode_col,
    }


def _l1(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sum(np.abs(a - b)))


def _window_signature(items: list[dict]) -> tuple[float, float, float, np.ndarray, np.ndarray, np.ndarray, float | None]:
    na = float(np.median([x["neutral_a"] for x in items]))
    nb = float(np.median([x["neutral_b"] for x in items]))
    nl = float(np.median([x["neutral_l"] for x in items]))
    hl = np.mean([x.get("hist_l", [0.0] * 8) for x in items], axis=0)
    ha = np.mean([x.get("hist_a", [0.0] * 12) for x in items], axis=0)
    hb = np.mean([x.get("hist_b", [0.0] * 12) for x in items], axis=0)
    bvs = [x["bv"] for x in items if x.get("bv") is not None]
    bv = float(np.median(bvs)) if bvs else None
    return na, nb, nl, hl, ha, hb, bv


def _pair_distance(sig1, sig2) -> tuple[float, float, float, float]:
    na1, nb1, nl1, hl1, ha1, hb1, bv1 = sig1
    na2, nb2, nl2, hl2, ha2, hb2, bv2 = sig2
    wb_dist = math.hypot(na2 - na1, nb2 - nb1)
    hist_dist = 0.5 * (_l1(ha2, ha1) + _l1(hb2, hb1)) + 0.3 * _l1(hl2, hl1)
    bv_dist = abs(bv2 - bv1) if (bv1 is not None and bv2 is not None) else 0.0
    l_dist = abs(nl2 - nl1)
    return wb_dist, hist_dist, bv_dist, l_dist


def segment_timeline(
    photos: list[dict],
    window: int = 5,
    min_segment: int = 5,
    time_gap_sec: float = 180.0,
    wb_jump_threshold: float = 6.0,
    sensitivity: float = 1.0,
) -> list[dict]:
    """Split chronologically ordered photos into lighting-consistent timeline segments."""
    if not photos:
        return []
    photos.sort(key=lambda x: (x["timestamp"] if x["timestamp"] is not None else float("inf"), x["stem"]))
    n = len(photos)
    if n == 1:
        return [_build_segment("T01", 0, 1, photos, "single")]

    sens = max(0.25, float(sensitivity))
    w = min(window, max(2, n // 4)) if n >= 6 else max(1, n // 2)
    eff_wb_thresh = wb_jump_threshold / sens
    eff_score_thresh = (wb_jump_threshold * 1.15) / sens

    scores = [0.0] * n
    reasons = [""] * n
    wb_jumps = [0.0] * n
    short_wb_peaks = [0.0] * n

    for i in range(1, n):
        left = photos[max(0, i - w):i]
        right = photos[i:min(n, i + w)]
        wb_d, _, bv_d, l_d = _pair_distance(_window_signature(left), _window_signature(right))

        t_prev = photos[i - 1].get("timestamp")
        t_curr = photos[i].get("timestamp")
        gap = (t_curr - t_prev) if (t_prev is not None and t_curr is not None and t_curr >= t_prev) else 0.0
        gap_bonus = min(3.0, gap / time_gap_sec) * 2.5 if time_gap_sec > 0 and gap >= time_gap_sec else 0.0

        # Consistency-gated 2-frame detector: triggers only when BOTH sides of boundary i
        # are internally consistent and the WB jump is dramatic (filters out colored clothes/1-frame outliers).
        wb_small = 0.0
        if i >= 2 and i + 2 <= n:
            a0, a1 = photos[i - 2], photos[i - 1]
            b0, b1 = photos[i], photos[i + 1]
            intra_l = math.hypot(a1["neutral_a"] - a0["neutral_a"], a1["neutral_b"] - a0["neutral_b"])
            intra_r = math.hypot(b1["neutral_a"] - b0["neutral_a"], b1["neutral_b"] - b0["neutral_b"])
            inter = math.hypot(
                0.5 * (b0["neutral_a"] + b1["neutral_a"]) - 0.5 * (a0["neutral_a"] + a1["neutral_a"]),
                0.5 * (b0["neutral_b"] + b1["neutral_b"]) - 0.5 * (a0["neutral_b"] + a1["neutral_b"]),
            )
            if inter >= eff_wb_thresh * 1.75 and intra_l <= inter * 0.40 and intra_r <= inter * 0.40:
                wb_small = inter

        # Score driven by ambient neutral WB shift + exposure BV shift + time gap (not subject clothing colors)
        lum_term = 1.6 * bv_d if bv_d > 0 else 0.12 * l_d
        score = wb_d + lum_term + gap_bonus
        if wb_small > 0.0:
            score = max(score, wb_small + gap_bonus)
            short_wb_peaks[i] = wb_small

        scores[i] = score
        wb_jumps[i] = max(wb_d, wb_small)
        tag_parts = []
        if max(wb_d, wb_small) >= eff_wb_thresh:
            tag_parts.append(f"wb_jump={max(wb_d, wb_small):.1f}")
        if gap >= time_gap_sec:
            tag_parts.append(f"gap={int(gap)}s")
        if bv_d >= 1.2:
            tag_parts.append(f"bv_jump={bv_d:.1f}")
        reasons[i] = ",".join(tag_parts) or f"score={score:.1f}"

    # Local-peak selection (plus direct short-visit WB peaks)
    candidate_cuts: list[int] = []
    for i in range(1, n):
        t_prev = photos[i - 1].get("timestamp")
        t_curr = photos[i].get("timestamp")
        gap = (t_curr - t_prev) if (t_prev is not None and t_curr is not None and t_curr >= t_prev) else 0.0
        strong_gap = time_gap_sec > 0 and gap >= 2.0 * time_gap_sec
        is_short_cut = short_wb_peaks[i] > 0.0 and short_wb_peaks[i] >= short_wb_peaks[i - 1] and (i == n - 1 or short_wb_peaks[i] > short_wb_peaks[i + 1])

        if not is_short_cut:
            if scores[i] < eff_score_thresh and not strong_gap:
                continue
            left_ok = i == 1 or scores[i] >= scores[i - 1]
            right_ok = i == n - 1 or scores[i] > scores[i + 1]
            if not (left_ok and right_ok):
                continue

        min_dist = 2 if is_short_cut else min_segment
        if not candidate_cuts:
            if i >= min_dist or strong_gap:
                candidate_cuts.append(i)
        else:
            prev_cut = candidate_cuts[-1]
            if (i - prev_cut) >= min_dist or strong_gap:
                candidate_cuts.append(i)
            elif is_short_cut and short_wb_peaks[prev_cut] == 0.0:
                candidate_cuts[-1] = i
            elif scores[i] > scores[prev_cut] and (is_short_cut or short_wb_peaks[prev_cut] == 0.0):
                candidate_cuts[-1] = i

    # Ensure final segment is not an accidental 1-frame noise tail unless huge gap/WB shift
    if candidate_cuts and (n - candidate_cuts[-1]) < 2:
        last_cut = candidate_cuts[-1]
        t_prev = photos[last_cut - 1].get("timestamp")
        t_curr = photos[last_cut].get("timestamp")
        gap = (t_curr - t_prev) if (t_prev is not None and t_curr is not None) else 0.0
        if wb_jumps[last_cut] < eff_wb_thresh * 1.8 and gap < 2.0 * time_gap_sec:
            candidate_cuts.pop()

    boundaries = [0] + candidate_cuts + [n]
    segments = []
    for idx in range(len(boundaries) - 1):
        s_idx, e_idx = boundaries[idx], boundaries[idx + 1]
        seg_id = f"T{idx + 1:02d}"
        reason = reasons[s_idx] if s_idx > 0 else "start"
        segments.append(_build_segment(seg_id, s_idx, e_idx, photos, reason))
    return segments


def _build_segment(seg_id: str, start: int, end: int, photos: list[dict], cut_reason: str) -> dict:
    sub = photos[start:end]
    na, nb, nl, hl, ha, hb, bv = _window_signature(sub)
    for item in sub:
        item["segment_id"] = seg_id
    return {
        "segment_id": seg_id,
        "start_index": start,
        "end_index": end,
        "count": len(sub),
        "start_stem": sub[0]["stem"],
        "end_stem": sub[-1]["stem"],
        "cut_reason": cut_reason,
        "neutral_a": round(na, 3),
        "neutral_b": round(nb, 3),
        "neutral_l": round(nl, 3),
        "median_bv": round(bv, 3) if bv is not None else None,
        "hist_l": hl.tolist(),
        "hist_a": ha.tolist(),
        "hist_b": hb.tolist(),
        "stems": [x["stem"] for x in sub],
    }


def _segment_distance(seg1: dict, seg2: dict) -> float:
    wb_d = math.hypot(seg2["neutral_a"] - seg1["neutral_a"], seg2["neutral_b"] - seg1["neutral_b"])
    bv1, bv2 = seg1.get("median_bv"), seg2.get("median_bv")
    if bv1 is not None and bv2 is not None:
        return wb_d + 1.4 * abs(bv2 - bv1)
    return wb_d + 0.12 * abs(seg2["neutral_l"] - seg1["neutral_l"])


def cluster_segments(
    segments: list[dict],
    photos: list[dict],
    cluster_threshold: float = 6.0,
) -> list[dict]:
    """Cluster non-adjacent timeline segments with similar lighting into unified Lighting Scenes."""
    if not segments:
        return []
    clusters: list[list[int]] = [[i] for i in range(len(segments))]

    while len(clusters) > 1:
        best_dist = float("inf")
        best_pair = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                dists = [_segment_distance(segments[a], segments[b]) for a in clusters[i] for b in clusters[j]]
                avg_d = sum(dists) / len(dists)
                if avg_d < best_dist:
                    best_dist = avg_d
                    best_pair = (i, j)
        if best_pair is None or best_dist > cluster_threshold:
            break
        i, j = best_pair
        clusters[i].extend(clusters[j])
        clusters.pop(j)

    # Absorb tiny isolated <=3-frame sub-clusters if reasonably close to a larger lighting cluster
    def _cluster_count(idxs: list[int]) -> int:
        return sum(segments[k]["count"] for k in idxs)

    major_indices = [idx for idx, c in enumerate(clusters) if _cluster_count(c) >= 6]
    if major_indices:
        changed = True
        while changed:
            changed = False
            for idx, c in enumerate(clusters):
                if _cluster_count(c) <= 3 and len(clusters) > 1:
                    best_target, best_d = None, float("inf")
                    for target_idx, tc in enumerate(clusters):
                        if target_idx == idx or _cluster_count(tc) < 6:
                            continue
                        d = sum(_segment_distance(segments[a], segments[b]) for a in c for b in tc) / (len(c) * len(tc))
                        if d < best_d:
                            best_d = d
                            best_target = target_idx
                    if best_target is not None and best_d <= max(cluster_threshold * 1.5, 9.0):
                        clusters[best_target].extend(c)
                        clusters.pop(idx)
                        changed = True
                        break

    # Order clusters by earliest timeline appearance
    clusters.sort(key=lambda idxs: min(idxs))
    photo_by_stem = {p["stem"]: p for p in photos}
    scenes = []
    for c_idx, seg_indices in enumerate(clusters):
        scene_id = f"S{c_idx + 1:02d}"
        seg_indices.sort()
        member_segs = [segments[k] for k in seg_indices]
        stems: list[str] = []
        for seg in member_segs:
            seg["scene_id"] = scene_id
            stems.extend(seg["stems"])
        scene_photos = [photo_by_stem[s] for s in stems]
        for p in scene_photos:
            p["scene_id"] = scene_id
        na = float(np.median([p["neutral_a"] for p in scene_photos]))
        nb = float(np.median([p["neutral_b"] for p in scene_photos]))
        nl = float(np.median([p["neutral_l"] for p in scene_photos]))
        bvs = [p["bv"] for p in scene_photos if p.get("bv") is not None]
        scenes.append({
            "scene_id": scene_id,
            "count": len(scene_photos),
            "segment_ids": [seg["segment_id"] for seg in member_segs],
            "ranges": [f"{seg['start_stem']}..{seg['end_stem']}" for seg in member_segs],
            "neutral_a": round(na, 3),
            "neutral_b": round(nb, 3),
            "neutral_l": round(nl, 3),
            "median_bv": round(float(np.median(bvs)), 3) if bvs else None,
            "stems": stems,
        })
    return scenes


def classify_photo_anomaly(
    photo: dict,
    scene_na: float,
    scene_nb: float,
    scene_med_l: float,
    *,
    wb_outlier_delta: float = 5.5,
    exp_l_delta: float = 14.0,
    clip_high_threshold: float = 4.0,
) -> tuple[list[str], float]:
    """Compare one photo against its scene's median cast and exposure."""
    da = photo["neutral_a"] - scene_na
    db = photo["neutral_b"] - scene_nb
    dl = photo["median_l"] - scene_med_l
    wb_err = math.hypot(da, db)

    reasons: list[str] = []
    if wb_err >= wb_outlier_delta:
        if db <= -wb_outlier_delta * 0.65:
            reasons.append("wb_shift_cool")
        elif db >= wb_outlier_delta * 0.65:
            reasons.append("wb_shift_warm")
        if da <= -wb_outlier_delta * 0.65:
            reasons.append("wb_shift_green")
        elif da >= wb_outlier_delta * 0.65:
            reasons.append("wb_shift_magenta")
        if not reasons:
            reasons.append("wb_shift")

    if dl >= exp_l_delta:
        reasons.append("overexposed")
    elif dl <= -exp_l_delta:
        reasons.append("underexposed")

    if photo["clip_high_pct"] >= clip_high_threshold:
        reasons.append("highlight_clip")
    if photo["clip_low_pct"] >= 35.0 and photo["median_l"] < 25.0:
        reasons.append("shadow_crush")

    score = (wb_err / max(1.0, wb_outlier_delta)) + (abs(dl) / max(1.0, exp_l_delta))
    if photo["clip_high_pct"] >= clip_high_threshold:
        score += 0.6
    return reasons, round(score, 3)


def pick_anchors_and_outliers(
    scenes: list[dict],
    photos: list[dict],
    *,
    wb_outlier_delta: float = 5.5,
    exp_l_delta: float = 14.0,
    clip_high_threshold: float = 4.0,
) -> None:
    """Pick one sharp, representative anchor photo and flag outliers inside each Lighting Scene."""
    photo_by_stem = {p["stem"]: p for p in photos}
    for scene in scenes:
        items = [photo_by_stem[s] for s in scene["stems"]]
        na = float(np.median([x["neutral_a"] for x in items]))
        nb = float(np.median([x["neutral_b"] for x in items]))
        med_l = float(np.median([x["median_l"] for x in items]))
        med_sharp = max(1.0, float(np.median([x["sharpness"] for x in items])))

        best_stem = items[0]["stem"]
        best_cost = float("inf")
        outliers = []

        for item in items:
            reasons, anom_score = classify_photo_anomaly(
                item, na, nb, med_l,
                wb_outlier_delta=wb_outlier_delta,
                exp_l_delta=exp_l_delta,
                clip_high_threshold=clip_high_threshold,
            )
            item["is_outlier"] = bool(reasons)
            item["outlier_reasons"] = reasons
            item["anomaly_score"] = anom_score
            if reasons:
                outliers.append({
                    "stem": item["stem"],
                    "reasons": reasons,
                    "anomaly_score": anom_score,
                    "neutral_a": item["neutral_a"],
                    "neutral_b": item["neutral_b"],
                    "median_l": item["median_l"],
                    "clip_high_pct": item["clip_high_pct"],
                })

            wb_err = math.hypot(item["neutral_a"] - na, item["neutral_b"] - nb)
            l_err = abs(item["median_l"] - med_l)
            sharp_ratio = min(2.5, item["sharpness"] / med_sharp)
            clip_pen = max(0.0, item["clip_high_pct"] - 1.5) * 0.4
            cost = (wb_err / 3.0) + (l_err / 10.0) + clip_pen - 0.35 * sharp_ratio
            if cost < best_cost:
                best_cost = cost
                best_stem = item["stem"]

        outliers.sort(key=lambda x: x["anomaly_score"], reverse=True)
        scene["median_l"] = round(med_l, 3)
        scene["anchor_stem"] = best_stem
        scene["outlier_count"] = len(outliers)
        scene["outliers"] = outliers


def render_color_barcode(
    photos: list[dict],
    segments: list[dict],
    scenes: list[dict],
    output_path: Path,
) -> Path:
    """Render a chronological Movie Barcode + neutral cast strip + scene cut markers."""
    n = len(photos)
    col_w = max(2, min(14, 1800 // max(1, n)))
    margin_x = 36
    top_bar_y, top_bar_h = 44, 24
    strip_y, strip_h = 74, 240
    neutral_y, neutral_h = strip_y + 180, 60
    footer_h = 84

    width = max(920, n * col_w + 2 * margin_x)
    height = strip_y + strip_h + footer_h
    canvas = Image.new("RGB", (width, height), (20, 22, 26))
    draw = ImageDraw.Draw(canvas)
    font_title = load_font(16)
    font_sm = load_font(12)

    draw.text((margin_x, 12), f"Fixed-WB Color Barcode ({n} photos | {len(segments)} timeline segs -> {len(scenes)} lighting scenes)", fill=(240, 242, 245), font=font_title)

    scene_colors = {s["scene_id"]: _SCENE_PALETTE[idx % len(_SCENE_PALETTE)] for idx, s in enumerate(scenes)}

    barcode_arr = np.zeros((strip_h, n * col_w, 3), dtype=np.uint8)
    for i, item in enumerate(photos):
        col = item["_barcode_col"]  # (32, 3)
        col_img = Image.fromarray(col[:, None, :]).resize((col_w, strip_h), Image.Resampling.NEAREST)
        barcode_arr[:, i * col_w:(i + 1) * col_w] = np.asarray(col_img, dtype=np.uint8)

        sc_color = scene_colors.get(item.get("scene_id", ""), (180, 180, 180))
        x0 = margin_x + i * col_w
        draw.rectangle([x0, top_bar_y, x0 + col_w - 1, top_bar_y + top_bar_h], fill=sc_color)

    canvas.paste(Image.fromarray(barcode_arr), (margin_x, strip_y))
    draw.line([(margin_x, neutral_y), (margin_x + n * col_w, neutral_y)], fill=(255, 255, 255), width=1)
    draw.text((margin_x + 4, neutral_y + 4), "Neutral Cast Strip (low-chroma Lab a*/b*)", fill=(25, 25, 25), font=font_sm)

    # Vertical segment cut lines + stem labels
    for seg_idx, seg in enumerate(segments):
        x = margin_x + seg["start_index"] * col_w
        if seg_idx > 0:
            draw.line([(x, top_bar_y), (x, strip_y + strip_h + 10)], fill=(255, 255, 255), width=2)
        label = f"{seg.get('scene_id', '')}:{seg['start_stem']}"
        y_off = strip_y + strip_h + 12 + (seg_idx % 2) * 14
        draw.text((x + 2, y_off), label, fill=(215, 220, 228), font=font_sm)

    # Legend row at bottom
    lx, ly = margin_x, height - 34
    for s in scenes:
        col = scene_colors[s["scene_id"]]
        txt = f"{s['scene_id']}({s['count']})"
        draw.rectangle([lx, ly + 2, lx + 12, ly + 14], fill=col)
        draw.text((lx + 16, ly), txt, fill=(225, 228, 232), font=font_sm)
        lx += 16 + len(txt) * 8 + 14
        if lx > width - 120:
            break

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="JPEG", quality=92)
    return output_path


def render_scene_overview(
    photos: list[dict],
    segments: list[dict],
    scenes: list[dict],
    output_path: Path,
    thumb_w: int = 280,
    thumb_h: int = 190,
) -> Path:
    """Render one visual row per Lighting Scene showing the anchor + representative frames."""
    photo_by_stem = {p["stem"]: p for p in photos}
    pad = 16
    row_header_h = 34
    max_thumbs = 4
    cols = max_thumbs
    width = pad + cols * (thumb_w + pad)
    row_h = row_header_h + thumb_h + 24 + pad
    title_h = 46
    height = title_h + max(1, len(scenes)) * row_h + pad

    canvas = Image.new("RGB", (width, height), (24, 26, 30))
    draw = ImageDraw.Draw(canvas)
    font_title = load_font(18)
    font_hdr = load_font(14)
    font_sm = load_font(12)

    draw.text(
        (pad, 12),
        f"Lighting Scenes Overview — {len(scenes)} Scenes from {len(photos)} Photos",
        fill=(245, 245, 248),
        font=font_title,
    )

    for r_idx, scene in enumerate(scenes):
        y0 = title_h + r_idx * row_h
        sc_color = _SCENE_PALETTE[r_idx % len(_SCENE_PALETTE)]
        ranges_txt = ", ".join(scene["ranges"][:4]) + (" ..." if len(scene["ranges"]) > 4 else "")
        hdr = (
            f"{scene['scene_id']} | {scene['count']} photos | {ranges_txt} | "
            f"a*={scene['neutral_a']:+.1f} b*={scene['neutral_b']:+.1f} | "
            f"Anchor={scene['anchor_stem']} | Outliers={scene['outlier_count']}"
        )
        draw.rectangle([pad, y0, width - pad, y0 + 26], fill=(38, 42, 48))
        draw.rectangle([pad, y0, pad + 8, y0 + 26], fill=sc_color)
        draw.text((pad + 14, y0 + 5), hdr, fill=(240, 242, 245), font=font_hdr)

        stems = scene["stems"]
        anchor = scene["anchor_stem"]
        reps = [anchor]
        if len(stems) > 1:
            picks = [stems[0], stems[len(stems) // 2], stems[-1]]
            for st in picks:
                if st not in reps:
                    reps.append(st)
        reps = reps[:max_thumbs]

        ty = y0 + row_header_h
        for c_idx, stem in enumerate(reps):
            tx = pad + c_idx * (thumb_w + pad)
            item = photo_by_stem[stem]
            try:
                im = load_preview_image(Path(item["path"]), max_edge=max(thumb_w, thumb_h))
                im.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            except OSError:
                im = Image.new("RGB", (thumb_w, thumb_h), (60, 40, 40))

            paste_x = tx + (thumb_w - im.width) // 2
            paste_y = ty + (thumb_h - im.height) // 2
            canvas.paste(im, (paste_x, paste_y))
            border_col = (52, 211, 153) if stem == anchor else (90, 96, 106)
            draw.rectangle([tx, ty, tx + thumb_w, ty + thumb_h], outline=border_col, width=3 if stem == anchor else 1)
            tag = "[ANCHOR] " if stem == anchor else ""
            cap = f"{tag}{stem} | L={item['median_l']:.0f}"
            draw.text((tx + 2, ty + thumb_h + 4), cap, fill=(220, 224, 230), font=font_sm)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="JPEG", quality=90)
    return output_path


def _finalize_segmentation(
    photos: list[dict],
    input_dir: str,
    output_dir: Path,
    *,
    window: int,
    min_segment: int,
    time_gap_sec: float,
    wb_jump_threshold: float,
    cluster_threshold: float,
    sensitivity: float,
    render_overview: bool,
    t0: float,
) -> dict:
    segments = segment_timeline(
        photos,
        window=window,
        min_segment=min_segment,
        time_gap_sec=time_gap_sec,
        wb_jump_threshold=wb_jump_threshold,
        sensitivity=sensitivity,
    )
    scenes = cluster_segments(segments, photos, cluster_threshold=cluster_threshold)
    pick_anchors_and_outliers(scenes, photos)

    output_dir.mkdir(parents=True, exist_ok=True)
    barcode_path = render_color_barcode(photos, segments, scenes, output_dir / "color_barcode.jpg")
    overview_path = (
        render_scene_overview(photos, segments, scenes, output_dir / "scene_overview.jpg")
        if render_overview
        else None
    )

    clean_photos = [
        {k: v for k, v in item.items() if not k.startswith("_") and not k.startswith("hist_")}
        for item in photos
    ]
    clean_segments = [
        {k: v for k, v in seg.items() if not k.startswith("hist_")}
        for seg in segments
    ]

    result_doc = {
        "schema_version": "scene-segmentation-v1",
        "input_dir": input_dir,
        "photo_count": len(clean_photos),
        "segment_count": len(clean_segments),
        "scene_count": len(scenes),
        "params": {
            "window": window,
            "min_segment": min_segment,
            "time_gap_sec": time_gap_sec,
            "wb_jump_threshold": wb_jump_threshold,
            "cluster_threshold": cluster_threshold,
            "sensitivity": sensitivity,
        },
        "scenes": scenes,
        "segments": clean_segments,
        "photos": clean_photos,
    }
    scenes_json = output_dir / "scenes.json"
    scenes_json.write_text(json.dumps(result_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "elapsed_sec": round(time.monotonic() - t0, 2),
        "photo_count": len(clean_photos),
        "segment_count": len(clean_segments),
        "scene_count": len(scenes),
        "scenes_json": str(scenes_json),
        "color_barcode": str(barcode_path),
        "scene_overview": str(overview_path) if overview_path else None,
        "scenes_summary": [
            {
                "scene_id": s["scene_id"],
                "count": s["count"],
                "ranges": s["ranges"],
                "anchor_stem": s["anchor_stem"],
                "neutral_a": s["neutral_a"],
                "neutral_b": s["neutral_b"],
                "outlier_count": s["outlier_count"],
            }
            for s in scenes
        ],
    }


def run_segmentation(
    input_dir: Path,
    output_dir: Path,
    *,
    stems_filter: set[str] | None = None,
    window: int = 5,
    min_segment: int = 5,
    time_gap_sec: float = 180.0,
    wb_jump_threshold: float = 6.0,
    cluster_threshold: float = 6.0,
    sensitivity: float = 1.0,
    analysis_edge: int = 160,
    workers: int = min(4, os.cpu_count() or 1),
) -> dict:
    """Scan an image/RAW folder, segment scenes, and write artifacts."""
    t0 = time.monotonic()
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    files = sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )
    if stems_filter:
        norm = {s.upper() for s in stems_filter}
        files = [p for p in files if p.stem.upper() in norm]
    if not files:
        raise ValueError(f"No supported image/RAW files found in {input_dir}")

    if workers <= 1:
        photos = [extract_photo_features(p, analysis_edge=analysis_edge) for p in files]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(files))) as pool:
            photos = list(pool.map(lambda p: extract_photo_features(p, analysis_edge=analysis_edge), files))

    return _finalize_segmentation(
        photos,
        str(input_dir),
        output_dir,
        window=window,
        min_segment=min_segment,
        time_gap_sec=time_gap_sec,
        wb_jump_threshold=wb_jump_threshold,
        cluster_threshold=cluster_threshold,
        sensitivity=sensitivity,
        render_overview=True,
        t0=t0,
    )


def resegment_from_json(
    scenes_json_path: Path,
    output_dir: Path,
    *,
    window: int = 5,
    min_segment: int = 5,
    time_gap_sec: float = 180.0,
    wb_jump_threshold: float = 6.0,
    cluster_threshold: float = 6.0,
    sensitivity: float = 1.0,
    render_images: bool = False,
) -> dict:
    """Re-run scene segmentation purely from cached scenes.json features (zero disk reads on photo folder)."""
    t0 = time.monotonic()
    scenes_json_path = Path(scenes_json_path)
    output_dir = Path(output_dir)
    doc = json.loads(scenes_json_path.read_text(encoding="utf-8"))
    photos: list[dict] = []
    for item in doc.get("photos", []):
        p = dict(item)
        na = float(p.get("neutral_a", 0.0))
        nb = float(p.get("neutral_b", 0.0))
        med_l = float(p.get("median_l", 50.0))
        top_swatch = np.array(_neutral_cast_rgb_swatch(na, nb, l_star=med_l), dtype=np.uint8)
        bot_swatch = np.array(_neutral_cast_rgb_swatch(na, nb, l_star=75.0), dtype=np.uint8)
        p["_barcode_col"] = np.vstack([
            np.repeat(top_swatch[None, :], 24, axis=0),
            np.repeat(bot_swatch[None, :], 8, axis=0),
        ])
        photos.append(p)
    if not photos:
        raise ValueError(f"No photos found in {scenes_json_path}")

    return _finalize_segmentation(
        photos,
        str(doc.get("input_dir", "")),
        output_dir,
        window=window,
        min_segment=min_segment,
        time_gap_sec=time_gap_sec,
        wb_jump_threshold=wb_jump_threshold,
        cluster_threshold=cluster_threshold,
        sensitivity=sensitivity,
        render_overview=render_images,
        t0=t0,
    )
