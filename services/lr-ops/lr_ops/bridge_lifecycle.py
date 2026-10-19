"""Bundled Bridge lifecycle. No installer dependency and no Lightroom writes.

The shared detached service outlives a client's stdio connection. Its journal
remains the authority for interrupted work; startup never replays a command.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .configuration import ConfigStore, active_store, process_environment, installed_runtime_root


class BridgeStartError(OSError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# Pin the package actually loaded, rather than resolving an active junction
# again after an installer swaps it to another generation.
PACKAGE_ROOT = Path(__file__).resolve().parent


def path_fingerprint(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()


def source_fingerprint(package_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def mac_process_identity(pid):
    """Match the existing macOS maintenance owner's creation identity format."""
    if sys.platform != "darwin":
        return None
    import ctypes
    class BsdInfo(ctypes.Structure):
        _fields_ = ([("field" + str(i), ctypes.c_uint32) for i in range(12)]
                    + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
                    + [("tail" + str(i), ctypes.c_uint32) for i in range(6)]
                    + [("seconds", ctypes.c_uint64), ("microseconds", ctypes.c_uint64)])
    try:
        query = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        info = BsdInfo()
        if query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            return None
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, check=True, timeout=2).stdout.strip()
        return {"created": f"darwin:{info.seconds}:{info.microseconds}", "command": command} if command else None
    except (OSError, subprocess.SubprocessError):
        return None


@contextmanager
def bridge_lock(path: Path, timeout: float = 5.0):
    """OS lock, released on process death; never infer ownership from a PID."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise BridgeStartError("bridge.lock_invalid: Bridge lock must not be a symlink")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        if os.name == "nt":
            import msvcrt
            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle, fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise BridgeStartError("bridge.busy: another Bridge owns this data root or startup") from exc
                time.sleep(0.025)
        try:
            if os.name == "nt" and os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            yield
        finally:
            release()


class BundledBridge:
    def __init__(self, data_root: Path, config: ConfigStore):
        self.data_root = Path(data_root).resolve()
        self.config = config
        self.managed = active_store()
        self.installed_root = installed_runtime_root(PACKAGE_ROOT.parents[2])
        self.expected_source = source_fingerprint(PACKAGE_ROOT)
        self.status: dict = {"mode": "builtin", "status": "not_started"}
        self._ensure_lock = threading.Lock()
        self._startup_lock = threading.Lock()
        self._startup_thread: threading.Thread | None = None

    def start_background(self, base: str, token: str | None = None):
        """Warm up an opted-in owner without delaying MCP tool discovery.

        External targets do not create a thread or touch the network. A builtin
        failure stays in ``status`` for health; operational calls still require
        their own successful ``ensure`` before submitting any HTTP request.
        """
        try:
            _, bridge = self._settings()
            endpoint = self._endpoint(base)
            if (bridge.get("mode", "external") == "external" or endpoint is None
                    or endpoint[0] not in ("127.0.0.1", "localhost", "::1")):
                self.status = {"mode": "external", "status": "client_managed"}
                return None
        except (OSError, ValueError) as exc:
            self._record_error(exc)
            return None

        def warm_up():
            try:
                self.ensure(base, token)
            except BridgeStartError:
                # ensure records a credential-safe diagnostic; the worker must
                # never write protocol bytes or turn a repair issue into EOF.
                pass

        with self._startup_lock:
            if self._startup_thread is not None and self._startup_thread.is_alive():
                return self._startup_thread
            self.status = {"mode": "builtin", "status": "starting"}
            self._startup_thread = threading.Thread(target=warm_up, daemon=True)
            self._startup_thread.start()
            return self._startup_thread

    def _settings(self):
        value = self.config.read()
        bridge = dict(value["bridge"])
        if self.managed is None:
            bridge["mode"] = os.environ.get("TONERELAY_BRIDGE_MODE", bridge.get("mode", "external"))
            bridge["host"] = os.environ.get("LR_XMP_MIMIC_BRIDGE_HOST", bridge["host"])
            bridge["port"] = int(os.environ.get("LR_XMP_MIMIC_BRIDGE_PORT", bridge["port"]))
        if bridge.get("mode", "external") not in ("builtin", "external"):
            raise BridgeStartError("bridge.invalid_mode: choose builtin or external")
        return value, bridge

    @staticmethod
    def _endpoint(base):
        url = urllib.parse.urlsplit(base)
        if url.scheme != "http" or url.path not in ("", "/") or url.query or url.fragment or url.username or url.password:
            return None
        return url.hostname, url.port or 80

    def _health(self, base):
        # A Bridge readiness check must never follow a redirect to another host.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(base.rstrip("/") + "/health", timeout=0.5) as response:
                value = json.loads(response.read(16384).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("health is not an object")
            return value
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _check(self, health, token, base):
        if (health.get("ok") is not True or health.get("source_sha256") != self.expected_source
                or health.get("data_root_sha256") != path_fingerprint(self.data_root)):
            raise BridgeStartError("bridge.identity_mismatch: occupied endpoint is not this bundled version/data root; use its owner to stop it or select external mode")
        if health.get("credential_configured") != bool(token):
            raise BridgeStartError("bridge.credential_mismatch: existing Bridge credentials differ; preserve and synchronize the configured credentials")
        req = urllib.request.Request(base.rstrip("/") + "/bridge/identity")
        if token:
            req.add_header("X-LR-XMP-Token", token)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(req, timeout=0.5) as response:
                valid = json.loads(response.read(4096).decode("utf-8")).get("ok") is True
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise BridgeStartError("bridge.credential_mismatch: existing Bridge rejected the configured credential")

    def ensure(self, base: str, token: str | None = None):
        try:
            return self._ensure(base, token)
        except (OSError, ValueError) as exc:
            reason = self._record_error(exc)
            raise BridgeStartError(reason) from exc

    def _record_error(self, exc):
        # Exception messages are our own fixed codes, never raw config or token.
        reason = str(exc) if isinstance(exc, BridgeStartError) else "bridge.start_failed: could not start bundled Bridge; inspect its local log"
        self.status = {"mode": "builtin", "status": "unavailable", "reason": reason}
        return reason

    def _ensure(self, base, token):
        value, bridge = self._settings()
        endpoint = self._endpoint(base)
        mode = bridge.get("mode", "external")
        # Remote or explicitly configured external targets are never managed here.
        if mode == "external" or endpoint is None or endpoint[0] not in ("127.0.0.1", "localhost", "::1"):
            self.status = {"mode": "external", "status": "client_managed"}
            return self.status
        # Startup warm-up and the first real request may race in one worker.
        # External mode never waits for this ownership lock. The shared OS
        # startup lock still coordinates different builtin MCP workers.
        with self._ensure_lock:
            return self._ensure_builtin(base, token, value, bridge, endpoint)

    def _ensure_builtin(self, base, token, value, bridge, endpoint):
        if (self.installed_root is not None
                and (self.installed_root / "active").resolve() / "source/services/lr-ops/lr_ops" != PACKAGE_ROOT):
            raise BridgeStartError("bridge.active_version_changed: refresh MCP before using or starting the bundled Bridge")
        if bridge["host"] not in ("127.0.0.1", "localhost") or endpoint[0] == "::1":
            raise BridgeStartError("bridge.invalid_bind: builtin Bridge requires IPv4 loopback")
        if endpoint[1] != bridge["port"]:
            raise BridgeStartError("bridge.endpoint_mismatch: local Target port differs from bridge.port; select external mode for an independent service")
        if self.managed and token != bridge["token"]:
            raise BridgeStartError("bridge.credential_mismatch: local Target and Bridge credentials differ")
        # Check the port before touching the journal. A different program is never
        # killed or treated as a working Bridge merely because its port is open.
        health = self._health(base)
        if health is not None:
            self._check(health, token, base)
            self.status = {"mode": "builtin", "status": "reused"}
            return self.status
        log_dir = self.data_root / "lr-ops"
        with bridge_lock(log_dir / ".startup.lock"):
            health = self._health(base)
            if health is not None:
                self._check(health, token, base)
                self.status = {"mode": "builtin", "status": "reused"}
                return self.status
            try:
                # A short connect timeout is not an occupied-port proof on
                # Windows (a closed port may take longer to reject a SYN).
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    if os.name == "nt":
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind((bridge["host"], bridge["port"]))
            except OSError as exc:
                raise BridgeStartError("bridge.port_occupied: endpoint does not answer Bridge health; no process was replaced") from exc
            env = dict(os.environ)
            if self.managed:
                env = process_environment(self.managed, env, value=value)
                logs = self.managed.paths(value)["logs"]
                working_dir = self.managed.paths(value)["run"]
            else:
                logs = self.data_root / "logs"
                working_dir = self.data_root
                if self.installed_root is not None:
                    logs = self.installed_root / "logs"
                    working_dir = self.installed_root / "run"
                env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"] = str(self.data_root)
                env["LR_XMP_MIMIC_LOCAL_TOKEN"] = token or ""
            # Works with 'python -m workbench_mcp' as well as installer launchers:
            # the Bridge uses the exact package bundled alongside this MCP.
            env["PYTHONPATH"] = os.pathsep.join([str(PACKAGE_ROOT.parent), env.get("PYTHONPATH", "")])
            env["PYTHONUTF8"] = "1"
            logs.mkdir(parents=True, exist_ok=True)
            working_dir.mkdir(parents=True, exist_ok=True)
            options = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                       if os.name == "nt" else {"start_new_session": True})
            command = [sys.executable, "-m", "lr_ops", "serve", "--host", bridge["host"], "--port", str(bridge["port"])]
            version_root = None
            if self.managed and sys.platform == "darwin":
                candidate = (self.managed.root / "active").resolve()
                python = candidate / ".venv/bin/python"
                if python.is_file() and candidate in PACKAGE_ROOT.parents:
                    version_root = candidate
                    command[0] = str(python)
            with (logs / "lr-ops.log").open("ab") as output:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=subprocess.STDOUT, env=env, cwd=working_dir, **options)
            # Retain/reap our child handle while the MCP lives. This daemon does
            # not stop the service or delay stdio shutdown.
            threading.Thread(target=process.wait, daemon=True).start()
            # Informational only. Never used as authority for process termination.
            ConfigStore._atomic_write(log_dir / "builtin-process.json", {
                "pid": process.pid, "command": command, "source_sha256": self.expected_source,
                "data_root_sha256": path_fingerprint(self.data_root), "started_at": time.time(),
            })
            # Preserve the original Windows maintenance PID interface when
            # running through an installed launcher. This is an ownership hint,
            # not permission to kill a PID without checking the actual process.
            if os.name == "nt" and self.installed_root is not None:
                pid_path = self.installed_root / "run" / "lr-ops.pid"
                pid_path.parent.mkdir(parents=True, exist_ok=True)
                pid_path.write_text(str(process.pid), encoding="utf-8")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                health = self._health(base)
                if health is not None:
                    self._check(health, token, base)
                    if version_root is not None:
                        ConfigStore._atomic_write(self.managed.paths(value)["run"] / "lr-ops.pid", {
                            "pid": process.pid, "identity": mac_process_identity(process.pid),
                            "runtime_root": str(self.managed.root), "version_root": str(version_root),
                            "base": base, "command": command,
                        })
                    self.status = {"mode": "builtin", "status": "started"}
                    return self.status
                if process.poll() is not None:
                    raise BridgeStartError("bridge.start_failed: bundled Bridge exited; inspect its local log")
                time.sleep(0.05)
            raise BridgeStartError("bridge.start_timeout: inspect the existing process before retrying; no process was killed")
