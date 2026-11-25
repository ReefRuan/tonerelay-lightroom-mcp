"""Standalone HTTP client for ToneRelay's local lr-ops Lightroom bridge."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:17777"
MAX_BATCH_COMMANDS = 64

DENY_SETTINGS = frozenset({
    "PresetType", "Cluster", "UUID", "HasSettings", "CameraProfileDigest",
    "History", "Snapshots", "ToneCurveName", "ToneCurveName2012",
})

PROTECTED_RE = re.compile(
    r"保护|锁定|勿改|只读|留档|\b(?:protected|locked|readonly|read[\s_-]+only|do[\s_-]+not[\s_-]+edit)\b",
    re.IGNORECASE,
)


def is_protected_name(copy_name: str | None) -> bool:
    """Mirror CopyPolicy.lua protected-name rules on the client side."""
    if not copy_name:
        return False
    return bool(PROTECTED_RE.search(copy_name))


class LrOpsError(RuntimeError):
    """Error communicating with lr-ops bridge or executing a Lightroom command."""


class BatchExecutionError(LrOpsError):
    """Raised when command_batch stops at a specific 1-based failed_at_index."""

    def __init__(self, message: str, failed_at_index: int | None, results_so_far: list[dict]):
        super().__init__(message)
        self.failed_at_index = failed_at_index
        self.results_so_far = results_so_far


def _env_fallback_config() -> dict:
    return {
        "base": str(os.environ.get("LROPS_BASE") or DEFAULT_BASE).rstrip("/"),
        "token": os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN"),
        "catalog_identity": None,
        "access_mode": None,
        "catalog_id": None,
    }


def _safe_home() -> Path | None:
    try:
        return Path.home()
    except RuntimeError:
        return None


def discover_tonerelay_config(config_path: Path | str | None = None) -> Path | None:
    """Locate ToneRelay's workbench config.json across platforms without hardcoded drive paths.

    Resolution order:
    1. Explicit ``config_path`` argument (returns None if explicitly passed path does not exist)
    2. ``TONERELAY_CONFIG`` environment variable
    3. ``TONERELAY_DATA_ROOT`` / ``LR_XMP_MIMIC_DATA_ROOT`` (``<root>/workbench/config.json`` or ``<root>/data/workbench/config.json``)
    4. OS user application data dir (Windows ``%LOCALAPPDATA%``, macOS ``~/Library/Application Support``, Linux ``$XDG_DATA_HOME`` / ``~/.local/share``)
    5. CWD relative (``./data/workbench/config.json``, ``./workbench/config.json``)
    """
    if config_path is not None:
        explicit = Path(config_path).expanduser()
        return explicit if explicit.is_file() else None

    candidates: list[Path] = []

    env_cfg = os.environ.get("TONERELAY_CONFIG")
    if env_cfg:
        candidates.append(Path(env_cfg).expanduser())

    for root_var in ("TONERELAY_DATA_ROOT", "LR_XMP_MIMIC_DATA_ROOT"):
        root_val = os.environ.get(root_var)
        if root_val:
            root_path = Path(root_val).expanduser()
            candidates.append(root_path / "workbench" / "config.json")
            candidates.append(root_path / "data" / "workbench" / "config.json")

    home = _safe_home()
    os_base: Path | None = None
    if sys.platform == "win32":
        local_raw = os.environ.get("LOCALAPPDATA")
        if local_raw:
            os_base = Path(local_raw) / "ToneRelay"
        elif home is not None:
            os_base = home / "AppData" / "Local" / "ToneRelay"
    elif sys.platform == "darwin":
        if home is not None:
            os_base = home / "Library" / "Application Support" / "ToneRelay"
    else:
        xdg_raw = os.environ.get("XDG_DATA_HOME")
        if xdg_raw:
            os_base = Path(xdg_raw) / "ToneRelay"
        elif home is not None:
            os_base = home / ".local" / "share" / "ToneRelay"

    if os_base is not None:
        candidates.append(os_base / "workbench" / "config.json")
        candidates.append(os_base / "data" / "workbench" / "config.json")

    cwd = Path.cwd()
    candidates.append(cwd / "data" / "workbench" / "config.json")
    candidates.append(cwd / "workbench" / "config.json")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_tonerelay_config(config_path: Path | str | None = None) -> dict:
    """Load active target settings from ToneRelay's shared workbench config.json."""
    resolved = discover_tonerelay_config(config_path)
    if resolved is None:
        return _env_fallback_config()
    cfg = json.loads(resolved.read_text(encoding="utf-8"))
    targets_cfg = cfg.get("targets") or {}
    active_id = targets_cfg.get("active")
    targets = targets_cfg.get("targets") or []
    target = next((t for t in targets if t.get("id") == active_id), None)
    if target is None and targets:
        target = targets[0]
    if target is None:
        return _env_fallback_config()
    return {
        "base": str(target.get("base") or os.environ.get("LROPS_BASE") or DEFAULT_BASE).rstrip("/"),
        "token": target.get("token") if target.get("token") is not None else os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN"),
        "catalog_identity": target.get("catalog_identity"),
        "access_mode": target.get("access_mode"),
        "catalog_id": target.get("catalog_id"),
    }


def validate_wb_patch(patch: dict, before_settings: dict) -> None:
    """Guard against partial WB updates on untouched 'As Shot' RAWs (which lack Temperature/Tint)."""
    if ("Temperature" in patch) ^ ("Tint" in patch):
        missing = "Tint" if "Temperature" in patch else "Temperature"
        if missing not in before_settings:
            raise ValueError(
                f"Cannot apply partial RAW WB patch: {missing} is absent on this 'As Shot' photo. "
                "Provide BOTH Temperature and Tint together."
            )
    if ("IncrementalTemperature" in patch) ^ ("IncrementalTint" in patch):
        missing = "IncrementalTint" if "IncrementalTemperature" in patch else "IncrementalTemperature"
        if missing not in before_settings:
            raise ValueError(
                f"Cannot apply partial rendered WB patch: {missing} is absent. "
                "Provide BOTH IncrementalTemperature and IncrementalTint together."
            )


class LrOpsBridgeClient:
    """Strict serial client for ToneRelay's lr-ops HTTP queue."""

    def __init__(
        self,
        base: str | None = None,
        token: str | None = None,
        catalog_identity: str | None = None,
        access_mode: str | None = None,
        config_path: Path | str | None = None,
    ):
        loaded = load_tonerelay_config(config_path)
        self.base = (base or loaded["base"] or DEFAULT_BASE).rstrip("/")
        self.token = token if token is not None else loaded.get("token")
        self.catalog_identity = catalog_identity if catalog_identity is not None else loaded.get("catalog_identity")
        self.access_mode = access_mode if access_mode is not None else loaded.get("access_mode")

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 15.0) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-LR-XMP-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LrOpsError(f"lr-ops HTTP {exc.code} at {self.base}{path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LrOpsError(f"lr-ops unreachable at {self.base}: {exc}") from exc

    def health(self) -> dict:
        return self._request("GET", "/health", timeout=5.0)

    def _attach_envelope(self, params: dict | None) -> dict:
        out = dict(params or {})
        if self.catalog_identity and "expected_catalog_identity" not in out:
            out["expected_catalog_identity"] = self.catalog_identity
        if self.access_mode and "expected_access_mode" not in out:
            out["expected_access_mode"] = self.access_mode
        return out

    def _submit_and_wait(
        self,
        command: str,
        params: dict,
        *,
        timeout: float = 120.0,
        group: str = "scene-batch",
        poll_interval: float = 0.25,
    ) -> dict:
        resp = self._request("POST", "/jobs", {"command": command, "params": params, "group": group})
        job_id = resp.get("job_id")
        if not job_id:
            raise LrOpsError(f"Bridge returned no job_id for {command}: {resp}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self._request("GET", f"/jobs/{job_id}")
            status = job.get("status")
            if status == "interrupted":
                detail = job.get("error") or "bridge restarted after dispatch; result unknown"
                raise LrOpsError(f"Job {job_id} ({command}) interrupted; never replaying: {detail}")
            if status in ("done", "failed"):
                result = job.get("result") or {}
                if not result.get("ok"):
                    msg = str(result.get("error") or "unknown bridge command error")
                    if command == "command_batch":
                        raise BatchExecutionError(
                            msg,
                            result.get("failed_at_index"),
                            result.get("results_so_far") or [],
                        )
                    raise LrOpsError(f"{command} failed: {msg}")
                return result.get("data") or {}
            time.sleep(poll_interval)
        raise LrOpsError(f"Job {job_id} ({command}) timed out after {timeout}s")

    def probe(self, timeout: float = 15.0) -> dict:
        """Probe Lightroom plugin, verify open/authorized catalog, and bind missing identity."""
        params = {}
        if self.catalog_identity:
            params["expected_catalog_identity"] = self.catalog_identity
        if self.access_mode:
            params["expected_access_mode"] = self.access_mode
        data = self._submit_and_wait("probe_client", params, timeout=timeout)
        if not data.get("catalog_open"):
            raise LrOpsError("Lightroom has no open Catalog.")
        if not data.get("catalog_authorized"):
            raise LrOpsError(f"Active Catalog is not authorized (access_mode={data.get('access_mode')}).")
        if self.catalog_identity and data.get("catalog_identity") != self.catalog_identity:
            raise LrOpsError(
                f"Catalog identity mismatch: expected {self.catalog_identity}, "
                f"active {data.get('catalog_identity')}"
            )
        if not self.catalog_identity:
            self.catalog_identity = data.get("catalog_identity")
        if not self.access_mode:
            self.access_mode = data.get("access_mode")
        return data

    def run_command(
        self,
        command: str,
        params: dict | None = None,
        *,
        timeout: float = 120.0,
        group: str = "scene-batch",
    ) -> dict:
        return self._submit_and_wait(command, self._attach_envelope(params), timeout=timeout, group=group)

    def run_batch(
        self,
        commands: list[dict],
        *,
        timeout: float = 180.0,
        group: str = "scene-batch",
    ) -> list[dict]:
        """Execute up to 64 commands atomically under Lightroom's command_batch runner."""
        if not commands:
            return []
        if len(commands) > MAX_BATCH_COMMANDS:
            raise ValueError(f"command_batch limit is {MAX_BATCH_COMMANDS}, got {len(commands)}")
        payload = self._attach_envelope({"commands": commands})
        data = self._submit_and_wait("command_batch", payload, timeout=timeout, group=group)
        return data.get("results") or []

    def fetch_output(self, output_ref: str, destination: Path, timeout: float = 60.0) -> Path:
        req = urllib.request.Request(self.base + f"/files/{output_ref}/output", method="GET")
        if self.token:
            req.add_header("X-LR-XMP-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise LrOpsError(f"fetch_output HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LrOpsError(f"fetch_output unreachable: {exc}") from exc
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def list_catalog_folders(self, name_contains: str = "", depth: int = 4, limit: int = 200) -> list[dict]:
        out = self.run_command(
            "list_catalog_folders",
            {"name_contains": name_contains, "depth": depth, "limit": limit, "offset": 0},
        )
        return out.get("folders") or []

    def list_folder_photos(
        self,
        folder_ids: list[str],
        *,
        include_subfolders: bool = False,
        include_virtual_copies: bool = True,
    ) -> list[dict]:
        out = self.run_command(
            "list_folder_photos",
            {
                "folder_ids": folder_ids,
                "include_subfolders": include_subfolders,
                "include_virtual_copies": include_virtual_copies,
            },
        )
        return out.get("photos") or []

    def get_photo_marks(self, photo_uuids: list[str]) -> list[dict]:
        results = []
        for i in range(0, len(photo_uuids), 200):
            chunk = photo_uuids[i:i + 200]
            out = self.run_command("get_photo_marks", {"photo_uuids": chunk})
            results.extend(out.get("results") or [])
        return results

    def get_develop_snapshot(self, photo_uuid: str) -> dict:
        return self.run_command("get_develop_snapshot", {"photo_uuid": photo_uuid})

    def create_virtual_copy(self, source_uuid: str, name: str = "TRLR Scene") -> dict:
        return self.run_command("create_virtual_copy", {"photo_uuid": source_uuid, "name": name})

    def export_preview(self, photo_uuid: str, long_edge: int = 0, timeout: float = 180.0) -> dict:
        return self.run_command(
            "export_preview",
            {"photo_uuid": photo_uuid, "long_edge": long_edge},
            timeout=timeout,
        )
