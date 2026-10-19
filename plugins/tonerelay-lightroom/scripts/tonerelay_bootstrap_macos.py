#!/usr/bin/env python3
"""Install, inspect and recover ToneRelay on macOS.

The macOS entry owns version directories and shell launchers, preserves selected
Runtime data, checks Bridge source/process identity, and manages its own stable
Lightroom Modules link. Lightroom owns Lua loading; the MCP client owns stdio.
The CLI refuses other hosts. Windows uses the separately maintained original
``tonerelay_bootstrap.py``; this file contains no Windows implementation.
"""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from functools import wraps
from pathlib import Path
from typing import Any

# Setup/removal must not create new cache files while inspecting an installation.
sys.dont_write_bytecode = True


# The installer is standalone by design: it runs before the Runtime exists, so it
# cannot import workbench.lrops. Keep this equal to that module's DEFAULT_BASE.
DEFAULT_BRIDGE_BASE = "http://127.0.0.1:17777"
BRIDGE_START_TIMEOUT_SECONDS = 3.0
BRIDGE_PROBE_TIMEOUT_SECONDS = 3.0
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUNTIME_SOURCE_DIRECTORIES = (
    "apps/workbench",
    "apps/workbench-mcp",
    "services/lr-ops",
    "plugins/lightroom",
    "plugins/tonerelay-lightroom/scripts",
    "prompts",
)
RUNTIME_SOURCE_FILES = ("apps/workbench/requirements.txt", "services/lr-ops/lr_ops/configuration.py")
RUNTIME_PYTHONPATH_ENTRIES = (
    "apps/workbench-mcp",
    "apps/workbench",
    "services/lr-ops",
)
SCHEME_DEFAULT_PORTS = {"http": 80, "https": 443}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


# The shared configuration module is standard-library only and is loaded from
# the verified bundle before dependencies exist. Do not write into source input.
_config_path = Path(__file__).resolve().parents[3] / "services/lr-ops/lr_ops/configuration.py"
if not _config_path.is_file():
    _config_path = Path(__file__).with_name("configuration.py")
_config_spec = importlib.util.spec_from_file_location("tonerelay_install_config", _config_path)
configuration = importlib.util.module_from_spec(_config_spec)
_old_bytecode = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    _config_spec.loader.exec_module(configuration)
finally:
    sys.dont_write_bytecode = _old_bytecode
InstallError = configuration.ConfigError


# ---------------------------------------------------------------- locations


def default_runtime_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "ToneRelay"

def fallback_runtime_root() -> Path:
    return default_runtime_root()

def config_store(runtime_root: Path):
    return configuration.ConfigStore(runtime_root / "config.json")


def known_installation(runtime_root: Path):
    for path in (runtime_root / "install-manifest.json", runtime_root.parent / "install-manifest.json"):
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("schema") == "tonerelay-install-manifest-v1" and record.get("runtime_root") == str(runtime_root.resolve()):
                return record
    return None


def data_root(runtime_root: Path) -> Path:
    if config_store(runtime_root).path.is_file():
        return config_store(runtime_root).paths()["data"]
    # Only a recognized legacy ledger opts into old selection semantics.
    legacy_installation = known_installation(runtime_root)
    if legacy_installation and legacy_installation.get("configuration"):
        raise InstallError("persisted config.json is missing; restore a verified backup")
    if not legacy_installation:
        return config_store(runtime_root).paths()["data"]
    explicit = os.environ.get("LR_XMP_MIMIC_RUNTIME_DATA_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    selection = runtime_root / "data-selection.json"
    if selection.exists():
        try:
            saved = json.loads(selection.read_text(encoding="utf-8"))["data_root"]
            if not isinstance(saved, str) or not Path(saved).is_absolute():
                raise ValueError("data_root must be an absolute path")
            return Path(saved)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise InstallError(f"cannot read {selection}; restore the saved data choice: {exc}") from exc
    for manifest in (runtime_root.parent / "install-manifest.json", runtime_root / "install-manifest.json"):
        if manifest.is_file():
            record = json.loads(manifest.read_text(encoding="utf-8"))
            saved = record.get("selected_data_root")
            if record.get("runtime_root") == str(runtime_root.resolve()) and saved:
                if not isinstance(saved, str) or not Path(saved).is_absolute():
                    raise InstallError("Manifest data root must be an absolute path")
                return Path(saved)
    local = runtime_root / "data"
    legacy = Path.home() / "lr-xmp-mimic-runtime-data"
    grouped = runtime_root.parent / "data" if (runtime_root.parent / "install-manifest.json").is_file() else local
    for candidate in (local, grouped, legacy):
        if candidate.is_dir() and any(candidate.iterdir()):
            return candidate.resolve()
    return grouped.resolve()

def remember_data_root(runtime_root: Path) -> Path:
    if config_store(runtime_root).path.is_file():
        return data_root(runtime_root)
    # Existing legacy installations retain their selection; fresh installs are
    # initialized by prepare_installation, never migrated implicitly here.
    selected = data_root(runtime_root)
    configuration.ConfigStore._atomic_write(runtime_root / "data-selection.json", {"data_root": str(selected)})
    return selected

def validate_install_root(runtime_root: Path) -> None:
    root = runtime_root.resolve()
    if root == root.parent or root == Path.home().resolve() or runtime_root.is_symlink():
        raise InstallError("choose a dedicated ToneRelay installation directory")
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise InstallError("an existing development checkout cannot be an installation root")
    if root.exists() and not (root / "managed-root.json").exists():
        allowed = {*configuration.PATH_NAMES, "config.json", ".config.lock", ".lifecycle.lock", "install-manifest.json"}
        if any(child.name not in allowed for child in root.iterdir()):
            raise InstallError("installation root contains unowned files; choose a dedicated directory")


def read_installation(runtime_root: Path, *, required=False):
    return lifecycle().Ledger(runtime_root, create=not required).record


def record_configuration_paths(ledger):
    store = config_store(ledger.runtime)
    if not store.path.is_file():
        return
    paths = store.paths()
    previous = str(ledger.run_directory())
    if previous != str(paths["run"]):
        history = ledger.record.setdefault("prior_run_directories", [])
        if previous not in history:
            history.append(previous)
    ledger.record["configuration"] = str(store.path)
    ledger.record["run_directory"] = str(paths["run"])
    ledger.record["backup_directory"] = str(paths["backups"])
    retained = ledger.record.setdefault("retained_paths", [])
    for path in (store.path, paths["data"], paths["backups"], ledger.runtime / ".config.lock"):
        if str(path) not in retained:
            retained.append(str(path))
    for path in paths.values():
        ledger.remember(path)
    if str(paths["data"]) not in ledger.record["data_roots"]:
        ledger.record["data_roots"].append(str(paths["data"]))
    ledger.save()


def record_installation(runtime_root: Path, *, source=None, version=None, stage=None, external=None):
    """Add installation evidence to the same exact-file ledger used for removal."""
    ledger = lifecycle().Ledger(runtime_root, create=True)
    record_configuration_paths(ledger)
    ledger.record["shared_interpreter"] = {"path": str(Path(sys.executable).resolve()), "owned": False}
    if source is not None:
        ledger.record["source"] = {"path": str(Path(source).resolve()), "version": version,
                                  "owned": False}
    if external is not None:
        entries = ledger.record.setdefault("external", [])
        entries[:] = [entry for entry in entries if (entry.get("kind"), entry.get("path")) != (external["kind"], external["path"])]
        entries.append(external)
    ledger.install_files([runtime_root / "managed-root.json", runtime_root / "dist.json",
                          runtime_root / "active", launcher_path(runtime_root), control_path(runtime_root)])
    if stage:
        ledger.checkpoint(stage, data_root(runtime_root))
    else:
        ledger.save()
    return ledger.record


def prepare_installation(runtime_root: Path):
    validate_install_root(runtime_root)
    store = config_store(runtime_root)
    existing = known_installation(runtime_root)
    if not store.path.is_file() and existing:
        if existing.get("configuration"):
            raise InstallError("persisted config.json is missing; restore a verified backup")
        if existing.get("sources") or existing.get("data_roots") or existing.get("phase") != "prepared":
            raise InstallError("legacy installation retained; unified configuration requires an explicitly chosen fresh installation, not implicit migration")
    store.initialize()
    for path in store.paths().values():
        path.mkdir(parents=True, exist_ok=True)
    remember_managed_root(runtime_root)
    record_installation(runtime_root, stage="prepared")
    return store


def _scoped_process(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        inherited = os.environ.copy()
        temporary, bytecode = tempfile.tempdir, sys.pycache_prefix
        cwd = Path.cwd()
        try:
            return method(*args, **kwargs)
        finally:
            os.environ.clear()
            os.environ.update(inherited)
            tempfile.tempdir, sys.pycache_prefix = temporary, bytecode
            if cwd.is_dir():
                os.chdir(cwd)
    return wrapped


def dist_version(runtime_root: Path) -> int:
    """Monotonic install counter, so 'build' can tell a rebuild from a no-op."""
    path = runtime_root / "dist.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("install_seq")
    except (OSError, ValueError, AttributeError):
        value = None
    return value if isinstance(value, int) and value >= 0 else 0


def bump_dist_version(runtime_root: Path) -> int:
    sequence = dist_version(runtime_root) + 1
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / "dist.json").write_text(
        json.dumps({"install_seq": sequence}, sort_keys=True) + "\n", encoding="utf-8")
    return sequence


def bundled_source_root() -> Path:
    """Locate the checked-out Runtime that contains this bootstrap script."""
    return Path(__file__).resolve().parents[3]


def validate_source_root(source_root: Path) -> Path:
    source = source_root.expanduser().resolve()
    if not source.is_dir():
        raise InstallError(f"source root does not exist: {source}")
    missing = [path for path in (*RUNTIME_SOURCE_DIRECTORIES, *RUNTIME_SOURCE_FILES)
               if not (source / path).exists()]
    if missing:
        raise InstallError("source bundle is incomplete: " + ", ".join(missing))
    return source


# ---------------------------------------------------------------- runtime


def copy_runtime_source(source_root: Path, destination: Path) -> None:
    """Copy only Runtime material; never bring Git state or private docs along."""
    for relative in RUNTIME_SOURCE_DIRECTORIES:
        source = source_root / relative
        target = destination / relative
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "*.pyc", ".DS_Store"))
    for relative in RUNTIME_SOURCE_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, target)


def create_virtualenv(version_root: Path) -> None:
    subprocess.run([sys.executable, "-m", "venv", "--symlinks", str(version_root / ".venv")],
                   env=runtime_env(version_root), cwd=version_root.parent.parent, check=True)


def venv_python(version_root: Path) -> Path:
    return version_root / ".venv" / "bin" / "python"

def install_dependencies(version_root: Path) -> None:
    python = venv_python(version_root)
    requirements = version_root / "source" / "apps" / "workbench" / "requirements.txt"
    subprocess.run([
        str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
        "-r", str(requirements),
    ], env=runtime_env(version_root), cwd=version_root.parent.parent, check=True)


def runtime_pythonpath(source: Path) -> str:
    return os.pathsep.join(
        str(source / relative) for relative in RUNTIME_PYTHONPATH_ENTRIES)


def runtime_env(version_root: Path) -> dict[str, str]:
    root = version_root.parent.parent
    if config_store(root).path.is_file():
        env = configuration.process_environment(config_store(root))
    else:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"] = str(data_root(root))
    env["PYTHONPATH"] = runtime_pythonpath(version_root / "source")
    return env

def link_present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _remove_dir_link(link: Path) -> None:
    """Remove the managed symlink itself; never descend into its target."""
    link.unlink()

def _create_dir_link(link: Path, target: Path) -> None:
    link.symlink_to(target, target_is_directory=True)

def _point_at(link: Path, target: Path) -> None:
    """Prepare a validated replacement and retain the previous link until success."""
    link.parent.mkdir(parents=True, exist_ok=True)
    candidate = link.with_name("." + link.name + "-next")
    backup = link.with_name("." + link.name + "-previous")
    if link_present(backup):
        raise InstallError(f"previous link recovery is pending at {backup}; inspect it before retrying")
    if link_present(candidate):
        _remove_dir_link(candidate)
    had_previous = link_present(link)
    previous_saved = False
    try:
        _create_dir_link(candidate, target)
        if candidate.resolve(strict=True) != target.resolve(strict=True):
            raise InstallError("candidate link did not resolve to the requested target")
        if had_previous:
            os.replace(link, backup)
            previous_saved = True
        os.replace(candidate, link)
    except (OSError, InstallError) as exc:
        if previous_saved:
            try:
                os.replace(backup, link)
            except OSError as restore_error:
                raise InstallError(f"activation failed: {exc}; previous link remains at {backup}; "
                                   f"restore failed: {restore_error}") from exc
        raise InstallError(f"activation failed; previous link retained: {exc}") from exc
    finally:
        if link_present(candidate):
            _remove_dir_link(candidate)
    if previous_saved:
        _remove_dir_link(backup)


def link_runtime_plugin(staging: Path) -> None:
    # Relative to its version: staging is renamed after dependency installation.
    (staging / "lightroom").symlink_to(Path("source") / "plugins" / "lightroom")

def activate_runtime(runtime_root: Path, version_root: Path) -> None:
    active = runtime_root / "active"
    temporary = runtime_root / ".active-next"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(version_root)
    os.replace(temporary, active)


def runtime_version_root(runtime_root: Path, version: str) -> Path:
    if not VERSION_PATTERN.fullmatch(version):
        raise InstallError("version must use only letters, numbers, dots, underscores, or dashes")
    return runtime_root / "versions" / version


def active_root(runtime_root: Path) -> Path | None:
    active = runtime_root / "active"
    try:
        resolved = active.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def launcher_path(runtime_root: Path) -> Path:
    return runtime_root / "bin" / "tonerelay-mcp"

def control_path(runtime_root: Path) -> Path:
    return runtime_root / "bin" / "tonerelay"

def control_command(runtime_root: Path) -> str:
    return shlex.quote(str(control_path(runtime_root)))

def bridge_pid_path(runtime_root: Path) -> Path:
    return config_store(runtime_root).paths()["run"] / "lr-ops.pid"


def log_path(runtime_root: Path, name: str) -> Path:
    return config_store(runtime_root).paths()["logs"] / name


# ---------------------------------------------------------------- bridge


def bridge_base() -> str:
    return os.environ.get("LROPS_BASE") or DEFAULT_BRIDGE_BASE


def bridge_endpoint(base: str | None = None) -> tuple[str, int]:
    """Host and port urllib would use for the configured bridge base."""
    raw = base or bridge_base()
    parsed = urllib.parse.urlsplit(raw if "//" in raw else f"//{raw}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or SCHEME_DEFAULT_PORTS.get(parsed.scheme, 80)
    return host, port


def bridge_is_local(base: str | None = None) -> bool:
    return bridge_endpoint(base)[0].lower() in LOOPBACK_HOSTS


def bridge_reachable(base: str | None = None) -> bool:
    host, port = bridge_endpoint(base)
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def bridge_record(runtime_root: Path) -> dict[str, Any] | None:
    try:
        text = bridge_pid_path(runtime_root).read_text(encoding="utf-8").strip()
        value = json.loads(text)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return {"pid": value}  # Legacy record: readable, never sufficient to kill.
        if isinstance(value, dict) and isinstance(value.get("pid"), int) and value["pid"] > 0:
            return value
    except (OSError, ValueError):
        pass
    return None


def bridge_pid(runtime_root: Path) -> int | None:
    record = bridge_record(runtime_root)
    return record["pid"] if record else None


def process_identity(pid: int) -> dict[str, str] | None:
    """Read an OS creation identity and executable/command; unknown fails closed."""
    try:
        created = posix_creation_identity(pid)
        if created is None:
            return None
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                capture_output=True, text=True, check=True)
        command = result.stdout.strip()
        return {"created": created, "command": command} if command else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def process_executable(pid: int) -> str | None:
    """Read the executable separately so shell command text cannot impersonate a service."""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                                capture_output=True, text=True, check=True, timeout=3)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def posix_creation_identity(pid: int) -> str | None:
    if sys.platform == "darwin":
        import ctypes
        # sys/proc_info.h: PROC_PIDTBSDINFO, including microsecond start time.
        class BsdInfo(ctypes.Structure):
            _fields_ = ([("field" + str(i), ctypes.c_uint32) for i in range(12)]
                        + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
                        + [("tail" + str(i), ctypes.c_uint32) for i in range(6)]
                        + [("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64)])
        api = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        query = api.proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        info = BsdInfo()
        if query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            return None
        return f"darwin:{info.start_seconds}:{info.start_microseconds}"
    return None  # Unknown creation identity must not authorize termination.


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process(pid: int, expected_identity: dict[str, str] | None = None) -> bool:
    """Stop only a process matching the caller's validated creation identity."""
    if not expected_identity:
        return False
    if process_identity(pid) != expected_identity:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def start_lr_ops(runtime_root: Path, version_root: Path) -> bool:
    """Start the loopback bridge detached and remember the pid we created."""
    if bridge_reachable():
        return bridge_build_status(version_root)["matches_active_source"]
    if not bridge_is_local():
        return False
    runtime_root = runtime_root.resolve()
    version_root = version_root.resolve()
    python = venv_python(version_root)
    if not python.is_file():
        return False
    host, port = bridge_endpoint()
    bridge = config_store(runtime_root).read()["bridge"]
    if port != bridge["port"]:
        raise InstallError("active local target port must match config.json bridge.port")
    host = bridge["host"]
    logs = config_store(runtime_root).paths()["logs"]
    logs.mkdir(parents=True, exist_ok=True)
    config_store(runtime_root).paths()["run"].mkdir(parents=True, exist_ok=True)
    log_file = log_path(runtime_root, "lr-ops.log").open("ab")
    process = subprocess.Popen(
        [str(python), "-m", "lr_ops", "serve", "--host", host, "--port", str(port)],
        cwd=config_store(runtime_root).paths()["run"],
        env=runtime_env(version_root),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    bridge_pid_path(runtime_root).parent.mkdir(parents=True, exist_ok=True)
    log_file.close()
    identity = process_identity(process.pid)
    def save_identity(value):
        record_next = bridge_pid_path(runtime_root).with_suffix(".next")
        record_next.write_text(json.dumps({
            "pid": process.pid, "identity": value,
            "runtime_root": str(runtime_root.resolve()),
            "version_root": str(version_root.resolve()),
            "base": bridge_base(),
            "command": [str(python), "-m", "lr_ops", "serve", "--host", host, "--port", str(port)],
        }, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(record_next, bridge_pid_path(runtime_root))
        ledger = lifecycle().Ledger(runtime_root, create=True)
        ledger.install_files([bridge_pid_path(runtime_root), log_path(runtime_root, "lr-ops.log")])
    save_identity(identity)
    deadline = time.monotonic() + BRIDGE_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if bridge_build_status(version_root)["matches_active_source"]:
            # Framework Python can re-exec within the same OS process during startup.
            # Retain creation identity and capture its final command after readiness.
            ready_identity = process_identity(process.pid)
            if (ready_identity and (not identity or ready_identity.get("created") == identity.get("created"))
                    and process.poll() is None):
                save_identity(ready_identity)
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------- probing


def lightroom_plugin_path(runtime_root: Path) -> Path:
    """Return the stable registration path that follows the active Runtime."""
    return runtime_root / "active" / "lightroom" / "lr-xmp-mimic.lrdevplugin"


def expected_plugin_build(version_root: Path) -> str | None:
    """Read the build identifier bundled with this Runtime, never from LR state."""
    commands = (version_root / "source" / "plugins" / "lightroom" /
                "lr-xmp-mimic.lrdevplugin" / "Commands.lua")
    try:
        match = re.search(r"build_id\s*=\s*['\"]([^'\"]+)['\"]",
                          commands.read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def supports_catalog_confirmation(version_root: Path) -> bool:
    """Check that this MCP Runtime can bind the first confirmed Catalog."""
    server = version_root / "source" / "apps" / "workbench-mcp" / "workbench_mcp" / "server.py"
    runtime = version_root / "source" / "apps" / "workbench" / "workbench" / "runtime.py"
    try:
        server_text = server.read_text(encoding="utf-8")
        runtime_text = runtime.read_text(encoding="utf-8")
    except OSError:
        return False
    return ('"name": "trlr_confirm_catalog"' in server_text
            and "def confirm_catalog(" in runtime_text)


def _lr_ops_request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        bridge_base().rstrip("/") + path if "://" in bridge_base()
        else "http://" + bridge_base().rstrip("/") + path,
        data=payload, method=method)
    request.add_header("Content-Type", "application/json")
    token = os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN")
    if token:
        request.add_header("X-LR-XMP-Token", token)
    with urllib.request.urlopen(request, timeout=1.0) as response:
        raw = response.read()
    value = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(value, dict):
        raise ValueError("lr-ops returned a non-object response")
    return value


def probe_loaded_plugin() -> dict[str, Any]:
    """Read the live plugin contract through lr-ops without changing a Catalog."""
    try:
        job_id = _lr_ops_request("POST", "/jobs", {
            "command": "probe_client", "params": {},
        }).get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return {"error": "lr-ops did not return a probe job id"}
        deadline = time.monotonic() + BRIDGE_PROBE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            job = _lr_ops_request("GET", f"/jobs/{job_id}")
            if job.get("status") in ("done", "failed", "interrupted"):
                result = job.get("result") or {}
                if not result.get("ok"):
                    return {"error": str(result.get("error") or "plugin probe failed")}
                data = result.get("data")
                return data if isinstance(data, dict) else {
                    "error": "plugin probe returned invalid data"}
            time.sleep(0.1)
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return {"error": str(exc)}
    return {"error": "plugin did not answer probe_client before timeout"}


def bridge_source_fingerprint(package_root: Path) -> str | None:
    """Match lr_ops.server.source_fingerprint without importing Runtime code."""
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


def bridge_build_status(version_root: Path) -> dict[str, Any]:
    expected = bridge_source_fingerprint(
        version_root / "source" / "services" / "lr-ops" / "lr_ops")
    try:
        health = _lr_ops_request("GET", "/health")
        loaded = health.get("source_sha256")
        if not isinstance(loaded, str) or not re.fullmatch(r"[0-9a-f]{64}", loaded):
            loaded = None
        return {
            "expected_source_sha256": expected,
            "loaded_source_sha256": loaded,
            "matches_active_source": bool(
                health.get("ok") is True and expected and loaded == expected),
        }
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {"expected_source_sha256": expected, "loaded_source_sha256": None,
                "matches_active_source": False, "error": str(exc)}


def plugin_bundle(runtime_root: Path) -> Path | None:
    """The bundle as reached through the stable ``active`` link.

    Registering or linking the resolved version path would pin Lightroom to one
    version: after an upgrade it would keep loading the old bundle. Everything
    that names a path for a human or for Lightroom uses this stable form.
    """
    stable = runtime_root / "active" / "lightroom" / "lr-xmp-mimic.lrdevplugin"
    try:
        resolved = stable.resolve(strict=True)
    except OSError:
        return None
    return stable if resolved.is_dir() else None


def lightroom_modules_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Adobe" / "Lightroom" / "Modules"

def lightroom_entry(runtime_root: Path) -> Path:
    return lightroom_modules_dir() / "lr-xmp-mimic.lrdevplugin"


def component_report(runtime_root: Path) -> dict[str, Any]:
    """What exists right now, per component, with the command that changes it."""
    active = active_root(runtime_root)
    launcher = launcher_path(runtime_root)
    control = control_command(runtime_root)
    bundle = plugin_bundle(runtime_root)
    entry = lightroom_entry(runtime_root)
    pid = bridge_pid(runtime_root)
    try:
        pid_live = bool(pid and process_alive(pid))
        process_error = None
    except OSError as exc:
        pid_live = None
        process_error = str(exc)
    return {
        "runtime": {
            "present": active is not None,
            "runtime_root": str(runtime_root),
            "data_root": str(data_root(runtime_root)),
            "active_version": active.name if active else None,
            "install_seq": dist_version(runtime_root),
            "interpreter": str(venv_python(active)) if active else None,
            "interpreter_present": bool(active and venv_python(active).is_file()),
            "dependencies": "unverified; run mcp start in the installed environment",
            "configuration_recorded": (runtime_root / "config.json").is_file(),
            "configuration": str(runtime_root / "config.json"),
            "paths": {key: str(path) for key, path in config_store(runtime_root).paths().items()},
            "next": (f"{str(control)} mcp start" if active else
                     f"{str(control)} runtime install --source-root <verified-checkout> --version <fixed-label>"),
        },
        "mcp": {
            "present": launcher.is_file(),
            "launcher": str(launcher),
            "next": None if launcher.is_file() else f"{control} runtime install ...",
        },
        "bridge": {
            "listening": bridge_reachable(),
            "base": bridge_base(),
            "local": bridge_is_local(),
            "pid": pid if pid_live else None,
            "process_state_known": pid_live is not None,
            "process_error": process_error,
            "next": None if bridge_reachable() else f"{control} bridge start",
        },
        "lightroom_plugin": {
            "bundle_present": bundle is not None,
            "bundle_path": str(bundle) if bundle else None,
            "bundle_target": str(bundle.resolve()) if bundle else None,
            "registered_entry": str(entry) if link_present(entry) else None,
            "entry_target": str(entry.resolve()) if link_present(entry) else None,
            "modules_dir": str(lightroom_modules_dir()),
            "next": (f"{control} lightroom-plugin install"
                     if bundle and not (link_present(entry)) else None),
        },
    }


def status(runtime_root: Path) -> dict[str, Any]:
    active = active_root(runtime_root)
    launcher = launcher_path(runtime_root)
    plugin = plugin_bundle(runtime_root)
    launcher_present = launcher.is_file() and os.access(launcher, os.X_OK)
    checks = {
        "runtime_active": active is not None,
        "launcher_present": launcher_present,
        "configuration_present": (runtime_root / "config.json").is_file(),
        "lr_ops_reachable": bridge_reachable(),
        "lightroom_plugin_bundle_present": bool(plugin and plugin.is_dir()),
        "catalog_confirmation_supported": bool(
            active and supports_catalog_confirmation(active)),
    }
    bridge_build = bridge_build_status(active) if active and checks["lr_ops_reachable"] else None
    checks["lr_ops_build_matches_active"] = bool(
        bridge_build and bridge_build["matches_active_source"])
    expected_build = expected_plugin_build(active) if active else None
    probe = probe_loaded_plugin() if checks["lr_ops_build_matches_active"] and checks["configuration_present"] else None
    capabilities = probe.get("capabilities") if isinstance(probe, dict) else None
    loaded_build = probe.get("build_id") if isinstance(probe, dict) else None
    checks["lightroom_plugin_loaded"] = (
        isinstance(probe, dict)
        and not probe.get("error")
        and expected_build is not None
        and loaded_build == expected_build
        and isinstance(capabilities, dict)
        and capabilities.get("current_folder_selection") is True
    )
    ready = all(checks.values())
    diagnostic = None
    if not ready:
        runtime_ready = (checks["runtime_active"] and checks["launcher_present"]
                         and checks["lightroom_plugin_bundle_present"])
        if runtime_ready and not checks["configuration_present"]:
            diagnostic = {
                "code": "bootstrap.configuration_missing", "safe_to_retry": False,
                "next": "config.json is missing; restore a verified configuration or prepare a fresh installation.",
            }
        elif runtime_ready and not checks["catalog_confirmation_supported"]:
            diagnostic = {
                "code": "bootstrap.runtime_upgrade_required",
                "safe_to_retry": True,
                "next": "Install a Runtime build with trlr_confirm_catalog support. Do not edit the Target mode or Catalog identity by hand.",
            }
        elif runtime_ready and checks["lr_ops_reachable"] and not checks["lr_ops_build_matches_active"]:
            diagnostic = {
                "code": "bootstrap.bridge_restart_required",
                "safe_to_retry": False,
                "next": "The running Bridge build is old or unverified. Finish or investigate outstanding jobs, stop the existing Bridge through its owner, then run repair for the active Runtime. Reconnect MCP and verify again; do not replay unknown writes.",
            }
        elif runtime_ready and checks["lr_ops_reachable"] and (
                not isinstance(probe, dict) or probe.get("error")
                or not isinstance(loaded_build, str) or not loaded_build
                or not isinstance(capabilities, dict)):
            diagnostic = {
                "code": "bootstrap.plugin_probe_unavailable",
                "safe_to_retry": False,
                "expected_build": expected_build,
                "loaded_build": loaded_build,
                "probe_error": (probe.get("error") if isinstance(probe, dict) else None)
                    or "plugin probe did not return a usable build and capabilities",
                "next": "The running Bridge matches the active source, but Lightroom did not return a usable plugin probe. Unlock the desktop session if locked, verify Lightroom is responsive and ToneRelay is enabled, and investigate outstanding jobs before retrying the read-only status check. Do not infer missing or outdated registration from an unanswered probe, add duplicate plugins, rebind a Catalog, or restart an application with unknown jobs.",
            }
        elif runtime_ready and checks["lr_ops_reachable"]:
            diagnostic = {
                "code": "bootstrap.plugin_reload_required",
                "safe_to_retry": False,
                "expected_build": expected_build,
                "loaded_build": loaded_build,
                "probe_error": probe.get("error") if isinstance(probe, dict) else None,
                "lightroom_plugin": str(plugin),
                "resolved_lightroom_plugin": str(plugin.resolve()),
                "next": "Check ToneRelay's registered path in File > Plug-in Manager against lightroom_plugin or resolved_lightroom_plugin. If it points to an older version, register the current bundle; do not add a duplicate when the path already matches. Save work, quit and reopen Lightroom, then verify the loaded build again. Updating the active link alone does not prove Lightroom's registration changed.",
            }
        elif runtime_ready:
            diagnostic = {
                "code": "bootstrap.bridge_unavailable",
                "safe_to_retry": True,
                "next": f"Run repair with {control_command(runtime_root)} repair to start the Bridge, then verify again. An unreachable Bridge cannot establish whether Lightroom registered the plugin.",
            }
        else:
            diagnostic = {
                "code": "bootstrap.runtime_incomplete",
                "safe_to_retry": True,
                "next": "Run tonerelay_bootstrap_macos.py repair or install.",
            }
    components = component_report(runtime_root)
    components["bridge"]["build"] = bridge_build
    components["bridge"]["matches_active_source"] = checks["lr_ops_build_matches_active"]
    if checks["lr_ops_reachable"] and not checks["lr_ops_build_matches_active"]:
        components["bridge"]["next"] = "Investigate outstanding jobs, stop through the Bridge owner, then repair."
    plugin_build = {"expected_build": expected_build, "loaded_build": loaded_build, "probe": probe}
    components["lightroom_plugin"].update({
        "loaded": checks["lightroom_plugin_loaded"], "expected_build": expected_build,
        "loaded_build": loaded_build, "probe": probe,
        "load_status": "matched" if checks["lightroom_plugin_loaded"] else
                       "unknown" if not loaded_build else "mismatch",
    })
    components["mcp"]["client_session"] = "unverified"
    components["mcp"]["next"] = "Use a new target-client session to call config, health and Catalog discovery/confirmation."
    return {
        "status": "ready" if ready else "repair_needed",
        "installation_complete": False,
        "acceptance_pending": "Target client's new session, configuration tutorial, health and explicit Catalog confirmation",
        "runtime_version": active.name if active else None,
        "checks": checks,
        "bridge_build": bridge_build,
        "plugin_build": plugin_build,
        "command_shell": "POSIX sh",
        "components": components,
        "diagnostic": diagnostic,
    }


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


# ---------------------------------------------------------------- bin scripts


def mcp_server_config(runtime_root: Path) -> dict[str, Any]:
    if not config_store(runtime_root).path.is_file() and (runtime_root / "data-selection.json").is_file():
        return {"tonerelay-lightroom": {"command": str(launcher_path(runtime_root)), "args": [],
                "env": {"LR_XMP_MIMIC_RUNTIME_DATA_ROOT": str(data_root(runtime_root)),
                        "LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES": os.environ.get("LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES", "1")}}}
    return {"tonerelay-lightroom": {
        "command": str(launcher_path(runtime_root)), "args": [],
        "env": {"TONERELAY_CONFIG": str(runtime_root / "config.json"),
                "LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES": os.environ.get("LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES", "1")},
    }}


def _write_script(path: Path, lines: list[str]) -> None:
    path.write_bytes("\n".join(lines + [""]).encode("utf-8"))
    path.chmod(0o755)

def write_bin_scripts(runtime_root: Path) -> None:
    if not config_store(runtime_root).path.is_file():
        return _write_legacy_bin_scripts(runtime_root)
    (runtime_root / "bin").mkdir(parents=True, exist_ok=True)
    shared = ["#!/bin/sh", "set -eu", f"ROOT={shlex.quote(str(runtime_root))}",
              'export TONERELAY_CONFIG="$ROOT/config.json"',
              'export PYTHONDONTWRITEBYTECODE=1',
              f"export PYTHONPATH={shlex.quote(runtime_pythonpath(runtime_root / 'active/source'))}",
              'cd "$ROOT"']
    runner = ('exec "$ROOT/active/.venv/bin/python" '
              '"$ROOT/active/source/services/lr-ops/lr_ops/configuration.py" launch -- '
              '"$ROOT/active/source/plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap_macos.py" '
              '--runtime-root "$ROOT" ')
    _write_script(launcher_path(runtime_root), shared + [runner + 'mcp-run'])
    _write_script(control_path(runtime_root), shared + [runner + '"$@"'])
    record_installation(runtime_root, stage="launchers")


def _write_legacy_bin_scripts(runtime_root: Path) -> None:
    """Write the stable launcher and the component control entry point."""
    bin_dir = runtime_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    active = runtime_root / "active"
    shared = remember_data_root(runtime_root)
    pythonpath = runtime_pythonpath(active / "source")
    control = control_path(runtime_root)
    bootstrap = active / "source" / "plugins" / "tonerelay-lightroom" / "scripts" / "tonerelay_bootstrap_macos.py"
    quoted_active = shlex.quote(str(active))
    quoted_data = shlex.quote(str(shared))
    quoted_root = shlex.quote(str(runtime_root))
    _write_script(launcher_path(runtime_root), [
        "#!/bin/sh",
        "set -eu",
        f"ROOT={quoted_active}",
        f'if [ -z "${{LR_XMP_MIMIC_RUNTIME_DATA_ROOT:-}}" ]; then LR_XMP_MIMIC_RUNTIME_DATA_ROOT={quoted_data}; fi',
        "export LR_XMP_MIMIC_RUNTIME_DATA_ROOT",
        f"PYTHONPATH={shlex.quote(pythonpath)}",
        "export PYTHONPATH",
        'exec "$ROOT/.venv/bin/python" '
        '"$ROOT/source/plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap_macos.py" '
        f'--runtime-root {quoted_root} mcp-run',
    ])
    _write_script(control, [
        "#!/bin/sh",
        "set -eu",
        f"ROOT={quoted_root}",
        f'if [ -z "${{LR_XMP_MIMIC_RUNTIME_DATA_ROOT:-}}" ]; then LR_XMP_MIMIC_RUNTIME_DATA_ROOT={quoted_data}; fi',
        "export LR_XMP_MIMIC_RUNTIME_DATA_ROOT",
        'exec "$ROOT/active/.venv/bin/python" '
        '"$ROOT/active/source/plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap_macos.py" '
        '--runtime-root "$ROOT" "$@"',
    ])

@_scoped_process
def install(runtime_root: Path, source_root: Path, version: str, *,
            source_origin: str | None = None, source_integrity: str | None = None) -> int:
    """Install an explicit, already-verified source tree without using Git/network refs."""
    try:
        source = validate_source_root(source_root)
        validate_install_root(runtime_root)
        downloads = config_store(runtime_root).paths()["downloads"]
        if (source == runtime_root.resolve() or runtime_root.resolve() in source.parents) and not (source == downloads or downloads in source.parents):
            raise InstallError("install input inside this root must be in its downloads directory")
        store = prepare_installation(runtime_root)
        configuration.apply_process_environment(store)
        record_installation(runtime_root, source=source, version=version, stage="install_started")
        version_root = runtime_version_root(runtime_root, version)
    except InstallError as exc:
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "bootstrap.invalid_source", "safe_to_retry": False,
            "next": str(exc),
        }}, 2)

    ledger = lifecycle().Ledger(runtime_root, create=True)
    ledger.record["sources"].append({"path": str(source), "version": version,
                                      "origin": source_origin, "integrity": source_integrity,
                                      "verification": "caller supplied; bootstrap does not verify remote provenance"})
    ledger.checkpoint("install_started", data_root(runtime_root))
    existing_version = version_root.exists()

    def checkpoint(phase):
        nonlocal ledger
        ledger = lifecycle().Ledger(runtime_root)
        if not existing_version:
            ledger.remember_tree(version_root)
        ledger.install_files([
            runtime_root / "managed-root.json", runtime_root / "data-selection.json",
            runtime_root / "dist.json", runtime_root / "active",
            launcher_path(runtime_root), control_path(runtime_root),
            bridge_pid_path(runtime_root), log_path(runtime_root, "lr-ops.log"),
        ])
        ledger.checkpoint(phase, data_root(runtime_root))

    if existing_version:
        try:
            remember_managed_root(runtime_root)
            remember_data_root(runtime_root)
            activate_runtime(runtime_root, version_root)
            write_bin_scripts(runtime_root)
            start_lr_ops(runtime_root, version_root)
        except (InstallError, OSError, subprocess.SubprocessError) as exc:
            checkpoint("install_failed")
            return emit({"status": "repair_needed", "diagnostic": {
                "code": "bootstrap.install_failed", "safe_to_retry": True,
                "next": str(exc)}}, 1)
        checkpoint("runtime_installed")
        return emit({
            "status": "already_installed",
            "runtime_root": str(runtime_root),
            "manifest": str(ledger.path),
            "runtime_version": version,
            "data_root": str(data_root(runtime_root)),
            "lightroom_plugin": str(runtime_root / "active" / "lightroom" / "lr-xmp-mimic.lrdevplugin"),
            "control": str(control_path(runtime_root)),
            "launcher": str(launcher_path(runtime_root)),
            "mcp_server": mcp_server_config(runtime_root),
            "verification": status(runtime_root),
        })

    staging = runtime_root / "versions" / f".{version}.installing"
    if staging.exists():
        return emit({"status": "repair_needed", "error": f"Partial installation remains at {staging}; inspect its manifest before resuming"}, 2)
    selected = data_root(runtime_root)
    if selected == staging or staging in selected.parents or selected == version_root or version_root in selected.parents:
        return emit({"status": "repair_needed", "error": "Selected data must not be inside the version being installed"}, 2)
    try:
        remember_managed_root(runtime_root)
        remember_data_root(runtime_root)
        staging.mkdir(parents=True)
        copy_runtime_source(source, staging / "source")
        ledger.remember_tree(staging)
        ledger.checkpoint("source_copied")
        create_virtualenv(staging)
        ledger.remember_tree(staging)
        ledger.checkpoint("environment_created")
        try:
            install_dependencies(staging)
        except subprocess.CalledProcessError as exc:
            raise InstallError(
                f"pip exited {exc.returncode} while installing "
                "apps/workbench/requirements.txt; review the pip error and network access. "
                "If a verified package mirror is needed, configure PIP_INDEX_URL; "
                "dependency download help: "
                "https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/faq.md") from exc
        link_runtime_plugin(staging)
        ledger.remember_tree(staging)
        ledger.checkpoint("dependencies_installed")
        version_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, version_root)
        activate_runtime(runtime_root, version_root)
        write_bin_scripts(runtime_root)
        bump_dist_version(runtime_root)
        start_lr_ops(runtime_root, version_root)
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        if staging.exists():
            shutil.rmtree(staging)
        checkpoint("install_failed")
        return emit({"status": "repair_needed", "diagnostic": {
            "code": "bootstrap.install_failed", "safe_to_retry": True,
            "next": str(exc),
        }}, 1)

    checkpoint("runtime_installed")
    return emit({
        "status": "installed",
        "runtime_root": str(runtime_root),
        "manifest": str(ledger.path),
        "runtime_version": version,
        "data_root": str(data_root(runtime_root)),
        "lightroom_plugin": str(runtime_root / "active" / "lightroom" / "lr-xmp-mimic.lrdevplugin"),
        "control": str(control_path(runtime_root)),
        "launcher": str(launcher_path(runtime_root)),
        "mcp_server": mcp_server_config(runtime_root),
        "verification": status(runtime_root),
        "next": f"Run {control_command(runtime_root)} lightroom-plugin install, "
                "register mcp_server in the client, then use a new client session for health and Catalog checks.",
    })


def repair(runtime_root: Path) -> int:
    active = active_root(runtime_root)
    if active is None:
        return emit({"status": "repair_needed", "diagnostic": {
            "code": "bootstrap.runtime_incomplete", "safe_to_retry": True,
            "next": "Run install with an explicit --source-root and --version.",
        }}, 1)
    write_bin_scripts(runtime_root)
    start_lr_ops(runtime_root, active)
    ledger = lifecycle().Ledger(runtime_root, create=True)
    ledger.install_files([launcher_path(runtime_root), control_path(runtime_root),
                          runtime_root / "data-selection.json", bridge_pid_path(runtime_root),
                          log_path(runtime_root, "lr-ops.log")])
    ledger.checkpoint("repaired", data_root(runtime_root))
    return emit(status(runtime_root))


def remember_managed_root(runtime_root: Path) -> None:
    runtime_root.mkdir(parents=True, exist_ok=True)
    temporary = runtime_root / ".managed-root-next.json"
    temporary.write_text(json.dumps({"schema": "tonerelay-managed-root-v1",
                                    "runtime_root": str(runtime_root.resolve())}) + "\n", encoding="utf-8")
    os.replace(temporary, runtime_root / "managed-root.json")


def managed_runtime_root(runtime_root: Path) -> bool:
    if runtime_root.parent == runtime_root:
        return False
    marker = runtime_root / "managed-root.json"
    if marker.is_file() and not marker.is_symlink():
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
            if (record.get("schema") == "tonerelay-managed-root-v1"
                    and record.get("runtime_root") == str(runtime_root.resolve())):
                return True
        except (OSError, ValueError, AttributeError):
            pass
    return False


def is_directory_link(path: Path) -> bool:
    return path.is_symlink()

def runtime_uninstall(runtime_root: Path, version: str | None) -> int:
    """Remove managed Runtime files while preserving selected data and its record."""
    if runtime_root.exists() and not managed_runtime_root(runtime_root):
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "runtime.unmanaged_root", "safe_to_retry": False,
            "next": "This root has no validated ToneRelay version; inspect the path before deleting anything."}}, 2)
    if runtime_root.exists():
        remember_managed_root(runtime_root)
    control = control_command(runtime_root)
    if version is None:
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "runtime.uninstall_needs_target", "safe_to_retry": True,
            "next": f"{control} runtime uninstall --version <label>, or {control} runtime uninstall --version all for all managed Runtime files",
        }}, 2)
    if version == "all":
        if not runtime_root.is_dir():
            return emit({"status": "not_installed", "runtime_root": str(runtime_root)})
        try:
            ledger = lifecycle().Ledger(runtime_root)
            selected = data_root(runtime_root)
            versions = runtime_root / "versions"
            if selected == versions or versions in selected.parents:
                raise InstallError("selected data is inside installed versions; preserve it before removal")
            pid = bridge_pid(runtime_root)
            if pid and process_alive(pid):
                raise InstallError("stop the owned Bridge before uninstalling Runtime files")
            for record_path in (path for directory in ledger.run_directories() for path in directory.glob("mcp-*.json")):
                record = json.loads(record_path.read_text(encoding="utf-8"))
                if record.get("runtime_root") != str(runtime_root.resolve()):
                    raise InstallError("MCP ownership record is inconsistent")
                if isinstance(record.get("pid"), int) and process_alive(record["pid"]):
                    raise InstallError("close the client-owned MCP session before uninstalling Runtime files")
            if lifecycle().process_snapshot(runtime_root, types.SimpleNamespace(**globals()))["runtime"]:
                raise InstallError("close the client-owned MCP session or stop the Bridge before uninstalling Runtime files")
            report = ledger.file_report()
            for relative in sorted(report["removable"], key=lambda value: len(Path(value).parts), reverse=True):
                ledger.local(relative).unlink()
            for relative in sorted(ledger.record["directories"], key=lambda value: len(Path(value).parts), reverse=True):
                path = ledger.local(relative)
                if ledger.retains(path) or path.is_symlink():
                    continue
                try:
                    path.rmdir()
                except OSError:
                    pass
            ledger.checkpoint("runtime_uninstalled")
            remaining = ledger.file_report()
            return emit({"status": "runtime_files_removed", "scope": "runtime_files",
                         "runtime_root": str(runtime_root), "data_root": str(data_root(runtime_root)),
                         "data_preserved": True, "files": remaining,
                         "external": ledger.record.get("external", []),
                         "next": "Configuration, data, backups and unrecorded files remain; use offline inventory to review host entries and residues."})
        except (OSError, ValueError, KeyError) as exc:
            return emit({"status": "uninstall_failed", "error": str(exc),
                         "next": "Resolve the reported ownership/process issue and retry."}, 1)
    try:
        target = runtime_version_root(runtime_root, version)
    except InstallError as exc:
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "runtime.invalid_version", "safe_to_retry": True, "next": str(exc)}}, 2)
    if target.parent.parent != runtime_root or runtime_root not in target.parents:
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "runtime.refused_outside_root", "safe_to_retry": False,
            "next": f"{target} is not inside {runtime_root}"}}, 2)
    active = active_root(runtime_root)
    was_active = active is not None and active == target.resolve()
    retained_data = data_root(runtime_root)
    if target.resolve() == retained_data or target.resolve() in retained_data.parents or is_directory_link(target):
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "runtime.data_preservation_required", "safe_to_retry": False,
            "next": "Selected data is inside this version or the version is a link; preserve it before uninstall."}}, 2)
    if target.is_dir():
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return emit({"status": "uninstall_failed", "runtime_version": version,
                         "runtime_root": str(runtime_root), "error": str(exc),
                         "next": "Stop anything using this version, then retry."}, 1)
    if was_active:
        active_link = runtime_root / "active"
        if link_present(active_link):
            _remove_dir_link(active_link)
    return emit({"status": "uninstalled", "scope": "runtime_version",
                 "runtime_version": version, "removed": True,
                 "active_cleared": was_active,
                 "next": f"{control} status" if not was_active
                         else "active was cleared; install another version to continue."})


def bridge_start(runtime_root: Path) -> int:
    active = active_root(runtime_root)
    if active is None:
        return emit({"status": "repair_needed", "diagnostic": {
            "code": "bridge.no_runtime", "safe_to_retry": True,
            "next": f"{control_command(runtime_root)} runtime install ..."}}, 1)
    if not bridge_is_local():
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "bridge.non_loopback_target", "safe_to_retry": False,
            "base": bridge_base(),
            "next": "A non-loopback bridge is another machine's service; start it there."}}, 2)
    if bridge_reachable():
        matches = bridge_build_status(active)["matches_active_source"]
        return emit({"status": "already_running" if matches else "build_unverified",
                     "base": bridge_base(), "pid": bridge_pid(runtime_root),
                     "matches_active_source": matches,
                     "next": None if matches else "Investigate jobs, stop the Bridge through its owner, then repair."},
                    0 if matches else 1)
    started = start_lr_ops(runtime_root, active)
    return emit({
        "status": "started" if started else "start_failed",
        "base": bridge_base(),
        "pid": bridge_pid(runtime_root) if started else None,
        "log": str(log_path(runtime_root, "lr-ops.log")),
    }, 0 if started else 1)


def bridge_stop(runtime_root: Path) -> int:
    """Stop only the bridge this installer started; never guess at a process."""
    if not bridge_is_local():
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "bridge.non_loopback_target", "safe_to_retry": False,
            "next": "Stop the configured service on its own host; local termination is refused."}}, 2)
    pid_file = bridge_pid_path(runtime_root)
    record = bridge_record(runtime_root)
    pid = record["pid"] if record else None
    if pid is None:
        return emit({"status": "not_started_by_installer",
                     "listening": bridge_reachable(), "base": bridge_base(),
                     "next": f"No pid record at {pid_file}; stop the process where it was "
                             "started (a bridge on a non-loopback host runs on that machine)."})
    try:
        running = process_alive(pid)
    except OSError as exc:
        return emit({"status": "manual_action_required", "pid": pid,
                     "error": str(exc), "os_error": getattr(exc, "errno", None),
                     "next": "Process state is unknown; stop it through its original owner."}, 2)
    if not running:
        pid_file.unlink(missing_ok=True)
        return emit({"status": "not_running", "pid": pid,
                     "listening": bridge_reachable(),
                     "next": "The recorded bridge process already exited; the pid record was "
                             "cleared."})
    identity = record.get("identity")
    version = record.get("version_root")
    owner_matches = (record.get("runtime_root") == str(runtime_root.resolve())
                     and isinstance(version, str)
                     and (runtime_root.resolve() / "versions") in Path(version).parents
                     and record.get("base") == bridge_base()
                     and isinstance(record.get("command"), list)
                     and record["command"][1:4] == ["-m", "lr_ops", "serve"]
                     and record["command"][0] == str(venv_python(Path(version))))
    if not owner_matches or not isinstance(identity, dict) or not identity or process_identity(pid) != identity:
        return emit({"status": "manual_action_required", "pid": pid, "diagnostic": {
            "code": "bridge.process_identity_unknown", "safe_to_retry": False,
            "next": "PID ownership is unknown or changed; stop the Bridge through its original owner. "
                    "A legacy PID record cannot authorize termination."}}, 2)
    if not terminate_process(pid, identity):
        return emit({"status": "stop_failed", "pid": pid,
                     "next": "Process identity or termination was refused; stop it through its original owner."}, 1)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and process_alive(pid):
        time.sleep(0.2)
    still_alive = process_alive(pid)
    if not still_alive:
        pid_file.unlink(missing_ok=True)
    return emit({"status": "stopped" if not still_alive else "still_running",
                 "pid": pid, "listening": bridge_reachable()},
                0 if not still_alive else 1)


def bridge_status(runtime_root: Path) -> int:
    report = component_report(runtime_root)["bridge"]
    active = active_root(runtime_root)
    build = bridge_build_status(active) if active and report["listening"] else None
    return emit({**report, "status": "listening" if report["listening"] else "not_listening",
                 "build": build, "matches_active_source": bool(build and build["matches_active_source"]),
                 "log": str(log_path(runtime_root, "lr-ops.log"))})


def plugin_install(runtime_root: Path) -> int:
    bundle = plugin_bundle(runtime_root)
    if bundle is None:
        return emit({"status": "repair_needed", "diagnostic": {
            "code": "lightroom_plugin.no_bundle", "safe_to_retry": True,
            "next": f"{control_command(runtime_root)} runtime install ..."}}, 1)
    entry = lightroom_entry(runtime_root)
    if link_present(entry):
        resolved = entry.resolve()
        owned = is_directory_link(entry)
        if not owned or runtime_root.resolve() not in resolved.parents:
            return emit({"status": "manual_action_required", "entry": str(entry),
                         "next": "This Modules entry belongs to another install or is an ordinary directory. "
                                 "Inspect it in Plug-in Manager and remove the confirmed obsolete entry explicitly."}, 2)
    try:
        _point_at(entry, bundle)
    except (InstallError, OSError) as exc:
        return emit({"status": "install_failed", "entry": str(entry),
                     "error": str(exc),
                     "next": "Add the bundle path manually in File > Plug-in Manager."}, 1)
    ledger = lifecycle().Ledger(runtime_root, create=True)
    ledger.record["lightroom_entry"] = {"path": str(entry), "target": os.readlink(entry)}
    ledger.checkpoint("plugin_registered", data_root(runtime_root))
    record_installation(runtime_root, external={"kind": "lightroom_modules", "path": str(entry),
        "target": str(bundle.resolve()), "owned": True, "remove": "owned link only"})
    return emit({
        "status": "installed",
        "entry": str(entry),
        "bundle": str(bundle),
        "next": "Restart Lightroom if it is running so it loads the plugin; a module "
                "discovered this way shows Remove greyed out in Plug-in Manager, so use "
                f"'{control_command(runtime_root)} lightroom-plugin uninstall' instead.",
    })


def plugin_uninstall(runtime_root: Path) -> int:
    entry = lightroom_entry(runtime_root)
    if not (link_present(entry)):
        return emit({"status": "not_installed", "entry": str(entry)})
    resolved = entry.resolve()
    root = runtime_root.resolve()
    if not is_directory_link(entry) or root not in resolved.parents:
        return emit({"status": "manual_action_required", "entry": str(entry),
                     "points_at": str(resolved),
                     "next": "This entry does not point inside the Runtime root; remove it "
                             "in Lightroom if it is a ToneRelay registration."}, 2)
    _remove_dir_link(entry)
    record_installation(runtime_root, external={"kind": "lightroom_modules", "path": str(entry),
        "owned": True, "removed": True})
    return emit({"status": "uninstalled", "entry": str(entry),
                 "next": "Restart Lightroom so it stops loading the plugin."})


def plugin_status(runtime_root: Path) -> int:
    report = status(runtime_root)
    component = report["components"]["lightroom_plugin"]
    return emit({**component,
                 "status": "loaded" if report["checks"]["lightroom_plugin_loaded"] else "unverified",
                 "diagnostic": report["diagnostic"],
                 "expected_build": expected_plugin_build(active_root(runtime_root)) if active_root(runtime_root) else None})


def mcp_status(runtime_root: Path) -> int:
    launcher = launcher_path(runtime_root)
    active = active_root(runtime_root)
    return emit({
        "status": "present" if launcher.is_file() else "missing",
        "launcher": str(launcher),
        "control": str(control_path(runtime_root)),
        "mcp_server": mcp_server_config(runtime_root) if active else None,
        "next": None if active else f"{control_command(runtime_root)} runtime install ...",
    })


def _mcp_send(process, message: dict[str, Any]) -> None:
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _mcp_receive(lines: "queue.Queue[str]", timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            break
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"error": "no MCP response before timeout"}


def mcp_start(runtime_root: Path, timeout: float = 30.0) -> int:
    """Start the stdio server, complete the handshake, and report what answered.

    An MCP stdio server has no life of its own: it exists only while a client
    holds its stdin and stdout. Starting one detached would leave an orphan
    holding no conversation, so this starts it, proves it speaks MCP, and stops
    it again. The client does the same thing and then keeps it open.
    """
    active = active_root(runtime_root)
    if active is None:
        return emit({"status": "not_applicable",
                     "reason": "the Runtime is not installed",
                     "next": f"{control_command(runtime_root)} runtime install ..."}, 1)
    python = venv_python(active)
    if not python.is_file():
        return emit({"status": "not_applicable",
                     "reason": f"missing interpreter {python}",
                     "next": f"{control_command(runtime_root)} repair"}, 1)
    launcher = launcher_path(runtime_root)
    if not launcher.is_file():
        return emit({"status": "repair_needed", "reason": "missing managed MCP launcher",
                     "next": f"{control_command(runtime_root)} repair"}, 1)
    environment = runtime_env(active)
    environment["TONERELAY_PROCESS_ROLE"] = "bootstrap-diagnostic"
    process = subprocess.Popen(
        [str(launcher)],
        cwd=str(config_store(runtime_root).paths()["run"]),
        env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
    lines: "queue.Queue[str]" = queue.Queue()

    def pump(stream) -> None:
        for line in stream:
            lines.put(line)

    threading.Thread(target=pump, args=(process.stdout,), daemon=True).start()
    try:
        _mcp_send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": "tonerelay-control",
                                                      "version": "0"}}})
        reply = _mcp_receive(lines, timeout)
        server = (reply.get("result") or {}).get("serverInfo")
        if reply.get("id") != 1 or reply.get("error") or not isinstance(server, dict) or not server:
            return emit({"status": "start_failed", "reply": reply}, 1)
        _mcp_send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _mcp_send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_reply = _mcp_receive(lines, timeout)
        tools = (tools_reply.get("result") or {}).get("tools")
        if (tools_reply.get("id") != 2 or tools_reply.get("error")
                or not isinstance(tools, list) or not tools
                or not all(isinstance(tool, dict) and isinstance(tool.get("name"), str) for tool in tools)
                or not any(tool["name"].startswith("trlr_") for tool in tools)):
            return emit({"status": "start_failed", "reply": tools_reply}, 1)
        names = [tool["name"] for tool in tools]
        return emit({"status": "handshake_ok", "server": server,
                     "tools": len(names),
                     "trlr_tools": sum(1 for name in names
                                       if isinstance(name, str) and name.startswith("trlr_")),
                     "next": f"{control_command(runtime_root)} bridge start"})
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        process.stdout.close()
        process.stderr.close()


def not_applicable(component: str, action: str, reason: str, next_command: str | None) -> int:
    """Answer an inapplicable verb explicitly instead of staying silent."""
    return emit({"status": "not_applicable", "component": component, "action": action,
                 "reason": reason, "next": next_command}, 0)


# ---------------------------------------------------------------- entry point


def lifecycle():
    # Loading by file also supports standalone bootstrap imports without changing
    # the interpreter's global module search path.
    global _lifecycle_module
    if "_lifecycle_module" not in globals():
        path = Path(__file__).with_name("tonerelay_lifecycle.py")
        spec = importlib.util.spec_from_file_location("tonerelay_lifecycle", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _lifecycle_module = module
    return _lifecycle_module


def mcp_run(runtime_root: Path) -> int:
    """Own one stdio child and record its actual identity for bounded removal."""
    process = None
    record_path = None
    previous_handlers = {}
    try:
        with lifecycle().operation_lock(runtime_root):
            ledger = lifecycle().Ledger(runtime_root)
            if ledger.record["phase"] in ("uninstall_started", "clients_removed", "processes_stopped",
                                           "registration_removed", "software_removed"):
                raise InstallError("Uninstallation is in progress; this client must remove its ToneRelay entry")
            active = active_root(runtime_root)
            if active is None:
                raise InstallError("No active Runtime")
            record_configuration_paths(ledger)
            ledger.run_directory().mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen([str(venv_python(active)), "-m", "workbench_mcp"],
                                       env=runtime_env(active), cwd=ledger.run_directory())
            record_path = ledger.run_directory() / f"mcp-{process.pid}.json"
            # Framework Python can re-exec during startup; require a stable identity.
            identity = process_identity(process.pid)
            stable = False
            for _ in range(10):
                if process.poll() is not None:
                    return process.returncode
                time.sleep(0.05)
                current = process_identity(process.pid)
                if current and current == identity:
                    stable = True
                    break
                identity = current
            if not stable:
                raise InstallError("Cannot record a stable MCP process identity")
            lifecycle().atomic_json(record_path, {"schema": "tonerelay-owned-mcp-v1",
                                                  "runtime_root": str(runtime_root),
                                                  "version_root": str(active),
                                                  "command": [str(venv_python(active)), "-m", "workbench_mcp"],
                                                  "pid": process.pid, "identity": identity})
            selected = data_root(runtime_root)
            if (str(record_path.parent.relative_to(ledger.base)) not in ledger.record["directories"]
                    or str(selected) not in ledger.record["data_roots"]):
                ledger.remember(record_path.parent)
                if str(selected) not in ledger.record["data_roots"]:
                    ledger.record["data_roots"].append(str(selected))
                ledger.save()
        def forward(signum, frame):
            if process.poll() is None:
                process.send_signal(signum)
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, forward)
        return process.wait()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if record_path:
            record_path.unlink(missing_ok=True)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


COMPONENTS = ("runtime", "bridge", "lightroom-plugin", "mcp")
ACTIONS = ("install", "uninstall", "start", "stop", "status")


def component_action(runtime_root: Path, component: str, action: str, *,
                     source_root: Path | None, version: str | None,
                     source_origin: str | None = None, source_integrity: str | None = None) -> int:
    """Dispatch one verb for one component; inapplicable verbs answer why."""
    control = control_command(runtime_root)
    if component == "runtime":
        if action == "install":
            if runtime_root.parent == runtime_root:
                return emit({"status": "manual_action_required", "diagnostic": {
                    "code": "cli.refused_root_path", "safe_to_retry": False,
                    "next": f"refusing to install into {runtime_root}"}}, 2)
            install_source = source_root or bundled_source_root()
            try:
                validate_install_root(runtime_root)
                source = install_source.expanduser().resolve()
                downloads = config_store(runtime_root).paths()["downloads"]
                if (source == runtime_root.resolve() or runtime_root.resolve() in source.parents) and source != downloads and downloads not in source.parents:
                    raise InstallError("install input inside this root must be in its downloads directory")
            except InstallError as exc:
                return emit({"status": "manual_action_required", "diagnostic": {
                    "code": "runtime.install_needs_source", "safe_to_retry": False, "next": str(exc)}}, 2)
            try:
                runtime_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return emit({"status": "manual_action_required", "diagnostic": {
                    "code": "cli.root_not_writable", "safe_to_retry": True,
                    "next": f"{runtime_root} is not writable ({exc}); "
                            f"re-run with --runtime-root {fallback_runtime_root()}"}}, 2)
            return install(runtime_root, install_source, version or "local",
                           source_origin=source_origin, source_integrity=source_integrity)
        if action == "uninstall":
            if version is None:
                return emit({"status": "manual_action_required", "diagnostic": {
                    "code": "runtime.uninstall_needs_target", "safe_to_retry": True,
                    "next": f"{control} runtime uninstall --version <label>, "
                            "or --version all for the whole root"}}, 2)
            return runtime_uninstall(runtime_root, version)
        if action == "status":
            return emit(status(runtime_root))
        return not_applicable(
            component, action,
            "the Runtime is a directory of installed versions, not a process",
            f"{control} bridge {action}")
    if component == "bridge":
        if action == "start":
            return bridge_start(runtime_root)
        if action == "stop":
            return bridge_stop(runtime_root)
        if action == "status":
            return bridge_status(runtime_root)
        return not_applicable(
            component, action,
            "the bridge ships inside the Runtime, so installing a version is what provides it",
            f"{control} runtime {action}")
    if component == "lightroom-plugin":
        if action == "install":
            return plugin_install(runtime_root)
        if action == "uninstall":
            return plugin_uninstall(runtime_root)
        if action == "status":
            return plugin_status(runtime_root)
        return not_applicable(
            component, action,
            "Lightroom owns the plug-in lifecycle; whether the bundle loads is controlled "
            "by the Modules entry, which install and uninstall manage",
            f"{control} lightroom-plugin "
            + ("install" if action == "start" else "uninstall"))
    if action == "start":
        return mcp_start(runtime_root)
    if action == "status":
        return mcp_status(runtime_root)
    if action == "stop":
        return not_applicable(
            component, action,
            "the stdio server lives only while its client holds stdin and stdout",
            "close the client session that started it")
    return not_applicable(
        component, action,
        "the launcher comes from the Runtime install; the client-side entry is mcp_server",
        f"{control} mcp status")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "install", "repair", "prepare", "inventory",
                        "uninstall", "uninstall-check", "record-client", "mcp-run", "paths", *COMPONENTS))
    parser.add_argument("action", nargs="?", default="status",
                        help="component verb: " + ", ".join(ACTIONS))
    parser.add_argument("--runtime-root", type=Path, default=default_runtime_root())
    parser.add_argument("--installation-root", type=Path,
                        help="group source/runtime/data/backups beneath this dedicated parent")
    parser.add_argument("--manage-source", action="store_true",
                        help="include the dedicated installation-root/source download in managed removal")
    parser.add_argument("--apply", action="store_true", help="apply the planned whole-installation removal")
    parser.add_argument("--clients-reviewed", action="store_true",
                        help="operator has checked every client and obtained any shared-use authorization")
    parser.add_argument("--client", help="client name for record-client")
    parser.add_argument("--config-path", "--client-config", dest="config_path", type=Path, help="client JSON mcpServers configuration")
    parser.add_argument("--server-key", default="tonerelay-lightroom")
    parser.add_argument("--client-kind", choices=("json", "official"), default="json")
    parser.add_argument("--removal-evidence", help="observed official-client removal result; never a model's guess")
    parser.add_argument("--source-root", type=Path,
                        help="verified Runtime checkout; never defaults to a floating remote ref")
    parser.add_argument("--source-origin", help="verified canonical source URL recorded by the operator")
    parser.add_argument("--source-integrity", help="operator's checked archive digest or immutable source identity")
    parser.add_argument("--version",
                        help="immutable Runtime label; also selects what 'runtime uninstall' removes")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        return emit({"status": "unsupported_host", "diagnostic": {
            "code": "bootstrap.macos_required", "safe_to_retry": False,
            "next": "This entry supports macOS only. Use the Windows owner's bootstrap on Windows."}}, 2)

    raw_root = (args.installation_root if args.installation_root else args.runtime_root).expanduser().absolute()
    if raw_root.is_symlink():
        return emit({"status": "manual_action_required", "error": "installation root must not be a symlink"}, 2)
    runtime_root = (args.installation_root.expanduser().resolve() / "runtime"
                    if args.installation_root else args.runtime_root.expanduser().resolve())
    try:
        mutating = (args.command in ("prepare", "install", "repair", "record-client")
                    or (args.command == "uninstall" and args.apply)
                    or (args.command in COMPONENTS and args.command != "mcp"
                        and args.action in ("install", "uninstall", "start", "stop")))
        if mutating:
            with lifecycle().operation_lock(runtime_root, args.installation_root):
                return dispatch(args, runtime_root)
        return dispatch(args, runtime_root)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return emit({"status": "manual_action_required", "error": str(exc)}, 2)


def dispatch(args, runtime_root):
    try:
        if config_store(runtime_root).path.is_file():
            env = configuration.process_environment(config_store(runtime_root))
            os.environ.clear()
            os.environ.update(env)
        if args.command == "prepare":
            if args.installation_root:
                return emit(lifecycle().prepare(args.installation_root))
            store = prepare_installation(runtime_root)
            return emit({"status": "prepared", "configuration": str(store.path),
                         "manifest": str(lifecycle().Ledger(runtime_root).path),
                         "paths": {key: str(path) for key, path in store.paths().items()}})
        if args.command == "paths":
            store = config_store(runtime_root)
            return emit({"configuration": str(store.path), "persisted": store.path.is_file(),
                         "revision": store.read()["revision"],
                         "paths": {key: str(path) for key, path in store.paths().items()}})
        if args.command == "mcp-run":
            return mcp_run(runtime_root)
        if args.command in ("inventory", "uninstall", "uninstall-check", "record-client"):
            ledger = lifecycle().Ledger(runtime_root)
            api = types.SimpleNamespace(**globals())
            if args.command == "record-client":
                if not args.client or not args.config_path:
                    raise InstallError("record-client requires --client and --config-path")
                result = lifecycle().record_client(ledger, args.client, args.config_path, args.server_key,
                                                   kind=args.client_kind, evidence=args.removal_evidence)
            elif args.command == "uninstall":
                result = lifecycle().uninstall(ledger, api, apply=args.apply,
                                               clients_reviewed=args.clients_reviewed)
            else:
                result = {"status": "observed", **lifecycle().inventory(ledger, api)}
                if args.command == "uninstall-check":
                    result["status"] = "software_removed" if all(result["checks"].values()) and result["phase"] == "software_removed" else "removal_pending"
            return emit(lifecycle().compact_report(result), 0 if result["status"] in ("recorded", "planned", "observed", "software_removed") else 2)
        if (args.installation_root or args.manage_source) and (args.command == "install" or
                                       (args.command == "runtime" and args.action == "install")):
            if not args.installation_root:
                prepare_installation(runtime_root)
            ledger = lifecycle().Ledger(runtime_root, installation_root=args.installation_root, create=True)
            if args.manage_source:
                if not args.source_root or args.source_root.expanduser().resolve() != (config_store(runtime_root).paths()["downloads"] / "source" if config_store(runtime_root).path.is_file() else ledger.base / "source"):
                    raise InstallError("--manage-source requires the dedicated downloads/source input (source for legacy grouped layouts)")
                validate_source_root(args.source_root)
                ledger.remember_tree(args.source_root.expanduser().resolve())
                ledger.record["managed_source"] = str(args.source_root.expanduser().resolve())
                ledger.save()
        elif args.manage_source:
            raise InstallError("--manage-source is only valid with install")
        if args.command in COMPONENTS:
            if args.action not in ACTIONS:
                return emit({"status": "manual_action_required", "diagnostic": {
                    "code": "cli.unknown_action", "safe_to_retry": True,
                    "next": f"{args.command} supports: " + ", ".join(ACTIONS)}}, 2)
            return component_action(runtime_root, args.command, args.action,
                                    source_root=args.source_root, version=args.version,
                                    source_origin=args.source_origin, source_integrity=args.source_integrity)
        if args.command == "status":
            return emit(status(runtime_root))
        if args.command == "install":
            return component_action(runtime_root, "runtime", "install",
                                    source_root=args.source_root, version=args.version,
                                    source_origin=args.source_origin, source_integrity=args.source_integrity)
        if args.command == "repair":
            return repair(runtime_root)
        return emit(status(runtime_root))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # MCP stdout is protocol-only; startup errors belong on stderr.
        if args.command == "mcp-run":
            print(f"ToneRelay MCP startup failed: {exc}", file=sys.stderr)
            return 2
        return emit({"status": "manual_action_required", "error": str(exc),
                     "next": "Resolve the reported checkpoint, then resume the same command."}, 2)


@_scoped_process
def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (InstallError, OSError) as exc:
        return emit({"status": "manual_action_required", "error": str(exc)}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
