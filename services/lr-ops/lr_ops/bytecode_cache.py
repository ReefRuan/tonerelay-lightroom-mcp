"""Owned worker bytecode generations; never remove live or legacy caches."""
from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time

GENERATION = re.compile(r"[0-9a-f]{32}\Z")
MARKER = ".tonerelay-bytecode.json"
MAX_INACTIVE_BYTES = 256 * 1024 * 1024
MAX_AGE = 24 * 60 * 60


def pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return True  # Unknown ownership is not deletion authority.
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Access denied is unknown/live.
        try:
            code = ctypes.c_uint32()
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def size_of(path):
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not linked(Path(directory) / d)]
        for name in files:
            item = Path(directory) / name
            if not linked(item):
                total += item.stat().st_size
    return total


class GenerationCache:
    def __init__(self, parent, generation):
        if not GENERATION.fullmatch(generation):
            raise ValueError("invalid bytecode generation")
        self.parent = Path(parent).resolve()
        self.parent.mkdir(parents=True, exist_ok=True)
        self.path = self.parent / generation
        self.path.mkdir()  # Never adopt an existing directory.
        self.owner = {"schema": "tonerelay-bytecode-v1", "generation": generation,
                      "supervisor_pid": os.getpid(), "worker_pid": None,
                      "created_at": time.time()}
        self._save()

    def _save(self):
        temporary = self.path / (MARKER + ".tmp")
        temporary.write_text(json.dumps(self.owner), encoding="utf-8")
        os.replace(temporary, self.path / MARKER)

    def attach(self, pid):
        self.owner["worker_pid"] = pid
        self._save()

    @staticmethod
    def _owned(parent, path):
        if (linked(path) or path.resolve().parent != parent.resolve()
                or not GENERATION.fullmatch(path.name)):
            return None
        marker = path / MARKER
        if linked(marker) or marker.stat().st_size > 4096:
            return None
        value = json.loads(marker.read_text(encoding="utf-8"))
        if value.get("schema") != "tonerelay-bytecode-v1" or value.get("generation") != path.name:
            return None
        if (type(value.get("created_at")) not in (int, float) or not math.isfinite(value["created_at"])
                or type(value.get("supervisor_pid")) is not int or value["supervisor_pid"] <= 0
                or (value.get("worker_pid") is not None
                    and (type(value["worker_pid"]) is not int or value["worker_pid"] <= 0))):
            return None
        return value

    @classmethod
    def _remove(cls, parent, path):
        if cls._owned(parent, path) is None:
            return False
        # Refuse the complete tree if a junction/symlink has been introduced.
        for directory, dirs, files in os.walk(path, followlinks=False):
            if any(linked(Path(directory) / n) for n in dirs + files):
                return False
        shutil.rmtree(path)
        return True

    def close(self):
        """Called only after the exact child process has exited."""
        try:
            return self._remove(self.parent, self.path)
        except (OSError, ValueError):
            return False

    @classmethod
    def prune(cls, parent, *, now=None, max_bytes=MAX_INACTIVE_BYTES, max_age=MAX_AGE):
        parent = Path(parent)
        if not parent.is_dir() or linked(parent):
            return 0
        now = time.time() if now is None else now
        inactive = []
        for path in parent.iterdir():
            try:
                if not path.is_dir():
                    continue
                owner = cls._owned(parent, path)
                if owner is None or pid_alive(owner.get("supervisor_pid")):
                    continue
                if owner.get("worker_pid") is not None and pid_alive(owner["worker_pid"]):
                    continue
                inactive.append((owner["created_at"], path, size_of(path)))
            except (OSError, ValueError, TypeError):
                continue
        total = sum(row[2] for row in inactive)
        removed = 0
        for created, path, size in sorted(inactive):
            if now - created <= max_age and total <= max_bytes:
                continue
            try:
                if cls._remove(parent, path):
                    removed += 1
                    total -= size
            except (OSError, ValueError):
                pass
        return removed
