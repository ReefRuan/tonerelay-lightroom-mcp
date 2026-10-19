import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from workbench.lrops import FastThumbnailUnavailable, LrOpsClient, LrOpsError
from workbench.runtime import Workbench
from workbench.store import Store


class FakeClient(LrOpsClient):
    def __init__(self, status, *, result=None):
        super().__init__(base="http://fake", timeout=10)
        self.status = status
        self.result = result
        self.requests = []

    def _request(self, method, path, body=None):
        self.requests.append((method, path))
        if method == "POST":
            return {"job_id": "job-1"}
        response = {
            "status": self.status,
            "error": "bridge restarted after dispatch; result unknown",
        }
        if self.result is not None:
            response["result"] = self.result
        return response


class TestLrOpsClient(unittest.TestCase):
    def run_queued_with_clock(self, client, command, *, timeout=None):
        elapsed = [0.0]
        def advance(seconds):
            elapsed[0] += seconds
        with patch("workbench.lrops.time.monotonic", side_effect=lambda: elapsed[0]), \
                patch("workbench.lrops.time.sleep", side_effect=advance):
            with self.assertRaisesRegex(LrOpsError, "timed out"):
                client.run(command, timeout=timeout)
        return elapsed[0]

    def test_identity_probe_uses_short_default_budget(self):
        client = FakeClient("queued")
        elapsed = self.run_queued_with_clock(client, "probe_client")
        self.assertGreaterEqual(elapsed, 3)
        self.assertLess(elapsed, 3.5)
        self.assertEqual(sum(method == "POST" for method, _ in client.requests), 1)

    def test_non_probe_keeps_ordinary_job_budget(self):
        elapsed = self.run_queued_with_clock(FakeClient("queued"), "export_photo")
        self.assertGreaterEqual(elapsed, 10)
        self.assertLess(elapsed, 10.5)

    def test_probe_preserves_explicit_and_shorter_client_budgets(self):
        elapsed = self.run_queued_with_clock(FakeClient("queued"), "probe_client", timeout=5)
        self.assertGreaterEqual(elapsed, 5)
        self.assertLess(elapsed, 5.5)
        client = FakeClient("queued")
        client.timeout = 1
        elapsed = self.run_queued_with_clock(client, "probe_client")
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 1.5)

    def test_selection_fails_probe_without_dispatching_selection(self):
        client = FakeClient("queued")
        elapsed = [0.0]
        def advance(seconds):
            elapsed[0] += seconds
        with tempfile.TemporaryDirectory() as root, \
                patch("workbench.lrops.time.monotonic", side_effect=lambda: elapsed[0]), \
                patch("workbench.lrops.time.sleep", side_effect=advance), \
                patch.object(client, "_request", wraps=client._request) as request:
            with self.assertRaisesRegex(LrOpsError, "probe_client timed out"):
                Workbench(Store(Path(root)), client).current_selection()
            submitted = [call.args[2]["command"] for call in request.call_args_list
                         if call.args[0] == "POST"]
        self.assertEqual(submitted, ["probe_client"])
        self.assertLess(elapsed[0], 3.5)

    def test_interrupted_is_immediate_terminal_error(self):
        client = FakeClient("interrupted")
        with self.assertRaisesRegex(LrOpsError, r"job-1 interrupted; result unknown"):
            client.run("apply_candidate")
        self.assertEqual(len(client.requests), 2)

    def test_stable_thumbnail_marker_becomes_dedicated_error(self):
        client = FakeClient("failed", result={
            "ok": False,
            "error": "Commands.lua:789: TRLR_FAST_THUMBNAIL_UNAVAILABLE: "
                     "callback=notAvailable, fired=true",
        })
        with self.assertRaisesRegex(FastThumbnailUnavailable,
                                    "fast thumbnail is unavailable"):
            client.run("request_jpeg_thumbnail")

    def test_other_lightroom_failure_stays_generic(self):
        client = FakeClient("failed", result={
            "ok": False,
            "error": "Catalog authorization changed after probe",
        })
        with self.assertRaises(LrOpsError) as caught:
            client.run("request_jpeg_thumbnail")
        self.assertNotIsInstance(caught.exception, FastThumbnailUnavailable)
        self.assertIn("Catalog authorization changed", str(caught.exception))

    def test_marker_from_another_command_stays_generic(self):
        client = FakeClient("failed", result={
            "ok": False,
            "error": "TRLR_FAST_THUMBNAIL_UNAVAILABLE: quoted diagnostic",
        })
        with self.assertRaises(LrOpsError) as caught:
            client.run("probe_client")
        self.assertNotIsInstance(caught.exception, FastThumbnailUnavailable)


if __name__ == "__main__":
    unittest.main()
