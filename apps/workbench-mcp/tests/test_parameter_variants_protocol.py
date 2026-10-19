"""Parameter-reference and parameter-variant protocol regression tests.

FakeLR replaces Lightroom I/O only. The tests exercise the real Studio state
machine plus the MCP adapter, while keeping recommendation and aesthetic
judgment outside the Runtime contract.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

WB = Path(__file__).resolve().parents[2] / "workbench"
sys.path.insert(0, str(WB))
sys.path.insert(0, str(WB / "tests"))

from test_studio import FakeLR  # noqa: E402
from workbench.runtime import Workbench  # noqa: E402
from workbench.store import Store  # noqa: E402
from workbench.studio.parameter_specs import parameter_records  # noqa: E402
from workbench_mcp.server import McpServer, TOOLS  # noqa: E402


class ParameterVariantProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wb = Workbench(Store(Path(self.tmp.name)), FakeLR())
        self.server = McpServer(self.wb)

    def result(self, name, **arguments):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "trlr_" + name, "arguments": arguments},
        })
        return response["result"]

    def call(self, name, **arguments):
        result = self.result(name, **arguments)
        self.assertFalse(result["isError"], result)
        return json.loads(result["content"][0]["text"])

    def wait_ready(self, episode_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = self.wb.studio.get(episode_id)
            if state["status"] != "rendering_parameter_variants":
                return state
            time.sleep(0.01)
        self.fail("parameter variants did not finish")

    def source_only_episode(self):
        return self.call("create_episode", source_uuid="source",
                         mode="source_edit", intent="参数变体协议", long_edge=256)

    def test_parameter_reference_is_the_standalone_parameter_documentation(self):
        names = [tool["name"] for tool in TOOLS]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("trlr_parameter_reference", names)

        reference = self.call("parameter_reference")
        expected_ids = {item["id"] for item in parameter_records()}
        self.assertEqual({item["id"] for item in reference["items"]}, expected_ids)
        self.assertEqual(reference["count"], len(expected_ids))
        self.assertTrue({
            "SplitToningShadowHue", "ColorGradeMidtoneHue",
            "PostCropVignetteAmount",
        } <= expected_ids)
        for item in reference["items"]:
            self.assertEqual(item["kind"], "parameter")
            self.assertNotIn("review_status", item)
            self.assertIn(item["support"]["native_xmp_import"],
                          ("unverified", "verified"))

        one = self.call("parameter_reference", identifier="Exposure2012")
        self.assertEqual([item["id"] for item in one["items"]], ["Exposure2012"])
        self.assertEqual(one["items"][0]["unit"], "EV")
        by_query = self.call("parameter_reference", query="Exposure2012")
        self.assertEqual([item["id"] for item in by_query["items"]],
                         ["Exposure2012"])

    def test_parameter_variant_tool_schemas_expose_execution_inputs_only(self):
        tools = {tool["name"]: tool for tool in TOOLS}
        render = tools["trlr_render_parameter_variants"]
        schema = render["inputSchema"]
        self.assertEqual(set(schema["required"]), {
            "episode_id", "base_revision", "request_id", "parameter", "values",
        })
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["values"]["minItems"], 1)
        self.assertEqual(schema["properties"]["values"]["maxItems"], 5)
        self.assertNotIn("technique_id", schema["properties"])

        listing = tools["trlr_list_parameter_variant_results"]["inputSchema"]
        self.assertEqual(set(listing["properties"]), {"query"})
        self.assertFalse(listing["additionalProperties"])
        for description in (render["description"],
                            tools["trlr_list_parameter_variant_results"]["description"]):
            self.assertRegex(description.casefold(), r"(never|does not).*recommend")

    def test_render_variants_keeps_a_fixed_baseline_and_does_not_select(self):
        state = self.source_only_episode()
        baseline = state["versions"][0]["snapshot"]
        accepted = self.call(
            "render_parameter_variants", episode_id=state["id"],
            base_revision=state["revision"], request_id=str(uuid.uuid4()),
            parameter="Exposure2012", values=[-0.25, 0.25])
        self.assertEqual(accepted["status"], "rendering_parameter_variants")
        self.assertNotIn("best_version", accepted)

        state = self.wait_ready(state["id"])
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["parameter_variants"]["values"], [-0.25, 0.25])
        self.assertEqual(state["parameter_variants"]["versions"], [1, 2])
        self.assertEqual(state["current_version"], 0)
        self.assertNotIn("best_version", state)
        self.assertEqual(state["versions"][0]["snapshot"], baseline)
        self.assertEqual(
            [version["parameter_variant"]["requested_value"]
             for version in state["versions"][1:]],
            [-0.25, 0.25])
        for version in state["versions"][1:]:
            record = version["parameter_variant"]
            for retired in ("technique_id", "knowledge", "summary", "response"):
                self.assertNotIn(retired, record)

        listing = self.call("list_parameter_variant_results", query="Exposure2012")
        self.assertEqual(listing["count"], 2)
        self.assertEqual([row["requested_value"] for row in listing["results"]],
                         [-0.25, 0.25])
        for row in listing["results"]:
            self.assertEqual(row["base_version"], 0)
            self.assertEqual(row["parameter"], "Exposure2012")
            self.assertEqual(row["color_space"], "sRGB")
            self.assertIn("candidate_artifact", row)
            for retired in ("technique_id", "knowledge", "summary", "response"):
                self.assertNotIn(retired, row)

    def test_variant_values_must_be_numerically_distinct(self):
        for parameter, values in (
                ("Exposure2012", [1, 1.0]),
                ("ToneCurvePV2012", [[0, 0, 255, 255],
                                     [0.0, 0.0, 255.0, 255.0]])):
            with self.subTest(parameter=parameter):
                state = self.source_only_episode()
                result = self.result(
                    "render_parameter_variants", episode_id=state["id"],
                    base_revision=state["revision"], request_id=str(uuid.uuid4()),
                    parameter=parameter, values=values)
                self.assertTrue(result["isError"])
                self.assertIn("distinct", result["content"][0]["text"])
                current = self.wb.studio.get(state["id"])
                self.assertEqual(current["status"], "ready")
                self.assertEqual(current["revision"], state["revision"])
                self.assertEqual(len(current["versions"]), 1)

    def test_historical_probe_record_is_listed_through_a_neutral_projection(self):
        state = self.source_only_episode()
        self.call(
            "render_parameter_variants", episode_id=state["id"],
            base_revision=state["revision"], request_id=str(uuid.uuid4()),
            parameter="Exposure2012", values=[0.4])
        state = self.wait_ready(state["id"])

        raw = self.wb.studio.raw(state["id"])
        version = raw["versions"][1]
        current = version.pop("parameter_variant")
        requested = current.pop("requested_value")
        current.pop("applied_value")
        version["parameter_probe"] = {
            **current,
            "value": requested,
            "technique_id": "retired-technique",
            "knowledge": ["retired-card"],
            "summary": "retired prose",
            "response": {"mean_abs_rgb": 12.0},
        }
        self.wb.studio.save(raw)

        listing = self.call("list_parameter_variant_results", query="Exposure2012")
        self.assertEqual(listing["count"], 1)
        row = listing["results"][0]
        self.assertEqual(row["requested_value"], 0.4)
        self.assertEqual(row["applied_value"], 0.4)
        for retired in ("technique_id", "knowledge", "summary", "response"):
            self.assertNotIn(retired, row)


if __name__ == "__main__":
    unittest.main()
