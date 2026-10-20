"""Explicit local-file ingestion with durable, at-most-once batch claims.

Catalog imports reference persistent source/organized files, never staging.
The Bridge verifies those bytes on its own host before dispatch. A lost write
result is recorded, not retried; confirmation belongs to Runtime Operator.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from PIL import Image

from .folders import DEFAULT_EXTENSIONS

IMPORT_EXTENSIONS = frozenset(DEFAULT_EXTENSIONS) | {
    ".raw", ".orf", ".pef", ".srw", ".3fr", ".fff", ".iiq", ".kdc",
    ".mef", ".mos", ".mrw", ".nrw", ".rwl", ".x3f",
}


@contextmanager
def regular_file(path):
    if Path(path).is_symlink():
        raise ValueError("not a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not a regular file")
        yield handle


def file_hash(handle):
    handle.seek(0)
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def capture_time(handle):
    """Only EXIF capture timestamps; filesystem dates are never substituted."""
    try:
        handle.seek(0)
        with Image.open(handle) as image:
            exif = image.getexif()
            details = exif.get_ifd(34665) if 34665 in exif else {}
            value = details.get(36867) or exif.get(36867) or details.get(36868) or exif.get(36868)
        if isinstance(value, bytes):
            value = value.decode("ascii")
        return datetime.strptime(value.strip("\x00 "), "%Y:%m:%d %H:%M:%S").isoformat()
    except (OSError, ValueError, TypeError, AttributeError, SyntaxError, KeyError,
            IndexError, OverflowError, Image.DecompressionBombError):
        return "unknown"


def _row(path, status="pending", reason=None):
    row = {"source_path": str(path), "path": str(path), "status": status,
           "master_uuid": None, "bytes": None, "sha256": None,
           "capture_time": "unknown", "moved": False, "error": None}
    if reason:
        row["skip_reason"] = reason
    return row


def discover(paths, recursive):
    rows, seen = [], set()

    def visit(path, explicit=False):
        if str(path) in seen:
            rows.append(_row(path, "skipped", "duplicate_path"))
            return
        seen.add(str(path))
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                rows.append(_row(path, "skipped", "symlink"))
            elif stat.S_ISDIR(mode):
                if explicit or recursive:
                    for child in sorted(path.iterdir()):
                        visit(child)
                else:
                    rows.append(_row(path, "skipped", "directory_not_recursive"))
            elif not stat.S_ISREG(mode):
                rows.append(_row(path, "skipped", "not_regular_file"))
            elif path.suffix.lower() not in IMPORT_EXTENSIONS:
                rows.append(_row(path, "skipped", "unsupported_extension"))
            else:
                rows.append(_row(path))
        except OSError as exc:
            row = _row(path, "error")
            row["error"] = str(exc)
            rows.append(row)

    for path in paths:
        visit(Path(path), explicit=True)
    return rows


def organize(source, root, timestamp, expected_hash):
    """Publish a verified complete copy without ever replacing an existing file."""
    date = timestamp[:10]
    directory = root / "unknown-date" if timestamp == "unknown" else root / date[:4] / date
    if not directory.resolve().is_relative_to(root):
        raise ValueError("organized directory escapes move_root; source retained")
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(root):
        raise ValueError("organized directory escapes move_root; source retained")
    fd, temporary = tempfile.mkstemp(prefix=".tonerelay-import-", dir=directory)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output, regular_file(source) as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        with regular_file(temporary) as handle:
            if file_hash(handle)[1] != expected_hash:
                raise ValueError("copy SHA-256 mismatch; source retained")
        index = 1
        while True:
            name = source.name if index == 1 else f"{source.stem}-{index}{source.suffix}"
            destination = directory / name
            try:
                os.link(temporary, destination)
                return destination
            except FileExistsError:
                index += 1
    finally:
        temporary.unlink(missing_ok=True)


class PhotoImport:
    def __init__(self, root, connect):
        self.path = Path(root) / "photo-import.sqlite3"
        self.connect = connect

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS imports (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, scope TEXT NOT NULL, record TEXT NOT NULL)")
            db.commit()
            with db:
                yield db
        finally:
            db.close()

    def _save(self, request_id, record):
        with self._db() as db:
            db.execute("UPDATE imports SET record=? WHERE request_id=?",
                       (json.dumps(record, ensure_ascii=False), request_id))

    @staticmethod
    def _public(record, replayed=False):
        return {**record, "replayed": replayed,
                "summary": {status: sum(row["status"] == status for row in record["results"])
                            for status in ("imported", "skipped", "error", "unknown", "pending", "importing")}}

    def import_photos(self, paths, request_id, recursive=False, move=False, move_root=None):
        if (not isinstance(paths, list) or not paths
                or any(not isinstance(p, str) or not p.strip() or "\x00" in p for p in paths)):
            raise ValueError("paths must be a non-empty list of explicit local paths")
        if type(recursive) is not bool or type(move) is not bool:
            raise ValueError("recursive and move must be booleans")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("request_id must contain 1 to 128 characters")
        if move_root is not None and (not isinstance(move_root, str) or not move_root.strip() or "\x00" in move_root):
            raise ValueError("move_root must be a non-empty local path")
        if move and move_root is None:
            raise ValueError("move=true requires move_root")
        # Normalize spelling without resolving leaf symlinks or requiring moved
        # sources to still exist when recovering a completed request.
        paths = [os.path.abspath(os.path.expanduser(p)) for p in paths]
        root = Path(move_root).expanduser().resolve() if move_root is not None else None
        payload = json.dumps({"paths": paths, "recursive": recursive, "move": move,
                              "move_root": str(root) if root else None}, sort_keys=True)
        client = self.connect()
        studio = client._bind()
        scope = json.dumps({"target_id": getattr(client.client, "target_id", None),
                            "endpoint": hashlib.sha256(str(getattr(client.client, "base", "")).encode()).hexdigest(),
                            "access_mode": getattr(studio.client, "access_mode", "full"),
                            "catalog_identity": client._catalog_identity}, sort_keys=True)
        with self._db() as db:
            # Claim before scanning/moving/submitting. Concurrent callers only
            # observe this claim; they cannot perform the same write twice.
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload, scope, record FROM imports WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if (payload, scope) != existing[:2]:
                    raise ValueError("request_id already belongs to different input or Catalog/Target")
                return self._public(json.loads(existing[2]), replayed=True)
            record = {"request_id": request_id, "status": "running", "move": move, "results": []}
            db.execute("INSERT INTO imports VALUES (?, ?, ?, ?)",
                       (request_id, payload, scope, json.dumps(record)))
        record["results"] = discover(paths, recursive)
        self._save(request_id, record)
        for row in record["results"]:
            if row["status"] != "pending":
                continue
            source = Path(row["source_path"])
            submitted = False
            try:
                with regular_file(source) as handle:
                    before = os.fstat(handle.fileno())
                    row["bytes"], row["sha256"] = file_hash(handle)
                    row["capture_time"] = capture_time(handle)
                destination = organize(source, root, row["capture_time"], row["sha256"]) if move else source
                row["path"] = str(destination)
                row["status"] = "importing"
                self._save(request_id, record)
                # The Bridge must see the same persistent file and verify its
                # bytes. No fallback to a temporary stage or path guessing.
                submitted = True
                data = client.run("import_photo", {"local_file": {
                    "path": str(destination), "bytes": row["bytes"], "sha256": row["sha256"]}})
                photo = data.get("photo") or {}
                if not isinstance(photo.get("uuid"), str) or not photo["uuid"] or photo.get("is_virtual_copy") is not False:
                    raise ValueError("import did not return a master photo UUID; result unknown")
                row["master_uuid"] = photo["uuid"]
                submitted = False
                if move:
                    # Import may yield while external software modifies the
                    # organized file. Never delete the last verified bytes.
                    with regular_file(destination) as handle:
                        captured = os.fstat(handle.fileno())
                        if ((captured.st_dev, captured.st_ino)
                                != (destination.lstat().st_dev, destination.lstat().st_ino)
                                or file_hash(handle) != (row["bytes"], row["sha256"])):
                            raise ValueError("destination changed during import; source retained")
                    with regular_file(source) as handle:
                        current = os.fstat(handle.fileno())
                        linked = source.lstat()
                        if ((current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)
                                != (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                                or file_hash(handle)[1] != row["sha256"]
                                or (linked.st_dev, linked.st_ino, linked.st_mtime_ns, linked.st_size)
                                != (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)):
                            raise ValueError("source changed during import; source retained")
                    source.unlink()
                    row["moved"] = True
                row["status"] = "imported"
            except Exception as exc:
                row["status"] = "unknown" if submitted else "error"
                row["error"] = str(exc)
            self._save(request_id, record)
        record["status"] = "completed"
        self._save(request_id, record)
        return self._public(record)
