"""Offline protocol tests for the fast photo preview tool and MCP startup.

`trlr_get_photo_preview` must go through ``Workbench.preview_photo`` with the
same validation, artifact publication, and pinned target semantics.
The server must use the canonical Runtime factory and registry path. Registry
objects remain process-local snapshots.
"""

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import workbench_mcp.server as server_module
from workbench.runtime import Workbench
from workbench.store import Store
from workbench.targets import LrOpsRouter
from workbench_mcp.server import McpServer, TOOLS, build_workbench

CATALOG_IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"


class ThumbnailBridge:
    """Minimal bridge fake for the fast catalog-thumbnail seam."""

    def __init__(self, base="http://127.0.0.1:17777", orientation="AB"):
        self.base = base
        self.token = None
        self.catalog_id = None
        self.orientation = orientation
        self.calls = []
        self.outputs = {}
        self.dimensions = (32, 24)

    def reachable(self):
        return True

    def run(self, command, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((command, params))
        if command == "probe_client":
            return {"access_mode": "full", "catalog_identity": CATALOG_IDENTITY,
                    "catalog_id": "test.lrcat", "sandbox": False,
                    "catalog_authorized": True,
                    "capabilities": {"expected_catalog_identity": True,
                                     "expected_access_mode": True,
                                     "develop_snapshot": True}}
        if command == "request_jpeg_thumbnail":
            assert params.get("expected_catalog_identity") == CATALOG_IDENTITY
            assert params.get("expected_access_mode") == "full"
            ref = f"thumb-{len(self.outputs)}"
            buffer = io.BytesIO()
            Image.new("RGB", self.dimensions, (120, 80, 40)).save(buffer, format="JPEG")
            self.outputs[ref] = buffer.getvalue()
            return {"output_ref": ref, "method": "requestJpegThumbnail",
                    "purpose": "fast", "freshness": "unverified",
                    "orientation": self.orientation}
        raise AssertionError(f"unexpected Lightroom command {command}")

    def fetch_output(self, output_ref, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.outputs[output_ref])
        return destination


def call_tool(server, name, arguments, msg_id=1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})


def tool_payload(response):
    result = response["result"]
    if result["isError"]:
        raise AssertionError(result["content"][0]["text"])
    return result


def payload_metadata(response):
    return json.loads(tool_payload(response)["content"][0]["text"])


def error_text(response):
    return response["result"]["content"][0]["text"]


class PreviewProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = ThumbnailBridge()
        self.workbench = Workbench(Store(Path(self.tmp.name)), self.bridge)
        self.server = McpServer(self.workbench)

    @staticmethod
    def displays(payload):
        return [json.loads(block["text"])["display"] for block in payload["content"]
                if block["type"] == "text" and "display" in json.loads(block["text"])]

    def test_local_reference_exists_for_oversized_lightroom_thumbnail(self):
        # A real Lightroom call requested 1024 and returned 1512x1008.
        # The body needs a real reference even when the native block is resized.
        self.bridge.dimensions = (1512, 1008)
        with mock.patch.dict("os.environ", {"LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES": "1"}):
            payload = tool_payload(call_tool(self.server, "trlr_get_photo_preview",
                                            {"photo_uuid": "photo-1", "max_edge": 1024}))
        displays = self.displays(payload)
        self.assertEqual(len(displays), 1, "missing usable reply-body image reference")
        reference = displays[0]
        self.assertEqual(reference["kind"], "local_file")
        path = Path(reference["path"])
        self.assertTrue(path.is_absolute() and path.is_file())
        native = next(block for block in payload["content"] if block["type"] == "image")
        self.assertEqual(path.read_bytes(), base64.b64decode(native["data"]))
        with Image.open(path) as image:
            self.assertLessEqual(max(image.size), 1024)

    def test_local_delivery_keeps_native_image_and_reuses_reference(self):
        with mock.patch.dict("os.environ", {"LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES": "0"}):
            normal = tool_payload(call_tool(self.server, "trlr_get_photo_preview", {"photo_uuid": "photo-1"}))
        self.assertEqual(self.displays(normal), [])
        with mock.patch.dict("os.environ", {"LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES": "1"}):
            first = tool_payload(call_tool(self.server, "trlr_get_photo_preview", {"photo_uuid": "photo-1"}))
            path = Path(self.displays(first)[0]["path"])
            modified = path.stat().st_mtime_ns
            again = tool_payload(call_tool(self.server, "trlr_get_photo_preview", {"photo_uuid": "photo-1"}))
        self.assertEqual(self.displays(first), self.displays(again))
        self.assertEqual(path.stat().st_mtime_ns, modified)
        native = lambda payload: [block for block in payload["content"] if block["type"] == "image"]
        self.assertEqual(native(normal), native(first))
        self.assertEqual(native(first), native(again))
        self.assertEqual(path.read_bytes(), base64.b64decode(native(first)[0]["data"]))
        self.assertEqual(sum(name == "request_jpeg_thumbnail" for name, _ in self.bridge.calls), 1)

    def test_local_delivery_is_default_and_only_explicit_zero_disables_it(self):
        native = lambda payload: [block for block in payload["content"] if block["type"] == "image"]
        with mock.patch.dict("os.environ", {"LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES": "0"}):
            disabled = tool_payload(call_tool(self.server, "trlr_get_photo_preview", {"photo_uuid": "photo-1"}))
        self.assertEqual(self.displays(disabled), [])
        self.assertFalse((self.workbench.store.root / "image-delivery").exists())
        for value in (None, "1"):
            with self.subTest(value=value), mock.patch.dict("os.environ", {}):
                if value is None:
                    server_module.os.environ.pop("LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES", None)
                else:
                    server_module.os.environ["LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES"] = value
                payload = tool_payload(call_tool(self.server, "trlr_get_photo_preview", {"photo_uuid": "photo-1"}))
            self.assertEqual(native(payload), native(disabled))
            references = self.displays(payload)
            self.assertEqual(len(references), 1)
            self.assertEqual(references[0]["kind"], "local_file")
            self.assertEqual(references[0]["image_index"], 0)
            self.assertEqual(Path(references[0]["path"]).read_bytes(),
                             base64.b64decode(native(payload)[0]["data"]))
        self.assertEqual(sum(name == "request_jpeg_thumbnail" for name, _ in self.bridge.calls), 1)

    def test_schema_pins_the_arguments_the_handler_validates(self):
        tools = {tool["name"]: tool for tool in self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        schema = tools["trlr_get_photo_preview"]["inputSchema"]
        self.assertEqual(schema["required"], ["photo_uuid"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["size"]["default"], 1024)
        self.assertEqual((schema["properties"]["size"]["minimum"],
                          schema["properties"]["size"]["maximum"]), (64, 2048))
        self.assertIs(schema["properties"]["force"]["default"], False)
        self.assertEqual(schema["properties"]["force"]["type"], "boolean")

    def test_invalid_arguments_are_errors_and_never_reach_lightroom(self):
        bad_calls = [
            {"photo_uuid": "photo-1", "size": True},
            {"photo_uuid": "photo-1", "size": 1024.5},
            {"photo_uuid": "photo-1", "size": 63},
            {"photo_uuid": "photo-1", "size": 2049},
            {"photo_uuid": "photo-1", "force": "false"},
            {"photo_uuid": "photo-1", "force": 1},
            {"photo_uuid": "photo-1", "max_edge": True},
            {"photo_uuid": "photo-1", "max_edge": 63},
            {"photo_uuid": "photo-1", "max_edge": 4096},
            {"photo_uuid": ""},
            {"photo_uuid": "photo-1", "unexpected": True},
        ]
        for arguments in bad_calls:
            with self.subTest(arguments=arguments):
                response = call_tool(self.server, "trlr_get_photo_preview", arguments)
                self.assertTrue(response["result"]["isError"], arguments)
                self.assertTrue(error_text(response).startswith("error:"), arguments)
        self.assertEqual(self.bridge.calls, [])

    def test_render_then_cache_returns_the_same_artifact(self):
        rendered = tool_payload(call_tool(self.server, "trlr_get_photo_preview",
                                          {"photo_uuid": "photo-1", "size": 640,
                                           "max_edge": 64}))
        self.assertEqual(len(rendered["content"]), 3)
        self.assertEqual(self.displays(rendered)[0]["kind"], "local_file")
        self.assertEqual(rendered["content"][2]["type"], "image")
        first = json.loads(rendered["content"][0]["text"])
        self.assertFalse(first["cached"])
        self.assertEqual(first["freshness"], "unverified")
        self.assertEqual(first["artifact"]["evidence_status"], "unverified")
        self.assertNotIn("preview_path", first)

        cached = payload_metadata(call_tool(self.server, "trlr_get_photo_preview",
                                            {"photo_uuid": "photo-1", "size": 640,
                                             "max_edge": 64}))
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["freshness"], "cached_unverified")
        for field in ("key", "actual_size", "bytes", "orientation", "method",
                      "color_space"):
            self.assertEqual(cached[field], first[field], field)
        self.assertNotIn("preview_url", first)
        self.assertNotIn("preview_url", first["artifact"])
        self.assertEqual(cached["artifact"]["sha256"], first["artifact"]["sha256"])
        self.assertEqual(cached["artifact"]["source"], first["artifact"]["source"])
        self.assertNotIn("preview_path", cached)
        # The cache hit re-probes Catalog identity, then answers from published
        # metadata without requesting thumbnail bytes again.
        self.assertEqual([name for name, _ in self.bridge.calls],
                         ["probe_client", "request_jpeg_thumbnail", "probe_client"])

class McpStartupTests(unittest.TestCase):
    def test_build_workbench_uses_the_canonical_registry_path(self):
        workbench = build_workbench()
        self.assertIsInstance(workbench.lrops, LrOpsRouter)
        self.assertEqual(workbench.lrops.registry.path,
                         workbench.store.root / "config.json")
        self.assertEqual(workbench.lrops.target_id, "local")

    def test_main_serves_a_router_backed_workbench(self):
        with mock.patch.object(server_module, "McpServer") as mcp:
            server_module.main()
        workbench = mcp.call_args[0][0]
        self.assertIsInstance(workbench.lrops, LrOpsRouter)
        self.assertEqual(workbench.lrops.registry.path,
                         workbench.store.root / "config.json")

    def test_the_preview_tool_is_still_registered(self):
        names = {tool["name"] for tool in TOOLS}
        self.assertIn("trlr_get_photo_preview", names)


if __name__ == "__main__":
    unittest.main()
