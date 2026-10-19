"""Tests for bridge-side file staging and the /files endpoints.

The point of this module under test is that the bridge -- not the caller --
decides where files live, so callers no longer need to share a disk with
Lightroom. These tests pin two things:

1. Path resolution and traversal safety (ids arrive over HTTP).
2. The path invariant that used to live on the workbench
   (``engine.py``: "LR wrote outside the scratch dir") still holds.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from lr_ops import server as server_mod
from lr_ops.staging import Staging, StagingError


class TestStagingUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = Staging(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_stage_roundtrip(self):
        stage_id = self.staging.create_stage()
        self.staging.put_input(stage_id, "IMG_0001.CR3", b"raw-bytes")
        path = self.staging.resolve_input(stage_id)
        self.assertEqual(path.read_bytes(), b"raw-bytes")
        self.assertEqual(path.name, "IMG_0001.CR3")

    def test_filename_cannot_escape_stage(self):
        stage_id = self.staging.create_stage()
        stored = self.staging.put_input(stage_id, "../../evil.jpg", b"x")
        self.assertEqual(stored.parent, self.staging.input_dir(stage_id))

    def test_ambiguous_stage_requires_a_name(self):
        """Two files and no name is an error, not an arbitrary pick."""
        stage_id = self.staging.create_stage()
        self.staging.put_input(stage_id, "a.jpg", b"a")
        self.staging.put_input(stage_id, "b.jpg", b"b")
        with self.assertRaises(StagingError):
            self.staging.resolve_input(stage_id)
        self.assertEqual(self.staging.resolve_input(stage_id, "b.jpg").read_bytes(), b"b")

    def test_invalid_stage_id_rejected(self):
        for bad in ["../../etc", "", "not-a-uuid", None, 17]:
            with self.assertRaises(StagingError):
                self.staging.resolve_input(bad)

    def test_inject_dispatch_replaces_caller_path(self):
        """The caller's output_dir is discarded -- the bridge owns this."""
        stage_id = self.staging.create_stage()
        self.staging.put_input(stage_id, "in.jpg", b"in")
        out = self.staging.inject_dispatch(
            "import_photo", {"stage_id": stage_id, "path": "/caller/disk/x.jpg"}, "job-1"
        )
        self.assertNotIn("stage_id", out)
        self.assertNotEqual(out["path"], "/caller/disk/x.jpg")
        self.assertEqual(Path(out["path"]).read_bytes(), b"in")

    def test_inject_dispatch_overwrites_output_dir(self):
        out = self.staging.inject_dispatch(
            "export_preview",
            {"photo_uuid": "u", "output_dir": "/caller/disk/previews"},
            "5c9d0c9e-1111-4222-8333-444455556666",
        )
        self.assertNotEqual(out["output_dir"], "/caller/disk/previews")
        self.assertEqual(
            Path(out["output_dir"]),
            self.staging.output_dir("5c9d0c9e-1111-4222-8333-444455556666"),
        )

    def test_verify_output_accepts_its_own_directory(self):
        job_id = "6c9d0c9e-1111-4222-8333-444455556666"
        directory = self.staging.prepare_output(job_id)
        produced = directory / "IMG_0001.jpg"
        produced.write_bytes(b"jpeg")
        self.assertEqual(self.staging.verify_output(job_id, str(produced)), produced.resolve())

    def test_verify_output_rejects_foreign_directory(self):
        """A plugin writing elsewhere must be caught loudly."""
        job_id = "7c9d0c9e-1111-4222-8333-444455556666"
        self.staging.prepare_output(job_id)
        elsewhere = Path(self.tmp.name) / "elsewhere.jpg"
        elsewhere.write_bytes(b"jpeg")
        with self.assertRaises(StagingError):
            self.staging.verify_output(job_id, str(elsewhere))

    def test_verify_output_rejects_missing_file(self):
        job_id = "8c9d0c9e-1111-4222-8333-444455556666"
        directory = self.staging.prepare_output(job_id)
        with self.assertRaises(StagingError):
            self.staging.verify_output(job_id, str(directory / "never-written.jpg"))

    def test_verify_output_rejects_empty_report(self):
        job_id = "9c9d0c9e-1111-4222-8333-444455556666"
        self.staging.prepare_output(job_id)
        for bad in [None, "", 42]:
            with self.assertRaises(StagingError):
                self.staging.verify_output(job_id, bad)

    def test_read_output_is_single_artifact(self):
        job_id = "ac9d0c9e-1111-4222-8333-444455556666"
        directory = self.staging.prepare_output(job_id)
        with self.assertRaises(StagingError):
            self.staging.read_output(job_id)          # nothing yet
        (directory / "one.jpg").write_bytes(b"1")
        self.assertEqual(self.staging.read_output(job_id), ("one.jpg", b"1"))
        (directory / "two.jpg").write_bytes(b"2")
        with self.assertRaises(StagingError):
            self.staging.read_output(job_id)          # ambiguous

    def test_prune_removes_only_old_directories(self):
        old = self.staging.create_stage()
        fresh = self.staging.create_stage()
        marker = Path(self.tmp.name) / old
        os.utime(marker, (0, 0))
        self.assertEqual(self.staging.prune(now=10 ** 9), 1)
        self.assertFalse(marker.exists())
        self.assertTrue((Path(self.tmp.name) / fresh).exists())


def http_raw(method, port, path, data=None, headers=None, content_type=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def http_json(method, port, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    status, raw, _ = http_raw(method, port, path, data, headers, "application/json")
    if raw and raw[:1] in (b"{", b"["):
        try:
            return status, json.loads(raw.decode())
        except ValueError:
            pass
    return status, raw


def http_upload(port, path, data, headers=None):
    """POST raw bytes to /files and decode the JSON reply."""
    status, raw, _ = http_raw("POST", port, path, data, headers,
                              "application/octet-stream")
    body = json.loads(raw.decode()) if raw and raw[:1] == b"{" else raw
    return status, body


class FileEndpointBase(unittest.TestCase):
    port = 17911

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = Staging(Path(self.tmp.name))
        self.queue = server_mod.JobQueue(log_dir=Path(self.tmp.name))
        self.server = server_mod.make_server(
            queue=self.queue, port=self.port, staging=self.staging
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        return http_json(method, self.port, path, body, headers)


class TestFileEndpoints(FileEndpointBase):
    port = 17912

    def test_upload_then_download_input(self):
        status, body = http_upload(self.port, "/files?name=IMG_0042.CR3", b"raw-file-bytes")
        self.assertEqual(status, 200)
        stage_id = body["stage_id"]
        self.assertEqual(body["name"], "IMG_0042.CR3")

        status, raw, headers = http_raw(
            "GET", self.port, f"/files/{stage_id}/input/IMG_0042.CR3"
        )
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"raw-file-bytes")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_upload_without_name_gets_a_fallback(self):
        status, body = http_upload(self.port, "/files", b"bytes")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "upload")

    def test_empty_upload_rejected(self):
        status, _ = http_upload(self.port, "/files?name=x.jpg", b"")
        self.assertEqual(status, 400)

    def test_download_output(self):
        job_id = "bc9d0c9e-1111-4222-8333-444455556666"
        directory = self.staging.prepare_output(job_id)
        (directory / "photo-name.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
        status, raw, _ = http_raw("GET", self.port, f"/files/{job_id}/output")
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\xff\xd8\xff"), "served bytes are not a JPEG")

    def test_output_404_when_nothing_produced(self):
        status, _ = self.call("GET", "/files/cc9d0c9e-1111-4222-8333-444455556666/output")
        self.assertEqual(status, 404)

    def test_delete_drops_stage(self):
        _, body = http_upload(self.port, "/files?name=x.jpg", b"bytes")
        stage_id = body["stage_id"]
        status, _ = self.call("DELETE", f"/files/{stage_id}")
        self.assertEqual(status, 200)
        with self.assertRaises(StagingError):
            self.staging.resolve_input(stage_id)

    def test_traversal_in_path_rejected(self):
        stage_id = self.staging.create_stage()
        status, _ = self.call("GET", f"/files/{stage_id}/input/..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(status, 404)

    def test_bad_stage_id_rejected(self):
        status, _ = self.call("GET", "/files/not-a-uuid/input/x.jpg")
        self.assertEqual(status, 404)


class TestDispatchInjection(FileEndpointBase):
    """The plugin-facing params must carry bridge-chosen paths, not caller paths."""

    port = 17913

    def _dispatch(self, command, params):
        _, submitted = self.call("POST", "/jobs", {"command": command, "params": params})
        _, job = self.call("GET", "/jobs/next")
        return submitted["job_id"], job

    def test_export_preview_gets_bridge_output_dir(self):
        _, job = self._dispatch(
            "export_preview", {"photo_uuid": "u", "output_dir": "/caller/disk/previews"}
        )
        self.assertNotEqual(job["params"]["output_dir"], "/caller/disk/previews")
        self.assertEqual(
            Path(job["params"]["output_dir"]), self.staging.output_dir(job["job_id"])
        )
        self.assertTrue(Path(job["params"]["output_dir"]).is_dir())

    def test_fast_thumbnail_gets_bridge_output_dir(self):
        _, job = self._dispatch(
            "request_jpeg_thumbnail", {"photo_uuid": "u", "size": 640,
                                        "output_dir": "/caller/disk/previews"}
        )
        self.assertNotEqual(job["params"]["output_dir"], "/caller/disk/previews")
        self.assertEqual(Path(job["params"]["output_dir"]),
                         self.staging.output_dir(job["job_id"]))

    def test_import_photo_gets_bridge_input_path(self):
        _, body = http_upload(self.port, "/files?name=shot.CR3", b"raw")
        stage_id = body["stage_id"]
        _, job = self._dispatch("import_photo", {"stage_id": stage_id})
        self.assertEqual(job["params"]["path"], str(self.staging.resolve_input(stage_id)))

    def test_import_photos_gets_a_list_of_paths(self):
        ids = []
        for name in ("a.jpg", "b.jpg"):
            _, body = http_upload(self.port, f"/files?name={name}", name.encode())
            ids.append(body["stage_id"])
        _, job = self._dispatch("import_photos", {"stage_ids": ids})
        self.assertEqual(len(job["params"]["paths"]), 2)
        self.assertTrue(all(Path(p).is_file() for p in job["params"]["paths"]))

    def test_missing_handle_fails_the_job_not_the_dispatch(self):
        """An unresolvable stage must surface as a failed job, not a crash."""
        _, submitted = self.call("POST", "/jobs", {
            "command": "import_photo",
            "params": {"stage_id": "dd9d0c9e-1111-4222-8333-444455556666"},
        })
        status, job = self.call("GET", "/jobs/next")
        self.assertEqual(status, 200)
        self.assertEqual(job["status"], "failed")
        self.assertIn("staged input", job["result"]["error"])

    def test_plugin_writing_elsewhere_fails_the_job(self):
        """The invariant that used to guard the workbench now guards the bridge."""
        job_id = "ec9d0c9e-1111-4222-8333-444455556666"
        self.staging.prepare_output(job_id)
        stray = Path(self.tmp.name) / "stray.jpg"
        stray.write_bytes(b"jpeg")
        # Only jobs the bridge knows about can be completed; register the id.
        self.queue._jobs[job_id] = {
            "job_id": job_id, "command": "export_preview", "params": {},
            "status": "dispatched",
        }
        self.queue._order.append(job_id)
        self.call("POST", f"/jobs/{job_id}/result", {
            "ok": True, "data": {"output_path": str(stray), "color_space": "sRGB"},
        })
        _, job = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(job["status"], "failed")
        self.assertIn("outside the staged output directory", job["result"]["error"])

    def test_reported_success_with_missing_file_fails_without_output_handle(self):
        job_id = "ac9d0c9e-1111-4222-8333-444455556666"
        directory = self.staging.prepare_output(job_id)
        self.queue._jobs[job_id] = {
            "job_id": job_id, "command": "export_preview", "params": {},
            "status": "dispatched",
        }
        self.queue._order.append(job_id)
        self.call("POST", f"/jobs/{job_id}/result", {
            "ok": True, "data": {"output_path": str(directory / "missing.jpg")},
        })
        _, job = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("output_ref", job["result"].get("data", {}))
        self.assertEqual(self.staging.collect_output(job_id), [])

    def test_honest_output_completes_with_a_handle_not_a_path(self):
        job_id = "fc9d0c9e-1111-4222-8333-444455556666"
        directory = self.staging.prepare_output(job_id)
        produced = directory / "real.jpg"
        produced.write_bytes(b"jpeg")
        self.queue._jobs[job_id] = {
            "job_id": job_id, "command": "export_preview", "params": {},
            "status": "dispatched",
        }
        self.queue._order.append(job_id)
        self.call("POST", f"/jobs/{job_id}/result", {
            "ok": True, "data": {"output_path": str(produced), "color_space": "sRGB"},
        })
        _, job = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(job["status"], "done")
        data = job["result"]["data"]
        # The caller gets a name it can ask for, not a path it cannot use.
        self.assertEqual(data["output_ref"], job_id)
        self.assertNotIn("output_path", data)
        self.assertEqual(data["color_space"], "sRGB")  # other fields survive

    def test_output_ref_is_retrievable_end_to_end(self):
        """The handle the workbench is given must actually fetch the artifact."""
        job_id = "fd9d0c9e-1111-4222-8333-444455556666"
        directory = self.staging.prepare_output(job_id)
        (directory / "lr-named-this.jpg").write_bytes(b"\xff\xd8\xff\xe0payload")
        self.queue._jobs[job_id] = {
            "job_id": job_id, "command": "export_preview", "params": {},
            "status": "dispatched",
        }
        self.queue._order.append(job_id)
        self.call("POST", f"/jobs/{job_id}/result", {
            "ok": True, "data": {"output_path": str(directory / "lr-named-this.jpg")},
        })
        _, job = self.call("GET", f"/jobs/{job_id}")
        ref = job["result"]["data"]["output_ref"]
        status, raw, headers = http_raw("GET", self.port, f"/files/{ref}/output")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"\xff\xd8\xff\xe0payload")
        # Lightroom names the file after the photo; the caller learns it here.
        self.assertEqual(headers.get("X-Staged-Filename"), "lr-named-this.jpg")


class TestFilesAuth(FileEndpointBase):
    port = 17914

    TOKEN = "".join(("file", "-", "token"))
    AUTH = {"X-LR-XMP-Token": "file-token"}

    def setUp(self):
        os.environ["LR_XMP_MIMIC_LOCAL_TOKEN"] = self.TOKEN
        super().setUp()
        self.server.shutdown()
        self.server.server_close()
        self.server = server_mod.make_server(
            queue=server_mod.JobQueue(log_dir=Path(self.tmp.name)),
            port=self.port, staging=self.staging,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        super().tearDown()
        os.environ.pop("LR_XMP_MIMIC_LOCAL_TOKEN", None)

    def test_upload_requires_token(self):
        status, _ = http_upload(self.port, "/files?name=x.jpg", b"bytes")
        self.assertEqual(status, 403)

    def test_download_requires_token(self):
        """Photographs must not be downloadable just because a port is open."""
        job_id = "1d9d0c9e-1111-4222-8333-444455556666"
        directory = self.staging.prepare_output(job_id)
        (directory / "private.jpg").write_bytes(b"\xff\xd8\xff\xe0secret")
        status, _, _ = http_raw("GET", self.port, f"/files/{job_id}/output")
        self.assertEqual(status, 403)
        status, raw, _ = http_raw("GET", self.port, f"/files/{job_id}/output", headers=self.AUTH)
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\xff\xd8\xff"))

    def test_delete_requires_token(self):
        stage_id = self.staging.create_stage()
        status, _ = self.call("DELETE", f"/files/{stage_id}")
        self.assertEqual(status, 403)


class TestBridgeHostConfig(unittest.TestCase):
    """Binding is configurable so the bridge can be reached over a LAN."""

    def test_default_host_is_loopback(self):
        self.assertEqual(server_mod.HOST, "127.0.0.1")

    def test_module_reads_env_override(self):
        source = Path(server_mod.__file__).read_text()
        self.assertIn('os.environ.get("LR_XMP_MIMIC_BRIDGE_HOST")', source)
        self.assertIn('os.environ.get("LR_XMP_MIMIC_BRIDGE_PORT")', source)


if __name__ == "__main__":
    unittest.main()
