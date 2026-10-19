"""Real stdio startup with the bundled service; no Lightroom/Catalog writes."""
import concurrent.futures
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from lr_ops.configuration import ConfigStore
from lr_ops import __version__
from workbench_mcp import __version__ as mcp_version
from workbench import __version__ as workbench_version

REPO = Path(__file__).resolve().parents[3]


class BundledBridgeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = ConfigStore(self.root / "config.json")
        self.config.initialize()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.config.update("bridge", lambda b: b.update(port=self.port))
        self.config.update("bridge", lambda b: b.update(mode="builtin"))
        self.config.update("targets", lambda t: t["targets"][0].update(base=self.base))
        self.env = dict(os.environ)
        self.env["TONERELAY_CONFIG"] = str(self.config.path)
        self.env.pop("TONERELAY_ACTIVE", None)
        self.env["PYTHONPATH"] = os.pathsep.join(str(REPO / p) for p in (
            "apps/workbench-mcp", "apps/workbench", "services/lr-ops"))

    def stop_test_bridge(self):
        # Exact subprocess created by this isolated test, never a discovered PID.
        record = self.root / "data/lr-ops/builtin-process.json"
        if record.exists():
            pid = json.loads(record.read_text())["pid"]
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                import signal
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        # Give child handles time to close before Windows TemporaryDirectory.
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    time.sleep(0.05)
            except OSError:
                break

    def tearDown(self):
        self.stop_test_bridge()
        self.tmp.cleanup()

    def start_mcp(self, wait_for_bridge=True):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        if wait_for_bridge:
            # initialize may finish while optional builtin warm-up is still
            # running. An actual request must pass the synchronous preflight.
            requests.append({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "trlr_health", "arguments": {}}})
        payload = "".join(json.dumps(request) + "\n" for request in requests)
        proc = subprocess.run([sys.executable, "-m", "workbench_mcp"],
                              input=payload, capture_output=True, text=True, encoding="utf-8",
                              env=self.env, cwd=REPO, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], __version__)
        if wait_for_bridge:
            health = json.loads(next(reply for reply in replies if reply.get("id") == 2)["result"]["content"][0]["text"])
            if health["lrops"]:
                # This test has no Lightroom plugin. health's timed-out probe
                # remains queued by design; finish just those test probes so
                # later job-order assertions concern the job under test.
                while probe := self.request("GET", "/jobs/next"):
                    self.assertEqual(probe["command"], "probe_client")
                    self.request("POST", f"/jobs/{probe['job_id']}/result", {
                        "ok": False, "error": "isolated protocol test has no Lightroom"})
        return proc

    def request(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body else None)
        with urllib.request.urlopen(req, timeout=3) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def test_builtin_starts_without_token_and_survives_client_refresh(self):
        self.start_mcp()
        health = self.request("GET", "/health")
        self.assertTrue(health["ok"])
        self.assertFalse(health["credential_configured"])
        record = self.root / "data/lr-ops/builtin-process.json"
        pid = json.loads(record.read_text())["pid"]
        # Existing Lua endpoints remain byte/protocol compatible.
        job_id = self.request("POST", "/jobs", {"command": "get_selection", "params": {}})["job_id"]
        dispatched = self.request("GET", "/jobs/next")
        self.assertEqual(dispatched["job_id"], job_id)
        self.start_mcp()
        self.assertEqual(json.loads(record.read_text())["pid"], pid)
        self.assertEqual(self.request("GET", f"/jobs/{job_id}")["status"], "dispatched")
        self.request("POST", f"/jobs/{job_id}/result", {"ok": True, "data": {"selection": []}})
        self.assertEqual(self.request("GET", f"/jobs/{job_id}")["status"], "done")

    def test_simultaneous_clients_share_one_bridge(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.start_mcp(), range(2)))
        self.assertTrue(all(not p.stderr for p in results), [p.stderr for p in results])
        self.assertTrue(self.request("GET", "/health")["ok"])

    def test_crashed_service_recovers_without_replaying_dispatched_work(self):
        self.start_mcp()
        job_id = self.request("POST", "/jobs", {"command": "apply_candidate", "params": {}})["job_id"]
        self.assertEqual(self.request("GET", "/jobs/next")["job_id"], job_id)
        self.stop_test_bridge()
        self.start_mcp()
        self.assertEqual(self.request("GET", f"/jobs/{job_id}")["status"], "interrupted")
        self.assertIsNone(self.request("GET", "/jobs/next"))

    def test_refresh_preserves_shared_bridge_and_dispatched_job(self):
        self.start_mcp()
        record = self.root / "data/lr-ops/builtin-process.json"
        pid = json.loads(record.read_text())["pid"]
        job_id = self.request("POST", "/jobs", {"command": "apply_candidate", "params": {}})["job_id"]
        self.request("GET", "/jobs/next")
        requests = [
            {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
            {"jsonrpc":"2.0","method":"notifications/initialized"},
            {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"trlr_refresh_mcp","arguments":{}}},
            {"jsonrpc":"2.0","id":3,"method":"ping"},
        ]
        proc = subprocess.run([sys.executable,"-m","workbench_mcp"],
            input=''.join(json.dumps(r)+'\n' for r in requests),capture_output=True,
            text=True,encoding='utf-8',env=self.env,cwd=REPO,timeout=30)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        refreshed = json.loads(next(r for r in replies if r.get('id')==2)['result']['content'][0]['text'])
        self.assertEqual(refreshed['status'],'refreshed',refreshed)
        self.assertEqual(json.loads(record.read_text())["pid"],pid)
        self.assertEqual(self.request("GET",f"/jobs/{job_id}")["status"],"dispatched")
        self.request("POST",f"/jobs/{job_id}/result",{"ok":True,"data":{}})

    def test_default_external_health_is_exposed_even_without_lightroom(self):
        # The default is client-managed: no ownership negotiation, no spawn.
        def drop_mode(bridge):
            bridge.pop("mode", None)
        self.config.update("bridge", drop_mode)
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "trlr_health", "arguments": {}}}) + "\n"
        proc = subprocess.run([sys.executable, "-m", "workbench_mcp"], input=payload,
                              capture_output=True, text=True, encoding="utf-8", env=self.env,
                              cwd=REPO, timeout=20)
        response = json.loads(proc.stdout)
        health = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(health["lrops"])
        self.assertFalse(health["ok"])
        self.assertEqual(health["bridge"]["mode"], "external")
        self.assertEqual(health["bridge"]["status"], "client_managed")
        self.assertIsNone(health["catalog_policy"])
        self.assertFalse((self.root / "data/lr-ops/builtin-process.json").exists())

    def test_second_service_on_same_data_root_is_refused_before_recovery(self):
        self.start_mcp()
        process = subprocess.run([sys.executable, "-m", "lr_ops", "serve", "--port", "0"],
                                 capture_output=True, text=True, encoding="utf-8", env=self.env,
                                 cwd=REPO, timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("bridge.busy", process.stderr)
        self.assertTrue(self.request("GET", "/health")["ok"])

    def test_external_mode_does_not_start_service(self):
        self.config.update("bridge", lambda b: b.update(mode="external"))
        self.start_mcp(wait_for_bridge=False)
        self.assertFalse((self.root / "data/lr-ops/builtin-process.json").exists())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=0.1)

    def test_occupied_port_is_reported_and_discovery_survives(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port))
            sock.listen()
            proc = self.start_mcp()
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        health = json.loads(next(reply for reply in replies if reply.get("id") == 2)["result"]["content"][0]["text"])
        self.assertIn("bridge.port_occupied", health["bridge"]["reason"])
        self.assertFalse(proc.stderr)
        self.assertFalse((self.root / "data/lr-ops/builtin-process.json").exists())


class VersionParityTests(unittest.TestCase):
    def test_plugin_mcp_and_runtime_share_one_version(self):
        plugin = json.loads((REPO / "plugins/tonerelay-lightroom/.codex-plugin/plugin.json").read_text(encoding="utf-8"))
        lua = (REPO / "plugins/lightroom/lr-xmp-mimic.lrdevplugin/Info.lua").read_text(encoding="utf-8")
        lua_version = ".".join(re.search(rf"\b{name}\s*=\s*(\d+)", lua).group(1)
                               for name in ("major", "minor", "revision"))
        self.assertEqual({__version__, mcp_version, workbench_version, plugin["version"], lua_version}, {__version__})
        commands = (REPO / "plugins/lightroom/lr-xmp-mimic.lrdevplugin/Commands.lua").read_text(encoding="utf-8")
        self.assertIn("(require 'Info').VERSION", commands)
        self.assertNotRegex(commands, r"plugin_version\s*=\s*['\"]")
