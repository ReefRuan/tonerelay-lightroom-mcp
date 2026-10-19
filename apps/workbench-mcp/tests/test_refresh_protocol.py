"""Real stable stdio refresh: fresh code, rollback, UTF-8 and discovery."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from workbench_mcp.server import McpServer
from workbench_mcp import __version__

REPO = Path(__file__).resolve().parents[3]


class RefreshProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        for relative in ("apps/workbench-mcp", "apps/workbench", "services/lr-ops", "prompts"):
            shutil.copytree(REPO / relative, self.source / relative,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = dict(os.environ)
        env.pop("TONERELAY_CONFIG", None)
        env.pop("TONERELAY_ACTIVE", None)
        env["TONERELAY_BRIDGE_MODE"] = "external"
        env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"] = str(self.root / "data")
        env["PYTHONPATH"] = os.pathsep.join(str(self.source / p) for p in (
            "apps/workbench-mcp", "apps/workbench", "services/lr-ops"))
        self.process = subprocess.Popen([sys.executable, "-m", "workbench_mcp"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", env=env, cwd=self.source)
        self.messages = queue.Queue()
        def read():
            try:
                for line in self.process.stdout:
                    self.messages.put(json.loads(line))
            finally:
                self.messages.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.counter = 0
        self.initial = self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                              "clientInfo": {"name": "refresh-test", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tearDown(self):
        self.process.stdin.close()
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()  # exact isolated test-owned process
            self.process.wait(5)
        self.reader.join(2)
        self.process.stdout.close()
        self.process.stderr.close()
        self.tmp.cleanup()

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def rpc(self, method, params=None):
        self.counter += 1
        self.send({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params or {}})
        result = self.messages.get(timeout=30)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("id"), self.counter, result)
        self.assertNotIn("error", result)
        return result["result"]

    def tool(self, action="refresh", extra=None):
        result = self.rpc("tools/call", {"name": "trlr_refresh_mcp",
                         "arguments": {"action": action, **(extra or {})}})
        return result, json.loads(result["content"][0]["text"])

    def test_refresh_keeps_stdio_and_announces_rediscovery(self):
        pid = self.process.pid
        self.assertTrue(self.initial["capabilities"]["tools"]["listChanged"])
        result, state = self.tool()
        self.assertFalse(result["isError"])
        self.assertEqual(state["status"], "refreshed")
        self.assertEqual(state["server_info"]["version"], __version__)
        self.assertEqual(state["generation"], 2)
        self.assertTrue(state["stdio_connection_preserved"])
        self.assertFalse(state["bridge_stopped"])
        self.assertFalse(state["pending_jobs_replayed"])
        notification = self.messages.get(timeout=5)
        self.assertEqual(notification["method"], "notifications/tools/list_changed")
        self.assertEqual(self.process.pid, pid)
        self.assertEqual(self.rpc("ping"), {})

    def test_changed_code_prompt_and_tool_schema_are_loaded(self):
        _, before = self.tool("inspect")
        version = self.source / "services/lr-ops/lr_ops/__init__.py"
        text = version.read_bytes()
        original_stat = version.stat()
        version.write_bytes(text.replace(__version__.encode(), b"9.9.9"))
        # Reproduce the old cache collision: preserve timestamp and file size.
        self.assertEqual(version.stat().st_size, original_stat.st_size)
        os.utime(version, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        prompt = self.source / "prompts/runtime-operator/core.md"
        with prompt.open("a", encoding="utf-8") as handle:
            handle.write("\n测试刷新规则：保留背景。\n")
        server = self.source / "apps/workbench-mcp/workbench_mcp/server.py"
        with server.open("a", encoding="utf-8") as handle:
            handle.write('\nTOOLS.append({"name":"trlr_refresh_fixture_marker","description":"fixture","inputSchema":{"type":"object"}})\n')
        _, stale = self.tool("inspect")
        self.assertTrue(stale["refresh_available"])
        self.assertEqual(stale["loaded_source_sha256"], before["loaded_source_sha256"])
        result, after = self.tool()
        self.assertFalse(result["isError"], after)
        self.messages.get(timeout=5)  # list_changed
        self.assertEqual(after["server_info"]["version"], "9.9.9")
        self.assertIn("测试刷新规则", after["runtime_operator_instructions"])
        self.assertNotEqual(after["loaded_source_sha256"], before["loaded_source_sha256"])
        names = {t["name"] for t in self.rpc("tools/list")["tools"]}
        self.assertIn("trlr_refresh_fixture_marker", names)

    def test_failed_update_keeps_previous_worker_and_connection(self):
        _, before = self.tool("inspect")
        server = self.source / "apps/workbench-mcp/workbench_mcp/server.py"
        with server.open("a", encoding="utf-8") as handle:
            handle.write("\nthis is invalid syntax !\n")
        result, failed = self.tool()
        self.assertTrue(result["isError"])
        self.assertEqual(failed["status"], "refresh_failed")
        self.assertTrue(failed["previous_worker_preserved"])
        self.assertFalse(failed["bridge_stopped"])
        self.assertEqual(self.rpc("ping"), {})
        _, after = self.tool("inspect")
        self.assertEqual(after["loaded_source_sha256"], before["loaded_source_sha256"])
        self.assertEqual(after["generation"], 1)

    def test_bad_arguments_do_not_restart_or_disconnect(self):
        result, state = self.tool("refresh", {"source_path": "arbitrary"})
        self.assertTrue(result["isError"])
        self.assertEqual(state["reason"], "mcp.invalid_refresh_arguments")
        self.assertEqual(self.rpc("ping"), {})
        result = self.rpc("tools/call", {"name": "trlr_refresh_mcp", "arguments": []})
        self.assertTrue(result["isError"])
        self.assertEqual(self.rpc("ping"), {})


class DirectAdapterRefreshTests(unittest.TestCase):
    def test_unsupervised_adapter_does_not_claim_refresh(self):
        result = McpServer(object()).call_tool("trlr_refresh_mcp", {})
        state = json.loads(result["content"][0]["text"])
        self.assertEqual(state["status"], "reconnect_required")
