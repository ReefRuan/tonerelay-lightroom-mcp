"""MCP contract tests for live Lightroom target/folder gallery reads.

The bridge fake returns deliberately path-tainted records, because the Runtime
must reconstruct the public selection page before it reaches Codex.  It also
models a missing Lightroom thumbnail independently per photo so the gallery's
partial-result contract is exercised without an export-session fallback.
"""

from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from workbench.lrops import FastThumbnailUnavailable, LrOpsError
from workbench.runtime import Workbench
from workbench.state import RuntimeState
from workbench_mcp.server import McpServer


CATALOG_IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"
FOLDER_ID = "folder-md5-v1:0123456789abcdef"


def photo(uuid, file_name, **extra):
    value = {
        "uuid": uuid,
        "file_name": file_name,
        "copy_name": "",
        "is_virtual_copy": False,
        "width": 6000,
        "height": 4000,
    }
    value.update(extra)
    return value


class SelectionGalleryBridge:
    """Fake only the three LR operations used by selection/gallery reads."""

    def __init__(self, *, no_target=False, unavailable=(), failures=None,
                 corrupt=()):
        self.base = "http://127.0.0.1:17777"
        self.token = None
        self.catalog_id = "test.lrcat"
        self.no_target = no_target
        self.current_folder_selection = True
        self.unavailable = set(unavailable)
        self.failures = dict(failures or {})
        self.corrupt = set(corrupt)
        self.calls = []
        self.thumbnail_sizes = []
        self.outputs = {}
        self.photos = [
            photo("photo-1", "IMG_0001.RAF", path="/private/leaf/IMG_0001.RAF"),
            photo("photo-2", "IMG_0002.RAF", path="/private/leaf/IMG_0002.RAF"),
        ]

    def reachable(self):
        return True

    def run(self, command, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((command, params))
        if command == "probe_client":
            return {
                "access_mode": "full",
                "catalog_identity": CATALOG_IDENTITY,
                "catalog_id": self.catalog_id,
                "sandbox": False,
                "catalog_authorized": True,
                "capabilities": {
                    "expected_catalog_identity": True,
                    "expected_access_mode": True,
                    "develop_snapshot": True,
                    "current_folder_selection": self.current_folder_selection,
                },
            }

        self.assert_bound(params)
        if command == "get_current_folder":
            return self.current_folder(params)
        if command == "request_jpeg_thumbnail":
            uuid = params["photo_uuid"]
            self.thumbnail_sizes.append(params["size"])
            if uuid in self.failures:
                raise self.failures[uuid]
            if uuid in self.unavailable:
                # The public gallery must not echo an underlying local path.
                raise FastThumbnailUnavailable(
                    "Lightroom fast thumbnail is unavailable")
            ref = "thumbnail-" + uuid
            if uuid in self.corrupt:
                self.outputs[ref] = b"not a jpeg"
            else:
                color = (220, 40, 40) if uuid == "photo-1" else (40, 120, 220)
                data = io.BytesIO()
                Image.new("RGB", (256, 192), color).save(data, format="JPEG")
                self.outputs[ref] = data.getvalue()
            return {
                "output_ref": ref,
                "method": "requestJpegThumbnail",
                "purpose": "fast",
                "freshness": "unverified",
            }
        raise AssertionError("unexpected Lightroom command " + command)

    @staticmethod
    def assert_bound(params):
        assert params.get("expected_access_mode") == "full", params
        assert params.get("expected_catalog_identity") == CATALOG_IDENTITY, params

    def current_folder(self, params):
        limit, offset = params["limit"], params["offset"]
        if self.no_target:
            return {
                "count": 0,
                "total": 0,
                "limit": limit,
                "offset": offset,
                "has_more": False,
                "photos": [],
            }
        page = self.photos[offset:offset + limit]
        return {
            "target": photo("photo-1", "IMG_0001.RAF", path="/private/leaf/IMG_0001.RAF"),
            "folder": {
                "id": FOLDER_ID,
                "label": "leaf",
                "path": "/private/leaf",
            },
            "count": len(page),
            "total": len(self.photos),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page) < len(self.photos),
            "photos": page,
        }

    def fetch_output(self, output_ref, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.outputs[output_ref])
        return destination


def call(server, name, arguments):
    response = server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return response["result"]


def success(result):
    if result["isError"]:
        raise AssertionError(result["content"][0]["text"])
    return result


class SelectionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = SelectionGalleryBridge()
        self.workbench = Workbench(RuntimeState(Path(self.tmp.name)), self.bridge)
        self.server = McpServer(self.workbench)

    def test_tools_have_bounded_and_path_free_schemas(self):
        tools = {tool["name"]: tool for tool in self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        current = tools["trlr_get_current_selection"]["inputSchema"]
        gallery = tools["trlr_get_current_folder_previews"]["inputSchema"]
        self.assertFalse(current["additionalProperties"])
        self.assertFalse(gallery["additionalProperties"])
        self.assertEqual((gallery["properties"]["limit"]["minimum"],
                          gallery["properties"]["limit"]["maximum"]), (1, 20))
        self.assertEqual(gallery["properties"]["limit"]["default"], 10)
        self.assertEqual(gallery["properties"]["size"]["default"], 512)
        self.assertEqual(gallery["properties"]["max_edge"]["default"], 512)

    def test_current_selection_returns_only_target_folder_summary(self):
        result = success(call(self.server, "trlr_get_current_selection", {}))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(set(payload), {"target", "folder", "total"})
        self.assertEqual(payload["target"]["file_name"], "IMG_0001.RAF")
        self.assertEqual(payload["folder"], {"id": FOLDER_ID, "label": "leaf"})
        self.assertEqual(payload["total"], 2)
        self.assertEqual(len(result["content"]), 1)
        self.assertNotIn("/private/", json.dumps(payload))
        self.assertEqual([name for name, _ in self.bridge.calls],
                         ["probe_client", "get_current_folder"])

    def test_gallery_orders_metadata_text_and_native_images_per_photo(self):
        result = success(call(self.server, "trlr_get_current_folder_previews", {
            "limit": 2, "offset": 0, "size": 512, "max_edge": 128,
        }))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual([item["photo"]["file_name"] for item in payload["items"]],
                         ["IMG_0001.RAF", "IMG_0002.RAF"])
        self.assertEqual([item["preview"]["state"] for item in payload["items"]],
                         ["ready", "ready"])
        self.assertEqual(payload["presentation"], {
            "requested_thumbnail_size": 512,
            "effective_thumbnail_size": 128,
            "effective_max_edge": 128,
        })
        self.assertNotIn("/private/", json.dumps(payload))
        # Page JSON, then per-photo label/display/native image in order.
        self.assertEqual([block["type"] for block in result["content"]],
                         ["text", "text", "text", "image", "text", "text", "image"])
        first_label = json.loads(result["content"][1]["text"])
        second_label = json.loads(result["content"][4]["text"])
        self.assertEqual([first_label["file_name"],
                          second_label["file_name"]],
                         ["IMG_0001.RAF", "IMG_0002.RAF"])
        self.assertEqual([first_label["index"], second_label["index"]], [0, 1])
        self.assertEqual({"index", "file_name"}, set(first_label))
        self.assertEqual({"index", "file_name"}, set(second_label))
        for image_index, position in enumerate((3, 6)):
            reference = json.loads(result["content"][position - 1]["text"])["display"]
            image_block = result["content"][position]
            self.assertEqual(reference["image_index"], image_index)
            self.assertEqual(reference["kind"], "local_file")
            self.assertEqual(Path(reference["path"]).read_bytes(), base64.b64decode(image_block["data"]))
            self.assertEqual(image_block["mimeType"], "image/jpeg")
            with Image.open(io.BytesIO(base64.b64decode(image_block["data"]))) as image:
                self.assertLessEqual(max(image.size), 128)
        self.assertEqual(self.bridge.thumbnail_sizes, [128, 128])
        self.assertEqual([name for name, _ in self.bridge.calls], [
            "probe_client", "get_current_folder",
            "request_jpeg_thumbnail", "request_jpeg_thumbnail",
        ])

    def test_one_unavailable_thumbnail_keeps_page_and_other_image(self):
        self.bridge.unavailable.add("photo-2")
        result = success(call(self.server, "trlr_get_current_folder_previews", {"limit": 2}))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual([item["preview"]["state"] for item in payload["items"]],
                         ["ready", "unavailable"])
        self.assertEqual(payload["items"][1]["preview"], {
            "state": "unavailable",
            "reason": "fast_thumbnail_unavailable",
        })
        self.assertEqual([block["type"] for block in result["content"]],
                         ["text", "text", "text", "image", "text"])
        unavailable_label = json.loads(result["content"][4]["text"])
        self.assertEqual(unavailable_label["index"], 1)
        self.assertEqual(unavailable_label["file_name"], "IMG_0002.RAF")
        self.assertEqual(unavailable_label["state"], "unavailable")
        self.assertNotIn("/private/", "".join(
            block.get("text", "") for block in result["content"]))
        self.assertEqual(self.bridge.thumbnail_sizes, [512, 512])

    def test_bridge_or_catalog_failure_aborts_gallery_and_records_issue(self):
        self.bridge.failures["photo-2"] = LrOpsError(
            "Catalog authorization changed after probe")
        result = call(self.server, "trlr_get_current_folder_previews", {"limit": 2})
        self.assertTrue(result["isError"])
        self.assertIn("Catalog authorization changed", result["content"][0]["text"])
        issue = self.workbench.issues.list(kind="tool_error", limit=1)[0]
        self.assertEqual(issue["tool"], "trlr_get_current_folder_previews")
        self.assertEqual(issue["detail"]["error_type"], "LrOpsError")

    def test_corrupt_thumbnail_aborts_gallery_and_records_issue(self):
        self.bridge.corrupt.add("photo-2")
        result = call(self.server, "trlr_get_current_folder_previews", {"limit": 2})
        self.assertTrue(result["isError"])
        self.assertIn("not a complete JPEG", result["content"][0]["text"])
        issue = self.workbench.issues.list(kind="tool_error", limit=1)[0]
        self.assertEqual(issue["detail"]["error_type"], "ValueError")

    def test_preview_cache_write_failure_aborts_gallery_and_records_issue(self):
        with mock.patch.object(self.workbench._previews, "publish",
                               side_effect=OSError("preview cache disk full")):
            result = call(self.server, "trlr_get_current_folder_previews", {"limit": 2})
        self.assertTrue(result["isError"])
        self.assertIn("preview cache disk full", result["content"][0]["text"])
        issue = self.workbench.issues.list(kind="tool_error", limit=1)[0]
        self.assertEqual(issue["detail"]["error_type"], "OSError")

    def test_presentation_cap_tightens_gallery_thumbnail_and_returned_image(self):
        configured = success(call(self.server, "trlr_set_presentation_config", {
            "codex_preview_max_edge": 64,
        }))
        self.assertEqual(json.loads(configured["content"][0]["text"])["codex_preview_max_edge"], 64)

        result = success(call(self.server, "trlr_get_current_folder_previews", {
            "limit": 2, "size": 1024, "max_edge": 1024,
        }))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["presentation"], {
            "requested_thumbnail_size": 1024,
            "effective_thumbnail_size": 64,
            "effective_max_edge": 64,
        })
        self.assertEqual(self.bridge.thumbnail_sizes, [64, 64])
        for block in result["content"]:
            if block["type"] == "image":
                with Image.open(io.BytesIO(base64.b64decode(block["data"]))) as image:
                    self.assertLessEqual(max(image.size), 64)

    def test_no_current_target_returns_empty_summary_and_no_image_request(self):
        self.bridge.no_target = True
        selection = success(call(self.server, "trlr_get_current_selection", {}))
        self.assertEqual(json.loads(selection["content"][0]["text"]), {
            "target": None, "folder": None, "total": 0,
        })

        self.bridge.calls.clear()
        result = success(call(self.server, "trlr_get_current_folder_previews", {}))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["target"], None)
        self.assertEqual(payload["folder"], None)
        self.assertEqual(payload["items"], [])
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual([name for name, _ in self.bridge.calls],
                         ["probe_client", "get_current_folder"])

    def test_legacy_plugin_missing_current_folder_command_has_recovery_error(self):
        self.bridge.current_folder_selection = False

        result = call(self.server, "trlr_get_current_selection", {})

        self.assertTrue(result["isError"])
        self.assertIn("plugin is incompatible", result["content"][0]["text"])
        self.assertIn("restart Lightroom", result["content"][0]["text"])
        self.assertEqual([name for name, _ in self.bridge.calls],
                         ["probe_client"])

    def test_invalid_gallery_arguments_do_not_reach_lightroom(self):
        bad = [
            {"limit": True},
            {"limit": 0},
            {"limit": 21},
            {"offset": -1},
            {"size": True},
            {"size": 63},
            {"max_edge": True},
            {"max_edge": 2049},
            {"unexpected": True},
        ]
        for arguments in bad:
            with self.subTest(arguments=arguments):
                result = call(self.server, "trlr_get_current_folder_previews", arguments)
                self.assertTrue(result["isError"])
                self.assertTrue(result["content"][0]["text"].startswith("error:"))
        self.assertEqual(self.bridge.calls, [])


if __name__ == "__main__":
    unittest.main()
