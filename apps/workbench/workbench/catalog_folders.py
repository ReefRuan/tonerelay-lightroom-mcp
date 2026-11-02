"""Selection-independent Catalog folders and bounded, identity-bound query snapshots."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
import zlib
from contextlib import contextmanager

from .catalog import CatalogTools
from .selection import FOLDER_ID_PATTERN

MAX_SNAPSHOTS = 32
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
TTL = 24 * 60 * 60


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def folder_id(value):
    if not isinstance(value, str) or not FOLDER_ID_PATTERN.fullmatch(value):
        raise ValueError("expected an opaque Catalog folder ID")
    return value


def folder_row(row):
    if not isinstance(row, dict):
        raise ValueError("invalid Catalog folder record")
    result = {"id": folder_id(row.get("id"))}
    for key in ("label",):
        if not isinstance(row.get(key), str) or any(c in row[key] for c in ("/", "\\", "\x00")):
            raise ValueError("invalid folder display label")
        result[key] = row[key]
    labels = row.get("path_labels")
    if not isinstance(labels, list) or not labels or len(labels) > 65:
        raise ValueError("invalid folder ancestry")
    if any(not isinstance(p, str) or any(c in p for c in ("/", "\\", "\x00")) for p in labels):
        raise ValueError("invalid folder ancestry label")
    result["path_labels"] = labels
    return result


class CatalogFolders:
    def __init__(self, root: Path, connect):
        self.path = Path(root) / "folder-queries.sqlite3"
        self.connect = connect

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA auto_vacuum=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS queries (id TEXT PRIMARY KEY, scope TEXT NOT NULL, created REAL NOT NULL, payload BLOB NOT NULL)")
            with db:
                yield db
        finally:
            db.close()

    def list(self, parent_id=None, name_contains="", depth=1, limit=100, offset=0):
        if parent_id is not None:
            folder_id(parent_id)
        if not isinstance(name_contains, str) or len(name_contains) > 256:
            raise ValueError("invalid folder name filter")
        integer(depth, "depth", 1, 16); integer(limit, "limit", 1, 500); integer(offset, "offset", 0, 1000000)
        run, _ = self.connect()
        args = dict(name_contains=name_contains, depth=depth, limit=limit, offset=offset)
        if parent_id is not None:
            args["parent_id"] = parent_id
        data = run("list_catalog_folders", args)
        rows = data.get("folders", [])
        if rows == {}:
            rows = []
        if not isinstance(rows, list):
            raise ValueError("invalid folder page")
        total = integer(data.get("total"), "total", 0, 10000)
        if (any(type(data.get(k)) is not int for k in ("count", "offset", "limit"))
                or type(data.get("has_more")) is not bool
                or data.get("offset") != offset or data.get("limit") != limit
                or data.get("count") != len(rows) or len(rows) != min(limit, max(0, total-offset))
                or data.get("has_more") != (offset+len(rows) < total)):
            raise ValueError("invalid folder pagination")
        clean = []
        for row in rows:
            item = folder_row(row)
            item["parent_id"] = folder_id(row["parent_id"]) if row.get("parent_id") is not None else None
            for key in ("child_count", "direct_photo_count", "subtree_photo_count"):
                item[key] = integer(row.get(key), key, 0, 100000000)
            if item["subtree_photo_count"] < item["direct_photo_count"]:
                raise ValueError("inconsistent folder counts")
            item.update(count_scope="catalog_photo_records", includes_virtual_copies=True, excludes_videos=True)
            clean.append(item)
        if len({f["id"] for f in clean}) != len(clean):
            raise ValueError("duplicate folder in directory page")
        return dict(folders=clean, count=len(clean), total=total, offset=offset, limit=limit,
                    has_more=offset+len(clean)<total)

    def photos(self, folder_ids=None, include_subfolders=None, rating=None,
               include_virtual_copies=None, limit=100, cursor=None):
        integer(limit, "limit", 1, 2000)
        if cursor is not None:
            if any(v is not None for v in (folder_ids, include_subfolders, rating, include_virtual_copies)):
                raise ValueError("cursor continuation accepts only cursor and limit")
            try:
                query_id, position = cursor.split(":")
                if str(uuid.UUID(query_id)) != query_id or not position.isdigit():
                    raise ValueError()
                offset = int(position)
            except (ValueError, AttributeError):
                raise ValueError("invalid folder query cursor") from None
        else:
            if not isinstance(folder_ids, list) or not 1 <= len(folder_ids) <= 200:
                raise ValueError("provide 1..200 folder IDs")
            for item in folder_ids:
                folder_id(item)
            if rating is not None:
                integer(rating, "rating", 0, 5)
            for v in (include_subfolders, include_virtual_copies):
                if v is not None and type(v) is not bool:
                    raise ValueError("folder query switches must be boolean")
            offset = 0
        run, scope = self.connect()  # Fresh authorization even for stored pages.
        fingerprint = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        now = time.time()
        if cursor is None:
            args = dict(folder_ids=list(dict.fromkeys(folder_ids)), include_subfolders=include_subfolders is True,
                        include_virtual_copies=include_virtual_copies is not False)
            if rating is not None:
                args["rating"] = rating
            data = run("list_folder_photos", args)
            rows = data.get("photos", [])
            if rows == {}:
                rows = []
            if (data.get("complete") is not True or not isinstance(rows, list)
                    or type(data.get("count")) is not int or data.get("count") != len(rows) or len(rows) > 100000):
                raise ValueError("incomplete folder photo inventory")
            photos, seen = [], set()
            for row in rows:
                item = CatalogTools._photo(row)
                item["folder_id"] = folder_id(row.get("folder_id"))
                if item["uuid"] in seen:
                    raise ValueError("duplicate photo in folder inventory")
                seen.add(item["uuid"])
                if row.get("flag") not in ("pick", "reject", "unflagged") or not isinstance(row.get("label_text"), str):
                    raise ValueError("invalid folder photo marks")
                item.update(flag=row["flag"], label_text=row["label_text"])
                photos.append(item)
            payload = dict(photos=photos, folders=[folder_row(f) for f in data.get("folders", [])],
                           filters=args, created_at=now)
            returned = {f["id"] for f in payload["folders"]}
            if (not set(args["folder_ids"]) <= returned or any(p["folder_id"] not in returned for p in photos)
                    or (not args["include_subfolders"] and returned != set(args["folder_ids"]))):
                raise ValueError("folder inventory does not match the requested scope")
            raw = json.dumps(payload, ensure_ascii=False).encode()
            if len(raw) > 64 * 1024 * 1024:
                raise ValueError("folder snapshot too large; narrow the scope")
            encoded = zlib.compress(raw)
            if len(encoded) > MAX_SNAPSHOT_BYTES:
                raise ValueError("folder snapshot too large; narrow the scope")
            query_id = str(uuid.uuid4())
            with self._db() as db:
                db.execute("DELETE FROM queries WHERE created < ?", (now-TTL,))
                db.execute("INSERT INTO queries VALUES (?,?,?,?)", (query_id, fingerprint, now, encoded))
                while True:
                    count, size = db.execute("SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM queries").fetchone()
                    if count <= MAX_SNAPSHOTS and size <= MAX_TOTAL_BYTES:
                        break
                    db.execute("DELETE FROM queries WHERE id=(SELECT id FROM queries WHERE id<>? ORDER BY created,id LIMIT 1)", (query_id,))
        else:
            with self._db() as db:
                row = db.execute("SELECT scope,created,payload FROM queries WHERE id=?", (query_id,)).fetchone()
            if row is None or row[1] < now-TTL:
                raise ValueError("folder query expired or evicted; create a new explicit scope")
            if row[0] != fingerprint:
                raise ValueError("folder query belongs to another Target/Catalog/access mode")
            payload = json.loads(zlib.decompress(row[2]))
        total = len(payload["photos"])
        if offset > total:
            raise ValueError("cursor position exceeds query size")
        page = payload["photos"][offset:offset+limit]
        more = offset+len(page)<total
        return dict(query_id=query_id, created_at=payload["created_at"], expires_at=payload["created_at"]+TTL,
                    total=total, count=len(page), offset=offset, limit=limit, has_more=more,
                    next_cursor=f"{query_id}:{offset+len(page)}" if more else None,
                    folders=payload["folders"], filters=payload["filters"], photos=page,
                    membership="fixed_at_query_creation", marks="snapshot_only_read_fresh_tokens_before_writing")
