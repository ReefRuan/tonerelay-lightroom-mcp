"""Offline protocol tests for the workbench MCP server.

Uses a fake Workbench so no Lightroom bridge is needed. Verifies the
JSON-RPC plumbing: initialize, tools/list, tools/call, error mapping.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.catalog import CatalogTools  # noqa: E402
from workbench_mcp.server import McpServer, PROTOCOL_VERSION, TOOLS  # noqa: E402


class FakeStore:
    pass


def photo(uuid, file_name):
    return {"uuid": uuid, "file_name": file_name, "copy_name": "",
            "is_virtual_copy": False, "width": 6000, "height": 4000,
            "rating": 0}


class FakeLrOps:
    """Bridge stand-in that filters and paginates like the plugin does."""

    def __init__(self):
        self.photos = [photo("u1", "IMG_0001.JPG"), photo("u2", "IMG_0002.CR2")]

    def reachable(self):
        return True

    def run(self, command, params=None):
        if command == "search_photos":
            params = dict(params or {})
            needle = (params.get("name_contains") or "").lower()
            rating = params.get("rating")
            matches = [p for p in self.photos
                       if needle in p["file_name"].lower()
                       and (rating is None or p["rating"] == rating)]
            offset = params.get("offset", 0)
            limit = params.get("limit", 100)
            page = matches[offset:offset + limit]
            return {"count": len(page), "total": len(matches),
                    "limit": limit, "offset": offset,
                    "has_more": offset + len(page) < len(matches),
                    "photos": page}
        if command == "list_photos":
            return {"count": 2, "photos": self.photos}
        return {"ok": True}


class FakeWorkbench:
    def __init__(self):
        self.store = FakeStore()
        self.lrops = FakeLrOps()
        self.catalog_tools = CatalogTools(self.run)

    def run(self, command, params=None):
        return self.lrops.run(command, params)

    def health(self):
        return {"ok": True, "lrops": True,
                "catalog_policy": {
                    "access_mode": "full",
                    "catalog_identity": "catalog-md5-v1:protocol-test",
                    "authorized": True, "reason": None}}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(FakeWorkbench())

    def rpc(self, method, params=None, msg_id=1):
        return self.server.handle({"jsonrpc": "2.0", "id": msg_id,
                                   "method": method, "params": params or {}})

    def test_initialize(self):
        resp = self.rpc("initialize")
        self.assertEqual(resp["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_tools_list(self):
        resp = self.rpc("tools/list")
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(len(TOOLS), len(names))
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(name.startswith("trlr_") for name in names))
        self.assertEqual([], [name for name in names
                              if not callable(getattr(self.server, f"_tool_{name}", None))])
        self.assertIn("trlr_replay_episode", names)
        self.assertIn("trlr_get_photo_settings", names)
        self.assertIn("trlr_get_photo_preview", names)

    def test_legacy_tool_prefix_is_not_aliased(self):
        resp = self.rpc("tools/call", {"name": "lrwb_health", "arguments": {}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("unknown tool", resp["result"]["content"][0]["text"])

    def test_call_health(self):
        resp = self.rpc("tools/call", {"name": "trlr_health", "arguments": {}})
        self.assertFalse(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["lrops"])
        self.assertTrue(payload["catalog_policy"]["authorized"])

    def test_call_list_photos_filter(self):
        resp = self.rpc("tools/call", {"name": "trlr_list_photos",
                                       "arguments": {"name_contains": ".cr2"}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["photos"][0]["uuid"], "u2")

    def test_unknown_tool_is_error_result(self):
        resp = self.rpc("tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("unknown tool", resp["result"]["content"][0]["text"])

    def test_missing_required_arg_is_error_result(self):
        resp = self.rpc("tools/call", {"name": "trlr_get_photo_settings", "arguments": {}})
        self.assertTrue(resp["result"]["isError"])

    def test_unknown_method(self):
        resp = self.rpc("bogus/method")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_notifications_are_ignored(self):
        self.assertIsNone(self.server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
