import copy
import tempfile
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

from workbench.photo_marks import PhotoMarks, MarkConnection


def snapshot(photo_uuid="p", rating=0, flag="unflagged"):
    return {"photo_uuid": photo_uuid, "rating": rating, "flag": flag,
            "color_label": "none", "color_label_status": "empty", "label_text": "",
            "marks_token": f"snapshot:{photo_uuid}:{rating}:{flag}",
            "has_any_marks": bool(rating or flag != "unflagged")}


class Connection:
    def __init__(self):
        self.scope = {"catalog_identity": "test", "access_mode": "full", "target_id": "local"}
        self.calls = []
        self.writes = 0
        self.fail = False
        self.recovery = None

    def run(self, command, params, on_submitted=None):
        self.calls.append((command, copy.deepcopy(params)))
        if command == "get_photo_marks":
            return {"results": [{"photo_uuid": p, "status": "read", "marks": snapshot(p)} for p in params["photo_uuids"]]}
        self.writes += 1
        on_submitted("abcdef-1234")
        if self.fail:
            raise TimeoutError("unknown write outcome")
        rows = []
        for item in params["items"]:
            p = item["photo_uuid"]
            before, after = snapshot(p), snapshot(p, 4)
            if command == "restore_photo_marks":
                before, after = after, before
            rows.append({"photo_uuid": p, "status": "applied", "before": before, "after": after,
                         "changed_fields": ["rating"], "skipped_fields": [], "readback_mismatches": []})
        result = {"results": rows}
        self.recovery = {"status": "done", "result": {"ok": True, "data": result}}
        return result

    def job(self, job_id):
        return self.recovery or {"status": "interrupted"}


class PhotoMarksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = Connection()
        self.marks = PhotoMarks(Path(self.temp.name), lambda: self.connection)
        self.items = [{"photo_uuid": "p", "expected_marks_token": snapshot()["marks_token"], "patch": {"rating": 4}}]

    def test_validation_precedes_any_connection_or_catalog_access(self):
        def forbidden():
            self.fail("validation touched the Catalog")
        marks = PhotoMarks(Path(self.temp.name), forbidden)
        for patch in ({}, {"rating": True}, {"rating": 6}, {"flag": None}, {"keywords": "x"}):
            with self.assertRaises(ValueError):
                marks.set([{**self.items[0], "patch": patch}], "request")
        with self.assertRaises(ValueError):
            marks.set(self.items * 2, "request")
        with self.assertRaises(ValueError):
            marks.set(self.items, "request", "overwrite")

    def test_request_deduplication_survives_new_runtime_instance(self):
        result = self.marks.set(self.items, "request")
        other = PhotoMarks(Path(self.temp.name), lambda: self.connection)
        repeat = other.set(self.items, "request")
        self.assertEqual(result, repeat)
        self.assertEqual(1, self.connection.writes)
        self.assertEqual("completed", result["status"])
        self.assertNotIn("params", result)
        self.assertNotIn("scope", result)

    def test_same_request_changed_payload_or_scope_is_rejected(self):
        self.marks.set(self.items, "request")
        with self.assertRaises(ValueError):
            self.marks.set(self.items, "request", "fill_empty_fields")
        self.connection.scope = {**self.connection.scope, "catalog_identity": "other"}
        with self.assertRaises(ValueError):
            self.marks.set(self.items, "request")
        self.assertEqual(1, self.connection.writes)

    def test_unknown_write_is_not_retried_and_job_can_be_reconciled(self):
        self.connection.fail = True
        result = self.marks.set(self.items, "request")
        self.assertEqual("unknown", result["status"])
        self.assertIn("job_id", self.marks.operation(result["operation_id"], details=True))
        self.connection.fail = False
        self.marks.set(self.items, "request")
        self.assertEqual(1, self.connection.writes)
        # A known Bridge result can resolve the existing operation, without
        # looking at live values or submitting the setter a second time.
        self.connection.recovery = {"status": "done", "result": {"ok": True, "data": {"results": [{
            "photo_uuid": "p", "status": "applied", "before": snapshot(), "after": snapshot(rating=4),
            "changed_fields": ["rating"], "skipped_fields": [], "readback_mismatches": []}]}}}
        resolved = self.marks.operation(result["operation_id"])
        self.assertEqual("completed", resolved["status"])
        self.assertEqual(1, self.connection.writes)

    def test_interrupted_job_remains_unknown_and_cannot_undo(self):
        self.connection.fail = True
        result = self.marks.set(self.items, "request")
        self.assertEqual("unknown", self.marks.operation(result["operation_id"])["status"])
        with self.assertRaises(ValueError):
            self.marks.undo(result["operation_id"], "undo")

    def test_undo_is_scoped_deduplicated_and_uses_only_recorded_fields(self):
        result = self.marks.set(self.items, "request")
        undo = self.marks.undo(result["operation_id"], "undo")
        self.assertEqual("completed", undo["status"])
        self.assertEqual(undo, self.marks.undo(result["operation_id"], "undo"))
        self.assertEqual(2, self.connection.writes)
        restored = [p for c, p in self.connection.calls if c == "restore_photo_marks"][0]
        self.assertEqual(["rating"], restored["items"][0]["fields"])
        with self.assertRaises(ValueError):
            self.marks.undo(result["operation_id"], "another-undo")
        self.connection.scope = {"catalog_identity": "other"}
        with self.assertRaises(ValueError):
            self.marks.undo(result["operation_id"], "scope-undo")

    def test_operation_remains_readable_offline(self):
        result = self.marks.set(self.items, "request")
        def offline():
            raise ConnectionError("offline")
        other = PhotoMarks(Path(self.temp.name), offline)
        self.assertEqual(result, other.operation(result["operation_id"]))

    def test_bulk_success_is_compact_but_full_evidence_and_undo_survive(self):
        items = [{"photo_uuid": f"p{i}", "expected_marks_token": snapshot(f"p{i}")["marks_token"],
                  "patch": {"rating": 4}} for i in range(200)]
        result = self.marks.set(items, "bulk")
        self.assertEqual({"total": 200, "applied": 200}, result["summary"])
        self.assertEqual({"operation_id", "status", "summary"}, set(result))
        detail = self.marks.operation(result["operation_id"], details=True)
        self.assertEqual(200, len(detail["results"]))
        self.assertEqual(0, detail["results"][0]["before"]["rating"])
        self.assertEqual(4, detail["results"][0]["after"]["rating"])
        self.assertEqual(1, self.connection.writes)
        undo = self.marks.undo(result["operation_id"], "undo-bulk")
        self.assertEqual("completed", undo["status"])
        restored = [p for c, p in self.connection.calls if c == "restore_photo_marks"][0]
        self.assertEqual(200, len(restored["items"]))

    def test_compact_result_keeps_partial_changes_and_protected_fields_visible(self):
        rows = [{"photo_uuid": "p", "status": "partial", "before": snapshot(),
                 "after": snapshot(rating=4), "readback": snapshot(rating=4),
                 "changed_fields": ["rating"], "skipped_fields": ["flag"],
                 "readback_mismatches": ["color_label"], "error": "color write failed"}]
        original = self.connection.run
        def run(command, params, on_submitted=None):
            if command == "get_photo_marks":
                return original(command, params, on_submitted)
            on_submitted("abc-123")
            return {"results": rows}
        with mock.patch.object(self.connection, "run", side_effect=run):
            result = self.marks.set(self.items, "partial")
        self.assertEqual("partial", result["status"])
        self.assertEqual({"rating": 4}, result["issues"][0]["changed"])
        self.assertEqual(["flag"], result["issues"][0]["skipped_fields"])
        self.assertEqual(["color_label"], result["issues"][0]["readback_mismatches"])
        self.assertNotIn("before", result["issues"][0])
        self.assertEqual(rows[0]["readback"],
                         self.marks.operation(result["operation_id"], details=True)["results"][0]["readback"])

    def test_protected_skip_and_failure_do_not_disappear_from_summary(self):
        original = self.connection.run
        rows = [{"photo_uuid": "p", "status": "skipped", "before": snapshot(rating=3),
                 "reason": "photo_already_marked"},
                {"photo_uuid": "missing", "status": "failed", "error": "photo not found"}]
        def run(command, params, on_submitted=None):
            if command == "get_photo_marks":
                return original(command, params, on_submitted)
            on_submitted("abc-123")
            return {"results": rows}
        with mock.patch.object(self.connection, "run", side_effect=run):
            result = self.marks.set(self.items + [{**self.items[0], "photo_uuid": "missing"}], "skip")
        self.assertEqual("partial", result["status"])
        self.assertEqual({"total": 2, "failed": 1, "skipped": 1}, result["summary"])
        self.assertEqual(["skipped", "failed"], [row["status"] for row in result["issues"]])
        self.assertEqual("photo_already_marked", result["issues"][0]["reason"])
        self.assertEqual("photo not found", result["issues"][1]["error"])

    def test_read_keeps_one_identity_and_validates_details_before_access(self):
        result = self.marks.get(["p"])
        self.assertEqual("p", result["results"][0]["photo_uuid"])
        self.assertNotIn("photo_uuid", result["results"][0]["marks"])
        self.assertEqual(snapshot()["marks_token"], result["results"][0]["marks"]["marks_token"])
        with self.assertRaises(ValueError):
            self.marks.operation("invalid", details="yes")

    def test_plugin_capability_and_identity_required(self):
        with self.assertRaises(ValueError):
            MarkConnection(object(), {})

    def test_concurrent_runtime_instances_reserve_one_request(self):
        barrier = Barrier(4)
        def connect():
            barrier.wait(timeout=5)
            return self.connection
        def worker(_):
            runtime = PhotoMarks(Path(self.temp.name), connect)
            return runtime.set(self.items, "concurrent")
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(worker, range(4)))
        self.assertEqual(1, self.connection.writes)
        self.assertEqual(1, len({result["operation_id"] for result in results}))
