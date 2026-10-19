import copy
import io
import json
import unittest

from workbench.lrops import LrOpsError
from workbench.presets import PresetTools
from workbench_mcp.server import McpServer

LIST_RESULT = {
    "presets": [{"name": "Alpha", "folder": "F", "scope": "lightroom", "uuid": "u1"}],
    "count": 1, "offset": 0, "limit": 100,
}
class FakeWorkbench:
    """Workbench stand-in wired to the real PresetTools validation."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.preset_tools = PresetTools(self.run)

    def run(self, command, params=None):
        self.calls.append((command, copy.deepcopy(params)))
        if self.error is not None:
            raise self.error
        return copy.deepcopy(LIST_RESULT)


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


class PresetProtocolTests(unittest.TestCase):
    def test_preset_tools_declare_strict_read_only_schemas(self):
        server = McpServer(FakeWorkbench())
        tools = {tool["name"]: tool for tool in server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})["result"]["tools"]}
        for name in ("trlr_list_develop_presets", "trlr_get_develop_preset",
                     "trlr_compare_develop_presets"):
            self.assertIn(name, tools)
            self.assertIn("Read-only", tools[name]["description"])
            self.assertIs(tools[name]["inputSchema"]["additionalProperties"], False)
        listing = tools["trlr_list_develop_presets"]["inputSchema"]["properties"]
        self.assertEqual(listing["limit"]["maximum"], 100)
        self.assertEqual(listing["limit"]["minimum"], 1)
        self.assertEqual(listing["offset"]["minimum"], 0)
        get_schema = tools["trlr_get_develop_preset"]["inputSchema"]
        self.assertIn("anyOf", get_schema)
        self.assertEqual(get_schema["properties"]["preset_scope"]["enum"], ["lightroom", "plugin"])
        compare_schema = tools["trlr_compare_develop_presets"]["inputSchema"]
        self.assertEqual(compare_schema["required"], ["base", "candidate"])
        self.assertIn("anyOf", compare_schema["properties"]["base"])

    def test_list_arguments_reach_preset_tools(self):
        wb = FakeWorkbench()
        server = McpServer(wb)
        payload = tool_payload(call_tool(server, "trlr_list_develop_presets",
                                         {"query": "alpha", "limit": 5, "offset": 2}))
        self.assertEqual(payload["presets"], LIST_RESULT["presets"])
        payload = tool_payload(call_tool(server, "trlr_list_develop_presets", {}))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(wb.calls, [
            ("list_develop_presets", {"query": "alpha", "limit": 5, "offset": 2}),
            ("list_develop_presets", {"query": "", "limit": 100, "offset": 0}),
        ])

    def test_invalid_arguments_are_errors_and_do_not_reach_runner(self):
        wb = FakeWorkbench()
        server = McpServer(wb)
        bad_calls = [
            ("trlr_list_develop_presets", {"limit": True}),
            ("trlr_list_develop_presets", {"limit": 0}),
            ("trlr_list_develop_presets", {"limit": 101}),
            ("trlr_list_develop_presets", {"limit": 1.5}),
            ("trlr_list_develop_presets", {"offset": -1}),
            ("trlr_list_develop_presets", {"unexpected": True}),
            ("trlr_get_develop_preset", {}),
            ("trlr_get_develop_preset", {"preset_name": "x", "extra": True}),
            ("trlr_get_develop_preset", {"preset_uuid": 3}),
            ("trlr_get_develop_preset", {"preset_name": "x", "preset_scope": "other"}),
            ("trlr_compare_develop_presets", {"base": {"preset_name": "x"}}),
            ("trlr_compare_develop_presets", {"base": {"preset_name": "x"},
                                              "candidate": {"preset_uuid": "y"}, "extra": 1}),
        ]
        for name, arguments in bad_calls:
            response = call_tool(server, name, arguments)
            self.assertTrue(response["result"]["isError"], (name, arguments))
            self.assertTrue(error_text(response).startswith("error:"), (name, arguments))
        self.assertEqual(wb.calls, [])

    def test_runner_errors_are_reported_once(self):
        wb = FakeWorkbench(error=LrOpsError("bridge down"))
        server = McpServer(wb)
        response = call_tool(server, "trlr_list_develop_presets", {})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("bridge down", error_text(response))
        self.assertEqual(wb.calls, [("list_develop_presets", {"query": "", "limit": 100, "offset": 0})])


class PresetStdioProtocolTests(unittest.TestCase):
    def test_stdio_serve_streams_jsonrpc_frames(self):
        wb = FakeWorkbench()
        server = McpServer(wb)
        stdin = io.StringIO("\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "trlr_list_develop_presets",
                                   "arguments": {"query": "alpha"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/nope"}),
        ]) + "\n")
        stdout = io.StringIO()
        server.serve(stdin, stdout)
        frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([frame["id"] for frame in frames], [1, 2, 3])
        self.assertEqual(frames[0]["result"]["serverInfo"]["name"], "tonerelay-lightroom-mcp")
        self.assertEqual(tool_payload(frames[1])["presets"], LIST_RESULT["presets"])
        self.assertEqual(frames[2]["error"]["code"], -32601)
        self.assertEqual(wb.calls, [("list_develop_presets",
                                     {"query": "alpha", "limit": 100, "offset": 0})])


if __name__ == "__main__":
    unittest.main()
