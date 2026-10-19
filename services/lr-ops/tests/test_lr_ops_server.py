"""Tests for the lr-ops service using a fake plugin over real HTTP."""

import json
import importlib.util
import os
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from lr_ops import server as server_mod


def http(method, port, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method
    )
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode()) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw.decode()) if raw else None)


class ServerTestBase(unittest.TestCase):
    port = 0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_log_dir = server_mod.LOG_DIR
        server_mod.LOG_DIR = Path(self.tmp.name) / "lr-ops"
        self.queue = server_mod.JobQueue(log_dir=Path(self.tmp.name))
        self.server = server_mod.make_server(
            queue=self.queue, port=0,
            staging=server_mod.Staging(Path(self.tmp.name) / "staging"))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        server_mod.LOG_DIR = self._old_log_dir
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        return http(method, self.port, path, body, headers)


class TestJobFlow(ServerTestBase):
    port = 0

    def test_health(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["source_sha256"], server_mod.SOURCE_SHA256)
        self.assertRegex(body["source_sha256"], r"^[0-9a-f]{64}$")
        script = Path(__file__).parents[3] / "plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap_macos.py"
        spec = importlib.util.spec_from_file_location("health_bootstrap", script)
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        package = Path(server_mod.__file__).resolve().parent
        self.assertEqual(body["source_sha256"], bootstrap.bridge_source_fingerprint(package))
        version = Path(self.tmp.name) / "version"
        service_root = version / "source/services"
        service_root.mkdir(parents=True)
        (service_root / "lr-ops").symlink_to(package.parent, target_is_directory=True)
        with mock.patch.dict(os.environ, {"LROPS_BASE": f"http://127.0.0.1:{self.port}"}):
            result = bootstrap.bridge_build_status(version)
        self.assertTrue(result["matches_active_source"])

    def test_health_fingerprint_stays_at_startup_value(self):
        with mock.patch.object(server_mod, "source_fingerprint", return_value="changed-on-disk"):
            _, body = self.call("GET", "/health")
        self.assertEqual(body["source_sha256"], server_mod.SOURCE_SHA256)

    def test_submit_poll_complete_cycle(self):
        # Local tool submits a job.
        status, body = self.call("POST", "/jobs", {
            "command": "probe_client", "params": {},
        })
        self.assertEqual(status, 200)
        job_id = body["job_id"]

        # Fake plugin picks it up.
        status, job = self.call("GET", "/jobs/next")
        self.assertEqual(status, 200)
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(job["command"], "probe_client")

        # Queue is now empty for polling.
        status, job = self.call("GET", "/jobs/next")
        self.assertEqual(status, 200)
        self.assertIsNone(job)

        # Fake plugin posts the result.
        status, _ = self.call("POST", f"/jobs/{job_id}/result", {
            "ok": True, "data": {"lr_version": "15.2.1"},
        })
        self.assertEqual(status, 200)

        # Local tool reads the finished job.
        status, job = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["data"]["lr_version"], "15.2.1")

    def test_failed_result_marks_job_failed(self):
        _, body = self.call("POST", "/jobs", {"command": "apply_candidate", "params": {}})
        job_id = body["job_id"]
        self.call("GET", "/jobs/next")
        self.call("POST", f"/jobs/{job_id}/result", {"ok": False, "error": "not sandbox"})
        _, job = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["result"]["error"], "not sandbox")

    def test_result_for_unknown_job_rejected(self):
        status, _ = self.call("POST", "/jobs/nope/result", {"ok": True})
        self.assertEqual(status, 409)

    def test_double_completion_rejected(self):
        _, body = self.call("POST", "/jobs", {"command": "probe_client", "params": {}})
        job_id = body["job_id"]
        self.call("GET", "/jobs/next")
        self.call("POST", f"/jobs/{job_id}/result", {"ok": True, "data": {}})
        status, _ = self.call("POST", f"/jobs/{job_id}/result", {"ok": True, "data": {}})
        self.assertEqual(status, 409)

    def test_invalid_job_payload_rejected(self):
        status, _ = self.call("POST", "/jobs", {"params": {}})
        self.assertEqual(status, 400)


class TestJobRecovery(unittest.TestCase):
    def test_dispatched_job_becomes_interrupted_without_replay(self):
        with tempfile.TemporaryDirectory() as root:
            first = server_mod.JobQueue(log_dir=Path(root))
            dispatched = first.submit("apply_candidate", {"Exposure2012": 1})
            queued = first.submit("probe_client", {})
            self.assertEqual(first.next_queued()["job_id"], dispatched)

            recovered = server_mod.JobQueue(log_dir=Path(root))
            self.assertEqual(recovered.get(queued)["status"], "queued")
            self.assertEqual(recovered.get(dispatched)["status"], "interrupted")
            self.assertEqual(recovered.next_queued()["job_id"], queued)

    def test_completed_job_is_available_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            first = server_mod.JobQueue(log_dir=Path(root))
            job_id = first.submit("probe_client", {})
            first.next_queued()
            first.complete(job_id, {"ok": True, "data": {"sandbox": True}})
            recovered = server_mod.JobQueue(log_dir=Path(root))
            row = recovered.get(job_id)
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["result"]["data"]["sandbox"], True)

    def test_result_for_queued_job_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            queue = server_mod.JobQueue(log_dir=Path(root))
            job_id = queue.submit("probe_client", {})
            self.assertFalse(queue.complete(job_id, {"ok": True, "data": {}}))
            self.assertEqual(queue.get(job_id)["status"], "queued")

    def test_dispatch_persistence_failure_does_not_allow_replay(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            first = server_mod.JobQueue(log_dir=path)
            job_id = first.submit("apply_candidate", {})
            first._append_log = lambda job: (_ for _ in ()).throw(
                server_mod.QueuePersistenceError("disk busy"))
            with self.assertRaises(server_mod.QueuePersistenceError):
                first.next_queued()
            recovered = server_mod.JobQueue(log_dir=path)
            self.assertEqual(recovered.get(job_id)["status"], "queued")

    def test_corrupt_journal_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Path(root) / "jobs.jsonl"
            journal.write_text(
                '{"job_id":"old","status":"queued"}\n{"job_id":',
                encoding="utf-8")
            with self.assertRaises(server_mod.QueuePersistenceError):
                server_mod.JobQueue(log_dir=Path(root))

    def test_submit_persistence_failure_does_not_queue_in_memory(self):
        with tempfile.TemporaryDirectory() as root:
            blocked = Path(root) / "file"
            blocked.write_text("x", encoding="utf-8")
            queue = server_mod.JobQueue(log_dir=blocked / "child")
            with self.assertRaises(server_mod.QueuePersistenceError):
                queue.submit("probe_client", {})
            self.assertEqual(queue.queued_count(), 0)

    def test_complete_persistence_failure_keeps_dispatched(self):
        with tempfile.TemporaryDirectory() as root:
            queue = server_mod.JobQueue(log_dir=Path(root))
            job_id = queue.submit("probe_client", {})
            queue.next_queued()
            queue._append_log = lambda job: (_ for _ in ()).throw(
                server_mod.QueuePersistenceError("disk busy"))
            with self.assertRaises(server_mod.QueuePersistenceError):
                queue.complete(job_id, {"ok": True, "data": {}})
            self.assertEqual(queue.get(job_id)["status"], "dispatched")

    def test_os_fsync_failure_is_persistence_failure(self):
        with tempfile.TemporaryDirectory() as root:
            queue = server_mod.JobQueue(log_dir=Path(root))
            with mock.patch.object(server_mod.os, "fsync", side_effect=OSError("disk busy")):
                with self.assertRaises(server_mod.QueuePersistenceError):
                    queue.submit("probe_client", {})
            self.assertEqual(queue.queued_count(), 0)

    def test_dispatch_persistence_failure_returns_503_without_job(self):
        with tempfile.TemporaryDirectory() as root:
            queue = server_mod.JobQueue(log_dir=Path(root))
            server = server_mod.make_server(queue=queue, port=0,
                                            staging=server_mod.Staging(Path(root) / "staging"))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                job_id = queue.submit("probe_client", {})
                queue._append_log = lambda job: (_ for _ in ()).throw(
                    server_mod.QueuePersistenceError("disk busy"))
                status, body = http("GET", port, "/jobs/next")
                self.assertEqual(status, 503)
                self.assertNotIn("command", body)
                self.assertEqual(queue.get(job_id)["status"], "queued")
            finally:
                server.shutdown()
                server.server_close()

    def test_journal_without_final_newline_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Path(root) / "jobs.jsonl"
            journal.write_text('{"job_id":"old","status":"queued"}', encoding="utf-8")
            with self.assertRaises(server_mod.QueuePersistenceError):
                server_mod.JobQueue(log_dir=Path(root))


class TestStagingSweep(ServerTestBase):
    """Abandoned staging must actually be reclaimed.

    A TTL constant and a ``prune()`` method are not a lifecycle: without a
    caller, staged exports accumulate forever. Submitting a job is the one
    moment a caller is provably active, so the sweep rides along there.
    """

    port = 0

    def setUp(self):
        import tempfile
        from pathlib import Path

        from lr_ops.staging import Staging

        self.tmp = tempfile.TemporaryDirectory()
        self._old_log_dir = server_mod.LOG_DIR
        server_mod.LOG_DIR = Path(self.tmp.name) / "lr-ops"
        self.stage_root = Path(self.tmp.name)
        self.staging = Staging(self.stage_root, max_age_sec=10 ** 9)
        self.queue = server_mod.JobQueue(log_dir=Path(self.tmp.name) / "queue")
        self.server = server_mod.make_server(
            queue=self.queue, port=0, staging=self.staging)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        server_mod.LOG_DIR = self._old_log_dir
        self.tmp.cleanup()

    def test_submitting_a_job_sweeps_expired_staging(self):
        old = self.staging.create_stage()
        os.utime(self.stage_root / old, (0, 0))

        status, _ = self.call("POST", "/jobs", {"command": "probe_client", "params": {}})
        self.assertEqual(status, 200)
        self.assertFalse((self.stage_root / old).exists(),
                         "an abandoned stage should have been swept on submit")

    def test_a_failed_sweep_still_accepts_the_command(self):
        """Reclaiming garbage must never be able to refuse a real command."""
        def boom(now=None):
            raise OSError("disk busy")

        self.staging.prune = boom
        status, body = self.call("POST", "/jobs", {"command": "probe_client", "params": {}})
        self.assertEqual(status, 200)
        self.assertIn("job_id", body)


class TestAuth(ServerTestBase):
    port = 0

    TOKEN = "".join(("test", "-", "token"))
    AUTH = {"X-LR-XMP-Token": "test-token"}

    def setUp(self):
        os.environ["LR_XMP_MIMIC_LOCAL_TOKEN"] = self.TOKEN
        super().setUp()

    def tearDown(self):
        super().tearDown()
        os.environ.pop("LR_XMP_MIMIC_LOCAL_TOKEN", None)

    def _rebuild(self):
        """Rebuild a server whose closure captured the token from the env."""
        self.server.shutdown()
        self.server.server_close()
        self.server = server_mod.make_server(
            queue=server_mod.JobQueue(log_dir=Path(self.tmp.name)), port=self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def test_plugin_endpoints_require_token_when_configured(self):
        self._rebuild()

        status, _ = self.call("GET", "/jobs/next")
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/jobs/next", headers=self.AUTH)
        self.assertEqual(status, 200)

    def test_command_submission_requires_token_when_configured(self):
        """POST /jobs used to be unguarded.

        Guarding only the plugin endpoints left command submission open, so any
        host that could reach the port could drive Lightroom. That matters the
        moment the service is bound to a LAN address, which is exactly what a
        workbench on another machine needs.
        """
        self._rebuild()
        payload = {"command": "probe_client", "params": {}}

        status, _ = self.call("POST", "/jobs", payload)
        self.assertEqual(status, 403)

        status, body = self.call("POST", "/jobs", payload, headers=self.AUTH)
        self.assertEqual(status, 200)
        self.assertIn("job_id", body)

    def test_log_and_echo_require_token_when_configured(self):
        """The plugin ships its log lines to POST /log, so this path is live.

        Bridge.lua must therefore send the token there too -- see
        plugins/lightroom/tests/token-log-test.lua for the plugin-side half.
        """
        self._rebuild()

        status, _ = self.call("POST", "/log", {})
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/echo", {"x": 1})
        self.assertEqual(status, 403)

    def test_only_health_stays_open_when_configured(self):
        """Only the health probe is anonymous once a token is configured."""
        self._rebuild()

        status, _ = self.call("GET", "/health")
        self.assertEqual(status, 200)

        status, body = self.call("POST", "/jobs", {"command": "probe_client", "params": {}}, headers=self.AUTH)
        job_id = body["job_id"]
        status, _ = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(status, 403)
        status, _ = self.call("GET", f"/jobs/{job_id}", headers=self.AUTH)
        self.assertEqual(status, 200)

    def test_wrong_token_rejected(self):
        self._rebuild()
        status, _ = self.call(
            "POST", "/jobs", {"command": "probe_client", "params": {}},
            headers={"X-LR-XMP-Token": "not-the-token"},
        )
        self.assertEqual(status, 403)

    def test_open_when_no_token_configured(self):
        """Same-machine development must keep working with no configuration."""
        os.environ.pop("LR_XMP_MIMIC_LOCAL_TOKEN", None)
        self._rebuild()
        status, _ = self.call("POST", "/jobs", {"command": "probe_client", "params": {}})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
