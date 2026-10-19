"""Protocol tests for the issue log: IssueStore semantics, tool registration,
and automatic tool_error recording in McpServer.call_tool."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.issues import IssueStore  # noqa: E402
from workbench_mcp.server import McpServer, TOOLS, _summarize_args  # noqa: E402


class IssueStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = IssueStore(Path(self.tmp.name) / "issues.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_store_lists_nothing(self):
        self.assertEqual(self.store.list(), [])

    def test_record_and_list_newest_first(self):
        self.store.record("manual", "first")
        second = self.store.record("tool_error", "second", tool="trlr_apply_settings")
        entries = self.store.list()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["id"], second["id"])
        self.assertEqual(entries[0]["tool"], "trlr_apply_settings")

    def test_filters_by_kind_and_session(self):
        self.store.record("manual", "a", session_id="s1")
        self.store.record("tool_error", "b", session_id="s2")
        self.assertEqual(len(self.store.list(kind="manual")), 1)
        self.assertEqual(len(self.store.list(session_id="s2")), 1)
        self.assertEqual(len(self.store.list(kind="manual", session_id="s2")), 0)

    def test_limit_is_respected(self):
        for i in range(10):
            self.store.record("manual", f"n{i}")
        self.assertEqual(len(self.store.list(limit=3)), 3)

    def test_corrupt_line_is_skipped(self):
        self.store.record("manual", "good")
        with self.store.path.open("a", encoding="utf-8") as fh:
            fh.write("not-json\n")
        entries = self.store.list()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["summary"], "good")


class SummarizeArgsTests(unittest.TestCase):
    def test_primitives_pass_through_and_strings_truncate(self):
        out = _summarize_args({"a": 1, "b": True, "c": "x" * 200, "d": None})
        self.assertEqual(out["a"], 1)
        self.assertEqual(out["b"], True)
        self.assertEqual(len(out["c"]), 121)
        self.assertIsNone(out["d"])

    def test_containers_collapse(self):
        out = _summarize_args({"settings": {"Vibrance": 5}, "pts": [1, 2, 3]})
        self.assertTrue(out["settings"].startswith("<dict"))
        self.assertEqual(out["pts"], "<list len=3>")


class _FailingWorkbench:
    class _Issues:
        def __init__(self):
            self.recorded = []

        def record(self, kind, summary, **kw):
            self.recorded.append({"kind": kind, "summary": summary, **kw})

    def __init__(self):
        self.issues = self._Issues()


class ToolErrorRecordingTests(unittest.TestCase):
    def setUp(self):
        self.wb = _FailingWorkbench()
        self.server = McpServer.__new__(McpServer)
        self.server.wb = self.wb

    def test_failing_tool_records_issue_and_reraises(self):
        def boom(**kwargs):
            raise RuntimeError("kaboom")

        self.server._tool_trlr_test_boom = boom
        with self.assertRaises(RuntimeError):
            self.server.call_tool("trlr_test_boom", {"x": "y"})
        self.assertEqual(len(self.wb.issues.recorded), 1)
        entry = self.wb.issues.recorded[0]
        self.assertEqual(entry["kind"], "tool_error")
        self.assertIn("kaboom", entry["summary"])
        self.assertEqual(entry["tool"], "trlr_test_boom")
        self.assertEqual(entry["detail"]["error_type"], "RuntimeError")

    def test_successful_tool_records_nothing(self):
        self.server._tool_trlr_test_ok = lambda **kw: {"ok": True}
        result = self.server.call_tool("trlr_test_ok", {})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.wb.issues.recorded, [])


class ToolRegistrationTests(unittest.TestCase):
    def test_issue_tools_are_registered(self):
        names = {t["name"] for t in TOOLS}
        self.assertIn("trlr_list_issues", names)
        self.assertIn("trlr_report_issue", names)


if __name__ == "__main__":
    unittest.main()
