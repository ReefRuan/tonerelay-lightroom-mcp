"""The stdio transport must speak UTF-8 regardless of the host locale.

A Windows host that spawns the server without ``PYTHONUTF8`` /
``PYTHONIOENCODING`` gives piped stdio the ANSI code page with
``surrogateescape``: non-ASCII arguments arrive as lone-surrogate mojibake
(which crashes JSON persistence with "surrogates not allowed") and non-ASCII
replies leave as GBK bytes that the client reads as mojibake. These tests force
a non-UTF-8 stdio environment and pin the transport-level round trip.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
APPS = REPOSITORY_ROOT / "apps"

SUMMARY = "中文摘要：编码检查，三脚架支线观察"


def run_server(requests, data_root: Path):
    """Run the real stdio server once under a non-UTF-8 stdio environment."""
    payload = "".join(
        json.dumps(request, ensure_ascii=False) + "\n" for request in requests
    ).encode("utf-8")
    env = os.environ.copy()
    env.pop("PYTHONUTF8", None)
    # Reproduce the affected-host condition on every platform; on Windows this
    # is also the codec an app-spawned server gets by default.
    env["PYTHONIOENCODING"] = "gbk:surrogateescape"
    env["PYTHONPATH"] = os.pathsep.join([
        str(APPS / "workbench-mcp"),
        str(APPS / "workbench"),
        str(REPOSITORY_ROOT / "services" / "lr-ops"),
    ])
    env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"] = str(data_root)
    # This fixture tests encoding only; it must not start a service on the
    # machine's Lightroom port. Bundled startup has isolated protocol tests.
    env["TONERELAY_BRIDGE_MODE"] = "external"
    proc = subprocess.run(
        [sys.executable, "-m", "workbench_mcp"],
        input=payload, capture_output=True, timeout=120,
        env=env, cwd=str(REPOSITORY_ROOT),
    )
    responses = [
        json.loads(line)
        for line in proc.stdout.decode("utf-8").splitlines()
        if line.strip()
    ]
    return proc.returncode, responses, proc.stderr


class StdioEncodingTests(unittest.TestCase):
    def test_non_ascii_tool_argument_is_persisted_as_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, responses, stderr = run_server([{
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "trlr_report_issue",
                           "arguments": {"summary": SUMMARY}},
            }], Path(tmp))
            self.assertEqual(code, 0, stderr[-400:])
            self.assertEqual(len(responses), 1)
            result = responses[0]["result"]
            self.assertFalse(result["isError"], result["content"][0]["text"])
            issue_file = Path(tmp) / "workbench" / "issues.jsonl"
            self.assertTrue(issue_file.exists())
            entries = [json.loads(line) for line in
                       issue_file.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
            self.assertIn(SUMMARY, [entry["summary"] for entry in entries])

    def test_initialize_and_tools_list_survive_a_non_utf8_stdio_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, responses, stderr = run_server([
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ], Path(tmp))
        self.assertEqual(code, 0, stderr[-400:])
        self.assertEqual(len(responses), 2)
        instructions = responses[0]["result"]["instructions"]
        self.assertTrue(instructions)
        self.assertNotIn("\ufffd", instructions)
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertIn("trlr_get_photo_preview", names)
        for name in ("trlr_get_photo_marks", "trlr_set_photo_marks",
                     "trlr_get_mark_operation", "trlr_undo_photo_marks"):
            self.assertIn(name, names)
            self.assertIn(name, instructions)
        mark_schema = next(tool["inputSchema"] for tool in responses[1]["result"]["tools"]
                           if tool["name"] == "trlr_set_photo_marks")
        self.assertEqual(["skip_marked_photos", "fill_empty_fields"],
                         mark_schema["properties"]["protection_mode"]["enum"])


if __name__ == "__main__":
    unittest.main()
