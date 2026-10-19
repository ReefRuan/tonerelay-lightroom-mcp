"""Explicit, protected Catalog organization with durable request deduplication.

The plugin makes protection decisions inside its write gate. This module pins
the Catalog, journals a request before submission, and never retries a write
whose outcome is unknown. It contains no photo-quality judgments.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from lr_ops.catalog_binding import CatalogBinding

MODES = ("skip_marked_photos", "fill_empty_fields")
FIELDS = ("rating", "flag", "color_label")
FLAGS = ("pick", "unflagged", "reject")
COLORS = ("none", "red", "yellow", "green", "blue", "purple")
MAX_PHOTOS = 200


class MarkConnection:
    """One already-probed Target, with the shared Catalog envelope contract."""
    def __init__(self, client, probe):
        if probe.get("capabilities", {}).get("photo_marks_v1") is not True:
            raise ValueError("Lightroom plugin lacks photo_marks_v1; load the matching plugin build")
        self.client = client
        self.binding = CatalogBinding.from_probe(probe)
        self.scope = {"target_id": getattr(client, "target_id", None),
                      "catalog_identity": self.binding.catalog_identity,
                      "access_mode": self.binding.access_mode,
                      "endpoint_fingerprint": hashlib.sha256(str(getattr(client, "base", "")).encode()).hexdigest()}

    def run(self, command, params, on_submitted=None):
        bound = self.binding.apply(params)
        if on_submitted is None:
            return self.client.run(command, bound)
        return self.client.run(command, bound, on_submitted=on_submitted)

    def job(self, job_id):
        return getattr(self.client, "_client", self.client).get_job(job_id)


def bounded(value, name, maximum=4096):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value


def photo_ids(values):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_PHOTOS:
        raise ValueError("photo_uuids must contain 1 to 200 photos")
    clean = [bounded(value, "photo_uuid", 256) for value in values]
    if len(set(clean)) != len(clean):
        raise ValueError("duplicate photo_uuid")
    return clean


def validate_patch(value):
    if not isinstance(value, dict) or not value or set(value) - set(FIELDS):
        raise ValueError("patch requires only rating, flag or color_label")
    if "rating" in value and (type(value["rating"]) is not int or not 0 <= value["rating"] <= 5):
        raise ValueError("rating must be an integer from 0 to 5")
    if "flag" in value and value["flag"] not in FLAGS:
        raise ValueError("invalid flag")
    if "color_label" in value and value["color_label"] not in COLORS:
        raise ValueError("invalid color_label")
    return dict(value)


def clean_marks(value):
    if not isinstance(value, dict):
        raise ValueError("invalid marks snapshot")
    validate_patch({"rating": value.get("rating"), "flag": value.get("flag")})
    color, status = value.get("color_label"), value.get("color_label_status")
    if status not in ("mapped", "empty", "unmapped") or (color not in COLORS and color is not None):
        raise ValueError("invalid color label readback")
    if (status == "unmapped") != (color is None):
        raise ValueError("inconsistent color label readback")
    if not isinstance(value.get("label_text"), str) or type(value.get("has_any_marks")) is not bool:
        raise ValueError("invalid label/protection readback")
    bounded(value.get("marks_token"), "marks_token")
    return {key: value.get(key) for key in
            ("photo_uuid", "rating", "flag", "color_label", "color_label_status",
             "label_text", "marks_token", "has_any_marks")}


def clean_results(result, ids, writing=False):
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise ValueError("incomplete photo marks response")
    clean = []
    for row, photo_uuid in zip(rows, ids):
        if not isinstance(row, dict) or row.get("photo_uuid") != photo_uuid:
            raise ValueError("photo marks response identity mismatch")
        allowed = ("applied", "partial", "skipped", "unchanged", "conflict", "failed", "unknown", "mismatch") if writing else ("read", "failed")
        if row.get("status") not in allowed:
            raise ValueError("invalid photo marks result status")
        item = {"photo_uuid": photo_uuid, "status": row["status"]}
        for key in ("marks", "before", "after", "readback"):
            if key in row:
                item[key] = clean_marks(row[key])
                if item[key]["photo_uuid"] != photo_uuid:
                    raise ValueError("snapshot identity mismatch")
        if not writing and row["status"] == "read" and "marks" not in item:
            raise ValueError("missing marks snapshot")
        for key in ("changed_fields", "skipped_fields", "readback_mismatches"):
            values = row.get(key, [])
            # Older JSON encoders represent an empty Lua table as {}.
            if values == {}:
                values = []
            if not isinstance(values, list) or any(field not in FIELDS for field in values) or len(set(values)) != len(values):
                raise ValueError("invalid marks result fields")
            if writing:
                item[key] = values
        if writing and item["changed_fields"] and ("before" not in item or "after" not in item):
            raise ValueError("changed marks require before/after evidence")
        for key in ("reason", "error"):
            if isinstance(row.get(key), str):
                item[key] = row[key][:500]
        clean.append(item)
    return clean


class PhotoMarks:
    def __init__(self, root: Path, connect):
        self.path = Path(root) / "photo-marks.sqlite3"
        self.connect = connect

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, undo_of TEXT UNIQUE, record TEXT NOT NULL)")
            db.commit()
            with db:
                yield db
        finally:
            db.close()

    def _save(self, record):
        with self._db() as db:
            db.execute("UPDATE operations SET record=? WHERE id=?",
                       (json.dumps(record, ensure_ascii=False), record["operation_id"]))

    def _load(self, operation_id):
        if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError("invalid operation_id")
        with self._db() as db:
            row = db.execute("SELECT record FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("unknown mark operation")
        return json.loads(row[0])

    @staticmethod
    def _public(record, details=False):
        if details:
            return {k: v for k, v in record.items()
                    if k not in ("params", "scope", "payload_hash", "prepared_marks")}
        # Keep undo evidence in the journal, not in every conversation turn.
        # Successful photos are counted; only protected or unresolved items
        # need identities/fields in the default response.
        result = {"operation_id": record["operation_id"], "status": record["status"],
                  "summary": {"total": len(record["params"]["items"]),
                              **record.get("summary", {})}}
        if record.get("undo_of"):
            result["undo_of"] = record["undo_of"]
        if record.get("error"):
            result["error"] = record["error"]
        issues = []
        for row in record.get("results", []):
            if (row["status"] in ("applied", "unchanged")
                    and not any(row.get(key) for key in ("skipped_fields", "readback_mismatches", "error"))):
                continue
            item = {"photo_uuid": row["photo_uuid"], "status": row["status"]}
            for key in ("skipped_fields", "readback_mismatches", "reason", "error"):
                if row.get(key):
                    item[key] = row[key]
            if row.get("changed_fields"):
                item["changed"] = {field: row["after"][field] for field in row["changed_fields"]}
            issues.append(item)
        if issues:
            result["issues"] = issues
        return result

    def get(self, photo_uuids):
        ids = photo_ids(photo_uuids)
        connection = self.connect()
        result = connection.run("get_photo_marks", {"photo_uuids": ids})
        rows = clean_results(result, ids)
        for row in rows:
            if "marks" in row:
                row["marks"].pop("photo_uuid", None)
        return {"results": rows}

    def set(self, items, request_id, protection_mode=MODES[0]):
        bounded(request_id, "request_id", 128)
        if protection_mode not in MODES:
            raise ValueError("invalid protection_mode")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_PHOTOS:
            raise ValueError("items must contain 1 to 200 photos")
        normalized = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"photo_uuid", "expected_marks_token", "patch"}:
                raise ValueError("each item requires photo_uuid, expected_marks_token and patch")
            normalized.append({"photo_uuid": bounded(item["photo_uuid"], "photo_uuid", 256),
                               "expected_marks_token": bounded(item["expected_marks_token"], "expected_marks_token"),
                               "patch": validate_patch(item["patch"])})
        photo_ids([item["photo_uuid"] for item in normalized])
        return self._execute("set_photo_marks", {"items": normalized, "protection_mode": protection_mode}, request_id)

    def undo(self, operation_id, request_id):
        bounded(request_id, "request_id", 128)
        original = self._load(operation_id)
        if original["status"] in ("prepared", "dispatching", "unknown"):
            raise ValueError("resolve the original operation before undo; do not replay unknown writes")
        if original.get("undo_of"):
            raise ValueError("undo of an undo is not supported")
        items = [{"photo_uuid": row["photo_uuid"], "before": row["before"],
                  "after": row["after"], "fields": row["changed_fields"]}
                 for row in original.get("results", []) if row.get("changed_fields")]
        if not items:
            raise ValueError("operation has no confirmed changed fields to undo")
        return self._execute("restore_photo_marks", {"items": items}, request_id,
                             undo_of=operation_id, expected_scope=original["scope"])

    def _execute(self, command, params, request_id, undo_of=None, expected_scope=None):
        # Reserve the request durably BEFORE any write. A process death at any
        # later instruction leaves a record that blocks duplicate submission.
        connection = self.connect()
        scope = connection.scope
        if expected_scope is not None and scope != expected_scope:
            raise ValueError("mark operation belongs to another Target/Catalog/mode")
        payload = {"command": command, "params": params, "scope": scope, "undo_of": undo_of}
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        record = {"operation_id": str(uuid.uuid4()), "request_id": request_id,
                  "status": "prepared", "created_at": time.time(), "scope": scope,
                  "params": params, "payload_hash": signature, "undo_of": undo_of}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT payload_hash, record FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous[0] != signature:
                    raise ValueError("request_id already used for a different payload or Catalog")
                return self._public(json.loads(previous[1]))
            if undo_of and db.execute("SELECT id FROM operations WHERE undo_of=?", (undo_of,)).fetchone():
                raise ValueError("an undo already exists for this operation; query that result")
            db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?)",
                       (record["operation_id"], request_id, signature, undo_of, json.dumps(record)))
        ids = [item["photo_uuid"] for item in params["items"]]
        try:
            # Persist actual metadata before submitting the mutation. Plugin
            # rechecks the caller's token and protection policy at execution.
            prepared = connection.run("get_photo_marks", {"photo_uuids": ids})
            record["prepared_marks"] = clean_results(prepared, ids)
        except Exception as exc:
            record.update(status="failed", error=str(exc)[:500], results=[])
            self._save(record)
            return self._public(record)
        record["status"] = "dispatching"
        self._save(record)

        def submitted(job_id):
            record["job_id"] = bounded(job_id, "job_id", 128)
            self._save(record)

        try:
            result = connection.run(command, params, on_submitted=submitted)
            self._finish(record, result)
        except Exception as exc:
            record.update(status="unknown", error=str(exc)[:500])
            self._save(record)
        return self._public(record)

    def _finish(self, record, result):
        ids = [item["photo_uuid"] for item in record["params"]["items"]]
        rows = clean_results(result, ids, writing=True)
        for row, item in zip(rows, record["params"]["items"]):
            permitted = item.get("fields", list(item.get("patch", {})))
            if any(field not in permitted for field in row["changed_fields"]):
                raise ValueError("plugin reported a change outside the requested marks patch")
        statuses = [row["status"] for row in rows]
        bad = {"failed", "unknown", "mismatch", "conflict"}
        status = "completed"
        if "unknown" in statuses:
            status = "unknown"
        elif "partial" in statuses:
            status = "partial"
        elif any(s in bad for s in statuses):
            status = "partial" if any(row["changed_fields"] or row["status"] in ("unchanged", "skipped") for row in rows) else "failed"
        record.update(status=status, results=rows, finished_at=time.time(),
                      summary={s: statuses.count(s) for s in sorted(set(statuses))})
        record.pop("error", None)
        self._save(record)

    def operation(self, operation_id, details=False):
        if type(details) is not bool:
            raise ValueError("details must be a boolean")
        record = self._load(operation_id)
        if record["status"] in ("prepared", "dispatching", "unknown") and record.get("job_id"):
            try:
                connection = self.connect()
                if connection.scope != record["scope"]:
                    return self._public(record, details)
                job = connection.job(record["job_id"])
                if job.get("status") == "done" and (job.get("result") or {}).get("ok") is True:
                    self._finish(record, job["result"].get("data"))
                elif job.get("status") in ("interrupted", "failed"):
                    record.update(status="unknown", error="Bridge job did not provide a complete successful result; do not replay")
                    self._save(record)
                # Interrupted/failed jobs may have partially written metadata.
                # Querying this operation never submits a replacement job.
            except Exception:
                pass  # Stored uncertainty remains visible even when offline.
        return self._public(record, details)
