"""MCP stdio adapter for ToneRelay for Lightroom.

``TOOLS`` is the executable tool reference; no hand-written tool count or list
is maintained in this module docstring. MCP initialization instructions are
compiled from ``prompts/manifest.json`` so the editing-agent prompt is a tested
project artifact rather than a second embedded documentation copy.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import sys
from pathlib import Path

from workbench_mcp import __version__
from workbench_mcp.prompt_loader import load_runtime_operator_prompt

# Reuse the protocol-neutral Runtime package in this repository.
_WORKBENCH_APP = Path(__file__).resolve().parents[2] / "workbench"
if str(_WORKBENCH_APP) not in sys.path:
    sys.path.insert(0, str(_WORKBENCH_APP))

from workbench.runtime import Workbench, build_workbench  # noqa: E402
from workbench.folders import FolderError  # noqa: E402
from workbench.artifacts import (  # noqa: E402
    build_artifact_ref,
    build_artifact_ref_from_bytes,
)
from workbench.lrops import LrOpsError  # noqa: E402
from workbench.store import StoreError  # noqa: E402
from workbench.configuration_help import CONFIG_TOPICS  # noqa: E402

from workbench_mcp.session_tools import EXPORT_FORMATS, SessionTools  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "tonerelay-lightroom-mcp", "version": __version__}
RUNTIME_OPERATOR_INSTRUCTIONS = load_runtime_operator_prompt()

PROTOCOL_SETTINGS_SCHEMA = {
    "type": "object",
    "description": "Lightroom develop settings, e.g. Exposure2012, Contrast2012, "
                   "Highlights2012, Shadows2012, Whites2012, Blacks2012, "
                   "Temperature, Tint, Vibrance, Saturation",
    "additionalProperties": True,
}

PRESET_SELECTOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "preset_name": {"type": "string", "minLength": 1},
        "preset_uuid": {"type": "string", "minLength": 1},
        "preset_folder": {"type": "string", "minLength": 1},
        "preset_scope": {"type": "string", "enum": ["lightroom", "plugin"]},
    },
    "anyOf": [{"required": ["preset_name"]}, {"required": ["preset_uuid"]}],
}

CANONICAL_UUID_PATTERN = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    "[0-9a-f]{4}-[0-9a-f]{12}$"
)

TOOLS = [
    {
        "name": "trlr_get_runtime_config",
        "description": "View current Runtime settings immediately, with configuration entry points and built-in tutorials. Call with no arguments for the redacted overview and help-topic index, or choose a topic for its step-by-step guide, effects and verification. Covers safe/full, Sandbox, Catalog, image sizes, folder access, connection, credentials, data root and execution flags. This read-only query works before Catalog binding, while authorization is blocked, and when Lightroom is disconnected; unavailable live values are marked unknown. catalog.binding_recorded reports whether the Target remembers an identity; active_identity_status reports the live observation. A null bound_catalog display name does not mean unbound, and a remembered binding does not mean authorized. catalog.access_mode.expected is the effective expected mode and does not prove it was saved; its exact source and persistence status are not reported. It never returns credentials, bridge URLs, absolute host/Catalog paths or opaque Catalog identities, and cannot write settings.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "topic": {"type": "string", "enum": list(CONFIG_TOPICS),
                          "default": "overview",
                          "description": "overview returns current settings and tutorial entry points; a specific topic adds its complete configuration tutorial to the same current-state snapshot."},
            },
        },
    },
    {
        "name": "trlr_get_presentation_config",
        "description": "Read the persistent image presentation budgets for this Runtime data root. codex_preview_max_edge caps every native image block returned to Codex; edit_render_max_edge caps later Session work renders and the render size captured by newly created Episodes. Neither setting changes Lightroom develop settings or final export output.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "trlr_set_presentation_config",
        "description": "Persist one or both image presentation budgets. The Codex cap takes effect for later native-image responses. The edit-render cap affects later Session work renders and newly created Episodes; existing rendered artifacts are immutable. This tool does not change Lightroom develop settings or final exports.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "codex_preview_max_edge": {"type": "integer", "minimum": 64, "maximum": 2048},
                "edit_render_max_edge": {"type": "integer", "minimum": 256, "maximum": 2048},
            },
            "anyOf": [
                {"required": ["codex_preview_max_edge"]},
                {"required": ["edit_render_max_edge"]},
            ],
        },
    },
    {
        "name": "trlr_health",
        "description": "Check bridge reachability and the read-only live Catalog policy. "
                       "Returns the configured/active access mode, opaque Catalog identity, "
                       "safe-mode registration match, authorization status and a reason when "
                       "blocked. An unconfigured Target requires one explicit Catalog "
                       "confirmation before any Catalog operation. This tool cannot change "
                       "or elevate the Lightroom access mode.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "trlr_list_catalogs",
        "description": "Discover accessible Lightroom .lrcat files on the local Runtime host and read Lightroom's live active Catalog through the plugin probe. This is read-only and reads only file names, sizes and modification times; it never opens Catalog databases. Results use home/volume-relative location hints, not absolute paths, and mark which local Catalogs can be switched to. For a remote Target, only the plugin-reported active Catalog is shown. Scanning uses a five-second cooperative budget and a directory limit; filesystem calls can still block. A partial scan reports scan_errors and scan_limit_reason, and does not prove other Catalogs are absent.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "trlr_confirm_catalog",
        "description": "Persist the user's Catalog binding for the active Runtime Target when the desired Catalog is already open. Call only after the user explicitly chooses and confirms the currently open Catalog. The selected opaque identity must exactly match the live plugin probe. It mirrors the plugin's current safe/full mode into targets.json without changing the plugin setting. To open another locally discovered Catalog, use trlr_switch_catalog instead. Safe mode still requires its existing Plugin Manager Sandbox registration.",
        "inputSchema": {
            "type": "object",
            "required": ["catalog_identity", "confirm_current_catalog"],
            "additionalProperties": False,
            "properties": {
                "catalog_identity": {"type": "string", "minLength": 1,
                                     "description": "Identity from trlr_list_catalogs."},
                "confirm_current_catalog": {"type": "boolean", "const": True,
                                             "description": "Must be true only after explicit user confirmation."},
            },
        },
    },
    {
        "name": "trlr_switch_catalog",
        "description": "Ask the local OS file association to open a Catalog selected from trlr_list_catalogs, wait until Lightroom's live plugin probe reports that exact Catalog, then bind it to the active Runtime Target. This changes Lightroom's current Catalog and may interrupt the visible workflow; call only after warning the user and receiving explicit confirmation to switch and bind this exact Catalog. Requires confirm_switch_and_bind=true. Available only for a local Lightroom Target on macOS or Windows; remote Targets must be switched on their host. After the OS launch, Runtime retries observation for a 20-second window. Preparation, OS launch and an in-flight network request may add time; this is not a total call deadline. If the change is not observed, the existing Target binding is preserved.",
        "inputSchema": {
            "type": "object",
            "required": ["catalog_identity", "confirm_switch_and_bind"],
            "additionalProperties": False,
            "properties": {
                "catalog_identity": {"type": "string", "minLength": 1,
                                     "description": "Identity from trlr_list_catalogs for the exact desired Catalog."},
                "confirm_switch_and_bind": {"type": "boolean", "const": True,
                                             "description": "True only after the user confirms switching Lightroom to this Catalog and binding it."},
            },
        },
    },
    {
        "name": "trlr_list_issues",
        "description": "List recorded toolchain issues (tool errors, unsettled write "
                       "readbacks, manual reports), newest first. Issues are recorded "
                       "automatically when a tool raises or an apply_settings readback "
                       "does not settle; use this instead of hand-written notes.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string",
                         "description": "Filter by kind: tool_error | after_pending | manual."},
                "session_id": {"type": "string",
                               "description": "Filter to one session."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                          "default": 50},
            },
        },
    },
    {
        "name": "trlr_report_issue",
        "description": "Manually record an issue or boundary observation into the "
                       "append-only issue log (e.g. 'bridge stopped polling after LR "
                       "restart, needed plugin reload'). Automatic events (tool errors, "
                       "unsettled readbacks) are already recorded without this tool.",
        "inputSchema": {
            "type": "object",
            "required": ["summary"],
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 500},
                "kind": {"type": "string", "default": "manual"},
                "session_id": {"type": "string"},
                "detail": {"type": "object"},
            },
        },
    },
    {
        "name": "trlr_list_photos",
        "description": "List one filtered page of photos in the current authorized "
                       "Lightroom Catalog. name_contains is an ASCII case-insensitive file-name "
                       "substring; rating filters by star rating and rating=0 also "
                       "keeps unrated photos. Filtering, sorting and pagination run "
                       "inside Lightroom. Returns count (photos in this page), total "
                       "(filtered total), limit, offset, has_more and photos.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000,
                          "default": 100, "description": "Page size."},
                "offset": {"type": "integer", "minimum": 0, "default": 0,
                           "description": "Photos to skip after filtering."},
                "name_contains": {"type": "string", "maxLength": 256, "default": "",
                                  "description": "Literal file-name substring."},
                "rating": {"type": "integer", "minimum": 0, "maximum": 5,
                           "description": "Exact rating; 0 keeps unrated photos. "
                                          "Omit for no rating filter."},
            },
        },
    },
    {
        "name": "trlr_get_current_selection",
        "description": "Read the current Lightroom target photo and its leaf-folder summary. This is a live, read-only UI query: it does not change Lightroom selection, read an absolute path, create a Catalog record, or return an image. When no target photo is selected it returns target=null and folder=null.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "trlr_get_current_folder_previews",
        "description": "Show one page of photos in the current Lightroom target's leaf folder. Returns path-free target/folder metadata and one native JPEG image block per available photo, in item order. Uses read-only Lightroom requestJpegThumbnail; it never runs an export session, creates a virtual copy, or imports a Catalog photo. A photo without a fast Lightroom thumbnail is reported as unavailable while the rest of the page remains usable. limit is intentionally capped to bound Codex image cost.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "size": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 512,
                         "description": "Requested Lightroom thumbnail edge before the presentation cap."},
                "max_edge": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 512,
                             "description": "Returned-image edge, further bounded by presentation config."},
            },
        },
    },
    {
        "name": "trlr_get_photo_preview",
        "description": "Get a fast read-only JPEG thumbnail for a photo already in the current authorized Catalog. Uses Lightroom requestJpegThumbnail, never creates a virtual copy or runs an export session. The requested thumbnail size is tightened to the effective returned-image edge so the bridge does not transfer unused pixels. The metadata reports embedded ICC and EXIF color-space declarations when present, but the image's actual color space and freshness remain unverified; use an episode render for strict acceptance evidence.",
        "inputSchema": {
            "type": "object",
            "required": ["photo_uuid"],
            "additionalProperties": False,
            "properties": {
                "photo_uuid": {"type": "string", "minLength": 1},
                "size": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 1024},
                "force": {"type": "boolean", "default": False},
                "max_edge": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 1024},
            },
        },
    },
    {
        "name": "trlr_create_session",
        "description": "Legacy compatibility only. Create a Session binding source and "
                       "reference photos, render both previews and capture a baseline. "
                       "color_match and style_match are caller-supplied relationship labels; "
                       "they do not trigger matching, parameter advice or aesthetic choices. "
                       "Use trlr_create_episode for new work.",
        "inputSchema": {
            "type": "object",
            "required": ["source_uuid", "reference_uuid"],
            "properties": {
                "source_uuid": {"type": "string"},
                "reference_uuid": {"type": "string"},
                "mode": {"type": "string", "enum": ["color_match", "style_match"],
                         "default": "color_match"},
            },
        },
    },
    {
        "name": "trlr_get_session",
        "description": "Get session state including all rounds (settings used, "
                       "preview hashes and before/after values). Server-local preview paths "
                       "are not exposed; use trlr_get_preview to read image bytes.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id"],
            "properties": {"session_id": {"type": "string"}},
        },
    },
    {
        "name": "trlr_apply_settings",
        "description": "Apply develop settings to the session's working copy in "
                       "Lightroom, render a real preview and store it as a new round. "
                       "Returns the round record; call trlr_get_preview to view it.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id", "settings"],
            "properties": {
                "session_id": {"type": "string"},
                "settings": PROTOCOL_SETTINGS_SCHEMA,
            },
        },
    },
    {
        "name": "trlr_get_preview",
        "description": "Get one already-persisted session JPEG without calling Lightroom. "
                       "Pass exactly one of round (an edited result) or slot (source/reference). "
                       "Current previews are checked against their persisted SHA-256; legacy "
                       "records without a hash are returned as unverified, and mismatches fail.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id"],
            "additionalProperties": False,
            "oneOf": [{"required": ["round"]}, {"required": ["slot"]}],
            "properties": {
                "session_id": {"type": "string", "pattern": CANONICAL_UUID_PATTERN},
                "round": {"type": "integer", "minimum": 1},
                "slot": {"type": "string", "enum": ["source", "reference"]},
                "max_edge": {"type": "integer", "minimum": 64, "maximum": 2048,
                             "default": 1024,
                             "description": "downscale longest edge for the returned image"},
            },
        },
    },
    {
        "name": "trlr_get_xmp",
        "description": "Current develop settings of the session's working copy, "
                       "as JSON and as XMP preset text. Covers the basic exposure group only, "
                       "not every field Lightroom reports: for a complete readback (curves, HSL, "
                       "grain, split toning, color grading, vignette, ...) use trlr_get_full_settings.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id"],
            "properties": {"session_id": {"type": "string"}},
        },
    },
    {
        "name": "trlr_reset",
        "description": "Reset the session's working copy back to the captured baseline.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id"],
            "properties": {"session_id": {"type": "string"}},
        },
    },
    {
        "name": "trlr_upload_photo",
        "description": "Read an image from the MCP Server's local filesystem, transfer "
                       "its bytes to the Lightroom host, and add a "
                       "master-photo record to the current authorized Catalog. Current "
                       "input staging is temporary, so this is not yet a durable long-term "
                       "asset-ingestion workflow. "
                       "This is a Catalog write in both safe and full modes; it does not "
                       "modify or move the source file on the MCP Server host.",
        "inputSchema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path readable on the MCP Server host; an Agent-side "
                                   "path is not uploaded automatically.",
                },
            },
        },
    },
    {
        "name": "trlr_list_develop_presets",
        "description": "List Lightroom develop preset metadata (public and this plugin's private presets). Read-only; settings are not returned and visual equivalence is not guaranteed.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "default": ""},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        }, "additionalProperties": False},
    },
    {
        "name": "trlr_get_develop_preset",
        "description": "Read one Lightroom develop preset's settings. Read-only; this does not prove visual equivalence.",
        "inputSchema": {**PRESET_SELECTOR_SCHEMA},
    },
    {
        "name": "trlr_compare_develop_presets",
        "description": "Compare two Lightroom develop presets by setting values. Read-only; ignored mask IDs do not establish visual equivalence.",
        "inputSchema": {"type": "object", "required": ["base", "candidate"], "properties": {
            "base": PRESET_SELECTOR_SCHEMA,
            "candidate": PRESET_SELECTOR_SCHEMA,
        }, "additionalProperties": False},
    },
]


# Episode tools share a persistent, versioned Runtime engine. The AI client
# owns decisions; the engine owns state, rendering, readback and artifacts.
def episode_tool(name, description, properties, required):
    return {"name": "trlr_" + name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False}}

SID = {"episode_id": {"type": "string"}}
WRITE = {**SID, "base_revision": {"type": "integer"}, "request_id": {"type": "string", "description": "Fresh UUID per logical action. Reuse only to retry that same action."}}
TOOLS.extend([
    episode_tool("studio_photos", "List all photos in the current authorized Catalog and verify plugin capabilities.", {}, []),
    episode_tool("parameter_reference", "Read the Runtime parameter operation documentation: for each develop field, its contract (name, group, value structure, unit, allowed range, input condition) plus a checked explanation of what it means, how to fill it in and known limitations. Read-only; never calls Lightroom and contains no recommendation, recipe or aesthetic judgment. Use it to fill in trlr_edit_episode settings or trlr_render_parameter_variants values.",
                 {"identifier": {"type": "string", "description": "Exact develop field name, e.g. Exposure2012. Returns one entry."},
                  "query": {"type": "string", "description": "Optional AND keyword search over field name, title, unit and explanation. Ignored when identifier is given."}}, []),
    episode_tool("list_parameter_variant_results", "List factual Lightroom parameter-variant render records. Each record is tied to one source baseline and render environment. The Runtime does not rank, recommend, summarize or generalize these records.",
                 {"query": {"type": "string", "description": "Optional AND search over parameter and source identifiers."}}, []),
    episode_tool("create_episode", "Create an editing context from a master photo only; source_uuid must identify a photo with is_virtual_copy=false. Lightroom rejects a virtual copy as the source. The Episode creates its own independent virtual copies for the baseline and each edit version. The default same_capture_match mode requires reference_uuid and same_capture=true, explicitly established by the user; image dimensions do not establish alignment. mode=source_edit is the source-only execution mode: it accepts direct parameter edits and parameter-variant renders, but supplies no recipes or aesthetic choices.",
                 {"source_uuid": {"type": "string"}, "reference_uuid": {"type": "string"}, "same_capture": {"type": "boolean"}, "mode": {"type": "string", "enum": ["same_capture_match", "source_edit"], "default": "same_capture_match"}, "intent": {"type": "string"}, "long_edge": {"type": "integer", "minimum": 256, "maximum": 2048}}, ["source_uuid"]),
    episode_tool("render_parameter_variants", "Asynchronously render 1 to 5 absolute values for one parameter on an Episode. Every render starts from the same fixed current version captured when the operation begins. Poll trlr_get_episode for completion, then inspect images or call trlr_image_measure/trlr_image_compare on demand. This tool records render, readback and provenance facts only; it computes no image-response score and never recommends or selects a result. Curves use arrays as candidate values.",
                 {**WRITE, "parameter": {"type": "string"},
                  "values": {"type": "array", "minItems": 1, "maxItems": 5,
                              "items": {"oneOf": [{"type": "number"},
                                                    {"type": "array", "items": {"type": "integer"}}]}}},
                 [*WRITE, "parameter", "values"]),
    episode_tool("list_episodes", "List editing contexts, most recently touched first, with their bound photo, version summaries and a truncated last user message. Empty drafts are hidden unless include_empty=true. The summary has no message timestamp or pending marker: shortlist here, then call trlr_get_episode before deciding whether work is outstanding.", {"include_empty": {"type": "boolean", "default": False}}, []),
    episode_tool("get_episode", "Read revision, user requests, immutable versions, artifact references, readbacks and errors before editing. Image measurement and comparison are separate, explicit operations through trlr_image_measure and trlr_image_compare.", SID, ["episode_id"]),
    episode_tool("edit_episode", "Apply a develop patch to the current context, render it in Lightroom and record a new version. settings accepts any crs: develop field, not just a fixed list: numbers, booleans, numeric arrays (curves/mixers) and nested tables (masks/retouch). Known fields are range-checked; everything else is passed through and Lightroom decides the range. Read the returned version's readback_mismatches to see which keys Lightroom clamped or ignored -- that is the real boundary, reported instead of hidden.",
                 {**WRITE, "settings": {"type": "object"}, "note": {"type": "string"}}, [*WRITE, "settings"]),
    episode_tool("stop_episode", "Request stopping after the current Lightroom render finishes. Completed immutable versions are retained without selecting a preferred result.", SID, list(SID)),
    episode_tool("restore_episode", "Select a saved immutable version in LR and make it current. Does not change the original master.",
                 {**WRITE, "version": {"type": "integer", "minimum": 0}}, [*WRITE, "version"]),
    episode_tool("message_episode", "Persist user editing instructions or an assistant explanation. Saving text does not execute edits; use explicit execution tools and report only observed results.",
                 {**WRITE, "text": {"type": "string"}, "role": {"type": "string", "enum": ["user", "assistant"]}}, [*WRITE, "text"]),
    episode_tool("verify_episode_xmp", "Serialize a version patch to XMP, parse its parameters, apply via LR SDK to a fresh inherited-baseline copy and compare real renders. Does NOT verify Lightroom's native preset-file importer or other photographs.",
                 {**WRITE, "version": {"type": "integer", "minimum": 0}}, [*WRITE, "version"]),
    episode_tool("episode_artifact", "Read a published version image, reference, verified XMP or verification report. names: reference.jpg, v0.jpg, v1.xmp, verification1.json. Optional normalized crop [x,y,width,height] for image inspection only; does not edit LR geometry.",
                 {**SID, "name": {"type": "string"}, "max_edge": {"type": "integer", "minimum": 64, "maximum": 2048}, "crop": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}}, [*SID, "name"]),
])

# Shared read-only image comparison. Unlike measurement's broad source support,
# comparison only accepts already-persisted server renders: session previews or
# hash-verified Episode artifacts. It never refreshes a catalog thumbnail or
# renders/imports a folder source as a side effect.
EPISODE_IMAGE_SOURCE_SCHEMA = {
    "type": "object",
    "description": "One published episode render, addressed by episode_id and name.",
    "required": ["episode_id", "name"],
    "additionalProperties": False,
    "properties": {
        "episode_id": {"type": "string", "pattern": CANONICAL_UUID_PATTERN,
                       "description": "Canonical lowercase Episode UUID."},
        "name": {"type": "string", "pattern": "^(reference|v[0-9]+)\\.jpg$",
                 "description": "Published render: reference.jpg or vN.jpg."},
    },
}
SESSION_ROUND_IMAGE_SOURCE_SCHEMA = {
    "type": "object",
    "description": "One stored session round preview.",
    "required": ["session_id", "round"],
    "additionalProperties": False,
    "properties": {
        "session_id": {"type": "string", "pattern": CANONICAL_UUID_PATTERN,
                       "description": "Canonical lowercase session UUID."},
        "round": {"type": "integer", "minimum": 1},
    },
}
SESSION_SLOT_IMAGE_SOURCE_SCHEMA = {
    "type": "object",
    "description": "One stored session source or reference preview.",
    "required": ["session_id", "slot"],
    "additionalProperties": False,
    "properties": {
        "session_id": {"type": "string", "pattern": CANONICAL_UUID_PATTERN,
                       "description": "Canonical lowercase session UUID."},
        "slot": {"type": "string", "enum": ["source", "reference"]},
    },
}
IMAGE_COMPARE_SOURCE_SCHEMA = {
    "description": "A persisted server render; fast catalog and folder sources are excluded.",
    "oneOf": [
        SESSION_ROUND_IMAGE_SOURCE_SCHEMA,
        SESSION_SLOT_IMAGE_SOURCE_SCHEMA,
        EPISODE_IMAGE_SOURCE_SCHEMA,
    ],
}
IMAGE_ROI_SCHEMA = {
    "type": "array", "minItems": 4, "maxItems": 4,
    "items": {"type": "number", "minimum": 0, "maximum": 1},
    "description": "Optional normalized [x,y,width,height], finite and inside 0..1.",
}
IMAGE_POINTS_SCHEMA = {
    "type": "array", "maxItems": 32,
    "items": {"type": "array", "minItems": 2, "maxItems": 2,
              "items": {"type": "number", "minimum": 0, "maximum": 1}},
    "description": "Optional normalized [x,y] sample points, at most 32; each finite and inside 0..1.",
}

COLOR_REFERENCE_SCHEMA = {
    "type": "object", "required": ["space", "values", "source_id"],
    "additionalProperties": False,
    "properties": {
        "space": {"type": "string", "enum": ["srgb8", "lab_d50", "lab_d65"]},
        "values": {"type": "array", "minItems": 3, "maxItems": 3,
                   "items": {"type": "number"}},
        "source_id": {"type": "string", "minLength": 1,
                      "description": "Caller-provided identifier for the source and edition of these reference values."},
    },
}

COLOR_PATCH_SCHEMA = {
    "type": "object", "required": ["id", "box"], "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "box": IMAGE_ROI_SCHEMA,
        "reference": COLOR_REFERENCE_SCHEMA,
    },
}

DIGITAL_PATCH_SCHEMA = {
    "type": "object", "required": ["id", "reference"],
    "additionalProperties": False,
    "properties": {"id": {"type": "string", "minLength": 1},
                   "reference": COLOR_REFERENCE_SCHEMA},
}

COLOR_REGION_SCHEMA = {
    "type": "object", "required": ["id", "box"],
    "additionalProperties": False,
    "properties": {"id": {"type": "string", "minLength": 1},
                   "box": IMAGE_ROI_SCHEMA},
}

TOOLS.extend([
    {
        "name": "trlr_capabilities",
        "description": "List the develop parameters, groups and known unsupported capabilities of the shared engine. Read-only metadata; does not call Lightroom.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "trlr_image_compare",
        "description": "Compare two persisted renders at their decoded native resolution. Each side is a stored session source/reference/round preview or a hash-verified Episode render; fast catalog thumbnails and folder files are excluded. distribution mode returns independent factual measurements and a right-minus-left scalar subset; histogram arrays remain per-image pixel counts, not a normalized distance. aligned mode additionally reports 0..255 encoded-sRGB 8-bit channel error and must only be used when the caller has asserted identical capture and alignment (the tool does not verify it). Returns metadata JSON plus a native JPEG preview image; it does not choose a side or make an aesthetic judgment. Unlike trlr_image_measure, comparison does not downscale before calculating metrics.",
        "inputSchema": {
            "type": "object",
            "required": ["left", "right"],
            "additionalProperties": False,
            "properties": {
                "left": IMAGE_COMPARE_SOURCE_SCHEMA,
                "right": IMAGE_COMPARE_SOURCE_SCHEMA,
                "mode": {"type": "string", "enum": ["distribution", "aligned"], "default": "distribution",
                         "description": "aligned = caller-asserted same capture/alignment; no automatic stretching."},
                "roi": IMAGE_ROI_SCHEMA,
                "max_edge": {"type": "integer", "minimum": 64, "maximum": 1024, "default": 512,
                             "description": "Longest edge of each side in the returned preview."},
            },
        },
    },
])


# Folder browsing uses an opaque root key plus a relative directory. With no
# explicit folder config, the root covers all paths available to Runtime.
FOLDER_ROOT_SCHEMA = {
    "type": "string",
    "pattern": "^[0-9a-f]{12}$",
    "description": "Root key from trlr_folder_roots. With default full access, this covers the Runtime host filesystem or one drive on a multi-volume host.",
}

FOLDER_REL_SCHEMA = {
    "type": "string",
    "default": "",
    "description": "Directory relative to the root, forward slashes, no '..'. "
                   "Empty means the root itself.",
}

TOOLS.extend([
    {
        "name": "trlr_folder_roots",
        "description": "List Runtime-host folder access roots as opaque keys with a "
                       "display label, plus which extensions are served directly and which need Lightroom. "
                       "Without a folder-library config these cover every filesystem path allowed by the Runtime OS account; an explicit config narrows the roots. "
                       "No machine paths are returned. Read-only; does not call Lightroom.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "trlr_folder_scan",
        "description": "List image files in one directory under the current folder-access roots. Returns file keys, names, sizes and "
                       "which preview route each extension takes. Scanning never imports into the Lightroom "
                       "catalog and never modifies any file.",
        "inputSchema": {
            "type": "object",
            "required": ["root"],
            "additionalProperties": False,
            "properties": {
                "root": FOLDER_ROOT_SCHEMA,
                "dir": FOLDER_REL_SCHEMA,
            },
        },
    },
    {
        "name": "trlr_folder_preview",
        "description": "Preview one file from an allowed Runtime-host folder. Configured direct formats (JPEG/PNG/TIFF) are read straight from disk "
                       "(method=direct). RAW needs Lightroom; with catalog_write=allow its bytes are "
                       "transferred to the Lightroom host and a persistent master-photo record is added "
                       "to the current authorized Catalog. The original is not moved or "
                       "modified. safe and full have the same tool set; safe only adds the registered-"
                       "Sandbox restriction. Rendered previews are cached and reused. Returns metadata "
                       "JSON plus a native JPEG image.",
        "inputSchema": {
            "type": "object",
            "required": ["root", "name"],
            "additionalProperties": False,
            "properties": {
                "root": FOLDER_ROOT_SCHEMA,
                "name": {"type": "string", "minLength": 1,
                         "description": "File name within the directory, without any path."},
                "dir": FOLDER_REL_SCHEMA,
                "max_edge": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 1024,
                             "description": "Longest edge of the returned image, to bound token cost."},
                "render": {"type": "string", "enum": ["auto", "direct", "lightroom"],
                           "default": "auto",
                           "description": "auto routes by extension. 'direct' asks for the original bytes "
                                          "and is refused for RAW."},
                "catalog_write": {"type": "string", "enum": ["allow", "never"], "default": "never",
                                  "description": "'never' keeps this a pure read. Lightroom-rendered "
                                                 "previews always need 'allow' because rendering adds a "
                                                 "persistent master-photo record to the Catalog."},
            },
        },
    },
])


# ---- Session and image capabilities -------------------------------------------
# Business logic lives in workbench_mcp.session_tools. Measurement accepts one
# explicit source shape; branches are strict so mixed addressing cannot silently
# select the wrong image.

IMAGE_MEASURE_SOURCE_SCHEMA = {
    "description": "Exactly one image source. Mixed or extra addressing fields are rejected.",
    "oneOf": [
        {
            "type": "object", "required": ["photo_uuid"],
            "additionalProperties": False,
            "properties": {"photo_uuid": {"type": "string", "minLength": 1}},
        },
        {
            "type": "object", "required": ["session_id", "round"],
            "additionalProperties": False,
            "properties": {
                "session_id": {"type": "string", "pattern": CANONICAL_UUID_PATTERN},
                "round": {"type": "integer", "minimum": 1},
            },
        },
        {
            "type": "object", "required": ["session_id", "slot"],
            "additionalProperties": False,
            "properties": {
                "session_id": {"type": "string", "pattern": CANONICAL_UUID_PATTERN},
                "slot": {"type": "string", "enum": ["source", "reference"]},
            },
        },
        {
            "type": "object", "required": ["root", "name"],
            "additionalProperties": False,
            "properties": {
                "root": FOLDER_ROOT_SCHEMA,
                "dir": FOLDER_REL_SCHEMA,
                "name": {"type": "string", "minLength": 1},
                "catalog_write": {"type": "string", "enum": ["allow", "never"],
                                  "default": "never"},
            },
        },
        {
            "type": "object", "required": ["episode_id", "name"],
            "additionalProperties": False,
            "properties": {
                "episode_id": {"type": "string", "pattern": CANONICAL_UUID_PATTERN},
                "name": {"type": "string", "pattern": "^(reference|v[0-9]+)\\.jpg$"},
            },
        },
    ],
}

TOOLS.extend([
    {
        "name": "trlr_export_image",
        "description": "Legacy compatibility export. Server-side render and downscale a catalog photo "
                       "(photo_uuid) or a session (session_id; with round = reuse that round's "
                       "stored verified preview, without = live-render the working copy). "
                       "Oversized-source export failures automatically retry at smaller edges "
                       "and the response records the actual edge. Returns JSON metadata only "
                       "(artifact_ref + sha256 + export_path); export_path is on the MCP Server "
                       "host and the image never enters context. JPEG only; the server does "
                       "not wrap a Lightroom JPEG in PNG because that cannot restore lost detail.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "photo_uuid": {"type": "string", "minLength": 1},
                "session_id": {"type": "string", "minLength": 1},
                "round": {"type": "integer", "minimum": 1,
                          "description": "Reuse the stored preview of this round (no LR call)."},
                "max_edge": {"type": "integer", "minimum": 64, "maximum": 4096, "default": 2048},
                "format": {"type": "string", "enum": list(EXPORT_FORMATS), "default": "jpeg"},
                "quality": {"type": "integer", "minimum": 1, "maximum": 100, "default": 90,
                            "description": "JPEG only."},
            },
        },
    },
    {
        "name": "trlr_image_measure",
        "description": "Convert one image into a bounded numeric representation after downscaling to analysis_edge: RGB and relative-luminance distributions, saturation/hue, tonal-band statistics, clipping, optional point samples and optional 8x8 luma grid. Source is exactly one of catalog photo, session round/slot, an allowed folder file or published episode render. Returns measurements and provenance only; the image stays server-side and no recommendation, comparison or aesthetic judgment is produced. Future semantic measurements can extend this representation without changing compare's native pairwise role.",
        "inputSchema": {
            "type": "object",
            "required": ["source"],
            "additionalProperties": False,
            "properties": {
                "source": IMAGE_MEASURE_SOURCE_SCHEMA,
                "roi": IMAGE_ROI_SCHEMA,
                "points": IMAGE_POINTS_SCHEMA,
                "sample_radius": {"type": "integer", "minimum": 0, "maximum": 32,
                                  "default": 2,
                                  "description": "Pixel radius around every requested point."},
                "grid": {"type": "boolean", "default": False,
                         "description": "Include an 8x8 per-cell luma grid."},
                "analysis_edge": {"type": "integer", "minimum": 64, "maximum": 4096,
                                  "default": 1024,
                                  "description": "Downscale cap before measuring."},
            },
        },
    },
    {
        "name": "trlr_color_patch_measure",
        "description": "Measure 1..32 caller-marked rectangles on one image at its available decoded resolution. Embedded ICC is converted to sRGB; absent ICC is assumed sRGB by the source adapter. Return median RGB8, hex and Lab D65 for each patch; with a caller-supplied sRGB8/Lab D50/Lab D65 reference, also return CIEDE2000. No chart detection or standard chart target is built in. A fast Catalog thumbnail remains unverified; use a published Episode render for strict comparison. Values are display-referred 8-bit facts, not RAW precision or proof of skin accuracy. Returns compact JSON only.",
        "inputSchema": {"type": "object", "required": ["source", "patches"],
                        "additionalProperties": False,
                        "properties": {
                            "source": IMAGE_MEASURE_SOURCE_SCHEMA,
                            "patches": {"type": "array", "minItems": 1,
                                        "maxItems": 32, "items": COLOR_PATCH_SCHEMA},
                        }},
    },
    {
        "name": "trlr_digital_color_references",
        "description": "Convert 1..32 caller-supplied sRGB8 or Lab D50/D65 patch references into a compact numeric sRGB8/hex display chart. Reports out-of-gamut clipping. Never supplies manufacturer reference data, discovers chart identity or certifies a physical chart. No image bytes or Lightroom call.",
        "inputSchema": {"type": "object", "required": ["patches"],
                        "additionalProperties": False,
                        "properties": {"patches": {"type": "array", "minItems": 1,
                                                  "maxItems": 32,
                                                  "items": DIGITAL_PATCH_SCHEMA}}},
    },
    {
        "name": "trlr_color_region_compare",
        "description": "Build a per-photo numeric color chart and compare it across two already-saved same-crop renders. Caller marks 1..12 rough regions once; the server deterministically selects low-texture tiles in the baseline, reuses exactly those coordinates in the candidate, and returns baseline/candidate RGB8, hex, Lab D65, Lab deltas and CIEDE2000 change plus alternative tile boxes for review. Optional marked baseline overlay helps confirm sampling positions. No skin/face detection, reference truth, aesthetic choice or Lightroom edit. Delta E here means rendered color change, not accuracy.",
        "inputSchema": {"type": "object",
                        "required": ["baseline", "candidate", "regions"],
                        "additionalProperties": False,
                        "properties": {
                            "baseline": IMAGE_COMPARE_SOURCE_SCHEMA,
                            "candidate": IMAGE_COMPARE_SOURCE_SCHEMA,
                            "regions": {"type": "array", "minItems": 1,
                                        "maxItems": 12, "items": COLOR_REGION_SCHEMA},
                            "include_overlay": {"type": "boolean", "default": False,
                                                "description": "Return one optional marked JPEG image block; default returns compact JSON only."},
                            "detail": {"type": "string", "enum": ["compact", "review"],
                                       "default": "compact",
                                       "description": "review includes alternate tiles and rough-box diagnostics."},
                        }},
    },
    {
        "name": "trlr_get_full_settings",
        "description": "Full develop snapshot of the session working copy: every field "
                       "Lightroom reports (curves, HSL, grain, calibration, ...) as structured "
                       "JSON, plus a standard XMP preset text assembled server-side. Nested "
                       "tables (masks) cannot be XMP attributes and are listed under "
                       "skipped_structured_keys instead of being dropped silently.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id"],
            "additionalProperties": False,
            "properties": {
                "session_id": {"type": "string"},
                "format": {"type": "string", "enum": ["json", "xmp", "both"], "default": "both"},
            },
        },
    },
    {
        "name": "trlr_round_diff",
        "description": "Diff two rounds of a session: submitted-settings diff, after-readback "
                       "diff when available, and image-measurement delta (reuses the same internal "
                       "measurement schema as trlr_image_measure).",
        "inputSchema": {
            "type": "object",
            "required": ["session_id", "round_a", "round_b"],
            "additionalProperties": False,
            "properties": {
                "session_id": {"type": "string"},
                "round_a": {"type": "integer", "minimum": 1},
                "round_b": {"type": "integer", "minimum": 1},
                "stats": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "trlr_replay_round",
        "description": "Full replay: reset the working copy to baseline, then apply one "
                       "complete target settings set in a single shot and store a new round. "
                       "Target is either explicit settings, or source_round (prefers that round's "
                       "after-readback full state, falls back to merged submitted settings) plus "
                       "an optional patch. WB is auto-filled like trlr_apply_settings.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id"],
            "additionalProperties": False,
            "properties": {
                "session_id": {"type": "string"},
                "settings": PROTOCOL_SETTINGS_SCHEMA,
                "source_round": {"type": "integer", "minimum": 1},
                "patch": PROTOCOL_SETTINGS_SCHEMA,
            },
        },
    },
    {
        "name": "trlr_export_session_context",
        "description": "Write a legacy Session context.md skeleton: session_id, "
                       "working-copy uuid, round timeline with per-round settings diff, final "
                       "settings and artifact sha256 list. The three narrative sections "
                       "(思路/翻车记录/修复与结论) are left blank for the caller to fill.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id", "output_dir"],
            "additionalProperties": False,
            "properties": {
                "session_id": {"type": "string"},
                "output_dir": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Absolute archive-directory path on the MCP Server host.",
                },
                "overwrite": {"type": "boolean", "default": False},
            },
        },
    },
])


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _json_content(data) -> dict:
    return {"content": [_text(json.dumps(data, ensure_ascii=False, indent=2))]}


def _public_session_data(value):
    """Remove internal persistence paths from legacy Session responses."""
    if isinstance(value, dict):
        return {key: _public_session_data(item) for key, item in value.items()
                if key != "preview_path"}
    if isinstance(value, list):
        return [_public_session_data(item) for item in value]
    return value


def _summarize_args(args: dict | None) -> dict:
    """Compact, JSON-safe view of tool arguments for the issue log: strings are
    truncated and anything non-primitive collapses to a type tag, so logging a
    failing call can never blow up the log or leak big payloads."""
    out = {}
    for key, value in (args or {}).items():
        if key in {"path", "folder_path", "absolute_path"}:
            out[key] = "<redacted>"
            continue
        if isinstance(value, str):
            out[key] = value[:120] + ("…" if len(value) > 120 else "")
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, tuple)):
            out[key] = f"<{type(value).__name__} len={len(value)}>"
        elif isinstance(value, dict):
            out[key] = f"<dict keys={sorted(value.keys())[:12]}>"
        else:
            out[key] = f"<{type(value).__name__}>"
    return out


def _image_content(path: Path, max_edge: int, extra: dict | None = None) -> dict:
    return _image_content_bytes(Path(path).read_bytes(), max_edge, extra)


def _jpeg_for_image_content(content: bytes, max_edge: int, *, quality: int = 85) -> tuple[bytes, tuple[int, int]]:
    """Encode one bounded JPEG image block and report its actual dimensions."""
    from PIL import Image
    with Image.open(io.BytesIO(content)) as source:
        # For an RGB JPEG, the encoded channel values remain in the source
        # colour space. Keep its ICC when resizing/re-encoding for Codex.
        icc_profile = source.info.get("icc_profile") if source.mode == "RGB" else None
        img = source.convert("RGB")
    if max(img.size) > max_edge:
        scale = max_edge / max(img.size)
        img = img.resize((max(1, round(img.width * scale)),
                          max(1, round(img.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    save_options = {"icc_profile": icc_profile} if icc_profile else {}
    img.save(buf, format="JPEG", quality=quality, **save_options)
    return buf.getvalue(), img.size


def _image_content_bytes(content: bytes, max_edge: int, extra: dict | None = None) -> dict:
    jpeg, _dimensions = _jpeg_for_image_content(content, max_edge)
    blocks = []
    if extra:
        blocks.append(_text(json.dumps(extra, ensure_ascii=False)))
    blocks.append({
        "type": "image",
        "data": base64.b64encode(jpeg).decode("ascii"),
        "mimeType": "image/jpeg",
    })
    return {"content": blocks}


_MISSING = object()


def _json_integer(value, name: str, minimum: int, maximum: int | None = None) -> int:
    """Normalize numbers JSON Schema considers integers; reject bool/non-finite values."""
    if type(value) is int:
        normalized = value
    elif type(value) is float and math.isfinite(value) and value.is_integer():
        normalized = int(value)
    else:
        raise ValueError(f"{name} must be an integer")
    if normalized < minimum or (maximum is not None and normalized > maximum):
        if maximum is None:
            raise ValueError(f"{name} must be an integer at least {minimum}")
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return normalized


def _normalize_compare_source(source):
    """Copy and normalize a session round without mutating caller-owned input."""
    if not isinstance(source, dict) or "round" not in source:
        return source
    normalized = dict(source)
    normalized["round"] = _json_integer(source["round"], "round", 1)
    return normalized


class McpServer:
    def __init__(self, wb: Workbench):
        self.wb = wb
        self._session_tools = None

    @property
    def session_tools(self) -> SessionTools:
        """Session execution and measurement layer, loaded without import side effects."""
        if self._session_tools is None:
            self._session_tools = SessionTools(self.wb)
        return self._session_tools

    # ---- tool implementations -------------------------------------------------

    def call_tool(self, name: str, args: dict) -> dict:
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise StoreError(f"unknown tool {name}")
        try:
            return handler(**(args or {}))
        except Exception as exc:
            # 工具炸了就自动落一条 issue——不靠 AI 或用户事后手写记录。
            # 记录本身再失败（比如磁盘满）不能掩盖原始错误。
            try:
                self.wb.issues.record(
                    "tool_error", f"{name}: {exc}", tool=name,
                    detail={"error_type": type(exc).__name__,
                            "args": _summarize_args(args)})
            except Exception:
                pass
            raise

    def _tool_trlr_list_issues(self, kind: str | None = None,
                               session_id: str | None = None,
                               limit: int = 50) -> dict:
        return _json_content(self.wb.list_issues(kind=kind, session_id=session_id,
                                                 limit=limit))

    def _tool_trlr_report_issue(self, summary: str, kind: str = "manual",
                                session_id: str | None = None,
                                detail: dict | None = None) -> dict:
        return _json_content(self.wb.report_issue(
            summary, kind=kind, session_id=session_id, detail=detail))

    def _tool_trlr_health(self) -> dict:
        return _json_content(self.wb.health())

    def _tool_trlr_get_runtime_config(self, topic: str = "overview") -> dict:
        return _json_content(self.wb.runtime_config(topic=topic))

    def _tool_trlr_list_catalogs(self) -> dict:
        return _json_content(self.wb.catalog_inventory())

    def _tool_trlr_confirm_catalog(self, catalog_identity: str,
                                   confirm_current_catalog: bool = False) -> dict:
        return _json_content(self.wb.confirm_catalog(
            catalog_identity, confirm_current_catalog))

    def _tool_trlr_switch_catalog(self, catalog_identity: str,
                                  confirm_switch_and_bind: bool = False) -> dict:
        return _json_content(self.wb.switch_catalog(
            catalog_identity, confirm_switch_and_bind))

    def _tool_trlr_get_presentation_config(self) -> dict:
        return _json_content(self.wb.presentation.get())

    def _tool_trlr_set_presentation_config(self, codex_preview_max_edge=None,
                                           edit_render_max_edge=None) -> dict:
        return _json_content(self.wb.presentation.update(
            codex_preview_max_edge=codex_preview_max_edge,
            edit_render_max_edge=edit_render_max_edge))

    def _codex_preview_edge(self, value, maximum=2048) -> int:
        requested = _json_integer(value, "max_edge", 64, maximum)
        return min(requested, self.wb.presentation.get()["codex_preview_max_edge"])

    def _tool_trlr_list_photos(self, limit: int = 100, offset: int = 0,
                               name_contains: str | None = None,
                               rating: int | None = None) -> dict:
        return _json_content(self.wb.catalog_tools.search(
            limit=limit, offset=offset, name_contains=name_contains, rating=rating))

    def _tool_trlr_get_current_selection(self) -> dict:
        return _json_content(self.wb.current_selection())

    def _tool_trlr_get_current_folder_previews(self, limit: int = 10, offset: int = 0,
                                                size: int = 512, max_edge: int = 512) -> dict:
        limit = _json_integer(limit, "limit", 1, 20)
        offset = _json_integer(offset, "offset", 0)
        size = _json_integer(size, "size", 64, 2048)
        max_edge = self._codex_preview_edge(max_edge)
        # A larger Lightroom thumbnail cannot improve an image block that will
        # immediately be downscaled. This is the important per-page cost cap.
        thumbnail_size = min(size, max_edge)
        gallery = self.wb.current_folder_gallery(
            limit=limit, offset=offset, size=thumbnail_size)
        page = gallery["page"]
        public_items = []
        display_items = []
        for index, item in enumerate(gallery["items"], start=page["offset"]):
            public = {"index": index, "photo": item["photo"]}
            if item["preview"]["state"] == "ready":
                record = item["record"]
                preview = {key: value for key, value in record.items()
                           if key != "preview_path"}
                public["preview"] = {"state": "ready", **preview}
                display_items.append((public, record))
            else:
                public["preview"] = dict(item["preview"])
                display_items.append((public, None))
            public_items.append(public)
        response = {
            "target": page["target"],
            "folder": page["folder"],
            "count": page["count"],
            "total": page["total"],
            "limit": page["limit"],
            "offset": page["offset"],
            "has_more": page["has_more"],
            "presentation": {
                "requested_thumbnail_size": size,
                "effective_thumbnail_size": thumbnail_size,
                "effective_max_edge": max_edge,
            },
            "items": public_items,
        }
        blocks = [_text(json.dumps(response, ensure_ascii=False))]
        # Place a concise name/metadata block immediately before each native
        # image. Codex owns visual layout, but this preserves an unambiguous
        # text-to-image pairing in every MCP client.
        for public, record in display_items:
            blocks.append(_text(json.dumps({
                "index": public["index"], "photo": public["photo"],
                "preview": public["preview"],
            }, ensure_ascii=False)))
            if record is not None:
                blocks.extend(_image_content(Path(record["preview_path"]), max_edge)["content"])
        return {"content": blocks}

    def _tool_trlr_get_photo_preview(self, photo_uuid: str, size: int = 1024,
                                     force: bool = False, max_edge: int = 1024) -> dict:
        max_edge = self._codex_preview_edge(max_edge)
        if not isinstance(photo_uuid, str) or not photo_uuid:
            raise ValueError("photo_uuid is required")
        if type(size) is not int or not 64 <= size <= 2048:
            raise ValueError("size must be 64..2048")
        if type(force) is not bool:
            raise ValueError("force must be a boolean")
        # ``size`` controls the LR thumbnail request while ``max_edge`` is
        # the image actually returned to Codex.  Never fetch a larger
        # thumbnail only to throw its pixels away in ``_image_content``.
        thumbnail_size = min(size, max_edge)
        record = self.wb.preview_photo(
            photo_uuid, size=thumbnail_size, force=force)
        path = Path(record["preview_path"])
        metadata = {k: v for k, v in record.items() if k != "preview_path"}
        return _image_content(path, max_edge, metadata)

    def _tool_trlr_create_session(self, source_uuid: str, reference_uuid: str,
                                  mode: str = "color_match") -> dict:
        return _json_content(_public_session_data(
            self.wb.create_session(source_uuid, reference_uuid, mode)))

    def _tool_trlr_get_session(self, session_id: str) -> dict:
        return _json_content(_public_session_data(
            self.wb.store.get_session(session_id)))

    def _tool_trlr_apply_settings(self, session_id: str, settings: dict) -> dict:
        # 缺省时显式补齐 WB（以当前回读值为基准）并回显实际写入值。
        return _json_content(_public_session_data(
            self.session_tools.apply_settings(session_id, settings)))

    def _tool_trlr_export_image(self, **args) -> dict:
        return _json_content(self.session_tools.export_image(**args))

    def _tool_trlr_image_measure(self, **args) -> dict:
        return _json_content(self.session_tools.image_measure(**args))

    def _tool_trlr_color_patch_measure(self, **args) -> dict:
        return _json_content(self.session_tools.color_patch_measure(**args))

    def _tool_trlr_digital_color_references(self, **args) -> dict:
        return _json_content(self.session_tools.digital_color_references(**args))

    def _tool_trlr_color_region_compare(self, **args) -> dict:
        detail = args.pop("detail", "compact")
        if detail not in ("compact", "review"):
            raise ValueError("detail must be compact or review")
        args["baseline"] = _normalize_compare_source(args["baseline"])
        args["candidate"] = _normalize_compare_source(args["candidate"])
        result = self.session_tools.color_region_compare(**args)
        overlay = result.pop("overlay_jpeg", None)
        if detail == "compact" and overlay is None:
            for region in result["regions"]:
                for key in ("rough_box", "alternatives", "selection_texture_mad",
                            "selection_clip_fraction"):
                    region.pop(key, None)
        if overlay is None:
            return _json_content(result)
        edge = self._codex_preview_edge(1024, 1024)
        jpeg, dimensions = _jpeg_for_image_content(overlay, edge, quality=85)
        result["overlay"] = {"mime_type": "image/jpeg", "dimensions": list(dimensions),
                             "legend": "yellow rough region, green selected tile, orange alternatives"}
        return {"content": [_text(json.dumps(result, ensure_ascii=False)),
                            {"type": "image", "data": base64.b64encode(jpeg).decode("ascii"),
                             "mimeType": "image/jpeg"}]}

    def _tool_trlr_image_compare(self, left, right, mode='distribution', roi=None,
                                 max_edge=512):
        left = _normalize_compare_source(left)
        right = _normalize_compare_source(right)
        max_edge = self._codex_preview_edge(max_edge, 1024)
        result = self.session_tools.image_compare(
            left=left, right=right, mode=mode, roi=roi, max_edge=max_edge)
        preview = result["preview"]
        # ``compare_images`` makes a side-by-side board, whose width can be
        # larger than the per-side preview edge.  Reapply the global Codex
        # budget to the single image block that actually enters the context.
        preview_jpeg, dimensions = _jpeg_for_image_content(
            base64.b64decode(preview["base64"]), max_edge, quality=90)
        # Bytes travel as a native image block; the JSON block keeps only
        # mime type and dimensions so nothing is duplicated.
        result["preview"] = {"mime_type": "image/jpeg",
                             "dimensions": list(dimensions)}
        return {"content": [
            _text(json.dumps(result, ensure_ascii=False, indent=2)),
            {"type": "image", "data": base64.b64encode(preview_jpeg).decode("ascii"),
             "mimeType": "image/jpeg"},
        ]}

    def _tool_trlr_get_full_settings(self, session_id: str, format: str = "both") -> dict:
        return _json_content(self.session_tools.full_settings(session_id, format=format))

    def _tool_trlr_round_diff(self, session_id: str, round_a: int, round_b: int,
                              stats: bool = True) -> dict:
        return _json_content(self.session_tools.round_diff(
            session_id, round_a, round_b, stats=stats))

    def _tool_trlr_replay_round(self, session_id: str, settings=None, source_round=None,
                                patch=None) -> dict:
        return _json_content(_public_session_data(
            self.session_tools.replay_round(
                session_id, settings=settings, source_round=source_round, patch=patch)))

    def _tool_trlr_export_session_context(self, session_id: str, output_dir: str,
                                          overwrite: bool = False) -> dict:
        return _json_content(self.session_tools.export_session_context(
            session_id, output_dir, overwrite=overwrite))

    def _tool_trlr_get_preview(self, session_id: str, round=_MISSING,
                               slot=_MISSING, max_edge=1024) -> dict:
        max_edge = self._codex_preview_edge(max_edge)
        has_round = round is not _MISSING
        has_slot = slot is not _MISSING
        if has_round == has_slot:
            raise StoreError("pass exactly one of round or slot")
        if has_round:
            round = _json_integer(round, "round", 1)
            source = {"session_id": session_id, "round": round}
            label = f"round {round}"
            artifact_id = f"session:{session_id}:round:{round}"
            artifact_source = {"kind": "session_round", "session_id": session_id,
                               "round": round}
        elif slot in ("source", "reference"):
            source = {"session_id": session_id, "slot": slot}
            label = f"slot {slot}"
            artifact_id = f"session:{session_id}:{slot}"
            artifact_source = {"kind": "session_slot", "session_id": session_id,
                               "slot": slot}
        else:
            raise StoreError("slot must be source or reference")
        payload = self.session_tools.load_session_preview(source)
        provenance = payload.provenance
        verified = provenance["freshness"] == "verified"
        artifact = build_artifact_ref_from_bytes(
            payload.content, filename=payload.filename,
            artifact_id=artifact_id, source=artifact_source,
            purpose="verified" if verified else "analysis",
            method="LrExportSession", freshness=provenance["freshness"],
            evidence_status="verified_render" if verified else "unverified",
            color_space="sRGB")
        return _image_content_bytes(
            payload.content, max_edge,
            {"session_id": session_id, "label": label,
             "integrity": provenance["integrity"],
             "artifact_ref": artifact})

    def _tool_trlr_get_xmp(self, session_id: str) -> dict:
        return _json_content(self.wb.xmp_view(session_id))

    def _tool_trlr_reset(self, session_id: str) -> dict:
        return _json_content(self.wb.reset(session_id))

    def _tool_trlr_upload_photo(self, path: str) -> dict:
        p = Path(path)
        if not p.is_file():
            raise StoreError(f"no such file: {path}")
        return _json_content(self.wb.upload_photo(p.name, p.read_bytes()))

    def _tool_trlr_list_develop_presets(self, query="", limit=100, offset=0):
        return _json_content(self.wb.preset_tools.list(query, limit, offset))

    def _tool_trlr_get_develop_preset(self, **selector):
        return _json_content(self.wb.preset_tools.get(selector))

    def _tool_trlr_compare_develop_presets(self, base, candidate):
        return _json_content(self.wb.preset_tools.compare(base, candidate))

    def _tool_trlr_studio_photos(self):
        return _json_content(self.wb.studio.photos())

    def _tool_trlr_parameter_reference(self, identifier: str = None, query: str = ""):
        return _json_content(self.wb.studio.parameter_reference(query, identifier))

    def _tool_trlr_list_parameter_variant_results(self, query: str = ""):
        return _json_content(self.wb.studio.list_parameter_variant_results(query))

    def _tool_trlr_create_episode(self, **args):
        configured_edge = self.wb.presentation.get()["edit_render_max_edge"]
        if "long_edge" in args:
            args["long_edge"] = min(_json_integer(args["long_edge"], "long_edge", 256, 2048),
                                    configured_edge)
        else:
            # Preserve Studio's established 1024px default while making it a
            # real cap when the user chose a smaller presentation budget.
            args["long_edge"] = min(1024, configured_edge)
        return _json_content(self.wb.studio.create(**args))

    def _tool_trlr_render_parameter_variants(self, episode_id, **args):
        return _json_content(self.wb.studio.start_parameter_variants(episode_id, **args))

    def _tool_trlr_list_episodes(self, include_empty: bool = False):
        """List editing contexts, most recently touched first.

        Empty drafts (no source photo bound yet) are skipped by default. Each
        row is only a shortlist summary; callers must read the full Episode
        before deciding whether any work is outstanding.
        """
        rows = self.wb.studio.list()
        if not include_empty:
            rows = [row for row in rows if row.get('source_uuid')]
        return _json_content(rows)

    def _tool_trlr_get_episode(self, episode_id):
        return _json_content(self.wb.studio.get(episode_id))

    def _tool_trlr_edit_episode(self, episode_id, **args):
        return _json_content(self.wb.studio.action(episode_id, 'edit', **args))

    def _tool_trlr_stop_episode(self, episode_id):
        return _json_content(self.wb.studio.stop(episode_id))

    def _tool_trlr_restore_episode(self, episode_id, **args):
        return _json_content(self.wb.studio.action(episode_id, 'restore', **args))

    def _tool_trlr_message_episode(self, episode_id, **args):
        return _json_content(self.wb.studio.action(episode_id, 'message', **args))

    def _tool_trlr_verify_episode_xmp(self, episode_id, **args):
        return _json_content(self.wb.studio.action(episode_id, 'verify', **args))

    def _tool_trlr_episode_artifact(self, episode_id, name, max_edge=1024, crop=None):
        path = self.wb.studio.artifact(episode_id, name)
        if path.suffix in ('.xmp', '.json'):
            return {"content": [_text(path.read_text())]}
        max_edge = self._codex_preview_edge(max_edge)
        if name == 'reference.jpg':
            artifact = build_artifact_ref(
                path, artifact_id=f'episode:{episode_id}:reference',
                source={'kind': 'episode_reference', 'episode_id': episode_id},
                purpose='verified', method='LrExportSession', freshness='verified',
                evidence_status='verified_render', color_space='sRGB')
        elif name.startswith('v') and name.endswith('.jpg'):
            number = int(name[1:-4])
            record = self.wb.studio.get(episode_id)['versions'][number]
            artifact = record.get('artifact') or build_artifact_ref(
                path, artifact_id=f'episode:{episode_id}:v{number}',
                source={'kind': 'episode_render', 'episode_id': episode_id,
                        'version': number, 'photo_uuid': record.get('photo_uuid')},
                purpose='verified', method='LrExportSession', freshness='verified',
                evidence_status='verified_render', color_space='sRGB')
        else:
            artifact = build_artifact_ref(
                path, artifact_id=f'episode:{episode_id}:{name}',
                source={'kind': 'derived_image', 'episode_id': episode_id, 'name': name},
                purpose='analysis', method='ImageTools.compare', freshness='derived',
                evidence_status='derived', color_space='sRGB')
        if crop is None:
            return _image_content(path, max_edge, {'episode_id': episode_id,
                                                    'artifact': name,
                                                    'artifact_ref': artifact})
        import math
        from PIL import Image
        if not isinstance(crop, list) or len(crop) != 4 or any(type(v) not in (float, int) or not math.isfinite(v) for v in crop):
            raise ValueError('crop must be [x,y,width,height]')
        x, y, w, h = crop
        if min(x,y) < 0 or min(w,h) <= 0 or x+w > 1 or y+h > 1:
            raise ValueError('crop outside normalized image bounds')
        with Image.open(path) as original:
            box = (round(x*original.width), round(y*original.height), round((x+w)*original.width), round((y+h)*original.height))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError('crop is smaller than one pixel')
            image = original.convert('RGB').crop(box)
            image.thumbnail((max_edge,max_edge))
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=90)
        return {'content': [_text(json.dumps({'artifact': name, 'crop': crop,
                                              'artifact_ref': artifact}, ensure_ascii=False)),
                               {'type':'image','mimeType':'image/jpeg','data':base64.b64encode(buffer.getvalue()).decode()}]}

    # Shared read-only image tools; no Lightroom call on any of these paths.

    def _tool_trlr_capabilities(self):
        data = dict(self.wb.studio.capabilities())
        # The folder capability triple (direct / lightroom / scan-only) lives
        # with the folder layer, not in either protocol adapter.
        data["folder"] = self.wb.folders.public_state()["capabilities"]
        return _json_content(data)

    # Local folder browsing delegates to the protocol-neutral FolderLibrary.

    def _tool_trlr_folder_roots(self):
        return _json_content(self.wb.folders.public_state())

    def _tool_trlr_folder_scan(self, root=None, dir="", path=None):
        if path is not None:
            raise ValueError("不接受绝对路径；请先用 trlr_folder_roots 取 root，再用 dir 定位。")
        return _json_content(self.wb.folders.scan(root=root, rel=dir or ""))

    def _tool_trlr_folder_preview(self, root, name, dir="", max_edge=1024,
                                  render="auto", catalog_write="never"):
        max_edge = self._codex_preview_edge(max_edge)
        if catalog_write not in ("allow", "never"):
            raise ValueError('catalog_write must be "allow" or "never"')
        if render not in ("auto", "direct", "lightroom"):
            raise ValueError("render must be auto, direct or lightroom")
        # A Lightroom-backed folder preview is only prepared for this image
        # response, so neither its render nor its returned block needs pixels
        # beyond the stricter of the work-render and Codex budgets.
        render_edge = min(self.wb.presentation.get()["edit_render_max_edge"], max_edge)
        record = self.wb.folders.preview(
            root=root, rel=dir or "", name=name, client=self.wb.catalog_client(),
            edge=render_edge,
            render=render, catalog_write=(catalog_write == "allow"))
        served = self.wb.folders.preview_bytes(record["key"])
        if served is None:
            raise StoreError("预览字节不可用；请重新扫描该目录。")
        return _image_content_bytes(served[0], int(max_edge), record)

    # ---- JSON-RPC plumbing ------------------------------------------------------

    def handle(self, msg: dict) -> dict | None:
        if not isinstance(msg, dict):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        method = msg.get("method")
        msg_id = msg.get("id")
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": RUNTIME_OPERATOR_INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                try:
                    if not isinstance(params, dict):
                        raise ValueError("params must be an object")
                    arguments = params.get("arguments", {})
                    if arguments is None:
                        arguments = {}
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be an object")
                    return {"jsonrpc": "2.0", "id": msg_id,
                            "result": {**self.call_tool(params.get("name", ""),
                                                        arguments),
                                       "isError": False}}
                except (StoreError, LrOpsError, FolderError, KeyError, ValueError,
                        TypeError, OSError) as exc:
                    return {"jsonrpc": "2.0", "id": msg_id,
                            "result": {"content": [_text(f"error: {exc}")],
                                       "isError": True}}
            else:
                return {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32601, "message": f"method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except Exception as exc:  # last-resort guard: never kill the loop
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": str(exc)}}

    def serve(self, stdin, stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }) + "\n")
                stdout.flush()
                continue
            response = self.handle(msg)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


def main() -> None:
    McpServer(build_workbench()).serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
