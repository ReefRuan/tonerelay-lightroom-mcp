"""Local installation ledger and offline removal; no Runtime or Lightroom calls.

The ledger records individual files, never ownership of arbitrary directory
contents. Client adapters deliberately cover JSON mcpServers only; other clients
use their official removal UI/API before the operator confirms the inventory.
"""
from __future__ import annotations

import hashlib
import contextlib
import fcntl
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

SCHEMA = "tonerelay-install-manifest-v1"
NAME = "install-manifest.json"


@contextlib.contextmanager
def operation_lock(runtime, installation_root=None):
    base = Path(installation_root).resolve() if installation_root else (
        runtime.parent if (runtime.parent / NAME).is_file() else runtime)
    if base == base.parent or base == Path.home():
        raise ValueError("Use a dedicated installation directory")
    base.mkdir(parents=True, exist_ok=True)
    path = base / ".lifecycle.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another installation operation is active; wait for it to finish") from exc
        yield
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            temporary.chmod(0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(path):
    if path.is_symlink():
        return {"kind": "link", "target": os.readlink(path)}
    if path.is_file():
        with path.open("rb") as stream:
            result = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(chunk)
        return {"kind": "file", "sha256": result.hexdigest()}
    return None


def present(path):
    return path.exists() or path.is_symlink()


class Ledger:
    def __init__(self, runtime_root, *, installation_root=None, create=False):
        self.runtime = Path(runtime_root).resolve()
        candidate = self.runtime.parent / NAME
        self.path = ((Path(installation_root).resolve() / NAME) if installation_root else
                     candidate if candidate.is_file() else self.runtime / NAME)
        self.base = self.path.parent
        if self.base == self.base.parent or self.base == Path.home():
            raise ValueError("Use a dedicated installation directory")
        if self.base != self.runtime and self.runtime != self.base / "runtime":
            raise ValueError("Grouped layout requires installation-root/runtime")
        if self.path.is_symlink():
            raise ValueError("Installation manifest must not be a symlink")
        if self.path.exists():
            self.record = json.loads(self.path.read_text(encoding="utf-8"))
            if (self.record.get("schema") != SCHEMA or
                    self.record.get("runtime_root") != str(self.runtime) or
                    self.record.get("installation_root") != str(self.base)):
                raise ValueError("Manifest identity does not match the requested installation")
        elif create:
            self.record = {"schema": SCHEMA, "installation_id": uuid.uuid4().hex,
                           "installation_root": str(self.base), "runtime_root": str(self.runtime),
                           "files": {}, "directories": [], "clients": {}, "sources": [],
                           "data_roots": [], "phase": "prepared"}
            self.save()
        else:
            raise ValueError("No installation manifest; inspect the legacy installation before removal")

    def save(self):
        self.record["updated_at"] = time.time()
        atomic_json(self.path, self.record)

    def backup_directory(self):
        directory = Path(self.record.get("backup_directory", self.base / "backups"))
        self.local(str(directory.relative_to(self.base)))
        if directory.is_symlink():
            raise ValueError("Backup directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        return directory

    def run_directory(self):
        directory = Path(self.record.get("run_directory", self.runtime / "run"))
        self.local(str(directory.relative_to(self.base)))
        return directory

    def run_directories(self):
        directories = {self.run_directory()}
        for value in self.record.get("prior_run_directories", []):
            path = Path(value)
            self.local(str(path.relative_to(self.base)))
            directories.add(path)
        return sorted(directories)

    def retains(self, path):
        retained = [Path(value) for value in self.record.get("retained_paths", [])]
        retained.extend(Path(value) for value in self.record["data_roots"])
        retained.append(Path(self.record.get("backup_directory", self.base / "backups")))
        return any(value == path or value in path.parents or path in value.parents for value in retained)

    def local(self, relative):
        value = Path(relative)
        if value.is_absolute() or ".." in value.parts or not value.parts:
            raise ValueError("Invalid manifest path")
        path = self.base / value
        if path.parent.resolve() != self.base and self.base not in path.parent.resolve().parents:
            raise ValueError(f"Manifest path traverses an external link: {relative}")
        return path

    def remember(self, path, *, mutable=False):
        # Canonicalize ancestors (e.g. macOS /var -> /private/var) while retaining
        # the leaf itself: a managed symlink is owned as a link, never its target.
        path = Path(path)
        path = path.parent.resolve() / path.name
        relative = str(path.relative_to(self.base))
        self.local(relative)
        value = fingerprint(path)
        if value:
            self.record["files"][relative] = {**value, "mutable": mutable}
        elif path.is_dir():
            if relative not in self.record["directories"]:
                self.record["directories"].append(relative)

    def remember_tree(self, path):
        """Only call for a newly created subtree or explicitly adopted source."""
        path = Path(path)
        path = path.parent.resolve() / path.name
        if not present(path):
            return
        self.remember(path)
        parent = path.parent
        while parent != self.base and self.base in parent.parents:
            self.remember(parent)
            parent = parent.parent
        if path.is_dir() and not path.is_symlink():
            for parent, directories, files in os.walk(path, followlinks=False):
                for name in directories + files:
                    self.remember(Path(parent) / name)
        self.save()

    def checkpoint(self, phase, data=None):
        self.record["phase"] = phase
        if data is not None and str(data) not in self.record["data_roots"]:
            self.record["data_roots"].append(str(data))
        if data is not None:
            self.record["selected_data_root"] = str(data)
        self.save()

    def install_files(self, paths):
        """Track exact files written by the installer, without adopting neighbours."""
        for path in paths:
            self.remember(path, mutable=Path(path).parent.name in ("run", "logs"))
            parent = Path(path).parent
            while parent != self.base and self.base in parent.parents:
                self.remember(parent)
                parent = parent.parent
        self.save()

    def file_report(self):
        removable, changed, protected = [], [], []
        for relative in self.record["directories"]:
            path = self.local(relative)
            if present(path) and (path.is_symlink() or not path.is_dir()):
                changed.append(relative)
        for relative, expected in self.record["files"].items():
            path = self.local(relative)
            if not present(path):
                continue
            # Never follow a directory replaced with a link, even within this root.
            if any(parent.is_symlink() for parent in path.parents if parent != self.base
                   and self.base in parent.parents):
                changed.append(relative)
                continue
            if self.retains(path):
                protected.append(relative)
                continue
            actual = fingerprint(path)
            if actual and (actual == {k: v for k, v in expected.items() if k != "mutable"}
                           or (expected.get("mutable") and actual["kind"] == expected["kind"] == "file")):
                removable.append(relative)
            else:
                changed.append(relative)
        return {"removable": removable, "changed": changed, "protected": protected}


def prepare(installation_root):
    base = installation_root.expanduser().resolve()
    if base == base.parent or base == Path.home():
        raise ValueError("Use a dedicated installation directory")
    if base.exists() and any(path.name != ".lifecycle.lock" for path in base.iterdir()) and not (base / NAME).is_file():
        raise ValueError("Prepare requires an empty directory; existing source can use install --installation-root")
    ledger = Ledger(base / "runtime", installation_root=base, create=True)
    # Leave source absent so git clone can create it; record the reserved location.
    ledger.record["source_destination"] = str(base / "source")
    ledger.save()
    return {"status": "prepared", "installation_root": str(base),
            "runtime_root": str(ledger.runtime), "source_root": str(base / "source"),
            "manifest": str(ledger.path)}


def record_client(ledger, client, config, key, *, kind="json", evidence=None):
    path = config.expanduser().absolute()
    if kind == "official":
        ledger.record["clients"][client] = {"kind": kind, "config": str(path), "key": key,
                                            "removal_evidence": evidence,
                                            "observed_fingerprint": fingerprint(path)}
        ledger.save()
        return {"status": "recorded", "client": client, "manifest": str(ledger.path)}
    if path.is_symlink():
        raise ValueError("Use the client's actual configuration file, not a symlink")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("mcpServers"), dict):
        raise ValueError("Expected a JSON object with mcpServers")
    entry = document["mcpServers"][key]
    command = entry.get("command") if isinstance(entry, dict) else None
    if not isinstance(command, str) or Path(command).expanduser().absolute() != ledger.runtime / "bin/tonerelay-mcp":
        raise ValueError("JSON adapter requires this Runtime's exact MCP launcher; use official client removal otherwise")
    ledger.record["clients"][client] = {"config": str(path), "key": key,
                                       "entry_sha256": digest(json.dumps(entry, sort_keys=True).encode())}
    environment = entry.get("env", {})
    if not isinstance(environment, dict):
        raise ValueError("Client environment must be an object")
    selected = environment.get("LR_XMP_MIMIC_RUNTIME_DATA_ROOT")
    if selected:
        if not isinstance(selected, str) or not Path(selected).is_absolute():
            raise ValueError("Client data root must be an absolute path")
        if selected not in ledger.record["data_roots"]:
            ledger.record["data_roots"].append(selected)
    ledger.save()
    return {"status": "recorded", "client": client, "manifest": str(ledger.path)}


def client_state(record):
    path = Path(record["config"])
    if record.get("kind") == "official":
        if record.get("removal_evidence") and fingerprint(path) == record.get("observed_fingerprint"):
            return "operator_verified_removed"
        return "official_removal_required"
    if path.is_symlink():
        return "changed"
    if not path.exists():
        return "removed"
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Client configuration must be a JSON object")
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Client mcpServers must be an object")
    if record["key"] not in servers:
        return "removed"
    value = digest(json.dumps(servers[record["key"]], sort_keys=True).encode())
    return "present" if value == record["entry_sha256"] else "changed"


def remove_client(ledger, name, record):
    if record.get("kind") == "official":
        if client_state(record) != "operator_verified_removed":
            raise ValueError(f"Remove {name} through its official interface and record the observed result")
        return
    if client_state(record) == "removed":
        return
    if client_state(record) != "present":
        raise ValueError(f"Client entry changed: {name}; inspect its official configuration")
    path = Path(record["config"])
    before = path.read_bytes()
    document = json.loads(before)
    entry = document["mcpServers"][record["key"]]
    if digest(json.dumps(entry, sort_keys=True).encode()) != record["entry_sha256"]:
        raise ValueError(f"Client entry changed during removal: {name}")
    backup = ledger.backup_directory() / f"{uuid.uuid4().hex}-{path.name}"
    with backup.open("xb") as stream:
        backup.chmod(0o600)
        stream.write(before)
        stream.flush()
        os.fsync(stream.fileno())
    record["backup"] = str(backup)
    ledger.save()
    del document["mcpServers"][record["key"]]
    if path.read_bytes() != before:
        raise ValueError(f"Client configuration changed concurrently: {name}; retry after inspecting it")
    mode = stat.S_IMODE(path.stat().st_mode)
    atomic_json(path, document)
    path.chmod(mode)


def process_snapshot(runtime, api):
    result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                            text=True, check=True, timeout=5)
    rows, lightroom, unattributed = [], [], []
    recorded = set()
    for directory in Ledger(runtime).run_directories():
        for path in directory.glob("mcp-*.json"):
            recorded.add(json.loads(path.read_text(encoding="utf-8"))["pid"])
    bridge = api.bridge_record(runtime)
    if bridge:
        recorded.add(bridge["pid"])
    for line in result.stdout.splitlines():
        columns = line.strip().split(None, 1)
        if len(columns) != 2:
            continue
        pid, command = int(columns[0]), columns[1]
        if "/Adobe Lightroom Classic.app/Contents/MacOS/" in command:
            lightroom.append(pid)
        if pid == os.getpid():
            continue
        service = any(marker in command for marker in ("-m workbench_mcp", "-m lr_ops", "mcp-run"))
        if service and pid not in recorded:
            executable = api.process_executable(pid)
            if executable and Path(executable).name in ("sh", "bash", "zsh", "fish", "csh", "tcsh", "ps", "rg", "grep"):
                continue
        if pid in recorded or (str(runtime) in command and service):
            rows.append({"pid": pid, "identity": api.process_identity(pid)})
        elif service:
            unattributed.append({"pid": pid, "identity": api.process_identity(pid)})
    return {"runtime": rows, "lightroom": lightroom, "unattributed": unattributed}


def job_state(ledger):
    latest = {}
    for data in ledger.record["data_roots"]:
        path = Path(data) / "lr-ops/jobs.jsonl"
        if not path.exists():
            continue
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Job journal exceeds inspection budget; inspect outstanding jobs explicitly")
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    raise ValueError("Incomplete job journal; outcome is unknown")
                row = json.loads(line)
                latest[(data, row["job_id"])] = row
    outstanding = [{"job_id": row["job_id"], "command": row.get("command"), "status": row.get("status")}
                   for row in latest.values() if row.get("status") not in ("done", "failed")]
    probes = [row for row in outstanding if row["command"] == "probe_client" and row["status"] == "queued"]
    return {"blocking": [row for row in outstanding if row not in probes], "queued_probes": probes}


def inventory(ledger, api):
    processes = process_snapshot(ledger.runtime, api)
    entry = api.lightroom_entry(ledger.runtime)
    files = ledger.file_report()
    clients = {name: client_state(record) for name, record in ledger.record["clients"].items()}
    plugin = {"entry": str(entry), "present": present(entry),
              "owned": entry.is_symlink() and ledger.runtime in entry.resolve().parents}
    known = set(ledger.record["files"]) | set(ledger.record["directories"]) | {NAME, ".lifecycle.lock"}
    known.update(str(Path(path).relative_to(ledger.base)) for path in ledger.record.get("retained_paths", []))
    residue = []
    scan_roots = [ledger.runtime]
    if ledger.record.get("managed_source"):
        scan_roots.append(Path(ledger.record["managed_source"]))
    for scan_root in scan_roots:
        for parent, directories, names in os.walk(scan_root, followlinks=False):
            directories[:] = [name for name in directories
                              if not any(Path(value) == Path(parent) / name or Path(value) in (Path(parent) / name).parents
                                         for value in [*ledger.record["data_roots"],
                                                       ledger.record.get("backup_directory", str(ledger.base / "backups"))])]
            for name in directories + names:
                path = Path(parent) / name
                relative = str(path.relative_to(ledger.base))
                if relative not in known and path != ledger.path:
                    residue.append(relative)
    checks = {"recorded_clients_removed": all(value in ("removed", "operator_verified_removed") for value in clients.values()),
              "owned_processes_stopped": not processes["runtime"] and not processes["unattributed"],
              "modules_entry_removed": not plugin["present"],
              "managed_files_removed": not files["removable"] and not files["changed"] and not residue}
    return {"manifest": str(ledger.path), "phase": ledger.record["phase"], "checks": checks,
            "resume_script": ledger.record.get("resume_script"),
            "clients": clients, "client_scope": "recorded entries; review other clients through their official interface",
            "processes": processes, "jobs": job_state(ledger), "lightroom_plugin": plugin,
            "files": files, "unrecorded": residue, "retained_data": ledger.record["data_roots"],
            "backups": ledger.record.get("backup_directory", str(ledger.base / "backups")),
            "verification": "offline; does not submit plugin jobs or verify client tool-list refresh"}


def compact_report(report):
    """Bound console output; the full owned-file inventory remains in the ledger."""
    if "files" in report:
        report["file_counts"] = {key: len(value) for key, value in report["files"].items()}
        report["files"] = {key: value[:25] for key, value in report["files"].items()}
    if "unrecorded" in report:
        report["unrecorded_count"] = len(report["unrecorded"])
        report["unrecorded"] = report["unrecorded"][:25]
    report["path_list_limit"] = 25
    return report


def stop_mcp(ledger, api):
    for path in (path for directory in ledger.run_directories() for path in directory.glob("mcp-*.json")):
        if path.is_symlink():
            raise ValueError("MCP process record must not be a symlink")
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("runtime_root") != str(ledger.runtime) or
                not isinstance(record.get("pid"), int) or isinstance(record["pid"], bool) or record["pid"] <= 0):
            raise ValueError("MCP process record belongs to another installation")
        pid, identity = record["pid"], record["identity"]
        version = record.get("version_root")
        if (record.get("schema") != "tonerelay-owned-mcp-v1" or
                path.name != f"mcp-{pid}.json" or not isinstance(version, str) or
                Path(version).parent != ledger.runtime / "versions" or
                Path(version).resolve().parent != ledger.runtime / "versions" or
                record.get("command") != [str(api.venv_python(Path(version))), "-m", "workbench_mcp"] or
                not isinstance(identity, dict) or
                not isinstance(identity.get("command"), str) or
                not identity["command"].endswith(" -m workbench_mcp")):
            raise ValueError("MCP launch ownership is unknown; stop it through its client")
        if not api.process_alive(pid):
            path.unlink()
            continue
        if not api.terminate_process(pid, identity):
            raise ValueError("MCP process identity changed; stop it through its client")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and api.process_alive(pid):
            time.sleep(0.1)
        if api.process_alive(pid):
            raise ValueError("MCP process did not exit; stop its client normally")
        path.unlink(missing_ok=True)


def uninstall(ledger, api, *, apply=False, clients_reviewed=False):
    report = inventory(ledger, api)
    if not apply:
        return {"status": "planned", **report}
    if not clients_reviewed:
        raise ValueError("Review all client integrations and shared-use impact, then pass --clients-reviewed within the authorized scope")
    if report["processes"]["unattributed"]:
        return {"status": "blocked_process_ownership", **report}
    if report["jobs"]["blocking"]:
        return {"status": "blocked_jobs", **report}
    if any(value not in ("present", "removed", "operator_verified_removed") for value in report["clients"].values()):
        return {"status": "blocked_client_changed", **report}
    # The installed interpreter/control and the downloaded source may be removed.
    # Save a standalone runner before touching them so interruption is recoverable.
    runner = ledger.backup_directory() / "uninstaller"
    if runner.is_symlink():
        raise ValueError("Recovery runner directory must not be a symlink")
    runner.mkdir(exist_ok=True)
    for name in ("tonerelay_bootstrap_macos.py", "tonerelay_lifecycle.py"):
        destination = runner / name
        if destination.is_symlink():
            raise ValueError("Recovery script must not be a symlink")
        source = Path(__file__).with_name(name)
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
    configuration_source = getattr(api, "_config_path", None)
    if configuration_source and Path(configuration_source).is_file():
        destination = runner / "configuration.py"
        if destination.is_symlink():
            raise ValueError("Recovery configuration module must not be a symlink")
        if Path(configuration_source).resolve() != destination.resolve():
            shutil.copyfile(configuration_source, destination)
    ledger.record["resume_script"] = str(runner / "tonerelay_bootstrap_macos.py")
    ledger.checkpoint("uninstall_started")
    for name, record in ledger.record["clients"].items():
        remove_client(ledger, name, record)
    ledger.checkpoint("clients_removed")
    # Read again after disconnecting clients; no new diagnostic jobs are created.
    if job_state(ledger)["blocking"]:
        raise ValueError("Outstanding jobs changed after disconnect; investigate before stopping processes")
    stop_mcp(ledger, api)
    # Existing Bridge stop performs creation-identity checks. Capture its JSON so
    # this orchestration still returns one structured result.
    import contextlib
    import io
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = api.bridge_stop(ledger.runtime)
    bridge = json.loads(output.getvalue())
    if code or bridge.get("listening"):
        return {"status": "blocked_bridge", "bridge": bridge, **inventory(ledger, api)}
    time.sleep(0.5)
    after = process_snapshot(ledger.runtime, api)
    if after["runtime"] or after["unattributed"]:
        return {"status": "blocked_processes", **inventory(ledger, api)}
    ledger.checkpoint("processes_stopped")
    # Ask for a normal quit before removing code potentially loaded in Lightroom.
    if after["lightroom"]:
        return {"status": "needs_lightroom_quit", "manifest": str(ledger.path),
                "resume_script": ledger.record["resume_script"],
                "next": "Save work and normally quit Lightroom, then resume the same uninstall command"}
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = api.plugin_uninstall(ledger.runtime)
    if code:
        return {"status": "blocked_registration", "plugin": json.loads(output.getvalue())}
    ledger.checkpoint("registration_removed")
    report = inventory(ledger, api)
    # Retain changed/unrecorded files for explicit inspection, never expand the
    # delete set to a recursive root deletion. Each successful unlink is resumable.
    # Children precede symlinks such as active; do not make ownership checks
    # depend on whether an unrelated earlier path has already disappeared.
    for relative in sorted(report["files"]["removable"], key=lambda value: len(Path(value).parts), reverse=True):
        path = ledger.local(relative)
        expected = ledger.record["files"][relative]
        actual = fingerprint(path)
        if not actual:
            continue
        if not expected.get("mutable") and actual != {k: v for k, v in expected.items() if k != "mutable"}:
            raise ValueError(f"File changed during removal: {relative}")
        path.unlink()
    for relative in sorted(ledger.record["directories"], key=lambda value: len(Path(value).parts), reverse=True):
        path = ledger.local(relative)
        if path.is_symlink() or ledger.retains(path):
            continue
        try:
            path.rmdir()
        except OSError:
            pass  # Nonempty directories are reported, never recursively removed.
    ledger.checkpoint("software_removed")
    report = inventory(ledger, api)
    incomplete = (report["files"]["removable"] or report["files"]["changed"] or report["unrecorded"]
                  or report["processes"]["runtime"] or report["processes"]["unattributed"] or report["lightroom_plugin"]["present"]
                  or any(value not in ("removed", "operator_verified_removed") for value in report["clients"].values()))
    return {"status": "residual_review_required" if incomplete else "software_removed",
            "resume_script": ledger.record["resume_script"],
            "client_refresh": "verify the real client's loaded integrations; reconnect only if needed", **report}
