"""Lightroom Classic crop, straighten-angle, and RAW orientation geometry helpers."""
from __future__ import annotations

import math

_EXIF_TO_LR_ORIENTATION = {
    1: "AB",
    3: "CD",
    6: "BC",
    8: "DA",
}

_VALID_LR_ORIENTATIONS = frozenset({"AB", "BC", "CD", "DA"})


def exif_to_lr_orientation(orientation: int | str | None) -> str:
    """Normalize an EXIF integer (1/3/6/8) or Lightroom string ('AB'/'BC'/'CD'/'DA') to LR orientation."""
    if isinstance(orientation, str):
        code = orientation.strip().upper()
        if code in _VALID_LR_ORIENTATIONS:
            return code
    if isinstance(orientation, int):
        return _EXIF_TO_LR_ORIENTATION.get(orientation, "AB")
    return "AB"


def parse_aspect(aspect: str | float | int | None, disp_w: float, disp_h: float) -> float | None:
    """Parse an aspect specification ('original', 'free', '3:2', 1.5, etc.) into width/height ratio."""
    if aspect is None or (isinstance(aspect, str) and aspect.strip().lower() in ("", "original", "keep")):
        return disp_w / disp_h if disp_h > 0 else 1.5
    if isinstance(aspect, str):
        clean = aspect.strip().lower()
        if clean == "free":
            return None
        if ":" in clean:
            parts = clean.split(":", 1)
            w_val = float(parts[0])
            h_val = float(parts[1])
            if w_val <= 0 or h_val <= 0:
                raise ValueError(f"Invalid aspect ratio: {aspect!r}")
            return w_val / h_val
        val = float(clean)
        if val <= 0:
            raise ValueError(f"Invalid aspect ratio: {aspect!r}")
        return val
    val = float(aspect)
    if val <= 0:
        raise ValueError(f"Invalid aspect ratio: {aspect!r}")
    return val


def display_box_to_lr_raw(
    u0: float,
    v0: float,
    u1: float,
    v1: float,
    lr_orientation: str = "AB",
) -> tuple[float, float, float, float]:
    """Map displayed upright normalized bounds [u0, v0, u1, v1] to Lightroom raw CropLeft/Top/Right/Bottom."""
    ori = exif_to_lr_orientation(lr_orientation)
    if ori == "AB":
        return u0, v0, u1, v1
    if ori == "DA":  # EXIF 8: 90 deg CCW -> display top is raw right
        return 1.0 - v1, u0, 1.0 - v0, u1
    if ori == "BC":  # EXIF 6: 90 deg CW -> display top is raw left
        return v0, 1.0 - u1, v1, 1.0 - u0
    # ori == "CD": EXIF 3: 180 deg
    return 1.0 - u1, 1.0 - v1, 1.0 - u0, 1.0 - v0


def lr_raw_to_display_box(
    crop_left: float,
    crop_top: float,
    crop_right: float,
    crop_bottom: float,
    lr_orientation: str = "AB",
) -> tuple[float, float, float, float]:
    """Inverse of display_box_to_lr_raw: map Lightroom raw Crop* back to displayed upright [u0, v0, u1, v1]."""
    ori = exif_to_lr_orientation(lr_orientation)
    if ori == "AB":
        return crop_left, crop_top, crop_right, crop_bottom
    if ori == "DA":
        return crop_top, 1.0 - crop_right, crop_bottom, 1.0 - crop_left
    if ori == "BC":
        return 1.0 - crop_bottom, crop_left, 1.0 - crop_top, crop_right
    return 1.0 - crop_right, 1.0 - crop_bottom, 1.0 - crop_left, 1.0 - crop_top


def _max_centered_half_dims(w: float, h: float, aspect: float, c: float, s: float, safety: float) -> tuple[float, float]:
    """Find largest unrotated half-dimensions (hw, hh) of given aspect fitting inside [0,W]x[0,H] when rotated."""
    lim_w = (w * 0.5) * safety
    lim_h = (h * 0.5) * safety
    denom_x = aspect * c + s
    denom_y = aspect * s + c
    hh = min(lim_w / denom_x if denom_x > 0 else lim_h, lim_h / denom_y if denom_y > 0 else lim_h)
    hw = hh * aspect
    return hw, hh


def compute_lr_crop(
    crop_spec: dict,
    *,
    disp_width: int = 0,
    disp_height: int = 0,
    lr_orientation: int | str = "AB",
) -> dict:
    """Convert semantic upright crop/straighten spec into Lightroom Crop* parameters.

    crop_spec supports:
      - angle: float degrees in [-45, 45] (default 0.0)
      - aspect: 'original' (default), 'free', '3:2', '2:3', '4:3', '1:1', or float
      - zoom: float >= 1.0 (default 1.0) with center_x, center_y in [0, 1] (default 0.5)
      - OR box: [left, top, right, bottom] in upright displayed [0, 1] coordinates
    """
    if not isinstance(crop_spec, dict):
        raise ValueError("crop_spec must be a dict")

    ori = exif_to_lr_orientation(lr_orientation)
    w = float(disp_width)
    h = float(disp_height)
    if w <= 0 or h <= 0:
        w, h = (2000.0, 3000.0) if ori in ("DA", "BC") else (3000.0, 2000.0)
    elif ori in ("DA", "BC") and w > h:
        # Handle raw sensor landscape dimensions passed for a portrait photo
        w, h = h, w

    angle = float(crop_spec.get("angle", 0.0))
    if not -45.0 <= angle <= 45.0:
        raise ValueError(f"Crop angle must be in [-45, 45], got {angle}")

    rad = math.radians(angle)
    c = abs(math.cos(rad))
    s = abs(math.sin(rad))
    safety = 0.998 if abs(angle) > 1e-4 else 1.0
    r_crop = parse_aspect(crop_spec.get("aspect", "original"), w, h)

    if "box" in crop_spec and crop_spec["box"] is not None:
        box = crop_spec["box"]
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError(f"crop.box must be [left, top, right, bottom], got {box!r}")
        u0, v0, u1, v1 = [float(v) for v in box]
        if not (0.0 <= u0 < u1 <= 1.0 and 0.0 <= v0 < v1 <= 1.0):
            raise ValueError(f"crop.box bounds must satisfy 0 <= l < r <= 1 and 0 <= t < b <= 1, got {box!r}")
        cx_px = 0.5 * (u0 + u1) * w
        cy_px = 0.5 * (v0 + v1) * h
        bw = (u1 - u0) * w
        bh = (v1 - v0) * h
        if r_crop is not None:
            if (bw / bh) > r_crop:
                hh = 0.5 * bh
                hw = hh * r_crop
            else:
                hw = 0.5 * bw
                hh = hw / r_crop
        else:
            hw = 0.5 * bw
            hh = 0.5 * bh
    else:
        zoom = float(crop_spec.get("zoom", 1.0))
        if zoom < 1.0:
            raise ValueError(f"crop.zoom must be >= 1.0, got {zoom}")
        cx_norm = float(crop_spec.get("center_x", 0.5))
        cy_norm = float(crop_spec.get("center_y", 0.5))
        if not (0.0 <= cx_norm <= 1.0 and 0.0 <= cy_norm <= 1.0):
            raise ValueError("center_x and center_y must be in [0, 1]")
        cx_px = cx_norm * w
        cy_px = cy_norm * h
        eff_aspect = r_crop if r_crop is not None else (w / h)
        max_hw, max_hh = _max_centered_half_dims(w, h, eff_aspect, c, s, safety)
        hw = max_hw / zoom
        hh = max_hh / zoom

    # Ensure rotated rectangle fits inside frame bounds; scale down uniformly if needed
    rx = hw * c + hh * s
    ry = hw * s + hh * c
    lim_w = (w * 0.5) * safety
    lim_h = (h * 0.5) * safety
    scale = min(
        1.0,
        lim_w / rx if rx > 0 else 1.0,
        lim_h / ry if ry > 0 else 1.0,
    )
    hw *= scale
    hh *= scale
    rx *= scale
    ry *= scale

    # Clamp center so all 4 rotated corners remain strictly inside [0, w] x [0, h] even after 6-decimal rounding
    eps_x = 2e-6 * w if abs(angle) > 1e-4 else 0.0
    eps_y = 2e-6 * h if abs(angle) > 1e-4 else 0.0
    cx_px = max(rx + eps_x, min(w - rx - eps_x, cx_px))
    cy_px = max(ry + eps_y, min(h - ry - eps_y, cy_px))

    u0_out = max(0.0, min(1.0, (cx_px - hw) / w))
    v0_out = max(0.0, min(1.0, (cy_px - hh) / h))
    u1_out = max(0.0, min(1.0, (cx_px + hw) / w))
    v1_out = max(0.0, min(1.0, (cy_px + hh) / h))

    cl, ct, cr, cb = display_box_to_lr_raw(u0_out, v0_out, u1_out, v1_out, ori)
    return {
        "CropAngle": round(angle, 2),
        "CropLeft": round(cl, 6),
        "CropTop": round(ct, 6),
        "CropRight": round(cr, 6),
        "CropBottom": round(cb, 6),
        "CropConstrainToWarp": 0,
    }
