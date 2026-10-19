"""Job queue and HTTP front for the Lightroom bridge.

Wire protocol (all JSON):

    GET  /health                      -> {"ok": true, "queued": n, "version": ..., "source_sha256": ...}
    POST /jobs                        <- {"command": str, "params": {...}, "group": str?}
                                     -> {"job_id": "..."}
    GET  /jobs/<id>                   -> full job record including result
    GET  /jobs/next                   -> oldest queued job, or empty body
    POST /jobs/<id>/result            <- {"ok": bool, "data"|"error": ...}

Two extensions keep Lightroom's strictly serial execution usable:

* ``command_batch`` -- one queue item whose params carry
  ``{"commands": [{"command": ..., "params": {...}}, ...]}``. The plugin runs
  the whole batch under a single catalog write gate, so a logical operation
  (read back, copy, write, render) cannot be interleaved with another
  caller's jobs. A failure stops the batch and posts
  ``{"ok": false, "error": ..., "failed_at_index": n, "results_so_far": [...]}``.
* ``group`` -- an optional caller-chosen string (session/episode id). Queued
  jobs are dispatched round-robin across groups, oldest first within a group,
  so one caller's 80-job render burst cannot starve another caller's export.
  Jobs without a group share the ``default`` group; a single group degrades
  to the exact FIFO behaviour the queue always had.

    POST /files                                 raw bytes + ?name= -> {"stage_id": ...}
    GET  /files/<job_id>/output                  -> bytes the plugin produced
    GET  /files/<stage_id>/input/<name>          -> bytes that were uploaded
    DELETE /files/<stage_id>                     -> drop a stage early

Files cross the wire, not paths. The two commands that used to take a path from
the caller now take a handle instead::

    import_photo  {"stage_id": "<id from POST /files>"}     (was: {"path": ...})
    export_preview {...}  ->  {"output_ref": "<job_id>"}    (was: {"output_path": ...})

so ``export_preview`` no longer accepts a caller-chosen ``output_dir``: the
bridge picks it, which is what removes the shared-disk assumption.

When LR_XMP_MIMIC_LOCAL_TOKEN is set, only /health stays anonymous. Job reads,
every POST and DELETE, and file reads all require the X-LR-XMP-Token header,
including GET /jobs/<id>, the plugin's GET /jobs/next, and
POST /jobs/<id>/result.

/jobs was previously exempt, which left command submission open to any host
that could reach the port. That was harmless only for as long as HOST stayed
loopback; the moment the service is bound to a LAN address (so a workbench on
another machine can drive this Lightroom), an unauthenticated /jobs is a
remote write channel into the catalog. The same reasoning applies to /files:
those bytes are the user's photographs. The token is the same one the plugin
reads from local-token.txt and the workbench reads from the environment.

When LR_XMP_MIMIC_LOCAL_TOKEN is unset the service stays open, so same-machine
development needs no configuration. Set it deliberately when exposing the port.

File staging lives in :mod:`lr_ops.staging`; this module only routes to it and
applies the token. See that module for why paths are resolved bridge-side.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .staging import BATCH_COMMAND, OUTPUT_COMMANDS, Staging, StagingError

HOST = os.environ.get("LR_XMP_MIMIC_BRIDGE_HOST") or "127.0.0.1"
PORT = int(os.environ.get("LR_XMP_MIMIC_BRIDGE_PORT") or 17777)
JOB_TIMEOUT_SEC = 120
# Same ceiling the workbench applies to its own uploads, so a file that the
# workbench accepted is never rejected here for being too large.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# A batch is one logical operation, not a way to dump a whole session into a
# single queue item -- the fairness below only works when bursts stay split
# into individual jobs. Keep batches short.
MAX_BATCH_COMMANDS = 64

# Jobs submitted without a group share this one, which keeps the historical
# pure-FIFO behaviour for every client that predates grouping.
DEFAULT_GROUP = "default"


def source_fingerprint(package_root: Path) -> str | None:
    """Opaque startup identity; keep in sync with standalone bootstrap."""
    try:
        files = sorted(package_root.rglob("*.py"))
        if not files:
            return None
        digest = hashlib.sha256()
        for path in files:
            relative = path.relative_to(package_root).as_posix().encode("utf-8")
            content = path.read_bytes()
            digest.update(relative + b"\0" + hashlib.sha256(content).digest())
        return digest.hexdigest()
    except OSError:
        return None


# Capture once at import, never recalculate after files are updated on disk.
SOURCE_SHA256 = source_fingerprint(Path(__file__).resolve().parent)


class QueuePersistenceError(RuntimeError):
    """The queue could not durably record a state transition."""

DATA_ROOT = Path(
    os.environ.get(
        "LR_XMP_MIMIC_RUNTIME_DATA_ROOT",
        Path.home() / "lr-xmp-mimic-runtime-data",
    )
)
LOG_DIR = DATA_ROOT / "lr-ops"
STAGING_DIR = LOG_DIR / "staging"


class JobQueue:
    def __init__(self, log_dir: Path = LOG_DIR):
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []
        # Group -> last dispatch timestamp. Never-dispatched groups sort as 0,
        # ahead of any group that has had a turn, so a latecomer is not
        # punished for jobs that ran before it existed.
        self._group_last_dispatch: dict[str, float] = {}
        self._log_dir = log_dir
        self._load()

    @property
    def _journal(self) -> Path:
        return self._log_dir / "jobs.jsonl"

    def _load(self) -> None:
        """Recover facts without ever replaying an uncertain LR command.

        The journal stores the latest snapshot for each job. A bridge restart
        turns a job that was handed to Lightroom into ``interrupted``; only
        jobs that had never been dispatched remain eligible for polling.
        """
        if not self._journal.exists():
            return
        if not self._journal.is_file():
            raise QueuePersistenceError("job journal is not a file")
        latest: dict[str, dict] = {}
        try:
            text = self._journal.read_text(encoding="utf-8")
        except OSError as exc:
            raise QueuePersistenceError("job journal unavailable") from exc
        if text and not text.endswith("\n"):
            raise QueuePersistenceError("job journal has an incomplete final record")
        lines = text.splitlines()
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, ValueError) as exc:
                raise QueuePersistenceError("job journal is corrupt") from exc
            if not isinstance(row, dict) or not isinstance(row.get("job_id"), str):
                raise QueuePersistenceError("job journal is corrupt")
            latest[row["job_id"]] = row
        for job in latest.values():
            if job.get("status") == "dispatched":
                job = {**job, "status": "interrupted",
                       "interrupted_at": time.time(),
                       "error": "bridge restarted after dispatch; result unknown"}
            self._jobs[job["job_id"]] = job
        self._order = [job["job_id"] for job in sorted(
            self._jobs.values(), key=lambda item: item.get("created_at", 0))]

    def submit(self, command: str, params: dict, group: str | None = None) -> str:
        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "command": command,
            "params": params,
            "group": group or DEFAULT_GROUP,
            "status": "queued",
            "created_at": time.time(),
            "result": None,
        }
        with self._lock:
            self._append_log(job)
            self._jobs[job_id] = job
            self._order.append(job_id)
        return job_id

    def next_queued(self) -> dict | None:
        """Dispatch the oldest queued job of the least recently served group.

        Round-robin is between groups only; inside a group jobs stay FIFO.
        With a single group the group key is constant and the selection below
        reduces to the first queued job in submission order -- identical to
        the original pure-FIFO scan.
        """
        with self._lock:
            pick: tuple[tuple[float, float], str] | None = None
            for job_id in self._order:
                job = self._jobs[job_id]
                if job["status"] != "queued":
                    continue
                group = job.get("group") or DEFAULT_GROUP
                key = (self._group_last_dispatch.get(group, 0.0),
                       job.get("created_at", 0))
                if pick is None or key < pick[0]:
                    pick = (key, job_id)
            if pick is None:
                return None
            job_id = pick[1]
            job = self._jobs[job_id]
            dispatched = {**job, "status": "dispatched"}
            self._append_log(dispatched)
            self._jobs[job_id] = dispatched
            self._group_last_dispatch[job.get("group") or DEFAULT_GROUP] = time.time()
            return {
                "job_id": dispatched["job_id"],
                "command": dispatched["command"],
                "params": dispatched["params"],
            }

    def complete(self, job_id: str, payload: dict) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] != "dispatched":
                return False
            snapshot = {**job,
                        "status": "done" if payload.get("ok") else "failed",
                        "result": payload,
                        "finished_at": time.time()}
            self._append_log(snapshot)
            self._jobs[job_id] = snapshot
        return True

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def queued_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["status"] == "queued")

    def _append_log(self, job: dict) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            with self._journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(job, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise QueuePersistenceError("job journal unavailable") from exc


def _validate_batch(params: dict) -> str | None:
    """Reject malformed batches at submit time, not after dispatch.

    A bad single command fails inside Lightroom and reports back; a malformed
    batch would do the same, but it would also have held the serial queue for
    a round trip before anyone learned the payload was junk. Failing fast here
    keeps the wire contract honest: every entry needs a real command name, and
    nesting batches is rejected outright (the plugin has no nested runner).
    """
    entries = params.get("commands")
    if not isinstance(entries, list) or not entries:
        return "command_batch requires a non-empty commands array"
    if len(entries) > MAX_BATCH_COMMANDS:
        return f"command_batch accepts at most {MAX_BATCH_COMMANDS} commands"
    for entry in entries:
        if not isinstance(entry, dict):
            return "each batch entry must be an object"
        name = entry.get("command")
        if not isinstance(name, str) or not name:
            return "each batch entry requires a command"
        if name == BATCH_COMMAND:
            return "command_batch cannot be nested"
        if "params" in entry and not isinstance(entry["params"], dict):
            return "batch entry params must be an object"
    return None


def _verify_batch_outputs(staging: Staging, job_id: str, body: dict) -> dict:
    """Apply the output-path invariant to every file-producing batch entry.

    Mirrors the single-command handling on POST /jobs/<id>/result: a reported
    path outside the job's staged output directory turns the whole result into
    a failure, and verified paths are replaced by the job's ``output_ref``. A
    batch that produced several files still serves them one at a time --
    ``Staging.read_output`` reports the ambiguity instead of guessing.
    """
    data = dict(body.get("data") or {})
    results = data.get("results")
    if not isinstance(results, list):
        return body
    checked = []
    for entry in results:
        entry = dict(entry) if isinstance(entry, dict) else entry
        if (isinstance(entry, dict) and entry.get("ok")
                and entry.get("command") in OUTPUT_COMMANDS):
            payload = dict(entry.get("data") or {})
            staging.verify_output(job_id, payload.get("output_path"))
            payload.pop("output_path", None)
            payload["output_ref"] = job_id
            entry["data"] = payload
        checked.append(entry)
    data["results"] = checked
    return {"ok": True, "data": data}


def make_server(queue: JobQueue | None = None, port: int = PORT,
                host: str = HOST, staging: Staging | None = None) -> ThreadingHTTPServer:
    queue = queue or JobQueue()
    staging = staging or Staging(STAGING_DIR)
    token = os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN") or None

    class Handler(BaseHTTPRequestHandler):
        server_version = "lr-ops/0.1"

        def log_message(self, *args):  # keep stdout clean
            pass

        def _access(self, code: int):
            line = f"{time.strftime('%H:%M:%S')} {self.command} {self.path} -> {code}\n"
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                with (LOG_DIR / "access.log").open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                pass

        def _send(self, code: int, body: dict | None):
            self._access(code)
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _send_bytes(self, code: int, filename: str, data: bytes):
            """Serve a staged artifact.

            Always attachment + nosniff: these bytes are user photographs, and
            the bridge should never let a browser decide to render them inline
            as something other than what they are.
            """
            self._access(code)
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Staged-Filename", filename)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def _authorized(self) -> bool:
            if token is None:
                return True
            return self.headers.get("X-LR-XMP-Token") == token

        def do_DELETE(self):
            parts = self.path.split("?")[0].strip("/").split("/")
            if not self._authorized():
                self._send(403, {"error": "bad token"})
                return
            if len(parts) == 2 and parts[0] == "files":
                try:
                    staging.discard_input(parts[1])
                except StagingError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                self._send(200, {"ok": True})
                return
            self._send(404, {"error": "not found"})

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True, "queued": queue.queued_count(),
                                 "version": "0.1.0", "source_sha256": SOURCE_SHA256})
                return
            parts = self.path.split("?")[0].strip("/").split("/")
            if parts[0] == "files":
                self._get_file(parts)
                return
            if self.path == "/jobs/next":
                if not self._authorized():
                    self._send(403, {"error": "bad token"})
                    return
                try:
                    job = queue.next_queued()
                except QueuePersistenceError:
                    self._send(503, {"error": "job persistence unavailable"})
                    return
                if job is not None:
                    # Resolve caller handles into bridge-chosen local paths here,
                    # after dequeue and before the plugin sees the job. See
                    # staging.inject_dispatch for why the bridge, not the caller,
                    # decides where files live.
                    try:
                        job = {**job, "params": staging.inject_dispatch(
                            job["command"], job.get("params") or {}, job["job_id"])}
                    except StagingError as exc:
                        try:
                            queue.complete(job["job_id"], {"ok": False, "error": str(exc)})
                        except QueuePersistenceError:
                            self._send(503, {"error": "job persistence unavailable"})
                            return
                        self._send(200, queue.get(job["job_id"]))
                        return
                self._send(200, job)  # empty body when nothing queued
                return
            if self.path.startswith("/jobs/"):
                if not self._authorized():
                    self._send(403, {"error": "bad token"})
                    return
                job = queue.get(self.path.rsplit("/", 1)[-1])
                if job is None:
                    self._send(404, {"error": "unknown job"})
                else:
                    self._send(200, job)
                return
            self._send(404, {"error": "not found"})

        def _get_file(self, parts: list[str]):
            """Serve staged bytes.

            Files and job details use the same configured token. Both carry
            user data; health is the only unauthenticated diagnostic read.
            Without a configured token this remains a local development service.
            """
            if not self._authorized():
                self._send(403, {"error": "bad token"})
                return
            try:
                if len(parts) == 3 and parts[2] == "output":
                    name, data = staging.read_output(parts[1])
                    self._send_bytes(200, name, data)
                    return
                if len(parts) == 4 and parts[2] == "input":
                    path = staging.resolve_input(parts[1], parts[3])
                    self._send_bytes(200, path.name, path.read_bytes())
                    return
            except StagingError as exc:
                self._send(404, {"error": str(exc)})
                return
            self._send(404, {"error": "not found"})

        def _post_file(self):
            """Accept raw bytes and stage them, returning a stage id.

            The body is the file itself and the name rides in the query string --
            no multipart parsing here. The bridge only ever needs "these bytes,
            called this", and keeping the transport dumb means the file route has
            one job: put bytes on the disk Lightroom can see.
            """
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            name = ""
            for pair in query.split("&"):
                key, _, value = pair.partition("=")
                if key == "name":
                    name = urllib.parse.unquote_plus(value)
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._send(400, {"error": "empty upload"})
                return
            if length > MAX_UPLOAD_BYTES:
                self._send(413, {"error": f"upload exceeds {MAX_UPLOAD_BYTES} bytes"})
                return
            data = self.rfile.read(length)
            stage_id = staging.create_stage()
            stored = staging.put_input(stage_id, name, data)
            self._send(200, {"stage_id": stage_id, "name": stored.name, "bytes": len(data)})

        def do_POST(self):
            parts = self.path.split("?")[0]
            # Guard every POST that accepts a command, moves bytes, or mutates
            # service state. /jobs is the one that matters most: it is how
            # commands reach Lightroom, so leaving it open would make binding a
            # LAN address equivalent to publishing an unauthenticated write
            # channel. /files carries the user's photographs. Inert while no
            # token is configured, which keeps loopback development unchanged.
            if (parts in ("/jobs", "/echo", "/log") or parts == "/files"
                    or parts.startswith("/files/")) and not self._authorized():
                self._send(403, {"error": "bad token"})
                return
            if parts == "/files":
                self._post_file()
                return
            if parts == "/echo":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                self._send(200, {"echo": raw.decode("utf-8", "replace")})
                return
            if parts == "/log":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    LOG_DIR.mkdir(parents=True, exist_ok=True)
                    with (LOG_DIR / "plugin.log").open("a", encoding="utf-8") as fh:
                        fh.write(raw.rstrip("\n") + "\n")
                except OSError:
                    pass
                self._send(200, {"ok": True})
                return
            if self.path == "/jobs":
                try:
                    body = self._read_json()
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "invalid JSON"})
                    return
                command = body.get("command")
                if not isinstance(command, str) or not command:
                    self._send(400, {"error": "command required"})
                    return
                params = body.get("params") or {}
                if not isinstance(params, dict):
                    self._send(400, {"error": "params must be an object"})
                    return
                group = body.get("group")
                if group is not None and (not isinstance(group, str) or not group):
                    self._send(400, {"error": "group must be a non-empty string"})
                    return
                if command == BATCH_COMMAND:
                    problem = _validate_batch(params)
                    if problem:
                        self._send(400, {"error": problem})
                        return
                # Reclaim abandoned staging here: submitting a job is the one
                # moment we know a caller is active. Bytes are only ever
                # collected by an explicit GET, so nothing else would ever
                # trigger the sweep -- without this the staging dir grows
                # without bound. Best-effort: a sweep failure must not refuse
                # the caller's command.
                try:
                    staging.prune()
                except OSError:
                    pass
                try:
                    job_id = queue.submit(command, params, group=group)
                except QueuePersistenceError:
                    self._send(503, {"error": "job persistence unavailable"})
                    return
                self._send(200, {"job_id": job_id})
                return
            if self.path.startswith("/jobs/") and self.path.endswith("/result"):
                if not self._authorized():
                    self._send(403, {"error": "bad token"})
                    return
                job_id = self.path.split("/")[2]
                try:
                    body = self._read_json()
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "invalid JSON"})
                    return
                # For commands that produce a file, confirm the plugin wrote into
                # the directory this bridge picked before the job is allowed to
                # look successful. The path invariant lives here now, not on the
                # workbench -- see Staging.verify_output.
                job = queue.get(job_id)
                if job is not None and job.get("command") in OUTPUT_COMMANDS and body.get("ok"):
                    data = dict(body.get("data") or {})
                    reported = data.get("output_path")
                    try:
                        staging.verify_output(job_id, reported)
                    except StagingError as exc:
                        body = {"ok": False, "error": str(exc)}
                    else:
                        # Hand back a name, not a path. The path the plugin
                        # reported is meaningful only on the machine Lightroom
                        # runs on; publishing it would invite a remote caller to
                        # use it and be silently wrong. The caller asks for "the
                        # output of this job" via GET /files/<output_ref>/output.
                        data.pop("output_path", None)
                        data["output_ref"] = job_id
                        body = {"ok": True, "data": data}
                elif job is not None and job.get("command") == BATCH_COMMAND and body.get("ok"):
                    # The same path invariant applied per batch entry: any
                    # entry that produced a file must have written it into the
                    # job's staged output directory. The raw path is stripped
                    # for the same reason as above.
                    try:
                        body = _verify_batch_outputs(staging, job_id, body)
                    except StagingError as exc:
                        body = {"ok": False, "error": str(exc)}
                try:
                    completable = queue.complete(job_id, body)
                except QueuePersistenceError:
                    self._send(503, {"error": "job persistence unavailable"})
                    return
                if not completable:
                    self._send(409, {"error": "job not completable"})
                    return
                self._send(200, {"ok": True})
                return
            self._send(404, {"error": "not found"})

    server = ThreadingHTTPServer((host, port), Handler)
    server.job_queue = queue  # type: ignore[attr-defined]
    server.staging = staging  # type: ignore[attr-defined]
    return server


def run(port: int = PORT, host: str = HOST):
    server = make_server(port=port, host=host)
    print(f"lr-ops listening on http://{host}:{port} (Ctrl+C to stop)")
    if host not in ("127.0.0.1", "localhost"):
        if os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN"):
            print("  token required (LR_XMP_MIMIC_LOCAL_TOKEN is set)")
        else:
            print("  WARNING: bound to a non-loopback address with NO token set.")
            print("           Anyone who can reach this port can drive Lightroom.")
            print("           Set LR_XMP_MIMIC_LOCAL_TOKEN before exposing it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
