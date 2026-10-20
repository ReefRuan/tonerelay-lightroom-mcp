"""Regression tests for capabilities retired from the Runtime core.

Recommendation, recipe selection, style composition, result ranking, prose
summarization and aesthetic judgment belong to a future Agent layer.  The old
Runtime tools must therefore be absent rather than silently aliased.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

WB = Path(__file__).resolve().parents[2] / "workbench"
sys.path.insert(0, str(WB))
sys.path.insert(0, str(WB / "tests"))

from test_studio import FakeLR  # noqa: E402
from workbench.runtime import Workbench  # noqa: E402
from workbench.state import RuntimeState  # noqa: E402
from workbench_mcp.server import McpServer, TOOLS  # noqa: E402


RETIRED_TOOLS = (
    "trlr_style_library",
    "trlr_style_cases",
    "trlr_knowledge",
    "trlr_knowledge_evidence",
    "trlr_explore_episode",
    "trlr_probe_episode",
    "trlr_search_episode",
    "trlr_best_episode",
    "trlr_image_inspect",
    "trlr_image_stats",
    "trlr_reference_match",
)


class RetiredMcpSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = McpServer(Workbench(RuntimeState(Path(self.tmp.name)), FakeLR()))

    def test_retired_tools_are_not_advertised(self):
        names = [tool["name"] for tool in TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for name in RETIRED_TOOLS:
            with self.subTest(name=name):
                self.assertNotIn(name, names)
        for name in ("trlr_parameter_reference",
                     "trlr_render_parameter_variants",
                     "trlr_list_parameter_variant_results",
                     "trlr_image_measure"):
            self.assertIn(name, names)

    def test_retired_tool_calls_fail_as_unknown_instead_of_aliasing(self):
        for name in RETIRED_TOOLS:
            with self.subTest(name=name):
                response = self.server.handle({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": {}},
                })
                result = response["result"]
                self.assertTrue(result["isError"])
                self.assertIn("unknown tool", result["content"][0]["text"])

    def test_public_episode_state_has_no_runtime_selected_best(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "trlr_create_episode", "arguments": {
                "source_uuid": "source", "reference_uuid": "reference",
                "same_capture": True, "long_edge": 256,
            }},
        })
        result = response["result"]
        self.assertFalse(result["isError"], result)
        state = json.loads(result["content"][0]["text"])
        self.assertNotIn("best_version", state)
        self.assertNotIn("search", state)


if __name__ == "__main__":
    unittest.main()
