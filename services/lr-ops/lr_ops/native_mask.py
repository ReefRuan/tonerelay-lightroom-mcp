"""Validation for native SDK masks on isolated virtual copies."""
import math
import re

COMMAND = "native_mask"
TOKEN = re.compile(r"^native-mask-md5-v1:[0-9a-f]{32}$")
LOCAL_FIELDS = frozenset("LocalExposure2012 LocalContrast2012 LocalHighlights2012 LocalShadows2012 LocalWhites2012 LocalBlacks2012 LocalClarity2012 LocalDehaze LocalTexture LocalSaturation LocalSharpness LocalLuminanceNoise LocalMoire LocalDefringe LocalTemperature LocalTint".split())


def writes(params):
    return params.get("action", "inspect") != "inspect"


def validate_request(params):
    allowed = {"photo_uuid", "action", "mask_type", "correction_id", "settings", "name", "expected_token", "request_id", "expected_access_mode", "expected_catalog_identity"}
    if not isinstance(params, dict) or set(params) - allowed:
        raise ValueError("Unknown native-mask arguments")
    for key, limit in (("photo_uuid", 256), ("name", 256), ("correction_id", 256), ("request_id", 128)):
        if key in params and (not isinstance(params[key], str) or not 1 <= len(params[key]) <= limit):
            raise ValueError("Invalid " + key)
    if not params.get("photo_uuid"):
        raise ValueError("photo_uuid is required")
    action = params.get("action", "inspect")
    if action not in {"inspect", "create", "update", "recompute"}:
        raise ValueError("Unsupported native-mask action")
    if writes(params):
        if not isinstance(params.get("expected_token"), str) or not TOKEN.fullmatch(params["expected_token"]) or not params.get("request_id"):
            raise ValueError("A fresh inspect token and unique request_id are required")
    if action == "create" and params.get("mask_type") not in {"subject", "sky", "background"}:
        raise ValueError("Native AI mask_type must be subject, sky or background")
    if action == "update" and not params.get("correction_id"):
        raise ValueError("update requires correction_id")
    settings = params.get("settings", {})
    if not isinstance(settings, dict) or len(settings) > len(LOCAL_FIELDS):
        raise ValueError("Invalid local settings")
    if action in {"inspect", "recompute"} and (settings or "name" in params):
        raise ValueError("This action does not accept adjustments or a name")
    if action == "update" and not settings and "name" not in params:
        raise ValueError("update requires settings or name")
    for key, value in settings.items():
        if key not in LOCAL_FIELDS or type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Use supported finite numeric Local* settings in native storage units")
