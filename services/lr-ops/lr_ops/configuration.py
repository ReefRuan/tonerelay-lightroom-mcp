"""One validated configuration file, shared by bootstrap, MCP and Bridge.

This module uses only the standard library so bootstrap can load it before
creating a virtual environment. Installation records and editing state are
separate from configuration. There is deliberately no legacy-file migration.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

CONFIG_ENV = "TONERELAY_CONFIG"
PATH_NAMES = ("data", "cache", "tmp", "logs", "backups", "downloads", "run")


class ConfigError(ValueError):
    pass


class ConfigConflict(ConfigError):
    pass


def default_root() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ToneRelay"
    return Path.home() / "Library" / "Application Support" / "ToneRelay"


def defaults() -> dict:
    return {
        "schema_version": 1, "revision": 0,
        "paths": {name: name for name in PATH_NAMES},
        "targets": {"active": "local", "targets": [{
            "id": "local", "label": "本机 Lightroom", "base": "http://127.0.0.1:17777",
            "token": None, "catalog_id": None, "access_mode": "full",
            "catalog_identity": None, "is_default": True,
        }]},
        "presentation": {"codex_preview_max_edge": 1024, "edit_render_max_edge": 2048},
        "folders": {"mode": "full", "roots": [], "extensions": None, "recursive": False},
        "execution": {"allow_passthrough": True, "allow_structured": True},
        "bridge": {"mode": "external", "host": "127.0.0.1", "port": 17777, "token": None},
    }


def _keys(value, required, label):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ConfigError(f"invalid {label} fields")


def _url(value):
    try:
        url = urlsplit(value)
        port = url.port
        return (url.scheme in ("http", "https") and bool(url.hostname)
                and not url.username and not url.password and not url.fragment
                and (port is None or 1 <= port <= 65535))
    except (ValueError, TypeError, AttributeError):
        return False


def validate(value: dict, root: Path) -> dict:
    """Reject damaged/incomplete configuration rather than replacing it with defaults."""
    _keys(value, defaults(), "config")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ConfigError("unsupported config schema_version")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise ConfigError("invalid config revision")
    _keys(value["paths"], PATH_NAMES, "paths")
    resolved = []
    for name, relative in value["paths"].items():
        if (not isinstance(relative, str) or not relative or "\\" in relative
                or ":" in relative or Path(relative).is_absolute()
                or any(not part or part.startswith(".") for part in relative.split("/"))
                or relative.split("/")[0] in ("versions", "bin", "active", "config.json", "install-manifest.json", "managed-root.json", "dist.json")):
            raise ConfigError(f"paths.{name} must be a dedicated relative directory")
        try:
            path = (root / relative).resolve()
        except (OSError, RuntimeError) as exc:
            raise ConfigError(f"cannot resolve paths.{name}") from exc
        if root not in path.parents or path == root:
            raise ConfigError(f"paths.{name} escapes the installation root")
        if any(p.is_symlink() for p in (root / relative, *(root / relative).parents) if p != root and root in p.parents):
            raise ConfigError(f"paths.{name} must not contain a symlink")
        if any(path == other or path in other.parents or other in path.parents for other in resolved):
            raise ConfigError("configured directories must not overlap")
        resolved.append(path)
    targets = value["targets"]
    _keys(targets, ("active", "targets"), "targets")
    if not isinstance(targets["targets"], list) or not targets["targets"]:
        raise ConfigError("targets must contain at least one target")
    ids = set()
    for target in targets["targets"]:
        _keys(target, defaults()["targets"]["targets"][0], "target")
        if (not isinstance(target, dict) or not isinstance(target.get("id"), str)
                or not target["id"] or target["id"] in ids or not _url(target.get("base"))
                or target.get("access_mode") not in ("safe", "full")):
            raise ConfigError("invalid target identity, endpoint or access_mode")
        if not isinstance(target["label"], str) or not target["label"] or type(target["is_default"]) is not bool:
            raise ConfigError("invalid target label or default flag")
        for field in ("token", "catalog_id", "catalog_identity"):
            if target.get(field) is not None and not isinstance(target[field], str):
                raise ConfigError(f"invalid target {field}")
        if target["catalog_identity"] is not None and not target["catalog_identity"].strip():
            raise ConfigError("invalid target catalog_identity")
        ids.add(target["id"])
    if not isinstance(targets["active"], str):
        raise ConfigError("active target must be an id")
    if targets["active"] not in ids:
        raise ConfigError("active target is not present")
    _keys(value["presentation"], ("codex_preview_max_edge", "edit_render_max_edge"), "presentation")
    for key, minimum in (("codex_preview_max_edge", 64), ("edit_render_max_edge", 256)):
        edge = value["presentation"][key]
        if type(edge) is not int or not minimum <= edge <= 2048:
            raise ConfigError(f"invalid presentation.{key}")
    folders = value["folders"]
    _keys(folders, ("mode", "roots", "extensions", "recursive"), "folders")
    if folders["mode"] not in ("full", "restricted") or type(folders["recursive"]) is not bool:
        raise ConfigError("invalid folders mode or recursive flag")
    if not isinstance(folders["roots"], list) or any(
            not isinstance(p, str) or not Path(p).expanduser().is_absolute() for p in folders["roots"]):
        raise ConfigError("folders.roots must contain absolute input directories")
    if folders["mode"] == "full" and folders["roots"]:
        raise ConfigError("full folder access cannot also specify restricted roots")
    extensions = folders["extensions"]
    if extensions is not None and (not isinstance(extensions, list) or not extensions or any(
            not isinstance(e, str) or len(e) < 2 or not e.startswith(".")
            or any(c in e for c in ("/", "\\", "\x00")) for e in extensions)):
        raise ConfigError("invalid folders.extensions")
    _keys(value["execution"], ("allow_passthrough", "allow_structured"), "execution")
    if any(type(flag) is not bool for flag in value["execution"].values()):
        raise ConfigError("execution flags must be booleans")
    bridge = value["bridge"]
    # Existing complete configurations remain valid without rewriting them.
    # An omitted mode means the client-managed default: connect to a Bridge
    # started elsewhere; owning the local Bridge (builtin) is opt-in.
    if not isinstance(bridge, dict) or set(bridge) not in (
            {"host", "port", "token"}, {"mode", "host", "port", "token"}):
        raise ConfigError("invalid bridge fields")
    if bridge.get("mode", "external") not in ("builtin", "external"):
        raise ConfigError("invalid bridge mode")
    if (not isinstance(bridge["host"], str) or not bridge["host"]
            or type(bridge["port"]) is not int or not 1 <= bridge["port"] <= 65535
            or (bridge["token"] is not None and not isinstance(bridge["token"], str))):
        raise ConfigError("invalid bridge settings")
    return value


class ConfigStore:
    def __init__(self, path: Path, *, required=False):
        raw = Path(path).expanduser().absolute()
        if raw.name != "config.json":
            raise ConfigError("the configuration entry point must be config.json")
        if raw.is_symlink():
            raise ConfigError("config.json must not be a symlink")
        self.path = raw.parent.resolve() / raw.name
        self.root = self.path.parent
        self.required = required

    def read(self) -> dict:
        if self.path.is_symlink():
            raise ConfigError("config.json must not be a symlink")
        if not self.path.exists():
            if self.required:
                raise ConfigError("config.json is missing; prepare this installation first")
            return validate(defaults(), self.root)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("config.json is unreadable; restore a verified backup") from exc
        return validate(value, self.root)

    def initialize(self) -> dict:
        with self._lock():
            if self.path.exists():
                return self.read()
            value = validate(defaults(), self.root)
            self._write(value)
            return value

    def update(self, section: str, change, *, expected_revision=None) -> dict:
        if section not in defaults() or section in ("schema_version", "revision"):
            raise ConfigError("unknown config section")
        with self._lock():
            current = self.read()
            if expected_revision is not None and current["revision"] != expected_revision:
                raise ConfigConflict("configuration changed; read it again before updating")
            changed = copy.deepcopy(current)
            result = change(changed[section])
            if result is not None:
                changed[section] = result
            changed["revision"] += 1
            validate(changed, self.root)
            if self.path.exists():
                backup = self.root / current["paths"]["backups"] / "config-last-good.json"
                self._atomic_write(backup, current)
            self._write(changed)
            return changed

    def paths(self, value=None) -> dict[str, Path]:
        return {name: (self.root / relative).resolve()
                for name, relative in (self.read() if value is None else value)["paths"].items()}

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".config.lock"
        if lock_path.is_symlink():
            raise ConfigError("config lock must not be a symlink")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            if os.name == "nt":
                import msvcrt
                def acquire():
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                def release():
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(handle, fcntl.LOCK_UN)
            deadline = time.monotonic() + 5
            while True:
                try:
                    acquire(); break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ConfigConflict("configuration is busy; retry after the other writer finishes") from exc
                    time.sleep(0.02)
            try:
                # Windows denies reading a byte locked by another process.
                # Lock even an empty file first, then initialize under that lock.
                if os.name == "nt" and os.fstat(handle.fileno()).st_size == 0:
                    handle.seek(0)
                    handle.write(b"\0")
                    handle.flush()
                yield
            finally:
                release()

    def _write(self, value):
        self._atomic_write(self.path, value)

    @staticmethod
    def _atomic_write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError as exc:
                    # A Windows reader may briefly deny replacement. Retry the
                    # same flushed commit, never its caller's change operation.
                    if (getattr(exc, "winerror", None) not in (5, 32, 33)
                            or time.monotonic() >= deadline):
                        raise
                    time.sleep(0.01)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def active_store() -> ConfigStore | None:
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return ConfigStore(Path(explicit), required=True)
    return None


def installed_runtime_root(source_root: Path) -> Path | None:
    """Recognize the installation that actually supplied code/interpreter.

    Older Windows clients launch Python directly without TONERELAY_ACTIVE.
    Directory names alone are insufficient: active source and venv must match.
    """
    source = Path(source_root).resolve()
    active = os.environ.get("TONERELAY_ACTIVE")
    if active and Path(active).resolve() / "source" == source:
        return Path(active).absolute().parent
    config = os.environ.get(CONFIG_ENV)
    if config:
        root = Path(config).absolute().parent
        if (root / "active").resolve() / "source" == source:
            return root
    if source.name == "source" and source.parent.parent.name == "versions":
        version = source.parent
        root = version.parent.parent
        if ((root / "active").resolve() == version
                and Path(sys.prefix).resolve() == version / ".venv"
                and (version / ".venv/pyvenv.cfg").is_file()):
            return root
    return None


def for_workbench(root: Path) -> ConfigStore:
    """Explicit isolated stores used by embedded callers never share installed config."""
    active = active_store()
    if active and Path(root).resolve() == active.paths()["data"] / "workbench":
        return active
    return ConfigStore(Path(root) / "config.json")


def runtime_data_root() -> Path:
    active = active_store()
    if active:
        return active.paths()["data"]
    # An explicit unmanaged data root is useful for embedded/test callers.
    # It does not import any old configuration files or change managed installs.
    return Path(os.environ.get("LR_XMP_MIMIC_RUNTIME_DATA_ROOT", default_root() / "data"))


def process_environment(store: ConfigStore, base=None, *, value=None) -> dict[str, str]:
    value = store.read() if value is None else value
    paths = store.paths(value)
    env = dict(os.environ if base is None else base)
    env.update({CONFIG_ENV: str(store.path),
                "LR_XMP_MIMIC_RUNTIME_DATA_ROOT": str(paths["data"]),
                "PIP_CACHE_DIR": str(paths["cache"] / "pip"),
                "XDG_CACHE_HOME": str(paths["cache"] / "xdg"),
                "PYTHONPYCACHEPREFIX": str(paths["cache"] / "python"),
                "TMPDIR": str(paths["tmp"]), "TEMP": str(paths["tmp"]), "TMP": str(paths["tmp"]),
                "LR_XMP_MIMIC_BRIDGE_HOST": value["bridge"]["host"],
                "LR_XMP_MIMIC_BRIDGE_PORT": str(value["bridge"]["port"]),
                "LR_XMP_MIMIC_ALLOW_PASSTHROUGH": str(value["execution"]["allow_passthrough"]).lower(),
                "LR_XMP_MIMIC_ALLOW_STRUCTURED": str(value["execution"]["allow_structured"]).lower()})
    active = next(t for t in value["targets"]["targets"] if t["id"] == value["targets"]["active"])
    env["LROPS_BASE"] = active["base"]
    # Compatibility callers connect to the selected Target; Bridge itself
    # reads its own credential from config.json.
    env["LR_XMP_MIMIC_LOCAL_TOKEN"] = active["token"] or ""
    env["LR_XMP_MIMIC_ACCESS_MODE"] = active["access_mode"]
    env.pop("LR_XMP_MIMIC_FOLDER_CONFIG", None)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    for name in ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT", "PIP_SRC", "PIP_LOG",
                 "PIP_REPORT", "PIP_BUILD_TRACKER"):
        env.pop(name, None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONUSERBASE", None)
    env["PIP_USER"] = "false"
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PYTHONNOUSERSITE"] = "1"
    return env


def apply_process_environment(store: ConfigStore):
    value = store.read()
    paths = store.paths(value)
    env = process_environment(store, value=value)
    generation = env.get("TONERELAY_MCP_GENERATION", "")
    if (env.get("TONERELAY_MCP_WORKER") == "1" and len(generation) == 32
            and all(c in "0123456789abcdef" for c in generation)):
        # Do not reuse same-second/same-size bytecode between MCP workers.
        env["PYTHONPYCACHEPREFIX"] = str(paths["cache"] / "python" / "mcp" / generation)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    os.environ.clear()
    os.environ.update(env)
    tempfile.tempdir = env["TMPDIR"]
    sys.pycache_prefix = env["PYTHONPYCACHEPREFIX"]
    os.chdir(paths["run"])


def process_start(pid):
    if os.name == "nt":
        return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "launch":
        raise ConfigError("expected launch -- <python arguments>")
    args = sys.argv[2:]
    if args[0] == "--":
        args = args[1:]
    if not args:
        raise ConfigError("missing Python entry point")
    store = active_store()
    if store is None:
        raise ConfigError("TONERELAY_CONFIG is required for a managed launch")
    apply_process_environment(store)
    if "workbench_mcp" in args:
        # An ownership hint for refusal during uninstall, never kill authority.
        role = "bootstrap-diagnostic" if os.environ.get("TONERELAY_PROCESS_ROLE") == "bootstrap-diagnostic" else "client"
        ConfigStore._atomic_write(store.paths()["run"] / f"mcp-{os.getpid()}.json", {
            "pid": os.getpid(), "started": process_start(os.getpid()),
            "runtime_root": str(store.root), "owner": role,
        })
    os.execv(sys.executable, [sys.executable, *args])


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
