"""Durable local import, filesystem safety and request deduplication."""
import copy
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from lr_ops.staging import persistent_import_path
from workbench.runtime import Workbench
from workbench.state import RuntimeState

IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"


class ImportBridge:
    base = "http://127.0.0.1:17777"
    target_id = "local"
    catalog_identity = IDENTITY
    access_mode = "full"

    def __init__(self):
        self.calls = []
        self.failure = None
        self.authorized = True
        self.on_import = None

    def run(self, command, params=None):
        params = copy.deepcopy(params or {})
        self.calls.append((command, params))
        assert params["expected_access_mode"] == self.access_mode
        if command == "probe_client":
            return {"access_mode": self.access_mode, "catalog_identity": self.catalog_identity,
                    "catalog_authorized": self.authorized,
                    "capabilities": {"expected_access_mode": True,
                                     "expected_catalog_identity": True, "develop_snapshot": True}}
        assert params["expected_catalog_identity"] == self.catalog_identity
        assert command == "import_photo"
        assert "stage_id" not in params
        persistent_import_path(params["local_file"])
        if self.on_import:
            self.on_import(params)
        if self.failure:
            raise self.failure
        return {"photo": {"uuid": f"master-{self.imports}", "is_virtual_copy": False}}

    @property
    def imports(self):
        return sum(command == "import_photo" for command, _ in self.calls)


class PhotoImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.target = self.root / "organized"
        self.bridge = ImportBridge()
        self.wb = Workbench(RuntimeState(self.root / "runtime"), self.bridge)

    def image(self, name="photo.jpg", date=None):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        exif = Image.Exif()
        if date:
            exif[34665] = {36867: date}
        Image.new("RGB", (8, 6), "red").save(path, exif=exif)
        return path

    def run_import(self, paths=None, request_id="request-1", **options):
        return self.wb.import_photos([str(p) for p in paths or [self.source]], request_id, **options)

    def test_filter_results_metadata_and_source_preservation(self):
        photo = self.image(date="2024:05:06 07:08:09")
        raw = self.source / "camera.ORF"
        raw.write_bytes(b"raw-not-decodable-by-pillow")
        (self.source / "notes.txt").write_text("skip")
        (self.source / "linked.jpg").symlink_to(photo)
        self.image("nested/child.jpg")
        original = photo.read_bytes()
        result = self.run_import()
        rows = {Path(row["source_path"]).name: row for row in result["results"]}
        self.assertEqual(rows["notes.txt"]["skip_reason"], "unsupported_extension")
        self.assertEqual(rows["linked.jpg"]["skip_reason"], "symlink")
        self.assertEqual(rows["nested"]["skip_reason"], "directory_not_recursive")
        self.assertEqual(rows["photo.jpg"]["capture_time"], "2024-05-06T07:08:09")
        self.assertEqual(rows["photo.jpg"]["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(rows["photo.jpg"]["bytes"], len(original))
        self.assertTrue(rows["photo.jpg"]["master_uuid"])
        self.assertFalse(rows["photo.jpg"]["moved"])
        self.assertEqual(rows["camera.ORF"]["capture_time"], "unknown")
        self.assertEqual(photo.read_bytes(), original)
        self.assertEqual(self.bridge.imports, 2)

    def test_recursive_duplicates_missing_and_nonregular(self):
        self.image("nested/child.jpg")
        photo = self.image()
        result = self.run_import([self.source, photo, self.root / "missing.jpg"], recursive=True)
        self.assertEqual(result["summary"]["imported"], 2)
        self.assertEqual(result["summary"]["error"], 1)
        self.assertTrue(any(row.get("skip_reason") == "duplicate_path" for row in result["results"]))

    def test_unavailable_exif_does_not_prevent_import(self):
        photo = self.image()
        with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            result = self.run_import([photo])
        row = result["results"][0]
        self.assertEqual(row["status"], "imported")
        self.assertEqual(row["capture_time"], "unknown")
        self.assertEqual(self.bridge.imports, 1)

    def test_request_id_survives_new_facade_and_rejects_changed_scope(self):
        self.image()
        first = self.run_import()
        self.wb = Workbench(self.wb.store, self.bridge)
        second = self.run_import()
        self.assertEqual(first["results"], second["results"])
        self.assertTrue(second["replayed"])
        self.assertEqual(self.bridge.imports, 1)
        with self.assertRaisesRegex(ValueError, "different input"):
            self.run_import(recursive=True)
        self.bridge.target_id = "other"
        with self.assertRaisesRegex(ValueError, "Catalog/Target"):
            self.run_import()
        self.assertEqual(self.bridge.imports, 1)

    def test_move_exif_unknown_conflict_and_retry_after_source_deleted(self):
        dated = self.image(date="2023:12:25 01:02:03")
        unknown = self.image("unknown.jpg")
        directory = self.target / "2023" / "2023-12-25"
        directory.mkdir(parents=True)
        conflict = directory / dated.name
        conflict.write_bytes(b"untouched")
        expected = dated.read_bytes()
        result = self.run_import([dated, unknown], move=True, move_root=str(self.target))
        rows = result["results"]
        self.assertEqual(rows[0]["path"], str(directory / "photo-2.jpg"))
        self.assertEqual(Path(rows[0]["path"]).read_bytes(), expected)
        self.assertEqual(rows[1]["path"], str(self.target / "unknown-date" / "unknown.jpg"))
        self.assertTrue(all(row["moved"] for row in rows))
        self.assertFalse(dated.exists())
        self.assertFalse(unknown.exists())
        self.assertEqual(conflict.read_bytes(), b"untouched")
        again = self.run_import([dated, unknown], move=True, move_root=str(self.target))
        self.assertTrue(again["replayed"])
        self.assertEqual(self.bridge.imports, 2)

    def test_failed_import_retains_source_and_unknown_write_is_not_replayed(self):
        photo = self.image()
        original = photo.read_bytes()
        self.bridge.failure = TimeoutError("unknown result")
        result = self.run_import([photo], move=True, move_root=str(self.target))
        self.assertEqual(result["results"][0]["status"], "unknown")
        self.assertIsNone(result["results"][0]["master_uuid"])
        self.assertEqual(photo.read_bytes(), original)
        self.assertEqual(Path(result["results"][0]["path"]).read_bytes(), original)
        self.run_import([photo], move=True, move_root=str(self.target))
        self.assertEqual(self.bridge.imports, 1)

    def test_copy_failure_preserves_source_and_never_submits(self):
        photo = self.image()
        with patch("workbench.photo_import.os.link", side_effect=OSError("disk error")):
            result = self.run_import([photo], move=True, move_root=str(self.target))
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertTrue(photo.is_file())
        self.assertEqual(self.bridge.imports, 0)
        self.assertFalse(list(self.target.rglob(".tonerelay-import-*")))

    def test_corrupted_copy_is_not_published_or_imported(self):
        photo = self.image()
        from workbench.photo_import import file_hash
        calls = []

        def corrupt(handle):
            calls.append(1)
            size, sha = file_hash(handle)
            return size, "0" * 64 if len(calls) == 2 else sha

        with patch("workbench.photo_import.file_hash", side_effect=corrupt):
            result = self.run_import([photo], move=True, move_root=str(self.target))
        self.assertIn("SHA-256 mismatch", result["results"][0]["error"])
        self.assertTrue(photo.is_file())
        self.assertEqual(self.bridge.imports, 0)
        self.assertFalse(list(self.target.rglob("*.jpg")))

    def test_source_modified_during_import_or_delete_failure_preserves_source(self):
        photo = self.image()
        self.bridge.on_import = lambda params: photo.write_bytes(b"user replacement")
        result = self.run_import([photo], move=True, move_root=str(self.target))
        self.assertEqual(photo.read_bytes(), b"user replacement")
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertTrue(result["results"][0]["master_uuid"])
        self.assertFalse(result["results"][0]["moved"])

    def test_delete_failure_keeps_both_files_and_returns_imported_uuid(self):
        photo = self.image()
        original_unlink = Path.unlink

        def refuse_source(path, *args, **kwargs):
            if path == photo:
                raise PermissionError("source deletion denied")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", refuse_source):
            result = self.run_import([photo], move=True, move_root=str(self.target))
        row = result["results"][0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["master_uuid"], "master-1")
        self.assertIn("deletion denied", row["error"])
        self.assertFalse(row["moved"])
        self.assertEqual(photo.read_bytes(), Path(row["path"]).read_bytes())

    def test_destination_modified_or_removed_during_import_keeps_source(self):
        for change in ("replace", "remove"):
            with self.subTest(change=change):
                photo = self.image(f"{change}.jpg")
                original = photo.read_bytes()

                def interfere(params):
                    destination = Path(params["local_file"]["path"])
                    if change == "replace":
                        destination.write_bytes(b"external replacement")
                    else:
                        destination.unlink()

                self.bridge.on_import = interfere
                result = self.run_import([photo], request_id=change, move=True, move_root=str(self.target))
                row = result["results"][0]
                self.assertEqual(row["status"], "error")
                self.assertFalse(row["moved"])
                self.assertTrue(row["master_uuid"])
                self.assertEqual(photo.read_bytes(), original)

    def test_organized_directory_symlink_cannot_escape_move_root(self):
        photo = self.image()
        elsewhere = self.root / "outside"
        elsewhere.mkdir()
        self.target.mkdir()
        (self.target / "unknown-date").symlink_to(elsewhere, target_is_directory=True)
        result = self.run_import([photo], move=True, move_root=str(self.target))
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertIn("escapes move_root", result["results"][0]["error"])
        self.assertTrue(photo.exists())
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(self.bridge.imports, 0)

    def test_escape_is_rejected_before_creating_outside_date_directory(self):
        photo = self.image(date="2024:05:06 07:08:09")
        elsewhere = self.root / "outside"
        elsewhere.mkdir()
        self.target.mkdir()
        (self.target / "2024").symlink_to(elsewhere, target_is_directory=True)
        result = self.run_import([photo], move=True, move_root=str(self.target))
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertTrue(photo.exists())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_partial_failures_continue_and_invalid_options_have_no_effect(self):
        first, second = self.image(), self.image("other.jpg")
        original = self.bridge.run

        def run(command, params=None):
            if command == "import_photo" and params["local_file"]["path"] == str(first):
                raise RuntimeError("failed first file")
            return original(command, params)

        self.bridge.run = run
        result = self.run_import([first, second])
        self.assertEqual([row["status"] for row in result["results"]], ["unknown", "imported"])
        calls = len(self.bridge.calls)
        for options in ({"move": True}, {"recursive": 1}, {"move": "true"}, {"move_root": ""}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_import([first], request_id="bad", **options)
        self.assertEqual(len(self.bridge.calls), calls)

    def test_safe_and_full_authorization_checked_before_move(self):
        photo = self.image()
        for mode in ("safe", "full"):
            self.bridge.access_mode = mode
            self.bridge.authorized = False
            with self.assertRaisesRegex(ValueError, "未获 Runtime 授权"):
                self.run_import([photo], request_id=mode, move=True, move_root=str(self.target))
            self.assertTrue(photo.is_file())
            self.assertFalse(self.target.exists())
        self.bridge.authorized = True
        result = self.run_import([photo], request_id="authorized-safe")
        self.assertEqual(result["summary"]["imported"], 1)

    def test_concurrent_same_request_claim_is_read_only(self):
        self.image()
        entered, release = threading.Event(), threading.Event()
        self.bridge.on_import = lambda _: (entered.set(), release.wait(5))
        outputs = []
        thread = threading.Thread(target=lambda: outputs.append(self.run_import()))
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.join(5)))
        self.assertTrue(entered.wait(5))
        result = self.run_import()
        self.assertTrue(result["replayed"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.bridge.imports, 1)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outputs[0]["summary"]["imported"], 1)


if __name__ == "__main__":
    unittest.main()
