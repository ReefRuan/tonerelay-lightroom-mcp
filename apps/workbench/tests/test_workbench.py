"""Workbench facade tests with a fake LR."""

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from workbench.runtime import Workbench
from workbench.state import RuntimeState, StoreError

_jpeg_buffer = io.BytesIO()
Image.new("RGB", (32, 24), (120, 80, 40)).save(_jpeg_buffer, format="JPEG")
JPEG_STUB = _jpeg_buffer.getvalue()
CATALOG_IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"


class FakeLrOps:
    """Simulates the lr-ops bridge + Lightroom plugin, file staging included.

    Deliberately mirrors the real contract: imports are addressed by a stage
    handle, exports come back as an output handle, and bytes move through
    upload()/fetch_output() rather than through paths. A fake that still passed
    paths around would let a regression back to shared-disk assumptions go
    unnoticed, which is the one thing this suite is here to catch.
    """

    def __init__(self):
        self.settings = {}
        self.copy_count = 0
        self.staged = {}    # stage_id  -> (name, bytes)
        self.outputs = {}   # output_ref -> (name, bytes)
        self._next = 0

    def reachable(self):
        return True

    def _handle(self, prefix):
        self._next += 1
        return f"{prefix}-{self._next}"

    def upload(self, filename, content):
        stage_id = self._handle("stage")
        self.staged[stage_id] = (filename, content)
        return stage_id

    def fetch_output(self, output_ref, destination):
        name, content = self.outputs[output_ref]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def discard_stage(self, stage_id):
        self.staged.pop(stage_id, None)

    def run(self, command, params=None, timeout=None):
        params = params or {}
        assert params.get('expected_access_mode') == 'full'
        if command == "probe_client":
            assert 'expected_catalog_identity' not in params
            return {"access_mode": "full", "catalog_identity": CATALOG_IDENTITY,
                    "catalog_id": "test.lrcat", "sandbox": False,
                    "catalog_authorized": True,
                    "capabilities": {"expected_catalog_identity": True,
                                     "expected_access_mode": True,
                                     "develop_snapshot": True}}
        assert params.get('expected_catalog_identity') == CATALOG_IDENTITY
        if command == "capture_baseline":
            self.baseline = dict(self.settings)
            return {"baseline": dict(self.settings)}
        if command == "create_virtual_copy":
            self.copy_count += 1
            return {
                "source": {"uuid": params["photo_uuid"]},
                "copy": {"uuid": f"copy-{self.copy_count}"},
            }
        if command == "apply_candidate":
            before = dict(self.settings)
            self.settings.update(params["settings"])
            return {"before": before, "after": dict(self.settings)}
        if command == "get_develop_state":
            return {"settings": dict(self.settings)}
        if command == "reset_to_baseline":
            self.settings = dict(self.baseline)
            return {"settings": dict(self.settings)}
        if command == "export_preview":
            # The bridge decides where the file lands; the caller only learns a
            # handle. Assert we were not handed a path to pick ourselves.
            self.assert_no_caller_path(params)
            ref = self._handle("out")
            self.outputs[ref] = (params["photo_uuid"] + ".jpg", JPEG_STUB)
            return {"output_ref": ref, "long_edge": params.get("long_edge", 2048)}
        if command == "request_jpeg_thumbnail":
            self.assert_no_caller_path(params)
            ref = self._handle("thumb")
            self.outputs[ref] = (params["photo_uuid"] + ".jpg", JPEG_STUB)
            return {"output_ref": ref, "method": "requestJpegThumbnail",
                    "purpose": "fast", "freshness": "unverified"}
        if command == "list_photos":
            return {"count": 0, "photos": []}
        if command == "import_photo":
            self.assert_no_caller_path(params)
            name, content = self.staged[params["stage_id"]]
            return {"photo": {
                "uuid": "imported-" + Path(name).stem,
                "file_name": name,
                "copy_name": "",
                "is_virtual_copy": False,
                "width": 100, "height": 80,
            }}
        raise AssertionError(f"unexpected command {command}")

    @staticmethod
    def assert_no_caller_path(params):
        for key in ("path", "paths", "output_dir"):
            assert key not in params, (
                f"caller supplied {key!r}; files must be addressed by handle "
                f"so the workbench never needs Lightroom's disk"
            )


class WorkbenchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RuntimeState(Path(self.tmp.name))
        self.fake = FakeLrOps()
        self.wb = Workbench(self.store, self.fake)


class HealthBridge:
    base = "http://127.0.0.1:17777"
    access_mode = "full"
    catalog_identity = None

    def __init__(self, *, current_folder_selection):
        self.current_folder_selection = current_folder_selection

    def reachable(self):
        return True

    def run(self, command, params=None, timeout=None):
        self.last_call = (command, params, timeout)
        assert command == "probe_client"
        return {
            "access_mode": "full",
            "catalog_identity": CATALOG_IDENTITY,
            "catalog_id": "test.lrcat",
            "sandbox": False,
            "sandbox_registered": False,
            "sandbox_matches_active": False,
            "catalog_authorized": True,
            "capabilities": {
                "expected_catalog_identity": True,
                "expected_access_mode": True,
                "develop_snapshot": True,
                "current_folder_selection": self.current_folder_selection,
            },
        }


class TestHealth(unittest.TestCase):
    def test_rejects_plugin_missing_current_selection_contract(self):
        with tempfile.TemporaryDirectory() as root:
            wb = Workbench(RuntimeState(Path(root)), HealthBridge(
                current_folder_selection=False))
            health = wb.health()

        self.assertFalse(health["ok"])
        self.assertFalse(health["catalog_policy"]["authorized"])
        self.assertEqual(health["catalog_policy"]["reason"],
                         "unsupported_plugin_contract")


class TestPreview(WorkbenchTestBase):
    def test_fast_catalog_preview_uses_thumbnail_output_ref_and_cache(self):
        record = self.wb.preview_photo("src", size=640)
        self.assertEqual(record["method"], "requestJpegThumbnail")
        self.assertEqual(record["purpose"], "fast")
        self.assertEqual(record["freshness"], "unverified")
        self.assertEqual(record["artifact"]["purpose"], "fast")
        self.assertEqual(record["artifact"]["evidence_status"], "unverified")
        self.assertEqual(record["artifact"]["source"]["photo_uuid"], "src")
        self.assertTrue(Path(record["preview_path"]).is_file())
        cached = self.wb.preview_photo("src", size=640)
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["key"], record["key"])


class TestUpload(WorkbenchTestBase):
    def test_upload_stages_on_bridge_and_imports(self):
        result = self.wb.upload_photo("test.jpg", JPEG_STUB)
        # The stored name is randomised to avoid catalog collisions; the photo
        # LR reports back is keyed to whatever name the bridge actually holds.
        self.assertTrue(result["name"].endswith(".jpg"))
        self.assertEqual(result["photo"]["file_name"], result["name"])
        self.assertEqual(result["photo"]["uuid"], "imported-" + Path(result["name"]).stem)
        # The bytes went to the bridge, not to a local uploads/ directory.
        self.assertEqual(self.fake.staged[result["stage_id"]][1], JPEG_STUB)
        self.assertFalse((self.store.root / "uploads").exists())

    def test_upload_drops_stage_when_import_fails(self):
        original = self.fake.run

        def explode(command, params=None, timeout=None):
            if command == "import_photo":
                raise RuntimeError("LR refused the import")
            return original(command, params, timeout)

        self.fake.run = explode
        with self.assertRaises(RuntimeError):
            self.wb.upload_photo("test.jpg", JPEG_STUB)
        self.assertEqual(self.fake.staged, {})

    def test_upload_rejects_bad_suffix(self):
        with self.assertRaises(StoreError):
            self.wb.upload_photo("notes.txt", b"hello")

if __name__ == "__main__":
    unittest.main()
