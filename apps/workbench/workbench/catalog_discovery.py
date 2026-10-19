"""Read-only discovery of Lightroom Catalog files on the Runtime host.

The Lightroom plugin remains authoritative about which Catalog is open. This
module only inventories ``.lrcat`` file names and filesystem metadata so the
operator can identify that active Catalog; it never opens or parses a Catalog.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path


_SKIP_DIRS = frozenset({
    "$RECYCLE.BIN", "SYSTEM VOLUME INFORMATION", ".TRASH", ".TRASHES",
    ".SPOTLIGHT-V100", ".FSEVENTSD", ".DOCUMENTREVISIONS-V100",
    ".TEMPORARYITEMS",
})
_SKIP_SUFFIXES = (".lrdata", ".lrprev")
SCAN_SECONDS = 5.0
SCAN_DIRECTORIES = 10000


def catalog_identity_for_path(path: Path) -> str:
    """Mirror Lightroom's path identity without persisting or returning path."""
    normalized = str(path).replace("\\", "/").rstrip("/")
    normalized = normalized.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    digest = hashlib.md5(
        normalized.encode("utf-8", errors="surrogateescape")).hexdigest()
    return f"catalog-md5-v1:{digest}"


def _scan_roots() -> list[tuple[str, Path, str]]:
    """Return the home directory and mounted local data volumes for this OS."""
    home = Path.home()
    roots: list[tuple[str, Path, str]] = [("Home", home, "home")]

    if sys.platform == "darwin":
        volumes = Path("/Volumes")
        try:
            for path in sorted(volumes.iterdir(), key=lambda item: item.name.casefold()):
                if path.is_dir() and not path.is_symlink():
                    roots.append((f"Volume: {path.name}", path, "volume"))
        except OSError:
            pass
    elif os.name == "nt":
        try:
            import ctypes

            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for index in range(26):
                if not mask & (1 << index):
                    continue
                drive = f"{chr(ord('A') + index)}:\\"
                # Avoid walking network shares; a stalled remote mount can
                # otherwise make first-run discovery hang indefinitely.
                if ctypes.windll.kernel32.GetDriveTypeW(drive) in (2, 3):
                    path = Path(drive)
                    if path != home and path.exists():
                        roots.append((f"Drive {drive[:2]}", path, "volume"))
        except (AttributeError, OSError):
            pass
    else:
        for mount_root in (Path("/mnt"), Path("/media"), Path("/run/media")):
            try:
                for path in sorted(mount_root.iterdir(), key=lambda item: item.name.casefold()):
                    if path.is_dir() and not path.is_symlink():
                        roots.append((f"Mounted volume: {path.name}", path, "volume"))
            except OSError:
                continue

    unique: list[tuple[str, Path, str]] = []
    seen: set[str] = set()
    for label, root, kind in roots:
        try:
            key = os.path.normcase(str(root.absolute()))
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            unique.append((label, root, kind))
    return unique


def _location_hint(path: Path, root: Path, kind: str, label: str) -> str:
    try:
        relative = path.parent.relative_to(root)
        suffix = relative.as_posix()
        if suffix == ".":
            suffix = ""
    except ValueError:
        suffix = ""
    if kind == "home":
        parent = "~" if not suffix else f"~/{suffix}"
    else:
        parent = label if not suffix else f"{label} / {suffix}"
    return parent


def discover_catalogs(*, active_identity: str | None, active_catalog_id: str | None,
                      scan_local: bool = True, include_paths: bool = False) -> dict:
    """List accessible Catalog files and mark Lightroom's live active Catalog.

    File contents are never read. Permission and I/O failures are counted but
    their paths are not returned to the MCP caller.
    """
    rows: dict[str, dict] = {}
    roots_scanned: list[str] = []
    errors = 0
    deadline = time.monotonic() + SCAN_SECONDS
    visited: set[str] = set()
    limit_reason = None

    if scan_local:
        for label, root, kind in _scan_roots():
            if limit_reason:
                break
            try:
                if not root.is_dir():
                    continue
            except OSError:
                errors += 1
                continue
            roots_scanned.append(label)

            def onerror(_error):
                nonlocal errors
                errors += 1

            for directory, dirnames, filenames in os.walk(
                    root, topdown=True, followlinks=False, onerror=onerror):
                # Roots can overlap (home and its data volume) or resolve to
                # the same mount. Visit a canonical directory only once.
                try:
                    key = os.path.normcase(str(Path(directory).resolve()))
                except (OSError, RuntimeError):
                    errors += 1
                    dirnames[:] = []
                    continue
                if key in visited:
                    dirnames[:] = []
                    continue
                if time.monotonic() >= deadline or len(visited) >= SCAN_DIRECTORIES:
                    limit_reason = "time_budget" if time.monotonic() >= deadline else "directory_budget"
                    break
                visited.add(key)
                dirnames[:] = [name for name in dirnames
                               if name.upper() not in _SKIP_DIRS
                               and not name.lower().endswith(_SKIP_SUFFIXES)
                               and not (Path(directory) / name).is_symlink()]
                for filename in filenames:
                    if time.monotonic() >= deadline:
                        limit_reason = "time_budget"
                        break
                    if not filename.lower().endswith(".lrcat"):
                        continue
                    path = Path(directory) / filename
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        info = path.stat()
                        resolved_path = path.resolve(strict=True)
                    except (OSError, RuntimeError, ValueError):
                        errors += 1
                        continue
                    path_identity = catalog_identity_for_path(path)
                    resolved_identity = catalog_identity_for_path(resolved_path)
                    identity = (active_identity
                                if active_identity in (path_identity, resolved_identity)
                                else path_identity)
                    try:
                        modified_at = datetime.fromtimestamp(
                            info.st_mtime).astimezone().isoformat(timespec="seconds")
                    except (OverflowError, OSError, ValueError):
                        modified_at = None
                    row = {
                        "catalog_identity": identity,
                        "catalog_id": path.name,
                        "location_hint": _location_hint(path, root, kind, label),
                        "size_bytes": info.st_size,
                        "modified_at": modified_at,
                        "is_active": identity == active_identity,
                    }
                    if include_paths:
                        row["_local_path"] = str(path)
                    rows.setdefault(identity, row)
                if limit_reason:
                    break

    # A remote Target cannot be scanned from this Runtime host. Keep the
    # bridge-reported active Catalog visible, with no claim about its location.
    if active_identity and active_identity not in rows:
        rows[active_identity] = {
            "catalog_identity": active_identity,
            "catalog_id": active_catalog_id,
            "location_hint": (
                "远端 Lightroom 当前打开；本机无法扫描远端文件系统"
                if not scan_local else
                "Lightroom 当前打开；本机扫描范围内未找到对应文件"
            ),
            "size_bytes": None,
            "modified_at": None,
            "is_active": True,
        }

    catalogs = sorted(rows.values(), key=lambda row: (
        not row["is_active"],
        (row.get("catalog_id") or "").casefold(),
        row["location_hint"].casefold(),
    ))
    status = "complete" if scan_local and errors == 0 and not limit_reason else (
        "partial" if scan_local else "remote_inventory_unavailable")
    return {
        "scan_status": status,
        "scan_scope": ("home_and_mounted_local_volumes" if scan_local
                       else "active_catalog_only_remote_target"),
        "scan_roots": roots_scanned,
        "scan_errors": errors,
        "scan_limit_reason": limit_reason,
        "catalogs": catalogs,
    }
