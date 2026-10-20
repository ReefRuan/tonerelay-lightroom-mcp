import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from workbench.artifacts import (SCHEMA_VERSION, build_artifact_ref,
                                 build_artifact_ref_from_bytes)


class ArtifactRefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "preview.jpg"
        Image.new("RGB", (31, 17), (70, 90, 120)).save(self.path, format="JPEG")

    def test_ref_contains_provenance_and_no_host_path(self):
        ref = build_artifact_ref(
            self.path,
            artifact_id="photo-preview:abc",
            source={"kind": "catalog_photo", "photo_uuid": "p1"},
            purpose="fast",
            method="requestJpegThumbnail",
            freshness="unverified",
            evidence_status="unverified",
            color_space="unknown",
            orientation="AB",
        )
        self.assertEqual(ref["schema_version"], SCHEMA_VERSION)
        self.assertEqual(ref["dimensions"], [31, 17])
        self.assertEqual(ref["mime_type"], "image/jpeg")
        self.assertNotIn("preview_url", ref)
        self.assertNotIn(str(self.path), ref)
        self.assertEqual(len(ref["sha256"]), 64)

    def test_missing_or_non_image_is_rejected(self):
        with self.assertRaises(ValueError):
            build_artifact_ref(
                self.path.with_name("missing.jpg"), artifact_id="x",
                source={"kind": "test"}, purpose="fast", method="test",
                freshness="unverified", evidence_status="unverified", color_space="unknown",
            )
        bad = Path(self.tmp.name) / "bad.bin"
        bad.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            build_artifact_ref(
                bad, artifact_id="x", source={"kind": "test"}, purpose="fast",
                method="test", freshness="unverified", evidence_status="unverified",
                color_space="unknown",
            )

    def test_ref_can_be_built_from_one_captured_byte_snapshot(self):
        content = self.path.read_bytes()
        self.path.unlink()
        ref = build_artifact_ref_from_bytes(
            content, filename="preview.jpg", artifact_id="snapshot:1",
            source={"kind": "episode_render"}, purpose="verified",
            method="LrExportSession", freshness="verified",
            evidence_status="verified_render", color_space="sRGB",
        )
        self.assertEqual(ref["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(ref["bytes"], len(content))
        self.assertEqual(ref["dimensions"], [31, 17])
        self.assertEqual(ref["mime_type"], "image/jpeg")


if __name__ == "__main__":
    unittest.main()
