"""Regression tests for malformed MCP input and bounded Runtime validation."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

WB = Path(__file__).resolve().parents[2] / "workbench"
sys.path.insert(0, str(WB))
sys.path.insert(0, str(WB / "tests"))

from test_studio import FakeLR  # noqa: E402
from workbench.studio.engine import Studio  # noqa: E402
from workbench_mcp.server import McpServer  # noqa: E402


class McpInputValidationTests(unittest.TestCase):
    def test_non_object_json_request_returns_invalid_request(self):
        server = McpServer(object())
        response = server.handle([])
        self.assertEqual(response["error"]["code"], -32600)

    def test_malformed_json_line_returns_parse_error_and_server_continues(self):
        server = McpServer(object())
        stdin = io.StringIO("{not json}\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        stdout = io.StringIO()
        server.serve(stdin, stdout)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"], {})

    def test_non_object_tool_arguments_are_tool_errors(self):
        server = McpServer(object())
        response = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "trlr_health", "arguments": []},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("object", response["result"]["content"][0]["text"])


class EpisodeInputValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lr = FakeLR()
        self.studio = Studio(Path(self.tmp.name), self.lr)
        self.state = self.studio.create(
            "source", mode="source_edit", long_edge=256)

    def _variants(self, parameter, values):
        return self.studio.start_parameter_variants(
            self.state["id"], self.state["revision"], str(uuid.uuid4()),
            parameter=parameter, values=values,
        )

    def test_empty_parameter_variant_values_are_rejected_without_writes(self):
        calls = len(self.lr.calls)
        with self.assertRaises(ValueError):
            self._variants("Exposure2012", [])
        current = self.studio.get(self.state["id"])
        self.assertEqual(current["status"], "ready")
        self.assertEqual(current["revision"], self.state["revision"])
        self.assertEqual(len(self.lr.calls), calls)

    def test_scalar_parameter_rejects_array_variant_value(self):
        with self.assertRaises(ValueError):
            self._variants("Exposure2012", [[0.4]])

    def test_variants_reject_control_missing_from_source(self):
        with self.assertRaises(ValueError):
            self._variants("Temperature", [5600])
        self.assertEqual(self.studio.get(self.state["id"])["status"], "ready")

    def test_cumulative_mixed_white_balance_patch_is_rejected_before_copy(self):
        # This synthetic source advertises both LR WB families so the failure
        # exercises cumulative patch validation rather than source capability.
        self.lr.settings["source"].update(Temperature=5600, Tint=0)
        self.lr.settings["reference"].update(Temperature=5600, Tint=0)
        self.studio = Studio(Path(self.tmp.name), self.lr)
        self.state = self.studio.create("source", "reference", True, long_edge=256)
        request = str(uuid.uuid4())
        self.state = self.studio.action(
            self.state["id"], "edit", self.state["revision"], request,
            settings={"Temperature": 5700},
        )
        copies = self.lr.copies
        revision = self.state["revision"]
        with self.assertRaises(ValueError):
            self.studio.action(
                self.state["id"], "edit", revision, str(uuid.uuid4()),
                settings={"IncrementalTemperature": 10},
            )
        current = self.studio.get(self.state["id"])
        self.assertEqual(current["status"], "ready")
        self.assertEqual(current["revision"], revision)
        self.assertEqual(self.lr.copies, copies)


if __name__ == "__main__":
    unittest.main()
