"""Explicit, Catalog-resolved RAW sidecar operations on the Lightroom host.

The plugin supplies the photo path and fresh Develop snapshot after its normal
Catalog guard. Callers supply UUIDs, patches and concurrency tokens, never paths.
Writing XML is not a Catalog import and is never reported as an applied edit.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
SIDECAR_COMMANDS = ("update_xmp_sidecar", "apply_mask")
RAW_SUFFIXES = frozenset(".arw .cr2 .cr3 .nef .nrw .raf .rw2 .orf .pef .srw .raw .rwl .3fr .iiq .mos .mrw .dcr .kdc .mef .erf".split())
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,60}$")
TOKEN = re.compile(r"^[0-9a-f]{64}$")
LOCAL = re.compile(r"^Local[A-Za-z0-9]+$")
MAX_XML_BYTES = 8 * 1024 * 1024
DENIED = {"UUID", "History", "Snapshots", "PresetType", "RawFileName", "CameraProfileDigest", "MaskGroupBasedCorrections"}
for prefix, namespace in (("x", "adobe:ns:meta/"), ("rdf", RDF), ("crs", CRS)):
    ET.register_namespace(prefix, namespace)


class SidecarError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def writes(command, params):
    return params.get("action", "inspect") in ({"update", "restore"} if command == "update_xmp_sidecar" else {"create", "update"})


def validate_request(command, params):
    if command not in SIDECAR_COMMANDS or not isinstance(params, dict):
        raise SidecarError("invalid_request", "Unknown sidecar command or invalid arguments")
    allowed = {"photo_uuid", "action", "confirm_xmp_mode", "expected_token", "request_id", "settings",
               "expected_access_mode", "expected_catalog_identity"}
    allowed |= {"backup_id"} if command == "update_xmp_sidecar" else {"correction_id", "name", "mask_xml", "mask_type"}
    if set(params) - allowed:
        raise SidecarError("invalid_request", "Unknown sidecar argument; filesystem paths are not accepted")
    if not isinstance(params.get("photo_uuid"), str) or not 1 <= len(params["photo_uuid"]) <= 256:
        raise SidecarError("invalid_request", "photo_uuid is required")
    action = params.get("action", "inspect")
    actions = {"inspect", "update", "restore"} if command == "update_xmp_sidecar" else {"inspect", "create", "update", "verify"}
    if action not in actions:
        raise SidecarError("invalid_request", "Unsupported sidecar action")
    if action == "inspect" and params.get("settings"):
        raise SidecarError("invalid_request", "inspect does not accept an adjustment patch")
    for key, maximum in (("name", 4096), ("mask_type", 128), ("correction_id", 256)):
        if key in params and (not isinstance(params[key], str) or not 1 <= len(params[key]) <= maximum):
            raise SidecarError("invalid_request", "Invalid " + key)
    if "confirm_xmp_mode" in params and type(params["confirm_xmp_mode"]) is not bool:
        raise SidecarError("invalid_request", "confirm_xmp_mode must be boolean")
    if writes(command, params):
        if params.get("confirm_xmp_mode") is not True:
            raise SidecarError("xmp_confirmation_required", "Explicit XMP-mode authorization is required: this addresses the master sidecar")
        if not isinstance(params.get("expected_token"), str) or not TOKEN.fullmatch(params["expected_token"]):
            raise SidecarError("invalid_request", "Use expected_token from a fresh inspect")
        if not isinstance(params.get("request_id"), str) or not 1 <= len(params["request_id"]) <= 128:
            raise SidecarError("invalid_request", "A unique request_id is required for a sidecar write")
    if action in {"update", "verify", "create"}:
        settings = params.get("settings", {})
        if not isinstance(settings, dict) or len(settings) > 128 or (action == "update" and not settings):
            raise SidecarError("invalid_request", "settings must be a nonempty object")
        for key, value in settings.items():
            if not isinstance(key, str) or not NAME.fullmatch(key) or key in DENIED:
                raise SidecarError("invalid_setting", "Invalid or protected Camera Raw setting")
            if command == "apply_mask" and not LOCAL.fullmatch(key):
                raise SidecarError("invalid_setting", "Mask adjustments must use explicit Local* Camera Raw fields")
            scalar(value)
        if command == "apply_mask" and action in {"update", "verify"} and not isinstance(params.get("correction_id"), str):
            raise SidecarError("invalid_request", "correction_id from inspect is required")
    if action == "restore":
        try:
            if str(uuid.UUID(params.get("backup_id", ""))) != params["backup_id"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise SidecarError("invalid_request", "backup_id must be a returned operation UUID")
    if action == "create" and params.get("mask_xml") is not None:
        if not isinstance(params["mask_xml"], str) or len(params["mask_xml"].encode("utf-8")) > MAX_XML_BYTES:
            raise SidecarError("invalid_request", "mask_xml must be a bounded XML string")


def scalar(value):
    if type(value) is bool:
        return "True" if value else "False"
    if type(value) in (int, float) and math.isfinite(value):
        return str(value)
    if isinstance(value, str) and len(value) <= 4096 and not any(ord(c) < 32 and c not in "\t\n\r" for c in value):
        return value
    raise SidecarError("invalid_setting", "Only finite numbers, booleans and strings are accepted")


def parse(data):
    if len(data) > MAX_XML_BYTES or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise SidecarError("invalid_xmp", "Oversized XML, DTDs and entities are not supported")
    try:
        return ET.fromstring(data, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True)))
    except ET.ParseError as exc:
        raise SidecarError("invalid_xmp", "XMP is not well formed") from exc


def description(root):
    rdf = root.find("{" + RDF + "}RDF")
    if rdf is None:
        raise SidecarError("invalid_xmp", "XMP requires a top-level rdf:RDF")
    descriptions = rdf.findall("{" + RDF + "}Description")
    if len(descriptions) != 1:
        raise SidecarError("ambiguous_xmp", "A single top-level rdf:Description is required")
    return descriptions[0]


def property_value(desc, key):
    tag = "{" + CRS + "}" + key
    return desc.get(tag) if tag in desc.attrib else desc.findtext(tag)


def same_value(actual, expected):
    if actual is None:
        return False
    if isinstance(expected, bool):
        return str(actual).lower() in ({"true", "1"} if expected else {"false", "0"})
    if type(expected) in (int, float):
        try:
            actual = {"true": 1, "false": 0}.get(str(actual).lower(), actual)
            return math.isclose(float(actual), expected, rel_tol=1e-9, abs_tol=1e-6)
        except (ValueError, TypeError):
            return False
    return str(actual) == str(expected)


def write_property(desc, key, value):
    tag = "{" + CRS + "}" + key
    for child in list(desc):
        if child.tag == tag:
            desc.remove(child)
    desc.set(tag, scalar(value))


def xml_masks(desc):
    group = desc.find("{" + CRS + "}MaskGroupBasedCorrections")
    if group is None:
        return []
    return group.findall("./{" + RDF + "}Seq/{" + RDF + "}li/{" + RDF + "}Description")


def summary(correction):
    fields = {k.rsplit("}", 1)[-1]: v for k, v in correction.attrib.items()}
    masks = [{k.rsplit("}", 1)[-1]: v for k, v in item.attrib.items()}
             for item in correction.findall("./{" + CRS + "}CorrectionMasks/{" + RDF + "}Seq/{" + RDF + "}li")]
    return {"correction_id": fields.get("CorrectionSyncID"), "name": fields.get("CorrectionName", ""),
            "active": str(fields.get("CorrectionActive", "true")).lower() == "true",
            "settings": {k: v for k, v in fields.items() if k.startswith("Local")},
            "components": [{k: m.get(k) for k in ("What", "MaskSyncID", "MaskName", "MaskActive", "ErrorReason") if k in m} for m in masks]}


def catalog_masks(settings):
    groups = settings.get("MaskGroupBasedCorrections") or []
    if not isinstance(groups, list):
        return []
    result = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        components = group.get("CorrectionMasks") or []
        result.append({"correction_id": group.get("CorrectionSyncID"), "name": group.get("CorrectionName", ""),
                       "active": group.get("CorrectionActive", True),
                       "settings": {k: v for k, v in group.items() if k.startswith("Local")},
                       "components": [{k: item.get(k) for k in ("What", "MaskSyncID", "MaskName", "MaskActive", "ErrorReason") if k in item}
                                      for item in components if isinstance(item, dict)]})
    return result


def fallback(code, action="create", mask_type=None):
    return {"required": True, "reason": code, "action": action, "mask_type": mask_type,
            "button_location": "Develop module, tool strip below the histogram: dashed-circle Masking icon",
            "shortcut": "Shift+W", "metadata_reload": "Library > Metadata > Read Metadata from File",
            "metadata_save": "Save Metadata to File (Ctrl+S on Windows, Cmd+S on macOS)",
            "next": "Use Agent Computer Use on the authorized photo, then inspect/verify and request a fresh preview"}


def baseline_mismatches(desc, settings):
    # SDK diagnostics/internal keys have no importable CRS representation.
    internal = DENIED | {"orientation", "LensProfileDigest", "UprightPreview", "HasSettings", "Cluster"}
    mismatches = []
    for key, value in settings.items():
        if key in internal or not NAME.fullmatch(key):
            continue
        if type(value) in (str, bool, int, float):
            if not same_value(property_value(desc, key), value):
                mismatches.append(key)
        elif key.startswith("ToneCurve") and isinstance(value, list):
            seq = desc.find("./{" + CRS + "}" + key + "/{" + RDF + "}Seq")
            try:
                points = [float(item.strip()) for li in seq for item in (li.text or "").split(",")]
                if len(points) != len(value) or any(not same_value(a, b) for a, b in zip(points, value)):
                    mismatches.append(key)
            except (TypeError, ValueError):
                mismatches.append(key)
        elif value and desc.find("{" + CRS + "}" + key) is None:
            mismatches.append(key)
    xmp_masks = {item["correction_id"]: item["settings"] for item in map(summary, xml_masks(desc))}
    native_masks = catalog_masks(settings)
    if set(xmp_masks) != {item["correction_id"] for item in native_masks}:
        mismatches.append("MaskGroupBasedCorrections")
    for item in native_masks:
        saved = xmp_masks.get(item["correction_id"])
        if saved is None or any(not same_value(saved.get(key), value) for key, value in item["settings"].items()):
            mismatches.append("MaskGroupBasedCorrections")
            break
    return sorted(set(mismatches))


def materialize_baseline(desc, settings):
    """Preserve omitted native defaults using actual Catalog values, not recipes.

    Lightroom omits inactive/default settings from native sidecars. Scalars and
    PV curves have an explicit CRS representation; opaque structured settings
    must already have native XML/ACR data and are never reconstructed here.
    """
    internal = DENIED | {"orientation", "LensProfileDigest", "UprightPreview", "HasSettings", "Cluster"}
    initialized = []
    for key, value in settings.items():
        if key in internal or not NAME.fullmatch(key):
            continue
        if type(value) in (str, bool, int, float) and property_value(desc, key) is None:
            write_property(desc, key, value)
            initialized.append(key)
        elif key.startswith("ToneCurvePV2012") and isinstance(value, list) and desc.find("{" + CRS + "}" + key) is None:
            if len(value) % 2 or any(type(item) not in (int, float) or not math.isfinite(item) for item in value):
                raise SidecarError("invalid_baseline", "Invalid native curve snapshot")
            seq = ET.SubElement(ET.SubElement(desc, "{" + CRS + "}" + key), "{" + RDF + "}Seq")
            for x, y in zip(value[::2], value[1::2]):
                ET.SubElement(seq, "{" + RDF + "}li").text = scalar(x) + ", " + scalar(y)
            initialized.append(key)
    return sorted(initialized)


def durable_write(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def finalize(command, params, context, backup_root, job_id):
    """Runs once inside JobQueue's durable finalizing transition."""
    validate_request(command, params)
    if not isinstance(context, dict) or context.get("photo_uuid") != params["photo_uuid"] or context.get("is_virtual_copy") is not False:
        raise SidecarError("invalid_photo_context", "XMP requires the exact master UUID; virtual copies share the master sidecar")
    source = Path(context.get("source_path", ""))
    if not source.is_absolute() or source.suffix.lower() not in RAW_SUFFIXES or source.is_symlink() or not source.is_file():
        raise SidecarError("unsupported_source", "A present, proprietary RAW master is required; embedded-XMP formats are not written")
    source = source.resolve()
    sidecar, acr = source.with_suffix(".xmp"), source.with_suffix(".acr")
    if sidecar.is_symlink() or acr.is_symlink():
        raise SidecarError("unsafe_sidecar", "Linked sidecars are not supported")
    settings = context.get("develop_settings")
    if not isinstance(settings, dict):
        raise SidecarError("invalid_photo_context", "A fresh Catalog Develop snapshot is required")
    original = sidecar.read_bytes() if sidecar.exists() else None
    acr_bytes = acr.read_bytes() if acr.exists() else None
    token = digest(canonical({"photo_uuid": params["photo_uuid"], "catalog_identity": params.get("expected_catalog_identity"),
                              "xmp": digest(original) if original is not None else None,
                              "acr": digest(acr_bytes) if acr_bytes is not None else None, "develop": settings}))
    root = parse(original) if original is not None else ET.fromstring(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="' + RDF + '"><rdf:RDF><rdf:Description rdf:about=""/></rdf:RDF></x:xmpmeta>')
    desc = description(root)
    current_catalog_masks = catalog_masks(settings)
    base = {"photo_uuid": params["photo_uuid"], "expected_token": token, "sidecar_exists": original is not None,
            "acr_exists": acr_bytes is not None, "sidecar_written": False, "catalog_applied": False,
            "masks_in_xmp": [summary(m) for m in xml_masks(desc)], "masks_in_catalog": current_catalog_masks,
            "coverage_verified": False}
    action = params.get("action", "inspect")
    if action == "inspect":
        return {**base, "status": "inspected"}
    if action == "verify":
        matches = [m for m in current_catalog_masks if m["correction_id"] == params["correction_id"]]
        mismatches = []
        if len(matches) == 1:
            mismatches = [k for k, v in params.get("settings", {}).items() if not same_value(matches[0]["settings"].get(k), v)]
        valid = len(matches) == 1 and str(matches[0]["active"]).lower() in {"true", "1"} and bool(matches[0]["components"])
        if valid:
            valid = all(str(m.get("ErrorReason", "0")) == "0" and str(m.get("MaskActive", "true")).lower() in {"true", "1"}
                        for m in matches[0]["components"])
        return {**base, "status": "catalog_mask_present" if valid and not mismatches else "mask_not_verified",
                "mask_exists": valid, "catalog_applied": valid and not mismatches, "readback_mismatches": mismatches,
                "preview_required": True,
                "fallback": None if valid and not mismatches else fallback("mask_not_verified", "verify")}
    if token != params["expected_token"]:
        raise SidecarError("sidecar_conflict", "Sidecar, ACR or Catalog settings changed; inspect again before writing")
    if command == "apply_mask" and action == "create" and not params.get("mask_xml"):
        return {**base, "status": "xmp_mask_generation_unavailable", "mask_exists": False,
                "fallback": fallback("xmp_mask_generation_unavailable", "create", params.get("mask_type"))}
    backup_root = Path(backup_root)
    correction_id = None
    if action == "restore":
        saved = backup_root / params["backup_id"]
        manifest = json.loads((saved / "manifest.json").read_text(encoding="utf-8"))
        if (manifest["photo_uuid"] != params["photo_uuid"] or manifest["source_path"] != str(source)
                or manifest["catalog_identity"] != params.get("expected_catalog_identity")):
            raise SidecarError("backup_mismatch", "Backup belongs to another photo")
        output = (saved / "original.xmp").read_bytes() if manifest["xmp_existed"] else None
        if output is not None and digest(output) != manifest["xmp_sha256"]:
            raise SidecarError("backup_mismatch", "Backup bytes do not match the saved digest")
    else:
        # Importing an incomplete/stale sidecar can replace unsaved Catalog
        # adjustments. Require native metadata save before any content patch.
        initialized = materialize_baseline(desc, settings)
        missing = baseline_mismatches(desc, settings)
        if missing:
            return {**base, "status": "metadata_save_required", "baseline_mismatches": missing,
                    "fallback": fallback("metadata_save_required", "save_metadata")}
        if command == "update_xmp_sidecar":
            for key, value in params["settings"].items():
                write_property(desc, key, value)
            if any(key in params["settings"] for key in ("Temperature", "Tint", "IncrementalTemperature", "IncrementalTint")) and "WhiteBalance" not in params["settings"]:
                write_property(desc, "WhiteBalance", "Custom")
        elif action == "update":
            matches = [m for m in xml_masks(desc) if m.get("{" + CRS + "}CorrectionSyncID") == params["correction_id"]]
            if len(matches) != 1:
                return {**base, "status": "mask_not_in_sidecar", "fallback": fallback("mask_not_in_sidecar", "save_metadata")}
            correction_id = params["correction_id"]
            for key, value in params["settings"].items():
                write_property(matches[0], key, value)
        else:
            template = parse(('<wrapper xmlns:crs="' + CRS + '" xmlns:rdf="' + RDF + '">' + params["mask_xml"] + '</wrapper>').encode())
            groups = list(template)
            if len(groups) != 1 or groups[0].tag != "{" + CRS + "}MaskGroupBasedCorrections":
                raise SidecarError("invalid_mask_xml", "Provide one MaskGroupBasedCorrections element")
            candidates = groups[0].findall("./{" + RDF + "}Seq/{" + RDF + "}li/{" + RDF + "}Description")
            if len(candidates) != 1:
                raise SidecarError("invalid_mask_xml", "Create accepts one correction with explicit mask components")
            candidate = candidates[0]
            if not summary(candidate)["components"]:
                raise SidecarError("invalid_mask_xml", "A correction requires mask components")
            candidate = copy.deepcopy(candidate)
            for key, value in params.get("settings", {}).items():
                write_property(candidate, key, value)
            correction_id = uuid.uuid4().hex.upper()
            candidate.set("{" + CRS + "}CorrectionSyncID", correction_id)
            if params.get("name"):
                candidate.set("{" + CRS + "}CorrectionName", scalar(params["name"]))
            for mask in candidate.findall("./{" + CRS + "}CorrectionMasks/{" + RDF + "}Seq/{" + RDF + "}li"):
                mask.set("{" + CRS + "}MaskSyncID", uuid.uuid4().hex.upper())
            group = desc.find("{" + CRS + "}MaskGroupBasedCorrections")
            if group is None:
                group = ET.SubElement(desc, "{" + CRS + "}MaskGroupBasedCorrections")
            seq = group.find("{" + RDF + "}Seq")
            if seq is None:
                seq = ET.SubElement(group, "{" + RDF + "}Seq")
            ET.SubElement(seq, "{" + RDF + "}li").append(candidate)
        output = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        parse(output)
    backup = backup_root / job_id
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {"photo_uuid": params["photo_uuid"], "source_path": str(source), "catalog_identity": params.get("expected_catalog_identity"),
                "xmp_existed": original is not None, "xmp_sha256": digest(original) if original is not None else None}
    durable_write(backup / "manifest.json", canonical(manifest))
    durable_write(backup / "develop-settings.json", canonical(settings))
    if original is not None:
        durable_write(backup / "original.xmp", original)
    if acr_bytes is not None:
        durable_write(backup / "original.acr", acr_bytes)
    # Recheck the bytes immediately before replacing. Other Lightroom/UI
    # writers are not locked by this service; this is not a Catalog transaction.
    if (sidecar.read_bytes() if sidecar.exists() else None) != original or (acr.read_bytes() if acr.exists() else None) != acr_bytes:
        raise SidecarError("sidecar_conflict", "Sidecars changed during preparation; no write performed")
    if output is None:
        sidecar.unlink(missing_ok=True)
    else:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=sidecar.parent, prefix=".tonerelay-xmp-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(output)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, sidecar)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    readback = sidecar.read_bytes() if sidecar.exists() else None
    verified = readback == output
    return {**base, "status": "sidecar_written_reload_required" if verified else "sidecar_readback_mismatch",
            "sidecar_written": True, "sidecar_verified": verified, "backup_id": job_id,
            "xmp_sha256": digest(readback) if readback is not None else None, "correction_id": correction_id,
            "initialized_keys": initialized if action != "restore" else [],
            "mask_exists": False if action == "create" else None, "preview_required": True,
            "fallback": fallback("metadata_reload_required", "read_metadata")}


def finalize_result(command, params, body, backup_root, job_id):
    if not body.get("ok"):
        return body
    try:
        data = finalize(command, params, (body.get("data") or {}).get("_sidecar_context"), backup_root, job_id)
        return {"ok": True, "data": data}
    except (SidecarError, OSError, ValueError, TypeError, KeyError) as exc:
        code = getattr(exc, "code", "sidecar_io_error")
        return {"ok": True, "data": {"status": "failed", "reason": code,
                "catalog_applied": False, "sidecar_write_unknown": isinstance(exc, OSError) and (Path(backup_root) / job_id).exists(),
                "fallback": fallback(code) if command == "apply_mask" else None}}
