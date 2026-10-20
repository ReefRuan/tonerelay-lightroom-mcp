"""Single-step import tool contract and real Runtime success/error paths."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

WB_TESTS = Path(__file__).resolve().parents[2] / "workbench" / "tests"
sys.path.insert(0, str(WB_TESTS))
from test_photo_import import ImportBridge
from PIL import Image

from workbench.runtime import Workbench
from workbench.state import RuntimeState
from workbench_mcp.prompt_loader import load_runtime_operator_prompt
from workbench_mcp.server import McpServer


class ImportProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.bridge = ImportBridge()
        self.server = McpServer(Workbench(RuntimeState(self.root / "runtime"), self.bridge))
        self.photo = self.root / "photo.jpg"
        Image.new("RGB", (8, 6), "red").save(self.photo)

    def rpc(self, method, params=None):
        return self.server.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                                   "params": params or {}})["result"]

    def call(self, **args):
        return self.rpc("tools/call", {"name": "trlr_import_photos", "arguments": args})

    def data(self, **args):
        result = self.call(**args)
        self.assertFalse(result["isError"], result)
        return json.loads(result["content"][0]["text"])

    def test_discovery_and_stdio_initialization_compile_prompt_confirmation(self):
        tools = {tool["name"]: tool for tool in self.rpc("tools/list")["tools"]}
        schema = tools["trlr_import_photos"]["inputSchema"]
        self.assertEqual(set(schema["properties"]), {"paths", "request_id", "recursive", "move", "move_root"})
        self.assertEqual(schema["required"], ["paths", "request_id"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["move"]["default"], False)
        self.assertEqual(schema["properties"]["recursive"]["default"], False)
        self.assertFalse(any("import" in name and ("plan" in name or "execute" in name) for name in tools))
        output = io.StringIO()
        self.server.serve(io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1,
                                                 "method": "initialize", "params": {}}) + "\n"), output)
        instructions = json.loads(output.getvalue())["result"]["instructions"]
        self.assertEqual(instructions, load_runtime_operator_prompt())
        self.assertIn("trlr_import_photos", instructions)
        self.assertIn("单独征得用户对移动的明确同意", instructions)

    def test_single_step_default_import_results_and_idempotency(self):
        unsupported = self.root / "notes.txt"
        unsupported.write_text("skip")
        paths = [str(self.photo), str(unsupported)]
        data = self.data(paths=paths, request_id="batch")
        self.assertEqual([row["status"] for row in data["results"]], ["imported", "skipped"])
        self.assertEqual(data["results"][0]["master_uuid"], "master-1")
        self.assertTrue(self.photo.exists())
        self.assertTrue(self.data(paths=paths, request_id="batch")["replayed"])
        self.assertEqual(self.bridge.imports, 1)
        self.assertTrue(self.call(paths=paths, request_id="batch", recursive=True)["isError"])

    def test_move_and_missing_root_errors(self):
        paths = [str(self.photo)]
        self.assertTrue(self.call(paths=paths, request_id="invalid", move=True)["isError"])
        self.assertTrue(self.photo.exists())
        root = self.root / "organized"
        row = self.data(paths=paths, request_id="move", move=True, move_root=str(root))["results"][0]
        self.assertTrue(row["moved"])
        self.assertEqual(row["path"], str(root / "unknown-date" / self.photo.name))
        self.assertFalse(self.photo.exists())

    def test_malformed_requests_rejected_without_catalog_calls(self):
        for args in ({"paths": []}, {"paths": [str(self.photo)]},
                     {"paths": [str(self.photo)], "request_id": "x", "confirm": True},
                     {"paths": [str(self.photo)], "request_id": "x", "plan_digest": "x"},
                     {"paths": [42], "request_id": "x"},
                     {"paths": [str(self.photo)], "request_id": "x", "recursive": 1}):
            with self.subTest(args=args):
                self.assertTrue(self.call(**args)["isError"])
        self.assertEqual(self.bridge.calls, [])

    def test_catalog_refusal_and_unknown_import_are_reported_without_replay(self):
        self.bridge.authorized = False
        self.assertTrue(self.call(paths=[str(self.photo)], request_id="denied")["isError"])
        self.bridge.authorized = True
        self.bridge.failure = TimeoutError("result unknown")
        paths = [str(self.photo)]
        data = self.data(paths=paths, request_id="uncertain")
        self.assertEqual(data["results"][0]["status"], "unknown")
        self.assertIn("result unknown", data["results"][0]["error"])
        self.data(paths=paths, request_id="uncertain")
        self.assertEqual(self.bridge.imports, 1)


if __name__ == "__main__":
    unittest.main()
