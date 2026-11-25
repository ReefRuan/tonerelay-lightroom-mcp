"""Zero-token Scheme A batch sync: anchor recipe extraction, planning, command_batch apply, and serial export."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
from pathlib import Path

from PIL import Image

from .crop_math import compute_lr_crop, exif_to_lr_orientation
from .lrops_client import (
    DENY_SETTINGS,
    MAX_BATCH_COMMANDS,
    BatchExecutionError,
    LrOpsBridgeClient,
    LrOpsError,
    is_protected_name,
    validate_wb_patch,
)

CROP_KEYS = frozenset({
    "CropAngle", "CropBottom", "CropLeft", "CropRight", "CropTop", "CropConstrainToWarp",
})

RESETTABLE_CURVE_KEYS = frozenset({
    "ToneCurvePV2012",
    "ToneCurvePV2012Red",
    "ToneCurvePV2012Green",
    "ToneCurvePV2012Blue",
})

NON_RECIPE_KEYS = frozenset({
    "ProcessVersion",
    "CameraProfile",
    "CameraProfileDigest",
    "ProfileName",
    "Look",
    "WhiteBalance",
    "MaskGroupBasedCorrections",
    "PaintBasedCorrections",
    "RetouchAreas",
    "CircularGradientBasedCorrections",
    "GradientBasedCorrections",
    "LensProfileName",
    "LensProfileFilename",
    "LensProfileDigest",
    "LensProfileSetup",
}) | DENY_SETTINGS


def _canonical_hash(obj: object) -> str:
    encoded = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _same_setting_value(a, b) -> bool:
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= 1e-4
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_setting_value(x, y) for x, y in zip(a, b))
    return a == b


def _is_recipe_value(val) -> bool:
    if isinstance(val, (bool, int, float)):
        return True
    if isinstance(val, list) and 2 <= len(val) <= 512 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in val):
        return True
    return False


def extract_recipe_diff(
    anchor_settings: dict,
    master_settings: dict | None = None,
    *,
    include_crop: bool = False,
    reset_curves: bool = True,
) -> dict:
    """Extract portable Develop settings from an anchor virtual copy relative to its master.

    When ``reset_curves`` is True (default), tone/RGB curve keys present on the anchor
    (including default linear ``[0, 0, 255, 255]``) are always emitted so syncing onto
    pre-existing working virtual copies deterministically clears any stale curves.
    """
    master = master_settings or {}
    recipe: dict = {}

    for key, val in anchor_settings.items():
        if key in NON_RECIPE_KEYS:
            continue
        if not include_crop and key in CROP_KEYS:
            continue
        if not _is_recipe_value(val):
            continue
        if not (reset_curves and key in RESETTABLE_CURVE_KEYS):
            if key in master and _same_setting_value(val, master[key]):
                continue
        recipe[key] = val

    # Ensure RAW or rendered WB axes are always emitted as an explicit pair when customized.
    if anchor_settings.get("WhiteBalance") == "Custom":
        temp = anchor_settings.get("Temperature")
        tint = anchor_settings.get("Tint")
        if isinstance(temp, (int, float)) and isinstance(tint, (int, float)):
            recipe["Temperature"] = temp
            recipe["Tint"] = tint
        else:
            inc_temp = anchor_settings.get("IncrementalTemperature")
            inc_tint = anchor_settings.get("IncrementalTint")
            if isinstance(inc_temp, (int, float)) and isinstance(inc_tint, (int, float)):
                recipe["IncrementalTemperature"] = inc_temp
                recipe["IncrementalTint"] = inc_tint

    if ("Temperature" in recipe) ^ ("Tint" in recipe):
        temp = anchor_settings.get("Temperature")
        tint = anchor_settings.get("Tint")
        if isinstance(temp, (int, float)) and isinstance(tint, (int, float)):
            recipe["Temperature"] = temp
            recipe["Tint"] = tint

    if ("IncrementalTemperature" in recipe) ^ ("IncrementalTint" in recipe):
        inc_temp = anchor_settings.get("IncrementalTemperature")
        inc_tint = anchor_settings.get("IncrementalTint")
        if isinstance(inc_temp, (int, float)) and isinstance(inc_tint, (int, float)):
            recipe["IncrementalTemperature"] = inc_temp
            recipe["IncrementalTint"] = inc_tint

    return dict(sorted(recipe.items()))


def _stem_of(file_name: str) -> str:
    return Path(file_name).stem.upper()


def read_anchors_from_lightroom(
    client: LrOpsBridgeClient,
    scenes_data: dict,
    anchor_map: dict[str, str] | None = None,
    *,
    folder_id: str | None = None,
    include_crop: bool = False,
    reset_curves: bool = True,
) -> dict:
    """Read calibrated anchor virtual copies from Lightroom and build scene_recipes.json."""
    client.probe()
    anchor_map = dict(anchor_map or {})

    catalog_vcs_by_stem: dict[str, list[dict]] = {}
    if folder_id:
        photos = client.list_folder_photos([folder_id], include_virtual_copies=True)
        for p in photos:
            if p.get("is_virtual_copy"):
                catalog_vcs_by_stem.setdefault(_stem_of(p["file_name"]), []).append(p)

    photo_meta_by_stem = {p["stem"].upper(): p for p in scenes_data.get("photos", [])}
    recipes: dict[str, dict] = {}

    for scene in scenes_data.get("scenes", []):
        sid = scene["scene_id"]
        anchor_stem = scene["anchor_stem"].upper()
        vc_uuid = anchor_map.get(sid) or anchor_map.get(anchor_stem)
        if not vc_uuid and anchor_stem in catalog_vcs_by_stem:
            candidates = [
                c for c in catalog_vcs_by_stem[anchor_stem]
                if not c.get("edit_protected") and not is_protected_name(c.get("copy_name"))
            ] or catalog_vcs_by_stem[anchor_stem]
            vc_uuid = candidates[-1]["uuid"]

        if not vc_uuid:
            raise LrOpsError(
                f"No anchor virtual copy UUID provided or discovered for scene {sid} (anchor={anchor_stem})."
            )

        anchor_snap = client.get_develop_snapshot(vc_uuid)
        anchor_photo = anchor_snap.get("photo") or {}
        if not anchor_photo.get("is_virtual_copy"):
            raise LrOpsError(f"Anchor {vc_uuid} for scene {sid} is not a virtual copy.")
        master_uuid = anchor_photo.get("master_uuid")
        master_snap = client.get_develop_snapshot(master_uuid) if master_uuid else {"settings": {}}

        recipe_settings = extract_recipe_diff(
            anchor_snap.get("settings") or {},
            master_snap.get("settings") or {},
            include_crop=include_crop,
            reset_curves=reset_curves,
        )
        pmeta = photo_meta_by_stem.get(anchor_stem, {})
        recipes[sid] = {
            "scene_id": sid,
            "anchor_stem": anchor_stem,
            "anchor_vc_uuid": vc_uuid,
            "anchor_master_uuid": master_uuid,
            "anchor_copy_name": anchor_photo.get("copy_name", ""),
            "anchor_bv": pmeta.get("bv"),
            "anchor_iso": pmeta.get("iso"),
            "settings": recipe_settings,
        }

    return {
        "schema_version": "scene-recipes-v1",
        "created_at": time.time(),
        "scenes": recipes,
    }


def _round_ev(val: float) -> float:
    return round(round(val / 0.05) * 0.05, 2)


def _auto_nr_for_iso(iso: int | float | None) -> int | None:
    if not isinstance(iso, (int, float)) or iso <= 0:
        return None
    if iso >= 3200:
        return 30
    if iso >= 1600:
        return 25
    if iso >= 800:
        return 20
    return 12


def _apply_overrides(settings: dict, override: dict, recipe_settings: dict) -> tuple[dict, dict | None]:
    out = dict(settings)
    crop_spec = None
    for k, v in override.items():
        if k == "crop":
            if not isinstance(v, dict):
                raise ValueError(f"override 'crop' must be a dict, got {type(v).__name__}")
            crop_spec = dict(v)
            continue
        if k.endswith("_delta"):
            base_k = k[:-6]
            cur = out.get(base_k, 0.0)
            if isinstance(cur, (int, float)) and isinstance(v, (int, float)):
                out[base_k] = round(float(cur) + float(v), 4)
            continue
        out[k] = v

    # Keep Temperature/Tint paired if an override touches only one axis
    if ("Temperature" in out) ^ ("Tint" in out):
        other = "Tint" if "Temperature" in out else "Temperature"
        if other in recipe_settings:
            out[other] = recipe_settings[other]
    return out, crop_spec


def build_sync_plan(
    scenes_data: dict,
    recipes_data: dict,
    catalog_photos: list[dict],
    marks_by_uuid: dict[str, dict] | None = None,
    *,
    flag_filter: str = "pick",
    stems_filter: set[str] | None = None,
    auto_exposure: bool = False,
    max_exp_delta: float = 0.35,
    auto_nr: bool = False,
    overrides: dict | None = None,
    copy_name_prefix: str = "TRLR Scene",
    create_if_protected: bool = False,
) -> dict:
    """Build a deterministic per-photo batch sync plan linking Catalog records to scene recipes."""
    marks_by_uuid = marks_by_uuid or {}
    overrides = {k.upper(): v for k, v in (overrides or {}).items()}
    recipes = recipes_data.get("scenes") or {}
    scene_photo_by_stem = {p["stem"].upper(): p for p in scenes_data.get("photos", [])}

    masters_by_stem: dict[str, dict] = {}
    vcs_by_master: dict[str, list[dict]] = {}
    for p in catalog_photos:
        if p.get("is_virtual_copy"):
            muid = p.get("master_uuid")
            if muid:
                vcs_by_master.setdefault(muid, []).append(p)
        else:
            masters_by_stem[_stem_of(p["file_name"])] = p

    norm_stems = {s.upper() for s in stems_filter} if stems_filter else None
    items: list[dict] = []
    skipped: list[dict] = []

    for pmeta in scenes_data.get("photos", []):
        stem = pmeta["stem"].upper()
        if norm_stems and stem not in norm_stems:
            continue
        master = masters_by_stem.get(stem)
        if not master:
            skipped.append({"stem": stem, "reason": "not_in_catalog"})
            continue

        muid = master["uuid"]
        mark_info = marks_by_uuid.get(muid) or {}
        flag = mark_info.get("flag") or master.get("flag") or "unflagged"
        if flag_filter != "all" and flag != flag_filter:
            continue

        sid = pmeta.get("scene_id")
        recipe = recipes.get(sid)
        if not recipe:
            skipped.append({"stem": stem, "scene_id": sid, "reason": "missing_scene_recipe"})
            continue

        existing_vcs = vcs_by_master.get(muid, [])
        editable_vcs = [
            vc for vc in existing_vcs
            if not vc.get("edit_protected") and not is_protected_name(vc.get("copy_name"))
        ]
        vc_uuid = editable_vcs[0]["uuid"] if editable_vcs else None
        copy_name = editable_vcs[0].get("copy_name", "") if editable_vcs else f"{copy_name_prefix} {sid}"

        if vc_uuid is None and existing_vcs and not create_if_protected:
            skipped.append({
                "stem": stem,
                "scene_id": sid,
                "master_uuid": muid,
                "reason": "all_existing_vcs_protected",
            })
            continue

        base_settings = dict(recipe.get("settings") or {})
        settings = dict(base_settings)

        if auto_exposure and pmeta.get("bv") is not None and recipe.get("anchor_bv") is not None:
            delta = (float(recipe["anchor_bv"]) - float(pmeta["bv"])) * 0.25
            delta = max(-max_exp_delta, min(max_exp_delta, delta))
            base_exp = float(settings.get("Exposure2012", 0.0))
            settings["Exposure2012"] = _round_ev(base_exp + delta)

        if auto_nr and "LuminanceSmoothing" in settings:
            nr = _auto_nr_for_iso(pmeta.get("iso"))
            if nr is not None:
                settings["LuminanceSmoothing"] = nr

        crop_spec = None
        if stem in overrides:
            settings, crop_spec = _apply_overrides(settings, overrides[stem], base_settings)

        settings = dict(sorted(settings.items()))
        recipe_sig = {"settings": settings, "crop_spec": crop_spec} if crop_spec is not None else settings
        items.append({
            "stem": stem,
            "file_name": master["file_name"],
            "scene_id": sid,
            "flag": flag,
            "master_uuid": muid,
            "vc_uuid": vc_uuid,
            "needs_vc": vc_uuid is None,
            "copy_name": copy_name,
            "bv": scene_photo_by_stem.get(stem, {}).get("bv"),
            "iso": scene_photo_by_stem.get(stem, {}).get("iso"),
            "orientation_hint": exif_to_lr_orientation(pmeta.get("orientation")),
            "dimensions_hint": [master.get("width", 0), master.get("height", 0)],
            "crop_spec": crop_spec,
            "recipe_hash": _canonical_hash(recipe_sig),
            "settings": settings,
        })

    return {
        "schema_version": "sync-plan-v1",
        "created_at": time.time(),
        "flag_filter": flag_filter,
        "item_count": len(items),
        "skipped_count": len(skipped),
        "items": items,
        "skipped": skipped,
    }


def _load_state(state_path: Path) -> dict:
    if state_path.is_file():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {"schema_version": "sync-state-v1", "items": {}}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_path)


def _save_jpeg_bytes(
    rgb: Image.Image,
    quality: int,
    icc: bytes | None,
    exif_bytes: bytes | None,
) -> bytes:
    buf = io.BytesIO()
    save_kw: dict = {
        "format": "JPEG",
        "quality": quality,
        "optimize": True,
        "subsampling": 2,
    }
    if icc:
        save_kw["icc_profile"] = icc
    if exif_bytes:
        save_kw["exif"] = exif_bytes
    rgb.save(buf, **save_kw)
    return buf.getvalue()


def _binary_search_quality(
    rgb: Image.Image,
    target_bytes: int,
    lo: int,
    hi: int,
    icc: bytes | None,
    exif_bytes: bytes | None,
) -> tuple[bytes, int] | None:
    best: tuple[bytes, int] | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _save_jpeg_bytes(rgb, mid, icc, exif_bytes)
        if len(candidate) <= target_bytes:
            best = (candidate, mid)
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _encode_jpeg_under_bytes(
    src_bytes: bytes,
    target_bytes: int,
    *,
    min_quality: int = 68,
    allow_downscale: bool = True,
    min_long_edge: int = 2048,
) -> tuple[bytes, int | None, list[int]]:
    """Compress/downscale JPEG smoothly so output bytes <= target_bytes while preserving ICC/EXIF."""
    with Image.open(io.BytesIO(src_bytes)) as dim_im:
        orig_dims = [dim_im.width, dim_im.height]
    if len(src_bytes) <= target_bytes:
        return src_bytes, None, orig_dims

    with Image.open(io.BytesIO(src_bytes)) as src:
        icc = src.info.get("icc_profile")
        exif_bytes = src.info.get("exif")
        rgb = src.convert("RGB")

    # 1. Try full resolution within [min_quality, 95]
    hit = _binary_search_quality(rgb, target_bytes, min_quality, 95, icc, exif_bytes)
    if hit is not None:
        return hit[0], hit[1], [rgb.width, rgb.height]

    # 2. Step down resolution cleanly if allowed so image stays crisp instead of blocky
    if allow_downscale:
        cur_long = max(rgb.size)
        while cur_long > min_long_edge:
            next_long = max(min_long_edge, int(round(cur_long * 0.85)))
            if next_long >= cur_long:
                break
            scale = next_long / max(rgb.size)
            resized = rgb.resize(
                (max(1, int(round(rgb.width * scale))), max(1, int(round(rgb.height * scale)))),
                Image.Resampling.LANCZOS,
            )
            hit = _binary_search_quality(resized, target_bytes, max(min_quality, 74), 92, icc, exif_bytes)
            if hit is not None:
                return hit[0], hit[1], [resized.width, resized.height]
            cur_long = next_long
        # Keep last resized image for fallback quality search if still over target
        if max(rgb.size) > min_long_edge:
            scale = min_long_edge / max(rgb.size)
            rgb = rgb.resize(
                (max(1, int(round(rgb.width * scale))), max(1, int(round(rgb.height * scale)))),
                Image.Resampling.LANCZOS,
            )

    # 3. Last resort: allow lower quality down to 30
    hit = _binary_search_quality(rgb, target_bytes, 30, max(30, min_quality - 1), icc, exif_bytes)
    if hit is not None:
        return hit[0], hit[1], [rgb.width, rgb.height]
    fallback = _save_jpeg_bytes(rgb, 30, icc, exif_bytes)
    return fallback, 30, [rgb.width, rgb.height]


def _export_atomic(
    client: LrOpsBridgeClient,
    vc_uuid: str,
    dest_path: Path,
    *,
    max_edge: int = 0,
    reencode_quality: int | None = None,
    max_file_kb: int | None = None,
    min_quality: int = 68,
    keep_resolution: bool = False,
) -> dict:
    res = client.export_preview(vc_uuid, long_edge=max_edge)
    out_ref = res.get("output_ref")
    if not out_ref:
        raise LrOpsError(f"export_preview returned no output_ref for {vc_uuid}")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest_path.stem}.", suffix=".tmp", dir=str(dest_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        client.fetch_output(out_ref, tmp_path)
        quality_used: int | None = None

        if max_file_kb is not None:
            raw_bytes = tmp_path.read_bytes()
            encoded, quality_used, _ = _encode_jpeg_under_bytes(
                raw_bytes,
                max_file_kb * 1000,
                min_quality=min_quality,
                allow_downscale=not keep_resolution,
            )
            if encoded is not raw_bytes:
                tmp_path.write_bytes(encoded)
        elif reencode_quality is not None:
            with Image.open(tmp_path) as im:
                rgb = im.convert("RGB")
                icc = im.info.get("icc_profile")
                exif_bytes = im.info.get("exif")
                encoded = _save_jpeg_bytes(rgb, reencode_quality, icc, exif_bytes)
            tmp_path.write_bytes(encoded)
            quality_used = reencode_quality

        with Image.open(tmp_path) as verify_im:
            verify_im.verify()
        with Image.open(tmp_path) as dim_im:
            dimensions = [dim_im.width, dim_im.height]
        payload = tmp_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        os.replace(tmp_path, dest_path)
        return {
            "output_path": str(dest_path),
            "bytes": len(payload),
            "kb": round(len(payload) / 1024.0, 1),
            "quality_used": quality_used,
            "sha256": digest,
            "dimensions": dimensions,
        }
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def apply_anchor_settings(
    client: LrOpsBridgeClient,
    settings: dict,
    *,
    vc_uuid: str | None = None,
    folder_id: str | None = None,
    stem: str | None = None,
    copy_name: str = "TRLR Anchor",
    render_path: Path | None = None,
    render_max_edge: int = 1600,
    create_if_protected: bool = False,
) -> dict:
    """Apply Develop settings (including paired Temperature+Tint on 'As Shot' RAWs) to one anchor VC."""
    client.probe()
    created_vc = False
    resolved_uuid = vc_uuid

    if not resolved_uuid:
        if not (folder_id and stem):
            raise ValueError("Provide either vc_uuid or both folder_id and stem")
        target_stem = stem.upper()
        photos = client.list_folder_photos([folder_id], include_virtual_copies=True)
        master = next((p for p in photos if not p.get("is_virtual_copy") and _stem_of(p["file_name"]) == target_stem), None)
        if not master:
            raise LrOpsError(f"Master photo {target_stem} not found in folder {folder_id}")
        muid = master["uuid"]
        existing_vcs = [p for p in photos if p.get("is_virtual_copy") and p.get("master_uuid") == muid]
        editable_vcs = [
            vc for vc in existing_vcs
            if not vc.get("edit_protected") and not is_protected_name(vc.get("copy_name"))
        ]
        if editable_vcs:
            resolved_uuid = editable_vcs[0]["uuid"]
        elif existing_vcs and not create_if_protected:
            raise LrOpsError(f"All existing virtual copies for {target_stem} are protected")
        else:
            res = client.create_virtual_copy(muid, name=copy_name)
            resolved_uuid = (res.get("copy") or {}).get("uuid")
            if not resolved_uuid:
                raise LrOpsError(f"create_virtual_copy returned no uuid for {target_stem}")
            created_vc = True

    snap = client.get_develop_snapshot(resolved_uuid)
    photo_info = snap.get("photo") or {}
    before_settings = snap.get("settings") or {}
    if not photo_info.get("is_virtual_copy"):
        raise LrOpsError(f"Refusing to modify master photo {resolved_uuid}")
    actual_copy_name = photo_info.get("copy_name") or ""
    if photo_info.get("edit_protected") or is_protected_name(actual_copy_name):
        raise LrOpsError(f"Refusing to modify protected virtual copy {resolved_uuid} ({actual_copy_name})")

    effective = {k: v for k, v in settings.items() if k != "crop"}
    if "crop" in settings:
        crop_spec = settings["crop"]
        if not isinstance(crop_spec, dict):
            raise ValueError("settings['crop'] must be a dict")
        disp_w = photo_info.get("width") or 0
        disp_h = photo_info.get("height") or 0
        lr_ori = before_settings.get("orientation") or "AB"
        effective.update(compute_lr_crop(crop_spec, disp_width=disp_w, disp_height=disp_h, lr_orientation=lr_ori))

    validate_wb_patch(effective, before_settings)
    client.run_batch([{
        "command": "apply_candidate",
        "params": {
            "photo_uuid": resolved_uuid,
            "settings": effective,
            "require_in_place_guard": True,
            "expected_settings_snapshot": before_settings,
            "expected_copy_name": actual_copy_name,
        },
    }])

    after_snap = client.get_develop_snapshot(resolved_uuid)
    after_settings = after_snap.get("settings") or {}
    mismatches = [
        {"key": k, "requested": v, "actual": after_settings.get(k)}
        for k, v in effective.items()
        if not _same_setting_value(after_settings.get(k), v)
    ]

    render_info = None
    if render_path is not None:
        render_info = _export_atomic(
            client,
            resolved_uuid,
            Path(render_path),
            max_edge=render_max_edge,
            reencode_quality=88,
        )

    return {
        "ok": len(mismatches) == 0,
        "vc_uuid": resolved_uuid,
        "copy_name": actual_copy_name,
        "created_vc": created_vc,
        "applied_keys": sorted(effective.keys()),
        "white_balance": after_settings.get("WhiteBalance"),
        "mismatches_count": len(mismatches),
        "mismatches": mismatches,
        "render": render_info,
    }


def execute_sync(
    client: LrOpsBridgeClient | None,
    plan: dict,
    state_path: Path,
    *,
    output_dir: Path | None = None,
    batch_size: int = 12,
    apply_only: bool = False,
    export_only: bool = False,
    stems_filter: set[str] | None = None,
    dry_run: bool = False,
    max_edge: int = 0,
    reencode_quality: int | None = None,
    max_file_kb: int | None = None,
    min_quality: int = 68,
    keep_resolution: bool = False,
    force_reapply: bool = False,
    force_reexport: bool = False,
    progress_stream=None,
) -> dict:
    """Execute or dry-run the batch sync plan serially with per-chunk persistence."""
    if apply_only and export_only:
        raise ValueError("apply_only and export_only cannot both be True")
    if not 1 <= batch_size <= min(32, MAX_BATCH_COMMANDS):
        raise ValueError(f"batch_size must be between 1 and {min(32, MAX_BATCH_COMMANDS)}")
    if max_file_kb is not None and max_file_kb < 20:
        raise ValueError("max_file_kb must be >= 20")

    state_path = Path(state_path)
    state = _load_state(state_path)
    state_items: dict[str, dict] = state.setdefault("items", {})
    norm_filter = {s.upper() for s in stems_filter} if stems_filter else None
    export_spec_hash = _canonical_hash({
        "max_edge": max_edge,
        "reencode_quality": reencode_quality,
        "max_file_kb": max_file_kb,
        "min_quality": min_quality,
        "keep_resolution": keep_resolution,
    })

    target_items = [
        it for it in plan.get("items", [])
        if not norm_filter or it["stem"].upper() in norm_filter
    ]

    def _resolve_vc(it: dict) -> str | None:
        rec = state_items.get(it["stem"], {})
        return rec.get("vc_uuid") or it.get("vc_uuid")

    def _needs_apply(it: dict) -> bool:
        if export_only:
            return False
        rec = state_items.get(it["stem"], {})
        if force_reapply:
            return True
        return rec.get("applied_hash") != it["recipe_hash"]

    def _needs_export(it: dict) -> bool:
        if apply_only or output_dir is None:
            return False
        rec = state_items.get(it["stem"], {})
        if force_reexport or _needs_apply(it):
            return True
        out_path = Path(output_dir) / f"{it['stem']}.jpg"
        return not (
            rec.get("exported_hash") == it["recipe_hash"]
            and rec.get("export_spec_hash") == export_spec_hash
            and out_path.is_file()
        )

    vc_needed = [it for it in target_items if _resolve_vc(it) is None and not export_only]
    apply_needed = [it for it in target_items if _needs_apply(it)]
    export_needed = [it for it in target_items if _needs_export(it)]

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "target_count": len(target_items),
            "vc_to_create": len(vc_needed),
            "to_apply": len(apply_needed),
            "to_export": len(export_needed),
            "sample_stems": [x["stem"] for x in target_items[:15]],
        }

    if client is None:
        raise ValueError("Live bridge client is required when dry_run=False")
    client.probe()
    t_sync_start = time.monotonic()
    last_log_t = t_sync_start

    def _record_progress(phase: str, done: int, total: int, *, force_log: bool = False, extra: str = "") -> None:
        nonlocal last_log_t
        now_m = time.monotonic()
        elapsed = max(0.0, now_m - t_sync_start)
        eta = round((elapsed / done) * (total - done), 1) if done > 0 and total > done else 0.0
        state["progress"] = {
            "phase": phase,
            "done": done,
            "total": total,
            "mismatches": len(mismatches_report) if phase != "create_vc" else 0,
            "elapsed_sec": round(elapsed, 1),
            "eta_sec": eta,
            "updated_at": time.time(),
        }
        if progress_stream is not None and (force_log or (now_m - last_log_t) >= 30.0):
            last_log_t = now_m
            eta_str = f" eta={int(eta // 60)}m{int(eta % 60):02d}s" if eta > 0 else ""
            suffix = f" {extra}" if extra else ""
            progress_stream.write(
                f"[scene_batch sync] phase={phase} {done}/{total} elapsed={int(elapsed)}s{eta_str}{suffix}\n"
            )
            progress_stream.flush()

    mismatches_report: list[dict] = []

    # Phase 1: Create missing virtual copies serially (without wasteful v0 exports)
    vcs_created = 0
    for it in vc_needed:
        res = client.create_virtual_copy(it["master_uuid"], name=it["copy_name"])
        copy_info = res.get("copy") or {}
        vc_uuid = copy_info.get("uuid")
        if not vc_uuid:
            raise LrOpsError(f"create_virtual_copy returned no uuid for {it['stem']}")
        rec = state_items.setdefault(it["stem"], {"master_uuid": it["master_uuid"]})
        rec["vc_uuid"] = vc_uuid
        rec["copy_name"] = copy_info.get("copy_name") or it["copy_name"]
        vcs_created += 1
        _record_progress("create_vc", vcs_created, len(vc_needed), force_log=(vcs_created == len(vc_needed)))
        _save_state(state_path, state)

    # Phase 2: Batch-apply Develop patches in chunks via command_batch
    applied_count = 0
    skipped_protected: list[str] = []

    for start in range(0, len(apply_needed), batch_size):
        chunk = apply_needed[start:start + batch_size]
        batch_cmds: list[dict] = []
        chunk_meta: list[tuple[dict, str, dict]] = []

        for it in chunk:
            vc_uuid = _resolve_vc(it)
            if not vc_uuid:
                raise LrOpsError(f"Missing vc_uuid for {it['stem']} during apply phase")
            snap = client.get_develop_snapshot(vc_uuid)
            photo_info = snap.get("photo") or {}
            before_settings = snap.get("settings") or {}
            if not photo_info.get("is_virtual_copy"):
                raise LrOpsError(f"Refusing to modify master photo {vc_uuid} ({it['stem']})")
            copy_name = photo_info.get("copy_name") or ""
            if photo_info.get("edit_protected") or is_protected_name(copy_name):
                skipped_protected.append(it["stem"])
                rec = state_items.setdefault(it["stem"], {"master_uuid": it["master_uuid"], "vc_uuid": vc_uuid})
                rec["status"] = "skipped_protected"
                _save_state(state_path, state)
                continue

            effective_settings = dict(it["settings"])
            if it.get("crop_spec"):
                dims = it.get("dimensions_hint") or [0, 0]
                disp_w = photo_info.get("width") or dims[0]
                disp_h = photo_info.get("height") or dims[1]
                lr_ori = before_settings.get("orientation") or it.get("orientation_hint") or "AB"
                effective_settings.update(
                    compute_lr_crop(it["crop_spec"], disp_width=disp_w, disp_height=disp_h, lr_orientation=lr_ori)
                )

            validate_wb_patch(effective_settings, before_settings)
            batch_cmds.append({
                "command": "apply_candidate",
                "params": {
                    "photo_uuid": vc_uuid,
                    "settings": effective_settings,
                    "require_in_place_guard": True,
                    "expected_settings_snapshot": before_settings,
                    "expected_copy_name": copy_name,
                },
            })
            chunk_meta.append((it, vc_uuid, effective_settings))

        if not batch_cmds:
            continue

        try:
            batch_results = client.run_batch(batch_cmds)
        except BatchExecutionError as exc:
            # Record any entries that committed before failed_at_index
            for idx, r in enumerate(exc.results_so_far):
                if idx < len(chunk_meta) and r.get("ok"):
                    it, vc_uuid, _ = chunk_meta[idx]
                    rec = state_items.setdefault(it["stem"], {"master_uuid": it["master_uuid"], "vc_uuid": vc_uuid})
                    rec["applied_hash"] = it["recipe_hash"]
                    rec["applied_at"] = time.time()
                    applied_count += 1
            _save_state(state_path, state)
            raise LrOpsError(f"Batch failed at index {exc.failed_at_index}: {exc}") from exc

        # Read back full snapshot per item in chunk so passthrough keys (Crop*, Clarity2012, etc.) are verified
        for idx, (it, vc_uuid, effective_settings) in enumerate(chunk_meta):
            if idx >= len(batch_results) or not batch_results[idx].get("ok"):
                raise LrOpsError(f"Unexpected batch result for {it['stem']}")
            after_snap = client.get_develop_snapshot(vc_uuid)
            after_settings = after_snap.get("settings") or {}
            mismatches = [
                {"key": k, "requested": v, "actual": after_settings.get(k)}
                for k, v in effective_settings.items()
                if not _same_setting_value(after_settings.get(k), v)
            ]
            rec = state_items.setdefault(it["stem"], {"master_uuid": it["master_uuid"], "vc_uuid": vc_uuid})
            rec["vc_uuid"] = vc_uuid
            rec["applied_hash"] = it["recipe_hash"]
            rec["readback_mismatches"] = mismatches
            rec["applied_at"] = time.time()
            applied_count += 1
            if mismatches:
                mismatches_report.append({"stem": it["stem"], "mismatches": mismatches})
        _record_progress(
            "apply",
            applied_count,
            len(apply_needed),
            force_log=True,
            extra=f"mismatches={len(mismatches_report)}",
        )
        _save_state(state_path, state)

    # Phase 3: Serial atomic export
    exported_count = 0
    if not apply_only and output_dir is not None:
        output_dir = Path(output_dir)
        export_total = len(export_needed)
        for it in target_items:
            if it["stem"] in skipped_protected:
                continue
            if not _needs_export(it):
                continue
            vc_uuid = _resolve_vc(it)
            if not vc_uuid:
                raise LrOpsError(f"Cannot export {it['stem']}: no vc_uuid resolved")
            dest = output_dir / f"{it['stem']}.jpg"
            exp_info = _export_atomic(
                client, vc_uuid, dest,
                max_edge=max_edge,
                reencode_quality=reencode_quality,
                max_file_kb=max_file_kb,
                min_quality=min_quality,
                keep_resolution=keep_resolution,
            )
            rec = state_items.setdefault(it["stem"], {"master_uuid": it["master_uuid"], "vc_uuid": vc_uuid})
            rec["exported_hash"] = it["recipe_hash"]
            rec["export_spec_hash"] = export_spec_hash
            rec["export"] = exp_info
            rec["exported_at"] = time.time()
            exported_count += 1
            _record_progress(
                "export",
                exported_count,
                export_total,
                force_log=(exported_count == 1 or exported_count % 10 == 0 or exported_count == export_total),
                extra=f"last={it['stem']} ({exp_info.get('kb')} KB, q={exp_info.get('quality_used')})",
            )
            _save_state(state_path, state)

    _record_progress("done", len(target_items), len(target_items), force_log=False)
    _save_state(state_path, state)

    return {
        "ok": True,
        "dry_run": False,
        "target_count": len(target_items),
        "vcs_created": vcs_created,
        "applied_count": applied_count,
        "exported_count": exported_count,
        "skipped_protected": skipped_protected,
        "mismatches_count": len(mismatches_report),
        "mismatches": mismatches_report[:20],
        "state_path": str(state_path),
    }
