"""Client for the lr-ops bridge service."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:17777"
DEFAULT_PROBE_TIMEOUT = 3.0
FAST_THUMBNAIL_UNAVAILABLE_MARKER = "TRLR_FAST_THUMBNAIL_UNAVAILABLE:"


class LrOpsError(RuntimeError):
    pass


class FastThumbnailUnavailable(LrOpsError):
    """Lightroom's thumbnail callback returned no JPEG for one photo."""


class LrOpsClient:
    """Talks to one bridge over HTTP.

    Two kinds of traffic: commands (JSON, the ``run`` path) and files (bytes).
    Files are addressed by *handle* -- a ``stage_id`` going up, an
    ``output_ref`` coming back -- rather than by path, because the bridge may
    well be on another machine with a different disk. See
    ``lr_ops.staging`` for the bridge side of that contract.
    """

    def __init__(self, base: str | None = None, token: str | None = None, timeout: float = 120.0):
        self.base = (base or os.environ.get("LROPS_BASE") or DEFAULT_BASE).rstrip("/")
        self.token = token if token is not None else os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN")
        self.timeout = timeout

    # ---- plumbing ----------------------------------------------------------

    def _urlopen(self, req: urllib.request.Request):
        try:
            return urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise LrOpsError(f"lr-ops at {self.base} returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LrOpsError(f"lr-ops unreachable at {self.base}: {exc}") from exc

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-LR-XMP-Token", self.token)
        with self._urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}

    def reachable(self) -> bool:
        try:
            self._request("GET", "/health")
            return True
        except LrOpsError:
            return False

    def run(self, command: str, params: dict | None = None, timeout: float | None = None) -> dict:
        """Submit a job and wait for the Lightroom plugin to finish it."""
        job_id = self._request("POST", "/jobs", {"command": command, "params": params or {}})["job_id"]
        # The identity probe does no rendering or Catalog write. A missing
        # plugin must fail this prerequisite promptly, rather than spend the
        # ordinary job budget before every selection/edit call. Keep explicit
        # caller budgets and the budget of actual Lightroom work unchanged.
        wait_timeout = timeout if timeout is not None else self.timeout
        if timeout is None and command == "probe_client":
            wait_timeout = min(wait_timeout, DEFAULT_PROBE_TIMEOUT)
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            job = self._request("GET", f"/jobs/{job_id}")
            if job["status"] == "interrupted":
                detail = (job.get("error") or "bridge restarted after dispatch; result unknown")
                raise LrOpsError(f"Job {job_id} interrupted; result unknown: {detail}")
            if job["status"] in ("done", "failed"):
                result = job.get("result") or {}
                if not result.get("ok"):
                    detail = str(result.get("error"))
                    if (command == "request_jpeg_thumbnail"
                            and FAST_THUMBNAIL_UNAVAILABLE_MARKER in detail):
                        raise FastThumbnailUnavailable(
                            "Lightroom fast thumbnail is unavailable; "
                            "build Standard previews and retry")
                    raise LrOpsError(detail)
                return result.get("data") or {}
            time.sleep(0.4)
        raise LrOpsError(f"Job {command} timed out; is the bridge running inside Lightroom?")

    # ---- files -------------------------------------------------------------

    def upload(self, filename: str, content: bytes) -> str:
        """Stage bytes on the bridge's disk; returns the ``stage_id``.

        The name travels in the query string rather than in multipart, because
        the bridge needs only "these bytes, called this" and Lightroom derives
        the photo name from the filename.
        """
        query = urllib.parse.urlencode({"name": filename or "upload"})
        req = urllib.request.Request(self.base + "/files?" + query, data=content, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        if self.token:
            req.add_header("X-LR-XMP-Token", self.token)
        with self._urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        stage_id = payload.get("stage_id")
        if not stage_id:
            raise LrOpsError("bridge accepted the upload but returned no stage id")
        return stage_id

    def fetch_output(self, output_ref: str, destination: Path) -> Path:
        """Download a job's artifact to ``destination``; returns that path.

        The bridge names exports after the photo, so the caller cannot predict
        the filename -- it asks for "the output of this job" and writes whatever
        comes back under the name it chose.
        """
        req = urllib.request.Request(self.base + f"/files/{output_ref}/output", method="GET")
        if self.token:
            req.add_header("X-LR-XMP-Token", self.token)
        with self._urlopen(req) as resp:
            content = resp.read()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def discard_stage(self, stage_id: str) -> None:
        """Release a staged upload that will not be imported after all."""
        req = urllib.request.Request(self.base + f"/files/{stage_id}", method="DELETE")
        if self.token:
            req.add_header("X-LR-XMP-Token", self.token)
        with self._urlopen(req):
            pass
