import json
import tempfile
import unittest
from pathlib import Path

from workbench.runtime import Workbench
from workbench.store import Store
from workbench_mcp.server import McpServer, TOOLS


IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"


def marks(rating=0):
    return {"photo_uuid": "photo", "rating": rating, "flag": "unflagged",
            "color_label": "none", "color_label_status": "empty", "label_text": "",
            "marks_token": f"marks:{rating}", "has_any_marks": rating > 0}


class Bridge:
    base = "http://localhost:17777"
    access_mode = "full"
    catalog_identity = IDENTITY
    target_id = "local"

    def __init__(self):
        self.calls = []
        self.capability = True

    def run(self, command, params=None, on_submitted=None):
        self.calls.append((command, params))
        if command == "probe_client":
            return {"access_mode": "full", "catalog_identity": IDENTITY, "catalog_authorized": True,
                    "capabilities": {"expected_catalog_identity": True, "expected_access_mode": True,
                                     "develop_snapshot": True, "photo_marks_v1": self.capability}}
        assert params["expected_catalog_identity"] == IDENTITY
        assert params["expected_access_mode"] == "full"
        if command == "get_photo_marks":
            return {"results": [{"photo_uuid": "photo", "status": "read", "marks": marks()}]}
        if command == "set_photo_marks":
            assert params["protection_mode"] == "skip_marked_photos"
            on_submitted("abc-123")
            return {"results": [{"photo_uuid": "photo", "status": "applied", "before": marks(), "after": marks(4),
                                 "changed_fields": ["rating"], "skipped_fields": [], "readback_mismatches": []}]}
        raise AssertionError(command)


class PhotoMarksProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = Bridge()
        self.server = McpServer(Workbench(Store(Path(self.temp.name)), self.bridge))

    def call(self, tool, arguments):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": tool, "arguments": arguments}})
        return response["result"]

    def test_schemas_publish_two_protected_modes_without_overwrite(self):
        tools = {tool["name"]: tool for tool in TOOLS}
        for name in ("get_photo_marks", "set_photo_marks", "get_mark_operation", "undo_photo_marks"):
            self.assertIn("trlr_" + name, tools)
        schema = tools["trlr_set_photo_marks"]["inputSchema"]
        self.assertEqual(["skip_marked_photos", "fill_empty_fields"], schema["properties"]["protection_mode"]["enum"])
        self.assertEqual("skip_marked_photos", schema["properties"]["protection_mode"]["default"])

    def test_real_runtime_read_then_write_pins_catalog_and_deduplicates(self):
        read = self.call("trlr_get_photo_marks", {"photo_uuids": ["photo"]})
        self.assertFalse(read.get("isError"))
        token = json.loads(read["content"][0]["text"])["results"][0]["marks"]["marks_token"]
        args = {"items": [{"photo_uuid": "photo", "expected_marks_token": token, "patch": {"rating": 4}}], "request_id": "test"}
        first = self.call("trlr_set_photo_marks", args)
        self.assertFalse(first.get("isError"))
        payload = json.loads(first["content"][0]["text"])
        self.assertEqual("completed", payload["status"])
        self.assertEqual({"total": 1, "applied": 1}, payload["summary"])
        self.assertNotIn("results", payload)
        self.assertEqual(first, self.call("trlr_set_photo_marks", args))
        self.assertEqual(1, sum(command == "set_photo_marks" for command, _ in self.bridge.calls))
        queried = self.call("trlr_get_mark_operation", {"operation_id": payload["operation_id"]})
        self.assertEqual(first, queried)
        detail = self.call("trlr_get_mark_operation", {"operation_id": payload["operation_id"], "details": True})
        evidence = json.loads(detail["content"][0]["text"])
        self.assertEqual(0, evidence["results"][0]["before"]["rating"])
        self.assertEqual(4, evidence["results"][0]["after"]["rating"])
        self.assertEqual(1, sum(command == "set_photo_marks" for command, _ in self.bridge.calls))

    def test_invalid_details_is_rejected_and_read_does_not_repeat_identity(self):
        invalid = self.call("trlr_get_mark_operation", {"operation_id": "00000000-0000-0000-0000-000000000000", "details": "yes"})
        self.assertTrue(invalid["isError"])
        self.assertEqual([], self.bridge.calls)
        read = self.call("trlr_get_photo_marks", {"photo_uuids": ["photo"]})
        payload = json.loads(read["content"][0]["text"])
        self.assertNotIn("photo_uuid", payload["results"][0]["marks"])
        self.assertEqual("marks:0", payload["results"][0]["marks"]["marks_token"])

    def test_invalid_write_and_old_plugin_fail_before_any_mutation(self):
        response = self.call("trlr_set_photo_marks", {"items": [{"photo_uuid": "photo", "expected_marks_token": "x", "patch": {"rating": 6}}], "request_id": "test"})
        self.assertTrue(response["isError"])
        self.assertEqual([], self.bridge.calls)
        self.bridge.capability = False
        response = self.call("trlr_get_photo_marks", {"photo_uuids": ["photo"]})
        self.assertTrue(response["isError"])
        self.assertNotIn("set_photo_marks", [command for command, _ in self.bridge.calls])
