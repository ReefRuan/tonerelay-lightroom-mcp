#!/usr/bin/env python3
"""Install, inspect and operate the local ToneRelay Runtime.

This executable deliberately lives in the client Plugin rather than the Runtime
checkout. It is usable before the MCP has started, installs a versioned Runtime
into a stable directory, and leaves the MCP client to own the stdio connection.

Everything the installer owns lives under one ``--runtime-root``: the versioned
source, the virtual environment, the launchers, the ``active`` link, the logs and
the Runtime data root. Nothing is written into the user profile beyond that root.

Every component answers the same five verbs -- install, uninstall, start, stop,
status -- so a caller never has to learn a different verb set per component. A
verb that cannot apply answers `not_applicable` with the reason and the command
that does apply, instead of staying silent:

    runtime           install / uninstall / status; it is a directory of
                      versions, so start and stop point at the bridge
    bridge            start / stop / status; it ships inside the Runtime
    lightroom-plugin  install / uninstall / status; Lightroom owns the
                      plug-in lifecycle, and the Modules entry decides loading
    mcp               start / status; an stdio server lives only while its
                      client holds stdin and stdout, so start proves the
                      handshake instead of orphaning a process

`status` is the probe for every component: it reports whether the component
exists, where it is, and the exact command that changes it. After the first
install, ``<runtime-root>/bin/tonerelay`` (POSIX) or
``<runtime-root>/bin/tonerelay.cmd`` (Windows) is the one entry point for all of
them; it forwards to this script inside the active Runtime version.

The root, virtual-environment layout, launchers and ``active`` link are resolved
per platform:

- macOS (``darwin``): ``~/Library/Application Support/ToneRelay`` with POSIX
  ``bin/`` scripts and a relative symlink for ``active``.
- Windows (``nt``): ``%SystemDrive%/ToneRelay`` -- one visible directory rather
  than a path buried in the user profile -- with ``bin/*.cmd`` scripts. ``active``
  is a directory junction because creating symlinks needs elevated rights the
  installer must not assume. When the drive root is not writable the installer
  falls back to ``%LOCALAPPDATA%/ToneRelay`` and says so.
- Other POSIX: ``$XDG_DATA_HOME/ToneRelay`` (or ``~/.local/share/ToneRelay``).
"""

from __future__ import annotations

import argparse
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import venv
from pathlib import Path
from typing import Any


# The installer is standalone by design: it runs before the Runtime exists, so it
# cannot import workbench.lrops. Keep this equal to that module's DEFAULT_BASE.
DEFAULT_BRIDGE_BASE = "http://127.0.0.1:17777"
BRIDGE_START_TIMEOUT_SECONDS = 15.0
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
RUNTIME_SOURCE_FILES = ("apps/workbench/requirements.txt",)
RUNTIME_PYTHONPATH_ENTRIES = (
    "apps/workbench-mcp",
    "apps/workbench",
    "services/lr-ops",
)
SCHEME_DEFAULT_PORTS = {"http": 80, "https": 443}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

IS_WINDOWS = os.name == "nt"


class InstallError(ValueError):
    """An explicit source bundle cannot safely become an active Runtime."""


# ---------------------------------------------------------------- locations


def default_runtime_root() -> Path:
    if IS_WINDOWS:
        system_drive = os.environ.get("SystemDrive") or "C:"
        return Path(f"{system_drive}/ToneRelay")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ToneRelay"
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "ToneRelay"


def fallback_runtime_root() -> Path:
    """Per-user location used when the platform default is not writable."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "ToneRelay"
    return default_runtime_root()


def data_root(runtime_root: Path) -> Path:
    """Runtime data lives inside the install root, never in the user profile."""
    return runtime_root / "data"


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
    environment = version_root / ".venv"
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    except subprocess.CalledProcessError:
        # Relocatable Python builds may not find their standard library when
        # venv copies the interpreter. Retry with a link to the base binary.
        shutil.rmtree(environment, ignore_errors=True)
        venv.EnvBuilder(with_pip=True, clear=True, symlinks=True).create(environment)


def venv_python(version_root: Path) -> Path:
    """Return the interpreter path inside a Runtime virtual environment."""
    if IS_WINDOWS:
        return version_root / ".venv" / "Scripts" / "python.exe"
    return version_root / ".venv" / "bin" / "python"


def install_dependencies(version_root: Path) -> None:
    python = venv_python(version_root)
    requirements = version_root / "source" / "apps" / "workbench" / "requirements.txt"
    subprocess.run([
        str(python), "-m", "pip", "install", "--disable-pip-version-check",
        "-r", str(requirements),
    ], check=True)


def runtime_pythonpath(source: Path) -> str:
    return os.pathsep.join(
        str(source / relative) for relative in RUNTIME_PYTHONPATH_ENTRIES)


def runtime_env(version_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = runtime_pythonpath(version_root / "source")
    env.setdefault("LR_XMP_MIMIC_RUNTIME_DATA_ROOT",
                   str(data_root(version_root.parent.parent)))
    return env


def _remove_dir_link(link: Path) -> None:
    """Remove a symlink, junction, or empty directory without touching its target."""
    try:
        link.unlink()
        return
    except OSError:
        pass
    # Windows junctions can surface as directories; RemoveDirectory drops the
    # link itself and never descends into the target.
    os.rmdir(link)


def _point_at(link: Path, target: Path) -> None:
    """Make ``link`` resolve to ``target``, falling back to a junction on Windows."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        _remove_dir_link(link)
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as symlink_error:
        if not IS_WINDOWS:
            raise
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(
            f"cannot link {link} -> {target}: {detail or symlink_error}")


def link_runtime_plugin(staging: Path) -> None:
    """Expose the Lightroom plugin as ``<version>/lightroom``.

    POSIX keeps a relative symlink because the staged directory is renamed below,
    so an absolute link back into staging would dangle. Windows cannot express a
    relative junction, so it copies the plugin bundle instead.
    """
    target = staging / "lightroom"
    if IS_WINDOWS:
        shutil.copytree(
            staging / "source" / "plugins" / "lightroom", target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".DS_Store"))
        return
    target.symlink_to(Path("source") / "plugins" / "lightroom")


def activate_runtime(runtime_root: Path, version_root: Path) -> None:
    if IS_WINDOWS:
        # Windows has no atomic directory-link swap; point the junction directly.
        _point_at(runtime_root / "active", version_root)
        return
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
    name = "tonerelay-mcp.cmd" if IS_WINDOWS else "tonerelay-mcp"
    return runtime_root / "bin" / name


def control_path(runtime_root: Path) -> Path:
    """The one command an agent needs for every component operation."""
    name = "tonerelay.cmd" if IS_WINDOWS else "tonerelay"
    return runtime_root / "bin" / name


def bridge_pid_path(runtime_root: Path) -> Path:
    return runtime_root / "run" / "lr-ops.pid"


def log_path(runtime_root: Path, name: str) -> Path:
    return runtime_root / "logs" / name


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


def bridge_pid(runtime_root: Path) -> int | None:
    try:
        return int(bridge_pid_path(runtime_root).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    """Whether ``pid`` is still running.

    ``os.kill(pid, 0)`` is not a safe existence probe on Windows: against a
    detached process it raises WinError 87 instead of reporting liveness, while
    against a same-console child it returns normally. Query the process handle
    directly there, and keep the zero-signal check for POSIX.
    """
    if IS_WINDOWS:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_process(pid: int) -> bool:
    if IS_WINDOWS:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if not handle:
            return False
        try:
            return bool(ctypes.windll.kernel32.TerminateProcess(handle, 1))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def start_lr_ops(runtime_root: Path, version_root: Path) -> bool:
    """Start the loopback bridge detached and remember the pid we created."""
    if bridge_reachable():
        return True
    if not bridge_is_local():
        return False
    python = venv_python(version_root)
    if not python.is_file():
        return False
    host, port = bridge_endpoint()
    logs = runtime_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_file = log_path(runtime_root, "lr-ops.log").open("ab")
    options: dict[str, Any] = {}
    if IS_WINDOWS:
        options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(
        [str(python), "-m", "lr_ops", "serve", "--host", host, "--port", str(port)],
        cwd=version_root / "source",
        env=runtime_env(version_root),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        **options,
    )
    bridge_pid_path(runtime_root).parent.mkdir(parents=True, exist_ok=True)
    bridge_pid_path(runtime_root).write_text(str(process.pid), encoding="utf-8")
    deadline = time.monotonic() + BRIDGE_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if bridge_reachable():
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------- probing


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


def _lr_ops_request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://{':'.join(str(part) for part in bridge_endpoint())}{path}",
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
    """Where Lightroom Classic auto-loads user plug-ins from, per platform."""
    if IS_WINDOWS:
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "Adobe" / "Lightroom" / "Modules"
    return Path.home() / "Library" / "Application Support" / "Adobe" / "Lightroom" / "Modules"


def lightroom_entry(runtime_root: Path) -> Path:
    return lightroom_modules_dir() / "lr-xmp-mimic.lrdevplugin"


def component_report(runtime_root: Path) -> dict[str, Any]:
    """What exists right now, per component, with the command that changes it."""
    active = active_root(runtime_root)
    launcher = launcher_path(runtime_root)
    control = control_path(runtime_root)
    bundle = plugin_bundle(runtime_root)
    entry = lightroom_entry(runtime_root)
    pid = bridge_pid(runtime_root)
    return {
        "runtime": {
            "present": active is not None,
            "runtime_root": str(runtime_root),
            "data_root": str(data_root(runtime_root)),
            "active_version": active.name if active else None,
            "install_seq": dist_version(runtime_root),
            "next": None if active else f"{control} runtime install --source-root <checkout> --version <label>",
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
            "pid": pid if pid and process_alive(pid) else None,
            "next": None if bridge_reachable() else f"{control} bridge start",
        },
        "lightroom_plugin": {
            "bundle_present": bundle is not None,
            "bundle_path": str(bundle) if bundle else None,
            "bundle_target": str(bundle.resolve()) if bundle else None,
            "registered_entry": str(entry) if entry.exists() or entry.is_symlink() else None,
            "next": (f"{control} lightroom-plugin install"
                     if bundle and not (entry.exists() or entry.is_symlink()) else None),
        },
    }


def status(runtime_root: Path) -> dict[str, Any]:
    active = active_root(runtime_root)
    launcher = launcher_path(runtime_root)
    plugin = plugin_bundle(runtime_root)
    launcher_present = launcher.is_file() and (IS_WINDOWS or os.access(launcher, os.X_OK))
    checks = {
        "runtime_active": active is not None,
        "launcher_present": launcher_present,
        "lr_ops_reachable": bridge_reachable(),
        "lightroom_plugin_bundle_present": plugin is not None,
    }
    expected_build = expected_plugin_build(active) if active else None
    probe = probe_loaded_plugin() if checks["lr_ops_reachable"] else None
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
        if runtime_ready and checks["lr_ops_reachable"] and (
                not isinstance(probe, dict) or probe.get("error")
                or not isinstance(loaded_build, str) or not loaded_build
                or not isinstance(capabilities, dict)):
            diagnostic = {
                "code": "bootstrap.plugin_probe_unavailable",
                "safe_to_retry": False,
                "expected_build": expected_build,
                "loaded_build": loaded_build,
                "probe_error": probe.get("error") if isinstance(probe, dict) else None,
                "next": "The Bridge is reachable, but the active Lightroom plugin contract is unknown. "
                        "Unlock the desktop, check Lightroom for open menus, dialogs or ongoing operations, "
                        "and inspect ToneRelay's bridge status in File > Plug-in Extras. "
                        "Do not infer an old or missing plugin from an unanswered probe. "
                        "After the application state changes, run status again; do not replay unknown writes.",
            }
        elif runtime_ready and checks["lr_ops_reachable"]:
            diagnostic = {
                "code": "bootstrap.plugin_reload_required",
                "safe_to_retry": False,
                "expected_build": expected_build,
                "loaded_build": loaded_build,
                "probe_error": probe.get("error") if isinstance(probe, dict) else None,
                "next": "Quit and reopen Lightroom so it loads the current ToneRelay plugin.",
            }
        elif runtime_ready:
            diagnostic = {
                "code": "bootstrap.lightroom_plugin_registration_required",
                "safe_to_retry": True,
                "lightroom_plugin": str(plugin),
                "next": f"Run {control_path(runtime_root)} lightroom-plugin install, "
                        "or add lightroom_plugin in File > Plug-in Manager.",
            }
        else:
            diagnostic = {
                "code": "bootstrap.runtime_incomplete",
                "safe_to_retry": True,
                "next": "Run tonerelay_bootstrap.py repair or install.",
            }
    return {
        "status": "ready" if ready else "repair_needed",
        "checks": checks,
        "components": component_report(runtime_root),
        "diagnostic": diagnostic,
    }


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


# ---------------------------------------------------------------- bin scripts


def mcp_server_config(runtime_root: Path, *, client: str = "native") -> dict[str, Any]:
    """Return a ready-to-paste stdio MCP entry for the current platform.

    The command resolves through the stable ``active`` link, so it keeps working
    across Runtime upgrades without editing the client configuration again.
    All local clients receive file references for assistant-body rendering.
    Clients without shared file access can set the local image environment to 0.
    """
    active = runtime_root / "active"
    env = {
        "PYTHONPATH": runtime_pythonpath(active / "source"),
        "LR_XMP_MIMIC_RUNTIME_DATA_ROOT": str(data_root(runtime_root)),
        "LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES": os.environ.get("LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES", "1"),
    }
    return {
        "tonerelay-lightroom": {
            "command": str(venv_python(active)),
            "args": ["-m", "workbench_mcp"],
            "env": env,
        }
    }


def _write_script(path: Path, lines: list[str]) -> None:
    path.write_text("\r\n".join(lines + [""]), encoding="utf-8", newline="")
    if not IS_WINDOWS:
        path.chmod(0o755)


def write_bin_scripts(runtime_root: Path) -> None:
    """Write the stable launcher and the component control entry point."""
    bin_dir = runtime_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    active = runtime_root / "active"
    shared = data_root(runtime_root)
    pythonpath = runtime_pythonpath(active / "source")
    control = control_path(runtime_root)
    bootstrap = active / "source" / "plugins" / "tonerelay-lightroom" / "scripts" / "tonerelay_bootstrap.py"
    if IS_WINDOWS:
        _write_script(launcher_path(runtime_root), [
            "@echo off",
            "setlocal",
            f'set "TONERELAY_ACTIVE={active}"',
            f'set "LR_XMP_MIMIC_RUNTIME_DATA_ROOT={shared}"',
            'set "PYTHONPATH='
            '%TONERELAY_ACTIVE%\\source\\apps\\workbench-mcp;'
            '%TONERELAY_ACTIVE%\\source\\apps\\workbench;'
            '%TONERELAY_ACTIVE%\\source\\services\\lr-ops"',
            '"%TONERELAY_ACTIVE%\\.venv\\Scripts\\python.exe" -m workbench_mcp %*',
        ])
        _write_script(control, [
            "@echo off",
            "setlocal",
            f'set "LR_XMP_MIMIC_RUNTIME_DATA_ROOT={shared}"',
            f'"{active}\\.venv\\Scripts\\python.exe" "{bootstrap}" '
            f'--runtime-root "{runtime_root}" %*',
        ])
        return
    quoted_active = shlex.quote(str(active))
    quoted_data = shlex.quote(str(shared))
    quoted_root = shlex.quote(str(runtime_root))
    _write_script(launcher_path(runtime_root), [
        "#!/bin/sh",
        "set -eu",
        f"ROOT={quoted_active}",
        f"LR_XMP_MIMIC_RUNTIME_DATA_ROOT={quoted_data}",
        "export LR_XMP_MIMIC_RUNTIME_DATA_ROOT",
        f'export PYTHONPATH="{pythonpath}"',
        'exec "$ROOT/.venv/bin/python" -m workbench_mcp',
    ])
    _write_script(control, [
        "#!/bin/sh",
        "set -eu",
        f"ROOT={quoted_root}",
        f"LR_XMP_MIMIC_RUNTIME_DATA_ROOT={quoted_data}",
        "export LR_XMP_MIMIC_RUNTIME_DATA_ROOT",
        'exec "$ROOT/active/.venv/bin/python" '
        '"$ROOT/active/source/plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap.py" '
        '--runtime-root "$ROOT" "$@"',
    ])


# ---------------------------------------------------------------- operations


def install(runtime_root: Path, source_root: Path, version: str, *, client: str = "native") -> int:
    """Install an explicit, already-verified source tree without using Git/network refs."""
    try:
        source = validate_source_root(source_root)
        version_root = runtime_version_root(runtime_root, version)
    except InstallError as exc:
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "bootstrap.invalid_source", "safe_to_retry": False,
            "next": str(exc),
        }}, 2)

    if version_root.exists():
        activate_runtime(runtime_root, version_root)
        write_bin_scripts(runtime_root)
        start_lr_ops(runtime_root, version_root)
        return emit({
            "status": "already_installed",
            "runtime_root": str(runtime_root),
            "runtime_version": version,
            "data_root": str(data_root(runtime_root)),
            "lightroom_plugin": str(runtime_root / "active" / "lightroom" / "lr-xmp-mimic.lrdevplugin"),
            "control": str(control_path(runtime_root)),
            "launcher": str(launcher_path(runtime_root)),
            "mcp_server": mcp_server_config(runtime_root, client=client),
            "verification": status(runtime_root),
        })

    staging = runtime_root / "versions" / f".{version}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        staging.mkdir(parents=True)
        copy_runtime_source(source, staging / "source")
        create_virtualenv(staging)
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
        version_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, version_root)
        activate_runtime(runtime_root, version_root)
        write_bin_scripts(runtime_root)
        bump_dist_version(runtime_root)
        start_lr_ops(runtime_root, version_root)
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        if staging.exists():
            shutil.rmtree(staging)
        return emit({"status": "repair_needed", "diagnostic": {
            "code": "bootstrap.install_failed", "safe_to_retry": True,
            "next": str(exc),
        }}, 1)

    return emit({
        "status": "installed",
        "runtime_root": str(runtime_root),
        "runtime_version": version,
        "data_root": str(data_root(runtime_root)),
        "lightroom_plugin": str(runtime_root / "active" / "lightroom" / "lr-xmp-mimic.lrdevplugin"),
        "control": str(control_path(runtime_root)),
        "launcher": str(launcher_path(runtime_root)),
        "mcp_server": mcp_server_config(runtime_root, client=client),
        "verification": status(runtime_root),
        "next": f"Use {control_path(runtime_root)} for start/stop/status, add mcp_server to the "
                "client's stdio MCP config, and register lightroom_plugin in Lightroom.",
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
    return emit(status(runtime_root))


def runtime_uninstall(runtime_root: Path, version: str | None) -> int:
    """Remove one installed version, or the whole install root with --all."""
    control = control_path(runtime_root)
    if version is None:
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "runtime.uninstall_needs_target", "safe_to_retry": True,
            "next": f"{control} runtime uninstall --version <label>, or --all for the whole root",
        }}, 2)
    if version == "all":
        if not runtime_root.is_dir():
            return emit({"status": "not_installed", "runtime_root": str(runtime_root)})
        try:
            shutil.rmtree(runtime_root)
        except OSError as exc:
            return emit({"status": "uninstall_failed",
                         "runtime_root": str(runtime_root), "error": str(exc),
                         "next": "Some files could not be removed. Stop the bridge and any MCP "
                                 "client using this root, then retry."}, 1)
        return emit({"status": "uninstalled", "scope": "runtime_root",
                     "runtime_root": str(runtime_root), "removed": True,
                     "next": "The Runtime, its data root and its logs are gone."})
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
    if target.is_dir():
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return emit({"status": "uninstall_failed", "runtime_version": version,
                         "runtime_root": str(runtime_root), "error": str(exc),
                         "next": "Stop anything using this version, then retry."}, 1)
    if was_active:
        active_link = runtime_root / "active"
        if active_link.exists() or active_link.is_symlink():
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
            "next": f"{control_path(runtime_root)} runtime install ..."}}, 1)
    if not bridge_is_local():
        return emit({"status": "manual_action_required", "diagnostic": {
            "code": "bridge.non_loopback_target", "safe_to_retry": False,
            "base": bridge_base(),
            "next": "A non-loopback bridge is another machine's service; start it there."}}, 2)
    if bridge_reachable():
        return emit({"status": "already_running", "base": bridge_base(),
                     "pid": bridge_pid(runtime_root)})
    started = start_lr_ops(runtime_root, active)
    return emit({
        "status": "started" if started else "start_failed",
        "base": bridge_base(),
        "pid": bridge_pid(runtime_root) if started else None,
        "log": str(log_path(runtime_root, "lr-ops.log")),
    }, 0 if started else 1)


def bridge_stop(runtime_root: Path) -> int:
    """Stop only the bridge this installer started; never guess at a process."""
    pid_file = bridge_pid_path(runtime_root)
    pid = bridge_pid(runtime_root)
    if pid is None:
        return emit({"status": "not_started_by_installer",
                     "listening": bridge_reachable(), "base": bridge_base(),
                     "next": f"No pid record at {pid_file}; stop the process where it was "
                             "started (a bridge on a non-loopback host runs on that machine)."})
    if not process_alive(pid):
        pid_file.unlink(missing_ok=True)
        return emit({"status": "not_running", "pid": pid,
                     "listening": bridge_reachable(),
                     "next": "The recorded bridge process already exited; the pid record was "
                             "cleared."})
    if not terminate_process(pid):
        return emit({"status": "stop_failed", "pid": pid,
                     "next": "OpenProcess was refused; stop it with the tool that started it."}, 1)
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
    pid = bridge_pid(runtime_root)
    return emit({
        "status": "listening" if bridge_reachable() else "not_listening",
        "base": bridge_base(),
        "local": bridge_is_local(),
        "pid": pid if pid and process_alive(pid) else None,
        "log": str(log_path(runtime_root, "lr-ops.log")),
    })


def plugin_install(runtime_root: Path) -> int:
    bundle = plugin_bundle(runtime_root)
    if bundle is None:
        return emit({"status": "repair_needed", "diagnostic": {
            "code": "lightroom_plugin.no_bundle", "safe_to_retry": True,
            "next": f"{control_path(runtime_root)} runtime install ..."}}, 1)
    entry = lightroom_entry(runtime_root)
    try:
        _point_at(entry, bundle)
    except (InstallError, OSError) as exc:
        return emit({"status": "install_failed", "entry": str(entry),
                     "error": str(exc),
                     "next": "Add the bundle path manually in File > Plug-in Manager."}, 1)
    return emit({
        "status": "installed",
        "entry": str(entry),
        "bundle": str(bundle),
        "next": "Restart Lightroom if it is running so it loads the plugin; a module "
                "discovered this way shows Remove greyed out in Plug-in Manager, so use "
                f"'{control_path(runtime_root)} lightroom-plugin uninstall' instead.",
    })


def plugin_uninstall(runtime_root: Path) -> int:
    entry = lightroom_entry(runtime_root)
    if not (entry.exists() or entry.is_symlink()):
        return emit({"status": "not_installed", "entry": str(entry)})
    resolved = entry.resolve()
    root = runtime_root.resolve()
    if root not in resolved.parents:
        return emit({"status": "manual_action_required", "entry": str(entry),
                     "points_at": str(resolved),
                     "next": "This entry does not point inside the Runtime root; remove it "
                             "in Lightroom if it is a ToneRelay registration."}, 2)
    _remove_dir_link(entry)
    return emit({"status": "uninstalled", "entry": str(entry),
                 "next": "Restart Lightroom so it stops loading the plugin."})


def plugin_status(runtime_root: Path) -> int:
    bundle = plugin_bundle(runtime_root)
    entry = lightroom_entry(runtime_root)
    probe = probe_loaded_plugin() if bridge_reachable() else None
    return emit({
        "status": "loaded" if isinstance(probe, dict) and not probe.get("error") else "not_loaded",
        "bundle_present": bundle is not None,
        "bundle_path": str(bundle) if bundle else None,
        "bundle_target": str(bundle.resolve()) if bundle else None,
        "registered_entry": str(entry) if entry.exists() or entry.is_symlink() else None,
        "entry_target": str(entry.resolve()) if entry.exists() or entry.is_symlink() else None,
        "modules_dir": str(lightroom_modules_dir()),
        "probe": probe,
    })


def mcp_status(runtime_root: Path, *, client: str = "native") -> int:
    launcher = launcher_path(runtime_root)
    active = active_root(runtime_root)
    return emit({
        "status": "present" if launcher.is_file() else "missing",
        "launcher": str(launcher),
        "control": str(control_path(runtime_root)),
        "mcp_server": mcp_server_config(runtime_root, client=client) if active else None,
        "next": None if active else f"{control_path(runtime_root)} runtime install ...",
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
                     "next": f"{control_path(runtime_root)} runtime install ..."}, 1)
    python = venv_python(active)
    if not python.is_file():
        return emit({"status": "not_applicable",
                     "reason": f"missing interpreter {python}",
                     "next": f"{control_path(runtime_root)} repair"}, 1)
    process = subprocess.Popen(
        [str(python), "-m", "workbench_mcp"],
        cwd=str((active / "source" / "apps" / "workbench-mcp").resolve()),
        env=runtime_env(active), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
        if not server:
            return emit({"status": "start_failed", "reply": reply}, 1)
        _mcp_send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _mcp_send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_reply = _mcp_receive(lines, timeout)
        names = [tool.get("name") for tool in
                 (tools_reply.get("result") or {}).get("tools", [])]
        return emit({"status": "handshake_ok", "server": server,
                     "tools": len(names),
                     "trlr_tools": sum(1 for name in names
                                       if isinstance(name, str) and name.startswith("trlr_")),
                     "next": f"{control_path(runtime_root)} bridge start"})
    finally:
        terminate_process(process.pid)


def not_applicable(component: str, action: str, reason: str, next_command: str | None) -> int:
    """Answer an inapplicable verb explicitly instead of staying silent."""
    return emit({"status": "not_applicable", "component": component, "action": action,
                 "reason": reason, "next": next_command}, 0)


# ---------------------------------------------------------------- entry point


COMPONENTS = ("runtime", "bridge", "lightroom-plugin", "mcp")
ACTIONS = ("install", "uninstall", "start", "stop", "status")


def component_action(runtime_root: Path, component: str, action: str, *,
                     source_root: Path | None, version: str | None,
                     client: str = "native") -> int:
    """Dispatch one verb for one component; inapplicable verbs answer why."""
    control = control_path(runtime_root)
    if component == "runtime":
        if action == "install":
            if runtime_root.parent == runtime_root:
                return emit({"status": "manual_action_required", "diagnostic": {
                    "code": "cli.refused_root_path", "safe_to_retry": False,
                    "next": f"refusing to install into {runtime_root}"}}, 2)
            install_source = source_root or bundled_source_root()
            try:
                inside_runtime = runtime_root.resolve() in install_source.resolve().parents
            except OSError:
                inside_runtime = False
            if inside_runtime:
                return emit({"status": "manual_action_required", "diagnostic": {
                    "code": "runtime.install_needs_source", "safe_to_retry": True,
                    "next": f"{install_source} is inside {runtime_root}; pass "
                            "--source-root <verified checkout> to install a source tree, "
                            f"or '{control_path(runtime_root)} repair' to rebuild in place"}}, 2)
            try:
                runtime_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return emit({"status": "manual_action_required", "diagnostic": {
                    "code": "cli.root_not_writable", "safe_to_retry": True,
                    "next": f"{runtime_root} is not writable ({exc}); "
                            f"re-run with --runtime-root {fallback_runtime_root()}"}}, 2)
            return install(runtime_root, install_source, version or "local", client=client)
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
        return mcp_status(runtime_root, client=client)
    if action == "stop":
        return not_applicable(
            component, action,
            "the stdio server lives only while its client holds stdin and stdout",
            "close the client session that started it")
    return not_applicable(
        component, action,
        "the launcher comes from the Runtime install; the client-side entry is mcp_server",
        f"{control} mcp status")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "install", "repair", *COMPONENTS))
    parser.add_argument("action", nargs="?", default="status",
                        help="component verb: " + ", ".join(ACTIONS))
    parser.add_argument("--runtime-root", type=Path, default=default_runtime_root())
    parser.add_argument("--source-root", type=Path,
                        help="verified Runtime checkout; never defaults to a floating remote ref")
    parser.add_argument("--version",
                        help="immutable Runtime label; also selects what 'runtime uninstall' removes")
    parser.add_argument("--client", choices=("native", "codex"), default="native",
                        help="MCP config recipient; local image references are enabled by default "
                             "for all clients sharing this host filesystem")
    args = parser.parse_args(argv)

    runtime_root = args.runtime_root.expanduser().resolve()
    if args.command in COMPONENTS:
        if args.action not in ACTIONS:
            return emit({"status": "manual_action_required", "diagnostic": {
                "code": "cli.unknown_action", "safe_to_retry": True,
                "next": f"{args.command} supports: " + ", ".join(ACTIONS)}}, 2)
        return component_action(runtime_root, args.command, args.action,
                                source_root=args.source_root, version=args.version,
                                client=args.client)
    if args.command == "status":
        return emit(status(runtime_root))
    if args.command == "install":
        return component_action(runtime_root, "runtime", "install",
                                source_root=args.source_root, version=args.version,
                                client=args.client)
    if args.command == "repair":
        return repair(runtime_root)
    return emit(status(runtime_root))


if __name__ == "__main__":
    raise SystemExit(main())
