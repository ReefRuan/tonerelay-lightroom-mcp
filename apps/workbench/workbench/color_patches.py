"""Compact, display-referred color patch measurements; no editing decisions."""

from __future__ import annotations

import math
import io

import numpy as np
from PIL import ImageDraw

from .image_tools import box_for
from .srgb_profile import SRGB_ICC

SCHEMA_VERSION = "color-patches-v1"
_D65 = np.array([0.95047, 1.0, 1.08883])
_D50 = np.array([0.96422, 1.0, 0.82521])
_RGB_TO_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                        [0.2126729, 0.7151522, 0.0721750],
                        [0.0193339, 0.1191920, 0.9503041]])
_XYZ_TO_RGB = np.linalg.inv(_RGB_TO_XYZ)
_D65_TO_D50 = np.array([[1.0478112, 0.0228866, -0.0501270],
                        [0.0295424, 0.9904844, -0.0170491],
                        [-0.0092345, 0.0150436, 0.7521316]])
_D50_TO_D65 = np.linalg.inv(_D65_TO_D50)


def _triplet(value, name, *, integer=False):
    if (not isinstance(value, list) or len(value) != 3 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                not math.isfinite(v) for v in value)):
        raise ValueError(f"{name} must be three finite numbers")
    if integer and any(type(v) is not int or not 0 <= v <= 255 for v in value):
        raise ValueError(f"{name} must contain three 8-bit integers")
    return np.array(value, dtype=np.float64)


def srgb_to_lab(rgb, whitepoint="D65"):
    encoded = _triplet(list(rgb), "sRGB") / 255.0
    linear = np.where(encoded <= 0.04045, encoded / 12.92,
                      ((encoded + 0.055) / 1.055) ** 2.4)
    xyz = _RGB_TO_XYZ @ linear
    if whitepoint == "D50":
        xyz = _D65_TO_D50 @ xyz
        white = _D50
    elif whitepoint == "D65":
        white = _D65
    else:
        raise ValueError("whitepoint must be D50 or D65")
    t = xyz / white
    f = np.where(t > 216 / 24389, np.cbrt(t), (24389 / 27 * t + 16) / 116)
    return [float(116 * f[1] - 16), float(500 * (f[0] - f[1])),
            float(200 * (f[1] - f[2]))]


def lab_to_srgb(lab, whitepoint="D50"):
    l, a, b = _triplet(lab, "Lab")
    fy = (l + 16) / 116
    f = np.array([fy + a / 500, fy, fy - b / 200])
    xyz_part = np.where(f ** 3 > 216 / 24389, f ** 3,
                        (116 * f - 16) / (24389 / 27))
    if whitepoint == "D50":
        xyz = _D50_TO_D65 @ (xyz_part * _D50)
    elif whitepoint == "D65":
        xyz = xyz_part * _D65
    else:
        raise ValueError("whitepoint must be D50 or D65")
    linear = _XYZ_TO_RGB @ xyz
    clipped = bool(np.any((linear < 0) | (linear > 1)))
    linear = np.clip(linear, 0, 1)
    encoded = np.where(linear <= 0.0031308, 12.92 * linear,
                       1.055 * linear ** (1 / 2.4) - 0.055)
    rgb = np.rint(encoded * 255).astype(int).tolist()
    return rgb, clipped


def delta_e_2000(lab1, lab2):
    """CIEDE2000 with unit weighting factors, for paired Lab values."""
    l1, a1, b1 = map(float, lab1)
    l2, a2, b2 = map(float, lab2)
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    mean_c = (c1 + c2) / 2
    g = (1 - math.sqrt(mean_c ** 7 / (mean_c ** 7 + 25 ** 7))) / 2
    ap1, ap2 = (1 + g) * a1, (1 + g) * a2
    cp1, cp2 = math.hypot(ap1, b1), math.hypot(ap2, b2)
    h1 = math.degrees(math.atan2(b1, ap1)) % 360 if cp1 else 0
    h2 = math.degrees(math.atan2(b2, ap2)) % 360 if cp2 else 0
    dl = l2 - l1
    dc = cp2 - cp1
    if cp1 * cp2 == 0:
        dh = 0
        mean_h = h1 + h2
    else:
        raw = h2 - h1
        dh = raw - 360 if raw > 180 else raw + 360 if raw < -180 else raw
        mean_h = (h1 + h2) / 2
        if abs(raw) > 180:
            mean_h += 180 if h1 + h2 < 360 else -180
    dh_term = 2 * math.sqrt(cp1 * cp2) * math.sin(math.radians(dh / 2))
    mean_l = (l1 + l2) / 2
    mean_cp = (cp1 + cp2) / 2
    t = (1 - 0.17 * math.cos(math.radians(mean_h - 30))
         + 0.24 * math.cos(math.radians(2 * mean_h))
         + 0.32 * math.cos(math.radians(3 * mean_h + 6))
         - 0.20 * math.cos(math.radians(4 * mean_h - 63)))
    sl = 1 + 0.015 * (mean_l - 50) ** 2 / math.sqrt(20 + (mean_l - 50) ** 2)
    sc = 1 + 0.045 * mean_cp
    sh = 1 + 0.015 * mean_cp * t
    delta_theta = 30 * math.exp(-((mean_h - 275) / 25) ** 2)
    rc = 2 * math.sqrt(mean_cp ** 7 / (mean_cp ** 7 + 25 ** 7))
    rt = -rc * math.sin(math.radians(2 * delta_theta))
    x, y, z = dl / sl, dc / sc, dh_term / sh
    return math.sqrt(max(0, x * x + y * y + z * z + rt * y * z))


def _reference(reference):
    if not isinstance(reference, dict) or set(reference) != {"space", "values", "source_id"}:
        raise ValueError("reference requires space, values and source_id only")
    if not isinstance(reference["source_id"], str) or not reference["source_id"]:
        raise ValueError("reference.source_id must identify the reference data")
    space = reference["space"]
    if space == "srgb8":
        rgb = _triplet(reference["values"], "reference.values", integer=True).astype(int).tolist()
        lab = srgb_to_lab(rgb)
        clipped = False
    elif space in ("lab_d50", "lab_d65"):
        whitepoint = "D50" if space == "lab_d50" else "D65"
        lab = _triplet(reference["values"], "reference.values").tolist()
        rgb, clipped = lab_to_srgb(lab, whitepoint)
    else:
        raise ValueError("reference.space must be srgb8, lab_d50 or lab_d65")
    return {"space": space, "values": reference["values"],
            "source_id": reference["source_id"], "display_srgb8": rgb,
            "display_hex": "#" + "".join(f"{v:02X}" for v in rgb),
            "display_gamut_clipped": clipped}, lab


def measure_patches(image, provenance, patches):
    if not isinstance(patches, list) or not 1 <= len(patches) <= 32:
        raise ValueError("patches must contain 1..32 entries")
    if image.mode != "RGB":
        raise ValueError("expected an sRGB 8-bit image")
    pixels = np.asarray(image)
    seen = set()
    results = []
    for patch in patches:
        if not isinstance(patch, dict) or not {"id", "box"} <= set(patch) or set(patch) - {"id", "box", "reference"}:
            raise ValueError("patch requires id, box and optional reference")
        patch_id = patch["id"]
        if not isinstance(patch_id, str) or not patch_id or patch_id in seen:
            raise ValueError("patch ids must be unique nonempty strings")
        seen.add(patch_id)
        x0, y0, x1, y1 = box_for(patch["box"], image.size)
        crop = pixels[y0:y1, x0:x1, :].reshape(-1, 3)
        rgb = np.rint(np.median(crop, axis=0)).astype(int).tolist()
        measured = {"id": patch_id, "box_pixels": [x0, y0, x1, y1],
                    "pixel_count": int(crop.shape[0]), "srgb8_median": rgb,
                    "hex": "#" + "".join(f"{v:02X}" for v in rgb),
                    "lab_d65": [round(v, 2) for v in srgb_to_lab(rgb)]}
        if "reference" in patch:
            ref, lab = _reference(patch["reference"])
            measured_lab = srgb_to_lab(rgb, "D50" if ref["space"] == "lab_d50" else "D65")
            measured["reference"] = ref
            measured["delta_e_00"] = round(delta_e_2000(measured_lab, lab), 2)
        results.append(measured)
    errors = [entry["delta_e_00"] for entry in results if "delta_e_00" in entry]
    reference_sets = {(entry["reference"]["space"], entry["reference"]["source_id"])
                      for entry in results if "reference" in entry}
    return {"schema_version": SCHEMA_VERSION, "source": provenance,
            "dimensions": list(image.size), "analysis_color_space": "sRGB 8-bit",
            "patches": results,
            "delta_e_00_median": (round(float(np.median(errors)), 2)
                                  if errors and len(reference_sets) == 1 else None),
            "summary_status": ("same_reference_set" if len(reference_sets) == 1 else
                               "mixed_reference_sets" if reference_sets else "no_reference"),
            "limits": "Values are rounded to 0.01 for display, not guaranteed accurate to 0.01. JPEG/ICC uncertainty, reference conditions, chart identity, illumination and render freshness must be assessed separately."}


def digital_references(patches):
    if not isinstance(patches, list) or not 1 <= len(patches) <= 32:
        raise ValueError("patches must contain 1..32 entries")
    results = []
    seen = set()
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {"id", "reference"}:
            raise ValueError("each patch requires id and reference")
        patch_id = patch["id"]
        if not isinstance(patch_id, str) or not patch_id or patch_id in seen:
            raise ValueError("patch ids must be unique nonempty strings")
        seen.add(patch_id)
        ref, _ = _reference(patch["reference"])
        results.append({"id": patch_id, **ref})
    return {"schema_version": "digital-color-references-v1", "patches": results,
            "limits": "These are encoded display swatches, not proof of a physical chart or a universal human skin target."}


def _candidate_boxes(pixels, roi):
    """Find small uniform tiles inside one caller-marked region, without semantics."""
    x0, y0, x1, y1 = roi
    width, height = x1 - x0, y1 - y0
    tile_w, tile_h = min(96, max(3, round(width * 0.18))), min(96, max(3, round(height * 0.18)))
    if tile_w > width or tile_h > height:
        raise ValueError("region is too small for a candidate tile")
    choices = []
    for center_y in np.linspace(y0 + tile_h / 2, y1 - tile_h / 2, 5):
        for center_x in np.linspace(x0 + tile_w / 2, x1 - tile_w / 2, 5):
            left = min(x1 - tile_w, max(x0, round(center_x - tile_w / 2)))
            top = min(y1 - tile_h, max(y0, round(center_y - tile_h / 2)))
            tile = pixels[top:top + tile_h, left:left + tile_w].reshape(-1, 3)
            median = np.median(tile, axis=0)
            texture = float(np.mean(np.abs(tile.astype(np.float64) - median)))
            clipped = float(np.mean(np.any((tile <= 2) | (tile >= 253), axis=1)))
            centrality = abs((left + tile_w / 2 - (x0 + x1) / 2) / width) + abs(
                (top + tile_h / 2 - (y0 + y1) / 2) / height)
            score = texture + 20 * clipped + 2 * centrality
            choices.append((score, (left, top, left + tile_w, top + tile_h),
                            texture, clipped))
    choices.sort(key=lambda item: item[0])
    selected = []
    for item in choices:
        _, (left, top, right, bottom), _, _ = item
        cx, cy = (left + right) / 2, (top + bottom) / 2
        if all(math.hypot(cx - (p[1][0] + p[1][2]) / 2,
                          cy - (p[1][1] + p[1][3]) / 2)
               >= min(tile_w, tile_h) for p in selected):
            selected.append(item)
        if len(selected) == 3:
            break
    return selected


def compare_region_signatures(left_image, left_provenance, right_image,
                              right_provenance, regions):
    """Build a numeric self-reference chart and compare matching rendered tiles."""
    if left_image.mode != "RGB" or right_image.mode != "RGB":
        raise ValueError("both images must be decoded RGB8")
    if left_image.size != right_image.size:
        raise ValueError("region comparison needs identical image dimensions and crop")
    if not isinstance(regions, list) or not 1 <= len(regions) <= 12:
        raise ValueError("regions must contain 1..12 rough boxes")
    left_pixels = np.asarray(left_image)
    seen = set()
    records = []
    for region in regions:
        if not isinstance(region, dict) or set(region) != {"id", "box"}:
            raise ValueError("each region requires id and rough box")
        region_id = region["id"]
        if not isinstance(region_id, str) or not region_id or region_id in seen:
            raise ValueError("region ids must be unique nonempty strings")
        seen.add(region_id)
        rough_box = box_for(region["box"], left_image.size)
        candidates = _candidate_boxes(left_pixels, rough_box)
        def normalized(box):
            x0, y0, x1, y1 = box
            return [x0 / left_image.width, y0 / left_image.height,
                    (x1 - x0) / left_image.width,
                    (y1 - y0) / left_image.height]
        chosen = candidates[0]
        sample = {"id": region_id, "box": normalized(chosen[1])}
        before = measure_patches(left_image, left_provenance, [sample])["patches"][0]
        after = measure_patches(right_image, right_provenance, [sample])["patches"][0]
        lab_before = srgb_to_lab(before["srgb8_median"])
        lab_after = srgb_to_lab(after["srgb8_median"])
        records.append({
            "id": region_id, "rough_box": region["box"],
            "selected_box": sample["box"],
            "alternatives": [{"box": normalized(box), "texture_mad": round(texture, 2),
                              "clip_fraction": round(clipped, 4)}
                             for _, box, texture, clipped in candidates[1:]],
            "selection_texture_mad": round(chosen[2], 2),
            "selection_clip_fraction": round(chosen[3], 4),
            "baseline": {key: before[key] for key in ("srgb8_median", "hex", "lab_d65")},
            "candidate": {key: after[key] for key in ("srgb8_median", "hex", "lab_d65")},
            "delta_lab_candidate_minus_baseline": [round(r - l, 2) for l, r in zip(lab_before, lab_after)],
            "delta_e_00_change": round(delta_e_2000(lab_before, lab_after), 2),
        })
    return {"schema_version": "color-region-signature-v1",
            "baseline_source": left_provenance, "candidate_source": right_provenance,
            "dimensions": list(left_image.size), "analysis_color_space": "sRGB 8-bit",
            "regions": records,
            "limits": "Values are rounded to 0.01 for display, not guaranteed accurate to 0.01. Tile selection minimizes local variation but does not detect skin or establish accuracy. Review boxes and alignment. Delta E is change, not error to a physical target."}


def region_overlay(image, records):
    """Optional marked preview; yellow=rough, green=selected, orange=alternatives."""
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    def rectangle(box):
        x, y, w, h = box
        return (round(x * canvas.width), round(y * canvas.height),
                round((x + w) * canvas.width), round((y + h) * canvas.height))
    line_width = max(2, round(max(canvas.size) / 512))
    for number, record in enumerate(records, start=1):
        rough = rectangle(record["rough_box"])
        draw.rectangle(rough, outline="#FFD500", width=line_width)
        draw.text((rough[0] + 3, rough[1] + 3), str(number), fill="#FFD500",
                  stroke_width=1, stroke_fill="#000000")
        for alternative in record["alternatives"]:
            draw.rectangle(rectangle(alternative["box"]), outline="#FF8C00",
                           width=line_width)
        draw.rectangle(rectangle(record["selected_box"]), outline="#00FF65",
                       width=line_width + 1)
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=88, icc_profile=SRGB_ICC)
    return out.getvalue()
