"""Tests for command_batch, group fairness, and restart recovery of results.

The plugin side is faked over real HTTP, exactly like test_lr_ops_server.py:
the tests drive the wire contract a real Bridge.lua would speak, so the Python
half of the batch protocol (dispatch shape, result shapes, staging injection,
output verification) is covered without Lightroom.
"""

import json
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from lr_ops import server as server_mod
from lr_ops.staging import Staging


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
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_log_dir = server_mod.LOG_DIR
        server_mod.LOG_DIR = Path(self.tmp.name) / "lr-ops"
        self.queue = server_mod.JobQueue(log_dir=Path(self.tmp.name) / "queue")
        self.staging = Staging(Path(self.tmp.name) / "staging")
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

    def call(self, method, path, body=None, headers=None):
        return http(method, self.port, path, body, headers)

    def submit(self, command, params, group=None):
        body = {"command": command, "params": params}
        if group is not None:
            body["group"] = group
        status, reply = self.call("POST", "/jobs", body)
        self.assertEqual(status, 200, reply)
        return reply["job_id"]


class TestCommandBatch(ServerTestBase):
    def test_batch_dispatches_as_one_queue_item(self):
        """A batch is a single dispatch: nothing can interleave mid-operation."""
        job_id = self.submit("command_batch", {"commands": [
            {"command": "get_develop_snapshot", "params": {"photo_uuid": "x"}},
            {"command": "apply_candidate",
             "params": {"photo_uuid": "x", "settings": {"Exposure2012": 0.5}}},
        ]})

        status, job = self.call("GET", "/jobs/next")
        self.assertEqual(status, 200)
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(job["command"], "command_batch")
        self.assertEqual(len(job["params"]["commands"]), 2)

        status, nxt = self.call("GET", "/jobs/next")
        self.assertEqual(status, 200)
        self.assertIsNone(nxt, "the batch must occupy the serial queue alone")

    def test_batch_success_result_roundtrip(self):
        job_id = self.submit("command_batch", {"commands": [
            {"command": "probe_client"},
            {"command": "get_selection"},
        ]})
        self.call("GET", "/jobs/next")
        status, _ = self.call("POST", f"/jobs/{job_id}/result", {
            "ok": True,
            "data": {"results": [
                {"command": "probe_client", "ok": True, "data": {"lr_version": "15.4.1"}},
                {"command": "get_selection", "ok": True, "data": {"target": None}},
            ]},
        })
        self.assertEqual(status, 200)
        _, job = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(job["status"], "done")
        results = job["result"]["data"]["results"]
        self.assertEqual([r["command"] for r in results],
                         ["probe_client", "get_selection"])

    def test_batch_failure_payload_is_stored_verbatim(self):
        """The failed_at_index contract must survive the round trip."""
        job_id = self.submit("command_batch", {"commands": [
            {"command": "probe_client"},
            {"command": "apply_candidate", "params": {}},
        ]})
        self.call("GET", "/jobs/next")
        status, _ = self.call("POST", f"/jobs/{job_id}/result", {
            "ok": False,
            "error": "Setting out of bounds: Exposure2012",
            "failed_at_index": 2,
            "results_so_far": [
                {"command": "probe_client", "ok": True, "data": {"sandbox": True}},
            ],
        })
        self.assertEqual(status, 200)
        _, job = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["result"]["failed_at_index"], 2)
        self.assertEqual(len(job["result"]["results_so_far"]), 1)

    def test_batch_validation_rejects_malformed_payloads(self):
        bad = [
            {},                                                  # no commands
            {"commands": []},                                    # empty
            {"commands": [{"params": {}}]},                      # entry without command
            {"commands": [{"command": ""}]},                     # empty command
            {"commands": [{"command": "command_batch",
                           "params": {"commands": [{"command": "probe_client"}]}}]},
            {"commands": [{"command": "probe_client", "params": []}]},
            {"commands": [{"command": "probe_client"}] * 65},    # over the cap
        ]
        for params in bad:
            status, reply = self.call(
                "POST", "/jobs", {"command": "command_batch", "params": params})
            self.assertEqual(status, 400, (params, reply))

    def test_batch_staging_handles_are_injected_per_entry(self):
        stage_id = self.staging.create_stage()
        self.staging.put_input(stage_id, "photo.jpg", b"bytes")
        job_id = self.submit("command_batch", {"commands": [
            {"command": "import_photo", "params": {"stage_id": stage_id}},
            {"command": "export_preview", "params": {"photo_uuid": "x"}},
        ]})

        status, job = self.call("GET", "/jobs/next")
        self.assertEqual(status, 200)
        first, second = job["params"]["commands"]
        self.assertEqual(
            first["params"]["path"],
            str(self.staging.input_dir(stage_id) / "photo.jpg"))
        self.assertNotIn("stage_id", first["params"])
        self.assertEqual(
            second["params"]["output_dir"],
            str(self.staging.output_dir(job_id)))

    def test_batch_output_paths_are_verified_and_masked(self):
        job_id = self.submit("command_batch", {"commands": [
            {"command": "export_preview", "params": {"photo_uuid": "x"}},
        ]})
        _, job = self.call("GET", "/jobs/next")
        out_dir = Path(job["params"]["commands"][0]["params"]["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        produced = out_dir / "x.jpg"
        produced.write_bytes(b"jpeg")

        status, _ = self.call("POST", f"/jobs/{job_id}/result", {
            "ok": True,
            "data": {"results": [
                {"command": "export_preview", "ok": True,
                 "data": {"output_path": str(produced)}},
            ]},
        })
        self.assertEqual(status, 200)
        _, stored = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(stored["status"], "done")
        payload = stored["result"]["data"]["results"][0]["data"]
        self.assertNotIn("output_path", payload)
        self.assertEqual(payload["output_ref"], job_id)

    def test_batch_output_outside_staging_fails_the_job(self):
        job_id = self.submit("command_batch", {"commands": [
            {"command": "export_preview", "params": {"photo_uuid": "x"}},
        ]})
        self.call("GET", "/jobs/next")
        stray = Path(self.tmp.name) / "stray.jpg"
        stray.write_bytes(b"jpeg")
        self.call("POST", f"/jobs/{job_id}/result", {
            "ok": True,
            "data": {"results": [
                {"command": "export_preview", "ok": True,
                 "data": {"output_path": str(stray)}},
            ]},
        })
        _, stored = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(stored["status"], "failed")
        self.assertIn("outside the staged output directory",
                      stored["result"]["error"])


class TestGroupField(ServerTestBase):
    def test_group_roundtrips_and_defaults(self):
        grouped = self.submit("probe_client", {}, group="episode-42")
        plain = self.submit("probe_client", {})
        _, job = self.call("GET", f"/jobs/{grouped}")
        self.assertEqual(job["group"], "episode-42")
        _, job = self.call("GET", f"/jobs/{plain}")
        self.assertEqual(job["group"], "default")

    def test_bad_group_rejected(self):
        status, _ = self.call(
            "POST", "/jobs", {"command": "probe_client", "params": {}, "group": 7})
        self.assertEqual(status, 400)

    def test_existing_clients_without_group_still_work(self):
        """The pre-grouping wire shape must dispatch unchanged."""
        job_id = self.submit("probe_client", {})
        _, job = self.call("GET", "/jobs/next")
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(job["command"], "probe_client")


class TestGroupFairness(unittest.TestCase):
    def queue(self, root):
        return server_mod.JobQueue(log_dir=Path(root))

    def test_round_robin_across_groups(self):
        """A burst in one group must not starve another group's single job."""
        with tempfile.TemporaryDirectory() as root:
            q = self.queue(root)
            a1 = q.submit("export_preview", {}, group="search")
            a2 = q.submit("export_preview", {}, group="search")
            a3 = q.submit("export_preview", {}, group="search")
            b1 = q.submit("export_preview", {}, group="export")
            order = [q.next_queued()["job_id"] for _ in range(4)]
            self.assertEqual(order, [a1, b1, a2, a3])

    def test_late_group_gets_the_next_turn(self):
        """Fairness is not submission-time: a group that appears later still
        alternates with the group that was there first."""
        with tempfile.TemporaryDirectory() as root:
            q = self.queue(root)
            a1 = q.submit("probe_client", {}, group="bulk")
            a2 = q.submit("probe_client", {}, group="bulk")
            self.assertEqual(q.next_queued()["job_id"], a1)
            b1 = q.submit("probe_client", {}, group="quick")
            self.assertEqual(q.next_queued()["job_id"], b1)
            self.assertEqual(q.next_queued()["job_id"], a2)

    def test_single_group_is_exactly_fifo(self):
        """One group (or no groups at all) degrades to historical FIFO."""
        with tempfile.TemporaryDirectory() as root:
            q = self.queue(root)
            ids = [q.submit("probe_client", {}) for _ in range(3)]
            ids.append(q.submit("probe_client", {}, group="only"))
            ids.append(q.submit("probe_client", {}, group="only"))
            order = [q.next_queued()["job_id"] for _ in range(5)]
            # default(3) alternates with only(2): d1, o1, d2, o2, d3
            self.assertEqual(order,
                             [ids[0], ids[3], ids[1], ids[4], ids[2]])

    def test_group_survives_restart_and_stays_dispatchable(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.queue(root)
            dispatched = first.submit("probe_client", {}, group="session-9")
            queued = first.submit("probe_client", {}, group="session-9")
            self.assertEqual(first.next_queued()["job_id"], dispatched)

            recovered = self.queue(root)
            self.assertEqual(recovered.get(queued)["group"], "session-9")
            self.assertEqual(recovered.get(dispatched)["status"], "interrupted")
            self.assertEqual(recovered.next_queued()["job_id"], queued)
            self.assertIsNone(recovered.next_queued(),
                              "an interrupted job must never be replayed")


class TestRecoveryWithResults(unittest.TestCase):
    def test_completed_result_is_served_after_server_restart(self):
        """Dispatched transitions and posted results are both journaled: a
        bridge that restarts after the plugin's result landed still serves it."""
        with tempfile.TemporaryDirectory() as root:
            log_dir = Path(root) / "queue"
            queue = server_mod.JobQueue(log_dir=log_dir)
            server = server_mod.make_server(
                queue=queue, port=0, staging=Staging(Path(root) / "staging"))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                _, reply = http("POST", port, "/jobs", {
                    "command": "command_batch",
                    "params": {"commands": [{"command": "probe_client"}]},
                    "group": "episode-1",
                })
                job_id = reply["job_id"]
                http("GET", port, "/jobs/next")
                http("POST", port, f"/jobs/{job_id}/result", {
                    "ok": True,
                    "data": {"results": [
                        {"command": "probe_client", "ok": True,
                         "data": {"sandbox": True}},
                    ]},
                })
            finally:
                server.shutdown()
                server.server_close()

            # Rebuild from the journal alone, as a real restart would.
            recovered = server_mod.JobQueue(log_dir=log_dir)
            row = recovered.get(job_id)
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["group"], "episode-1")
            self.assertEqual(
                row["result"]["data"]["results"][0]["data"]["sandbox"], True)

    def test_dispatched_batch_recovers_as_interrupted_with_error(self):
        with tempfile.TemporaryDirectory() as root:
            first = server_mod.JobQueue(log_dir=Path(root))
            job_id = first.submit("command_batch", {"commands": [
                {"command": "apply_candidate", "params": {}},
            ]})
            self.assertEqual(first.next_queued()["job_id"], job_id)

            recovered = server_mod.JobQueue(log_dir=Path(root))
            row = recovered.get(job_id)
            self.assertEqual(row["status"], "interrupted")
            self.assertIn("bridge restarted", row["error"])
            self.assertIsNone(row["result"])
            self.assertIsNone(recovered.next_queued())


if __name__ == "__main__":
    unittest.main()
