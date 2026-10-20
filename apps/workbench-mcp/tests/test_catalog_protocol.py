"""Offline protocol tests for the paginated catalog photo tool.

`trlr_list_photos` must go through CatalogTools with the same validation,
normalized payload, and single Lightroom command per call.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from workbench.runtime import Workbench
from workbench.catalog import CatalogTools
from workbench.lrops import LrOpsError
from workbench.state import RuntimeState
from workbench_mcp.server import McpServer, TOOLS

CATALOG_IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"
PROBE = {
    "access_mode": "full",
    "catalog_identity": CATALOG_IDENTITY,
    "catalog_id": "test.lrcat",
    "sandbox": False,
    "catalog_authorized": True,
    "capabilities": {"expected_catalog_identity": True,
                     "expected_access_mode": True,
                     "develop_snapshot": True},
}


def photo(uuid, file_name, rating=0):
    return {"uuid": uuid, "file_name": file_name, "copy_name": "",
            "is_virtual_copy": False, "width": 6000, "height": 4000,
            "rating": rating}


def page_for(params, photos=None):
    """The page the plugin would return for these params (echoing them)."""
    limit = params.get("limit", 100)
    offset = params.get("offset", 0)
    photos = [photo("u-page", f"page_{offset}.NEF",
                    rating=params.get("rating") or 0)] if photos is None else photos
    return {"count": len(photos), "total": offset + len(photos),
            "limit": limit, "offset": offset,
            "has_more": offset + len(photos) < offset + len(photos),
            "photos": photos}


class FakeWorkbench:
    """Workbench stand-in wired to the real CatalogTools validation."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.catalog_tools = CatalogTools(self.run)

    def run(self, command, params=None):
        params = dict(params or {})
        self.calls.append((command, copy.deepcopy(params)))
        if self.error is not None:
            raise self.error
        return page_for(params)


class FakeBridge:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = dict(responses or {})
        self.target_id = None

    def run(self, command, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((command, copy.deepcopy(params)))
        if command == "probe_client":
            return copy.deepcopy(PROBE)
        assert params.get("expected_catalog_identity") == CATALOG_IDENTITY
        assert params.get("expected_access_mode") == "full"
        value = self.responses.get(command)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return copy.deepcopy(value(params))
        if value is None:
            raise AssertionError(f"unexpected command {command}")
        return copy.deepcopy(value)


def call_tool(server, name, arguments, msg_id=1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})


def tool_payload(response):
    result = response["result"]
    if result["isError"]:
        raise AssertionError(result["content"][0]["text"])
    return json.loads(result["content"][0]["text"])


def error_text(response):
    return response["result"]["content"][0]["text"]


class CatalogProtocolTests(unittest.TestCase):
    def test_list_photos_schema_is_extended_without_a_synonym_tool(self):
        server = McpServer(FakeWorkbench())
        tools = {tool["name"]: tool for tool in server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {}})["result"]["tools"]}
        schema = tools["trlr_list_photos"]["inputSchema"]
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["limit"]["minimum"], 1)
        self.assertEqual(schema["properties"]["limit"]["maximum"], 2000)
        self.assertEqual(schema["properties"]["limit"]["default"], 100)
        self.assertEqual(schema["properties"]["offset"]["minimum"], 0)
        self.assertEqual(schema["properties"]["offset"]["default"], 0)
        self.assertEqual(schema["properties"]["name_contains"]["maxLength"], 256)
        self.assertEqual(schema["properties"]["rating"]["minimum"], 0)
        self.assertEqual(schema["properties"]["rating"]["maximum"], 5)
        self.assertIn("unrated", schema["properties"]["rating"]["description"])
        # The listing tool keeps its name; no second, synonymous photo tool.
        photo_tools = sorted(name for name in tools if "photo" in name)
        self.assertEqual(photo_tools, ["trlr_get_photo_marks", "trlr_get_photo_preview",
                                       "trlr_get_photo_settings",
                                       "trlr_import_photos",
                                       "trlr_list_photos", "trlr_set_photo_marks",
                                       "trlr_studio_photos", "trlr_undo_photo_marks",
                                       "trlr_upload_photo"])

    def test_arguments_reach_catalog_tools_and_page_fields_survive(self):
        wb = FakeWorkbench()
        server = McpServer(wb)
        payload = tool_payload(call_tool(server, "trlr_list_photos", {
            "limit": 5, "offset": 2, "name_contains": "late", "rating": 0}))
        self.assertEqual(wb.calls, [("search_photos", {
            "limit": 5, "offset": 2, "name_contains": "late", "rating": 0})])
        self.assertEqual(set(payload), {"count", "total", "limit", "offset",
                                        "has_more", "photos"})
        self.assertEqual(payload["count"], len(payload["photos"]))
        self.assertEqual(payload["limit"], 5)
        self.assertEqual(payload["offset"], 2)
        self.assertIn("total", payload)
        self.assertIn("has_more", payload)

        server = McpServer(wb)
        payload = tool_payload(call_tool(server, "trlr_list_photos", {}))
        self.assertEqual(wb.calls[-1], ("search_photos", {
            "limit": 100, "offset": 0, "name_contains": ""}))
        self.assertEqual(set(payload["photos"][0]), {
            "uuid", "file_name", "copy_name", "is_virtual_copy",
            "width", "height", "rating"})

        tool_payload(call_tool(server, "trlr_list_photos", {"name_contains": None}))
        self.assertEqual(wb.calls[-1][1]["name_contains"], "")

    def test_invalid_arguments_are_errors_and_do_not_reach_runner(self):
        wb = FakeWorkbench()
        server = McpServer(wb)
        bad_calls = [
            {"limit": True},
            {"limit": 0},
            {"limit": 2001},
            {"limit": 1.5},
            {"offset": -1},
            {"offset": True},
            {"name_contains": 5},
            {"name_contains": "x" * 257},
            {"rating": -1},
            {"rating": 6},
            {"rating": True},
            {"rating": 1.5},
            {"unexpected": True},
        ]
        for arguments in bad_calls:
            response = call_tool(server, "trlr_list_photos", arguments)
            self.assertTrue(response["result"]["isError"], arguments)
            self.assertTrue(error_text(response).startswith("error:"), arguments)
        self.assertEqual(wb.calls, [])

    def test_runner_errors_are_reported_once(self):
        wb = FakeWorkbench(error=LrOpsError("bridge down"))
        server = McpServer(wb)
        response = call_tool(server, "trlr_list_photos", {})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("bridge down", error_text(response))
        self.assertEqual(wb.calls, [("search_photos", {
            "limit": 100, "offset": 0, "name_contains": ""})])


class CatalogRuntimeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_lightroom_page_is_not_refiltered_by_the_mcp_surface(self):
        # One small page with a name that misses the needle: the plugin is the
        # filter authority and the MCP payload must match Lightroom's page.
        late = photo("u9", "zzz_last.NEF", rating=5)
        bridge = FakeBridge({"search_photos": lambda params: page_for(params, [late])})
        wb = Workbench(RuntimeState(Path(self.tmp.name)), bridge)
        server = McpServer(wb)
        payload = tool_payload(call_tool(server, "trlr_list_photos",
                                         {"limit": 1, "name_contains": "aaa"}))
        self.assertEqual(payload["photos"], [late])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(bridge.calls[-1][1]["limit"], 1)
        self.assertEqual(bridge.calls[-1][1]["name_contains"], "aaa")


if __name__ == "__main__":
    unittest.main()
