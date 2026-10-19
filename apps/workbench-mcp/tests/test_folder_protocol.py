"""Protocol tests for the folder tools, including one over real stdio.

The tools delegate directly to ``FolderLibrary``; what is worth pinning down
here is the protocol-neutral *contract* around them:

* callers use an opaque root key and relative directory; no folder tool
  accepts an arbitrary absolute path or returns one;
* ``catalog_write`` defaults to ``never``, so an agent gets a pure read;
* a refusal arrives as a tool error (``isError: true``), not as a JSON-RPC
  -32603, which is what a ``FolderError`` would produce if it were missing
  from the server's except tuple.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from workbench.runtime import Workbench
from lr_ops.configuration import defaults
from workbench.folders import DEFAULT_EXTENSIONS
from workbench.store import Store
from workbench_mcp.server import TOOLS, McpServer

MCP_DIR = Path(__file__).resolve().parents[1]


def _jpeg(size=(40, 24)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


JPEG_BYTES = _jpeg()
RAW_BYTES = b"pretend-cr2"


class FolderBridge:
    """Answers only the commands a folder preview is allowed to send."""

    def __init__(self):
        self.calls = []
        self.export_edges = []
        self.base = "http://127.0.0.1:17777"

    def run(self, command, params=None):
        self.calls.append(command)
        if command == "probe_client":
            return {
                "access_mode": "full",
                "catalog_identity": "catalog-md5-v1:folder-protocol-test",
                "catalog_id": "test.lrcat",
                "sandbox": False,
                "catalog_authorized": True,
                "capabilities": {
                    "develop_snapshot": True,
                    "expected_access_mode": True,
                    "expected_catalog_identity": True,
                },
            }
        assert params.get("expected_access_mode") == "full", params
        assert params.get("expected_catalog_identity") == \
            "catalog-md5-v1:folder-protocol-test", params
        if command == "import_photo":
            return {"photo": {"uuid": "PHOTO-UUID"}}
        if command == "export_preview":
            self.export_edges.append(params["long_edge"])
            return {"output_ref": "ref", "color_space": "sRGB"}
        raise AssertionError(f"unexpected Lightroom command {command}")

    def upload(self, filename, content):
        self.calls.append("upload")
        return "stage"

    def fetch_output(self, ref, destination):
        self.calls.append("fetch_output")
        Path(destination).write_bytes(JPEG_BYTES)
        return Path(destination)

    def discard_stage(self, stage_id):
        self.calls.append("discard")

    def reachable(self):
        return True


def call_tool(server, name, arguments, msg_id=1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})


def payload_text(response) -> str:
    return response["result"]["content"][0]["text"]


class FolderToolTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.photos = Path(self.tmp.name) / "photos"
        self.photos.mkdir(parents=True)
        self.data.mkdir(parents=True)
        config = defaults()
        config["folders"] = {"mode": "restricted", "roots": [str(self.photos)],
                             "extensions": list(DEFAULT_EXTENSIONS), "recursive": False}
        (self.data / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.bridge = FolderBridge()
        self.server = McpServer(Workbench(Store(self.data), self.bridge))

    def put(self, name, content=JPEG_BYTES, directory=None):
        target = (directory or self.photos) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def root_key(self):
        response = call_tool(self.server, "trlr_folder_roots", {})
        return json.loads(payload_text(response))["roots"][0]["key"]


class SchemaTestCase(unittest.TestCase):
    def test_the_three_tools_are_declared_and_none_takes_a_path(self):
        by_name = {tool["name"]: tool for tool in TOOLS}
        self.assertIn("trlr_folder_roots", by_name)
        self.assertIn("trlr_folder_scan", by_name)
        self.assertIn("trlr_folder_preview", by_name)
        for name in ("trlr_folder_roots", "trlr_folder_scan", "trlr_folder_preview"):
            with self.subTest(tool=name):
                schema = by_name[name]["inputSchema"]
                self.assertFalse(schema.get("additionalProperties", True))
                self.assertNotIn("path", schema.get("properties", {}))
                self.assertNotIn("root_path", schema.get("properties", {}))

    def test_preview_defaults_to_a_pure_read(self):
        schema = {tool["name"]: tool for tool in TOOLS}["trlr_folder_preview"]["inputSchema"]
        self.assertEqual(schema["properties"]["catalog_write"]["default"], "never")
        self.assertEqual(schema["properties"]["catalog_write"]["enum"], ["allow", "never"])
        self.assertEqual(schema["properties"]["render"]["default"], "auto")
        self.assertEqual(schema["properties"]["root"]["pattern"], "^[0-9a-f]{12}$")

    def test_tool_registry_has_unique_names_and_required_folder_surfaces(self):
        names = [tool["name"] for tool in TOOLS]
        self.assertEqual(len(names), len(set(names)), "工具名必须唯一")
        for required in ("trlr_health", "trlr_list_photos", "trlr_get_photo_preview",
                         "trlr_create_session", "trlr_capabilities",
                         "trlr_image_measure", "trlr_image_compare",
                         "trlr_list_develop_presets", "trlr_render_parameter_variants",
                         "trlr_export_image",
                         "trlr_get_full_settings", "trlr_round_diff",
                         "trlr_replay_round", "trlr_export_session_context"):
            self.assertIn(required, names)


class FolderToolProtocolTest(FolderToolTestCase):
    def test_roots_returns_keys_labels_and_capabilities_without_paths(self):
        response = call_tool(self.server, "trlr_folder_roots", {})
        text = payload_text(response)
        body = json.loads(text)
        self.assertNotIn(str(self.photos), text)
        self.assertEqual(set(body["roots"][0]), {"key", "label"})
        self.assertEqual(body["roots"][0]["label"], self.photos.name)
        self.assertIn(".cr2", body["capabilities"]["lightroom_preview"])
        self.assertIn(".jpg", body["capabilities"]["direct_preview"])

    def test_scan_matches_what_http_would_return_for_the_same_directory(self):
        self.put("one.jpg")
        self.put("two.cr2", RAW_BYTES)
        self.put("notes.txt", b"x")
        root = self.root_key()
        response = call_tool(self.server, "trlr_folder_scan", {"root": root})
        body = json.loads(payload_text(response))
        direct = self.server.wb.folders.scan(root=root)
        self.assertEqual(body["count"], direct["count"])
        self.assertEqual([f["key"] for f in body["files"]],
                         [f["key"] for f in direct["files"]])
        self.assertNotIn(str(self.photos), payload_text(response))
        routes = {f["name"]: f["preview"] for f in body["files"]}
        self.assertEqual(routes, {"one.jpg": "direct", "two.cr2": "lightroom_export"})

    def test_scan_refuses_an_absolute_path_argument(self):
        response = call_tool(self.server, "trlr_folder_scan",
                             {"root": self.root_key(), "path": str(self.photos)})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("绝对路径", payload_text(response))

    def test_scanning_never_calls_lightroom(self):
        self.put("one.jpg")
        self.put("two.cr2", RAW_BYTES)
        call_tool(self.server, "trlr_folder_scan", {"root": self.root_key()})
        self.assertEqual(self.bridge.calls, [])

    def test_direct_preview_returns_metadata_and_an_image_block(self):
        self.put("one.jpg")
        response = call_tool(self.server, "trlr_folder_preview",
                             {"root": self.root_key(), "name": "one.jpg"})
        result = response["result"]
        self.assertFalse(result["isError"])
        metadata = json.loads(result["content"][0]["text"])
        self.assertEqual(metadata["method"], "direct")
        self.assertEqual(metadata["cache_status"], "direct")
        self.assertIsNone(metadata["uuid"])
        self.assertEqual(metadata["artifact"]["schema_version"], "artifact-ref-v1")
        self.assertEqual(metadata["artifact"]["method"], "direct_read")
        self.assertNotIn("url", metadata)
        self.assertNotIn("preview_url", metadata["artifact"])
        self.assertNotIn("resolved", metadata)
        self.assertNotIn(str(self.photos), result["content"][0]["text"])
        image = next(block for block in result["content"] if block["type"] == "image")
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["mimeType"], "image/jpeg")
        self.assertTrue(image["data"])
        self.assertEqual(self.bridge.calls, [], "默认纯读不应产生任何桥调用")

    def test_raw_preview_is_refused_by_default_and_explains_why(self):
        self.put("one.cr2", RAW_BYTES)
        response = call_tool(self.server, "trlr_folder_preview",
                             {"root": self.root_key(), "name": "one.cr2"})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("catalog_write=allow", payload_text(response))
        self.assertEqual(self.bridge.calls, [])

    def test_raw_preview_with_allow_goes_through_lightroom(self):
        self.put("one.cr2", RAW_BYTES)
        response = call_tool(self.server, "trlr_folder_preview",
                             {"root": self.root_key(), "name": "one.cr2",
                              "catalog_write": "allow"})
        metadata = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(metadata["method"], "lightroom_export")
        self.assertEqual(metadata["uuid"], "PHOTO-UUID")
        self.assertEqual(self.bridge.calls, ["probe_client", "upload", "import_photo",
                                             "export_preview", "fetch_output"])

    def test_raw_preview_does_not_render_beyond_the_effective_image_budget(self):
        self.put("one.cr2", RAW_BYTES)
        configured = call_tool(self.server, "trlr_set_presentation_config", {
            "codex_preview_max_edge": 128,
            "edit_render_max_edge": 512,
        })
        self.assertFalse(configured["result"]["isError"])

        response = call_tool(self.server, "trlr_folder_preview", {
            "root": self.root_key(), "name": "one.cr2", "catalog_write": "allow",
            "max_edge": 64,
        })

        self.assertFalse(response["result"]["isError"])
        self.assertEqual(self.bridge.export_edges, [64])

    def test_the_second_raw_preview_is_served_from_cache(self):
        self.put("one.cr2", RAW_BYTES)
        arguments = {"root": self.root_key(), "name": "one.cr2", "catalog_write": "allow"}
        call_tool(self.server, "trlr_folder_preview", arguments)
        self.bridge.calls = []
        response = call_tool(self.server, "trlr_folder_preview", arguments)
        metadata = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(metadata["cache_status"], "hit")
        self.assertEqual(self.bridge.calls, [])

    def test_arguments_are_validated_before_anything_else_happens(self):
        self.put("one.jpg")
        root = self.root_key()
        for arguments, fragment in (
                ({"root": root, "name": "one.jpg", "catalog_write": "maybe"}, "catalog_write"),
                ({"root": root, "name": "one.jpg", "render": "sure"}, "render"),
                ({"root": root, "name": "one.jpg", "max_edge": 3}, "max_edge"),
                ({"root": root, "name": "one.jpg", "max_edge": True}, "max_edge"),
                ({"root": "../etc", "name": "one.jpg"}, "根目录"),
                ({"root": root, "name": "../one.jpg"}, "文件名")):
            with self.subTest(arguments=arguments):
                response = call_tool(self.server, "trlr_folder_preview", arguments)
                self.assertTrue(response["result"]["isError"])
                self.assertIn(fragment, payload_text(response))

    def test_the_key_namespace_is_pinned_to_the_live_cache(self):
        """The same hash the blobs on disk are named under."""
        from workbench.folders import file_key_for
        # The cached macOS /var path resolves to /private/var; Windows keeps
        # its drive and backslashes. Pin each resolved spelling on every host.
        for resolved, key in (
                ("/private/var/tmp/tonerelay-fixtures/IMG_0004.CR2", "7d5a4125b75d460c18f9"),
                ("/var/tmp/tonerelay-fixtures/IMG_0004.CR2", "495216a882d2b29261c5"),
                (r"C:\var\tmp\tonerelay-fixtures\IMG_0004.CR2", "b6ac31594a6e21adcad5")):
            with self.subTest(resolved=resolved), mock.patch("workbench.folders.Path") as path:
                path.return_value.resolve.return_value = resolved
                self.assertEqual(file_key_for("fixture"), key)

    def test_folder_errors_are_tool_errors_not_protocol_errors(self):
        """A FolderError escaping to -32603 would break every client."""
        response = call_tool(self.server, "trlr_folder_scan", {"root": "f" * 12})
        self.assertNotIn("error", response, "不应变成 JSON-RPC 协议错误")
        self.assertTrue(response["result"]["isError"])
        self.assertIn("没有这个根目录", payload_text(response))

    def test_capabilities_expose_the_folder_three_way_split(self):
        response = call_tool(self.server, "trlr_capabilities", {})
        body = json.loads(payload_text(response))
        folder = body["folder"]
        self.assertIn(".jpg", folder["direct_preview"])
        self.assertIn(".cr2", folder["lightroom_preview"])
        self.assertTrue(folder["scan"])
        self.assertNotIn(str(self.photos), payload_text(response))


class StdioProtocolTest(unittest.TestCase):
    """One real subprocess, to prove the tools are on the wire, not just in a list."""

    def test_tools_list_over_stdio_advertises_the_folder_tools(self):
        env = dict(os.environ)
        env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"] = tempfile.mkdtemp(prefix="mcp-folder-")
        self.addCleanup(shutil.rmtree, env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"],
                        ignore_errors=True)
        env["LROPS_BASE"] = "http://127.0.0.1:1"  # nothing connects during tools/list
        env["PYTHONPATH"] = os.pathsep.join(
            [str(MCP_DIR), str(MCP_DIR.parent / "workbench"), str(env.get("PYTHONPATH", ""))])
        process = subprocess.Popen(
            [sys.executable, "-m", "workbench_mcp"], cwd=str(MCP_DIR), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        self.addCleanup(process.kill)
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        process.stdin.write("".join(json.dumps(item) + "\n" for item in requests))
        process.stdin.flush()
        deadline = time.monotonic() + 20
        replies = []
        while len(replies) < 2 and time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            replies.append(json.loads(line))
        process.stdin.close()
        process.wait(timeout=10)
        stderr = process.stderr.read()
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(len(replies), 2, f"stderr: {stderr[:400]}")
        self.assertEqual(replies[0]["id"], 1)
        from workbench_mcp import __version__
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], __version__)
        names = [tool["name"] for tool in replies[1]["result"]["tools"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("trlr_parameter_reference", names)
        self.assertIn("trlr_folder_roots", names)
        self.assertIn("trlr_folder_scan", names)
        self.assertIn("trlr_folder_preview", names)
        self.assertIn("trlr_export_image", names)
        self.assertIn("trlr_image_measure", names)


if __name__ == "__main__":
    unittest.main()
