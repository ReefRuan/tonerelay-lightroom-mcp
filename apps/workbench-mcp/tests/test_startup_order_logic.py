"""Startup/recovery logic with fake processes and HTTP, never real applications."""
import concurrent.futures
import io
import itertools
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from lr_ops.bridge_lifecycle import BundledBridge, BridgeStartError, path_fingerprint
from lr_ops.configuration import ConfigStore
from lr_ops.server import JobQueue
from workbench.bundled_bridge import make_router
from workbench.lrops import LrOpsClient, LrOpsError
from workbench.targets import TargetRegistry
from workbench_mcp import server


class StartupOrderLogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.running = False
        self.lightroom_running = False
        self.spawn_count = 0
        self.processes = []
        self.base = "http://127.0.0.1:17777"

        def health(manager, base):
            if not self.running:
                return None
            return {"ok": True, "source_sha256": manager.expected_source,
                    "data_root_sha256": path_fingerprint(self.root),
                    "credential_configured": False}

        def spawn(*args, **kwargs):
            self.spawn_count += 1
            self.running = True
            process = mock.Mock(pid=12345)
            process.poll.return_value = None
            process.wait.return_value = 0
            self.processes.append(process)
            return process

        self.health_patch = mock.patch.object(BundledBridge, "_health", autospec=True, side_effect=health)
        self.health_probe = self.health_patch.start()
        self.addCleanup(self.health_patch.stop)
        self.spawn_patch = mock.patch("lr_ops.bridge_lifecycle.subprocess.Popen", side_effect=spawn)
        self.spawn = self.spawn_patch.start()
        self.addCleanup(self.spawn_patch.stop)
        socket_patch = mock.patch("lr_ops.bridge_lifecycle.socket.socket")
        socket_patch.start()
        self.addCleanup(socket_patch.stop)
        opener_patch = mock.patch("lr_ops.bridge_lifecycle.urllib.request.build_opener")
        opener = opener_patch.start()
        self.addCleanup(opener_patch.stop)
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'

    def manager(self, name, mode):
        config = ConfigStore(self.root / name / "config.json")
        config.initialize()
        config.update("bridge", lambda bridge: bridge.update(mode=mode))
        return BundledBridge(self.root, config)

    def start(self, manager):
        thread = manager.start_background(self.base)
        if thread is not None:
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_all_lightroom_codex_kimi_orders_without_owner_remain_external(self):
        for order in itertools.permutations(("LR", "Codex", "Kimi")):
            with self.subTest(order=order):
                self.running = False
                self.lightroom_running = False
                clients = {}
                for name in order:
                    if name == "LR":
                        self.lightroom_running = True
                    else:
                        clients[name] = self.manager(name, "external")
                        self.start(clients[name])
                for manager in clients.values():
                    self.assertEqual(manager.ensure(self.base)["status"], "client_managed")
                self.assertFalse(self.running)
        self.health_probe.assert_not_called()
        self.spawn.assert_not_called()

    def test_all_orders_with_explicit_owner_start_once_and_reuse(self):
        for order in itertools.permutations(("LR", "Codex", "Kimi")):
            with self.subTest(order=order):
                self.running = False
                self.lightroom_running = False
                before = self.spawn_count
                clients = {}
                for name in order:
                    if name == "LR":
                        self.lightroom_running = True
                    else:
                        clients[name] = self.manager(name, "builtin" if name == "Codex" else "external")
                        self.start(clients[name])
                self.assertTrue(self.running)
                self.assertTrue(self.lightroom_running)
                self.assertEqual(self.spawn_count, before + 1)
                self.assertEqual(clients["Codex"].ensure(self.base)["status"], "reused")
                self.assertEqual(clients["Kimi"].ensure(self.base)["mode"], "external")

    def test_simultaneous_builtin_clients_share_the_startup_lock(self):
        first, second = self.manager("Codex", "builtin"), self.manager("Kimi", "builtin")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda manager: manager.ensure(self.base), (first, second)))
        self.assertEqual(self.spawn_count, 1)
        self.assertEqual({result["status"] for result in results}, {"started", "reused"})

    def test_owner_shutdown_keeps_bridge_and_external_never_recovers_a_crash(self):
        owner, client = self.manager("Codex", "builtin"), self.manager("Kimi", "external")
        self.start(owner)
        del owner  # A worker owns no stop/kill destructor for the detached Bridge.
        self.assertTrue(self.running)
        self.processes[0].terminate.assert_not_called()
        self.processes[0].kill.assert_not_called()
        client.ensure(self.base)
        self.assertEqual(self.spawn_count, 1)
        self.running = False
        client.ensure(self.base)
        self.assertFalse(self.running)
        self.assertEqual(self.spawn_count, 1)
        replacement_owner = self.manager("Codex", "builtin")
        replacement_owner.ensure(self.base)
        self.assertTrue(self.running)
        self.assertEqual(self.spawn_count, 2)

    def test_builtin_request_recovers_a_crash_but_old_active_worker_is_blocked(self):
        manager = self.manager("Codex", "builtin")
        self.start(manager)
        self.running = False
        manager.ensure(self.base)
        self.assertEqual(self.spawn_count, 2)
        self.running = False
        manager.installed_root = self.root / "replaced-install"
        with self.assertRaisesRegex(BridgeStartError, "active_version_changed"):
            manager.ensure(self.base)
        self.assertFalse(self.running)
        self.assertEqual(self.spawn_count, 2)

    def test_initialize_and_tools_list_respond_while_builtin_negotiation_is_blocked(self):
        manager = self.manager("Codex", "builtin")
        entered, release = threading.Event(), threading.Event()

        def delayed_ensure(base, token):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release negotiation")
            return {"mode": "builtin", "status": "reused"}

        wb = mock.Mock()
        wb.lrops.bridge_manager = manager
        wb.lrops.base = self.base
        wb.lrops.target.token = None
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        stdin = io.StringIO("".join(json.dumps(req) + "\n" for req in requests))
        stdin.reconfigure = mock.Mock()
        stdout = io.StringIO()
        stdout.reconfigure = mock.Mock()
        try:
            with (mock.patch.object(manager, "_ensure", side_effect=delayed_ensure),
                  mock.patch.object(server, "build_workbench", return_value=wb),
                  mock.patch.object(server.sys, "stdin", stdin),
                  mock.patch.object(server.sys, "stdout", stdout)):
                server.main()
                self.assertTrue(entered.wait(2))
                self.assertTrue(manager._startup_thread.is_alive())
                replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
                self.assertIn("serverInfo", replies[0]["result"])
                self.assertTrue(replies[1]["result"]["tools"])
        finally:
            release.set()
            if manager._startup_thread is not None:
                manager._startup_thread.join(3)

    def test_external_real_request_recovers_when_bridge_arrives_later_without_replay(self):
        config = ConfigStore(self.root / "routing/config.json")
        config.initialize()
        registry = TargetRegistry(config.path, config=config)
        router = make_router(registry, self.root)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'

        def open_request(request):
            if not self.running:
                raise LrOpsError("offline test Bridge unavailable")
            return response

        with mock.patch.object(LrOpsClient, "_urlopen", side_effect=open_request) as http:
            self.assertFalse(router.reachable())
            self.running = True  # Independent maintenance entry starts Bridge.
            self.assertTrue(router.reachable())
            self.assertEqual(http.call_count, 2)
        self.spawn.assert_not_called()

    def test_unknown_http_write_is_not_replayed_by_builtin_preflight(self):
        config = ConfigStore(self.root / "routing/config.json")
        config.initialize()
        config.update("bridge", lambda bridge: bridge.update(mode="builtin"))
        registry = TargetRegistry(config.path, config=config)
        router = make_router(registry, self.root)
        with mock.patch.object(LrOpsClient, "_urlopen", side_effect=LrOpsError("result unknown")) as http:
            with self.assertRaisesRegex(LrOpsError, "result unknown"):
                router._current()._request("POST", "/jobs", {"command": "apply_candidate", "params": {}})
            http.assert_called_once()
        self.assertEqual(self.spawn_count, 1)

    def test_timeout_before_lightroom_start_does_not_cancel_or_repeat_the_job(self):
        queue = JobQueue(self.root / "queue")
        client = LrOpsClient(self.base, token=None)
        submitted = []

        def request(method, path, body=None):
            if method == "POST":
                return {"job_id": queue.submit(body["command"], body["params"])}
            return queue.get(path.rsplit("/", 1)[1])

        with mock.patch.object(client, "_request", side_effect=request) as http:
            with self.assertRaisesRegex(LrOpsError, "timed out"):
                client.run("apply_candidate", {"Exposure2012": 1}, timeout=0, on_submitted=submitted.append)
            self.assertEqual(len(submitted), 1)
            job_id = submitted[0]
            self.assertEqual(queue.get(job_id)["status"], "queued")
            self.assertEqual(queue.next_queued()["job_id"], job_id)
            self.assertIsNone(queue.next_queued())
            self.assertTrue(queue.complete(job_id, {"ok": True, "data": {"applied": True}}))
            self.assertEqual(client.get_job(job_id)["status"], "done")
            self.assertEqual(sum(call.args[0] == "POST" for call in http.call_args_list), 1)

    def test_same_client_probe_recovers_after_lightroom_starts_or_restarts(self):
        queue = JobQueue(self.root / "queue")
        client = LrOpsClient(self.base, token=None)

        def request(method, path, body=None):
            if method == "POST":
                return {"job_id": queue.submit(body["command"], body["params"])}
            if self.lightroom_running:
                while job := queue.next_queued():
                    queue.complete(job["job_id"], {"ok": True, "data": {"catalog_identity": "test-catalog"}})
            return queue.get(path.rsplit("/", 1)[1])

        with mock.patch.object(client, "_request", side_effect=request):
            for _ in range(2):
                self.lightroom_running = False
                with self.assertRaisesRegex(LrOpsError, "timed out"):
                    client.run("probe_client", timeout=0)
                self.lightroom_running = True
                self.assertEqual(client.run("probe_client", timeout=1), {"catalog_identity": "test-catalog"})

    def test_bridge_restart_preserves_queued_but_never_replays_dispatched_work(self):
        queue = JobQueue(self.root / "queue")
        dispatched = queue.submit("apply_candidate", {"Exposure2012": 1})
        queued = queue.submit("get_selection", {})
        self.assertEqual(queue.next_queued()["job_id"], dispatched)
        restarted = JobQueue(self.root / "queue")
        self.assertEqual(restarted.get(dispatched)["status"], "interrupted")
        self.assertEqual(restarted.get(queued)["status"], "queued")
        self.assertEqual(restarted.next_queued()["job_id"], queued)
        self.assertIsNone(restarted.next_queued())
