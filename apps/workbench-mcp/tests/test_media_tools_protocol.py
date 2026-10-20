"""Independent export, image measurement and XMP serialization contracts."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.runtime import Workbench  # noqa: E402
from workbench.lrops import LrOpsError  # noqa: E402
from workbench.state import RuntimeState  # noqa: E402
from workbench_mcp.server import McpServer, TOOLS  # noqa: E402
from workbench.xmp_full import serialize_full_settings  # noqa: E402

CATALOG_IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"

def jpeg_bytes(rgb, size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="JPEG")
    return buf.getvalue()

class FakeLrOps:
    """带故障注入的桥替身。"""

    def __init__(self):
        self.settings = {"IncrementalTemperature": 5, "IncrementalTint": -3}
        self.outputs = {}
        self._next = 0
        self.export_calls = []      # 每次 export_preview 的 long_edge
        self.export_edge_limit = None  # 超过即失败，模拟超大原图导出
        # Keep the default fixtures small, then let scaling regressions opt in
        # to a genuine 2048px source without changing every existing assertion.
        self.export_size = (64, 48)
        self.thumbnail_size = (64, 48)

    def reachable(self):
        return True

    def _handle(self, prefix):
        self._next += 1
        return f"{prefix}-{self._next}"

    def fetch_output(self, output_ref, destination):
        name, content = self.outputs[output_ref]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def run(self, command, params=None, timeout=None):
        params = params or {}
        assert params.get("expected_access_mode") == "full"
        if command == "probe_client":
            assert "expected_catalog_identity" not in params
            return {"access_mode": "full", "catalog_identity": CATALOG_IDENTITY,
                    "catalog_id": "test.lrcat", "sandbox": False,
                    "catalog_authorized": True,
                    "capabilities": {"expected_catalog_identity": True,
                                     "expected_access_mode": True,
                                     "develop_snapshot": True}}
        assert params.get("expected_catalog_identity") == CATALOG_IDENTITY
        if command == "get_develop_state":
            return {"settings": {k: v for k, v in self.settings.items()
                                 if isinstance(v, (int, float))}}
        if command == "get_develop_snapshot":
            return {"settings": {
                **self.settings,
                "GrainAmount": 25, "GrainSize": 30,
                "ToneCurvePV2012": [0, 0, 64, 48, 255, 255],
                "SaturationAdjustmentOrange": -12,
                "ConvertToGrayscale": False,
                "WhiteBalance": "Custom",
                "ProcessVersion": "6.7",
                "HasSettings": True,  # DENY: 不应进入 XMP
                "MaskGroupBasedCorrections": [{"what": "Mask", "enabled": True}],
            }}
        if command == "export_preview":
            edge = params.get("long_edge", 2048)
            self.export_calls.append(edge)
            if self.export_edge_limit is not None and edge > self.export_edge_limit:
                raise LrOpsError("Export finished but the expected file is missing: /x.jpg")
            ref = self._handle("out")
            rgb = {"src": (60, 50, 40), "ref": (180, 170, 150)}.get(
                params["photo_uuid"], (120, 80, 40))
            self.outputs[ref] = (params["photo_uuid"] + ".jpg",
                                 jpeg_bytes(rgb, size=self.export_size))
            return {"output_ref": ref, "long_edge": edge, "color_space": "sRGB"}
        if command == "request_jpeg_thumbnail":
            ref = self._handle("thumb")
            self.outputs[ref] = (params["photo_uuid"] + ".jpg",
                                 jpeg_bytes((90, 60, 30), size=self.thumbnail_size))
            return {"output_ref": ref, "method": "requestJpegThumbnail",
                    "purpose": "fast", "freshness": "unverified"}
        raise AssertionError(f"unexpected command {command}")

class MediaToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RuntimeState(Path(self.tmp.name))
        self.fake = FakeLrOps()
        self.wb = Workbench(self.store, self.fake)
        self.server = McpServer(self.wb)

    def call(self, tool, **arguments):
        resp = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": arguments}})
        result = resp["result"]
        if result["isError"]:
            raise AssertionError(result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def call_error(self, tool, **arguments):
        resp = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": arguments}})
        assert resp["result"]["isError"], resp
        return resp["result"]["content"][0]["text"]

    # ---- 导出 ------------------------------------------------------------------

    def test_export_live_fallback_records_actual_edge(self):
        self.fake.export_edge_limit = 2048  # 7008px 场景：>2048 必失败
        result = self.call("trlr_export_image", photo_uuid="u-big", max_edge=4096)
        self.assertEqual(result["actual_edge"], 2048)
        self.assertEqual(result["basis"], "live_export")
        self.assertIn("attempts", result)
        self.assertFalse(result["attempts"][0]["ok"])
        self.assertTrue(result["attempts"][-1]["ok"])
        self.assertEqual(self.fake.export_calls[-2:], [4096, 2048])
        self.assertEqual(len(result["sha256"]), 64)
        self.assertTrue(Path(result["export_path"]).exists())
        self.assertEqual(result["artifact_ref"]["sha256"], result["sha256"])

    def test_live_export_download_destination_is_unlocked_and_removed(self):
        destinations = []
        fetch = self.fake.fetch_output

        def check_destination(ref, destination):
            destinations.append(Path(destination))
            self.assertFalse(destination.exists())
            return fetch(ref, destination)

        with patch.object(self.fake, "fetch_output", side_effect=check_destination):
            self.call("trlr_export_image", photo_uuid="u-catalog", max_edge=1024)
        self.assertEqual(len(destinations), 1)
        self.assertFalse(destinations[0].parent.exists())

    def test_live_export_partial_downloads_are_removed_on_failure(self):
        destinations = []

        def fail_download(ref, destination):
            destinations.append(Path(destination))
            destination.write_bytes(b"partial transfer")
            raise LrOpsError("injected transfer failure")

        with patch.object(self.fake, "fetch_output", side_effect=fail_download):
            self.assertIn("injected transfer failure", self.call_error(
                "trlr_export_image", photo_uuid="u-catalog", max_edge=1024))
        self.assertEqual(len(destinations), 2)
        self.assertTrue(all(not path.parent.exists() for path in destinations))

    def test_export_defaults_to_full_resolution(self):
        """不传 max_edge 默认全尺寸：向桥请求 long_edge=0，服务端不再缩放。"""
        self.fake.export_size = (2048, 1536)
        result = self.call("trlr_export_image", photo_uuid="u-catalog")
        self.assertIsNone(result["requested_max_edge"])
        self.assertEqual(result["basis"], "live_export")
        self.assertEqual(self.fake.export_calls[-1], 0)
        self.assertEqual(result["dimensions"], [2048, 1536])
        self.assertEqual(result["actual_edge"], 2048)
        with Image.open(result["export_path"]) as image:
            self.assertEqual(image.size, (2048, 1536))

    def test_export_jpeg_1024_scales_2048_source(self):
        """A direct catalog export preserves the requested downscale behavior."""
        self.fake.export_size = (2048, 1536)
        result = self.call("trlr_export_image", photo_uuid="u-catalog",
                           max_edge=1024, format="jpeg")
        self.assertEqual(result["basis"], "live_export")
        self.assertEqual(result["format"], "jpeg")
        self.assertEqual(result["dimensions"], [1024, 768])
        self.assertEqual(result["actual_edge"], 1024)
        self.assertTrue(result["export_path"].endswith(".jpg"))
        with Image.open(result["export_path"]) as image:
            self.assertEqual(image.size, (1024, 768))

    # ---- 统一图像测量 ------------------------------------------------------------

    def test_measure_via_photo_uuid_uses_fast_preview(self):
        self.fake.thumbnail_size = (2048, 1536)
        result = self.call("trlr_image_measure", source={"photo_uuid": "u-catalog"})
        self.assertEqual(result["source"]["kind"], "catalog_photo_fast_preview")
        self.assertEqual(result["source"]["freshness"], "unverified")
        self.assertEqual(result["analysis_edge"], 1024)
        self.assertAlmostEqual(result["metrics"]["midtone_r_minus_b"], 60, delta=2)

    def test_measure_via_whitelisted_folder(self):
        class FakeFolders:
            def preview(self, **kwargs):
                assert kwargs["catalog_write"] is False
                return {"key": "k1", "method": "direct"}

            def preview_bytes(self, key):
                assert key == "k1"
                return jpeg_bytes((40, 90, 160)), "image/jpeg"

        self.wb._folders = FakeFolders()
        result = self.call("trlr_image_measure",
                           source={"root": "abcdef012345", "name": "IMG_01.JPG"})
        self.assertEqual(result["source"]["kind"], "whitelisted_folder")
        self.assertEqual(result["source"]["method"], "direct")
        self.assertAlmostEqual(result["metrics"]["midtone_r_minus_b"], -120, delta=2)

    def test_folder_measurement_uses_the_catalog_bound_client(self):
        captured = {}

        class FakeFolders:
            def preview(self, **kwargs):
                captured.update(kwargs)
                return {"key": "k1", "method": "direct"}

            def preview_bytes(self, key):
                return jpeg_bytes((40, 90, 160)), "image/jpeg"

        self.wb._folders = FakeFolders()
        self.call(
            "trlr_image_measure",
            source={
                "root": "abcdef012345",
                "name": "IMG_01.CR2",
                "catalog_write": "allow",
            },
        )
        self.assertTrue(captured["catalog_write"])
        self.assertIsNot(captured["client"], self.fake)
        self.assertIs(captured["client"].client, self.fake)

    def test_measure_via_published_episode_artifact(self):
        episode_id = str(uuid.uuid4())
        directory = self.store.root / "episodes" / episode_id
        directory.mkdir(parents=True)
        content = jpeg_bytes((30, 90, 180))
        (directory / "v0.jpg").write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        (directory / "episode.json").write_text(json.dumps({
            "reference_sha256": digest,
            "versions": [{"sha256": digest}],
        }))
        result = self.call("trlr_image_measure",
                           source={"episode_id": episode_id, "name": "v0.jpg"},
                           points=[[0.5, 0.5]], sample_radius=0)
        self.assertEqual(result["source"]["kind"], "episode_artifact")
        self.assertEqual(result["source"]["sha256"], digest)
        self.assertEqual(result["samples"][0]["pixel"], [32, 24])

    def test_measure_rejects_mixed_or_retired_addresses(self):
        for source in ({"session_id": "retired", "round": 1},
                       {"photo_uuid": "u", "episode_id": "other", "name": "v0.jpg"},
                       {"photo_uuid": "u", "session_id": "retired"},
                       {"photo_uuid": "u", "name": "v0.jpg"}):
            with self.subTest(source=source):
                self.call_error("trlr_image_measure", source=source)

    def test_export_requires_photo_and_rejects_retired_address(self):
        self.call_error("trlr_export_image")
        self.call_error("trlr_export_image", photo_uuid="u", session_id="retired")

class UnitTests(unittest.TestCase):
    """不经过 RPC 的纯函数核对。"""

    def test_xmp_serializer_skips_safely(self):
        result = serialize_full_settings({
            "Exposure2012": 0.4, "Temperature": 5200,
            "ToneCurvePV2012": [0, 0, 255, 255],
            "OddArray": [1, 2, 3],
            "MaskGroupBasedCorrections": [{"what": "Mask"}],
            "HasSettings": True,
            "bad key!": 1,
        })
        root = ET.fromstring(result["xmp"])  # 必须是合法 XML
        self.assertIsNotNone(root)
        self.assertIn('Exposure2012="0.4"', result["xmp"])
        self.assertIn('Temperature="5200"', result["xmp"])
        self.assertIn("OddArray", result["skipped_shape_keys"])
        self.assertIn("MaskGroupBasedCorrections", result["skipped_structured_keys"])
        self.assertIn("HasSettings", result["skipped_metadata_keys"])
        self.assertIn("bad key!", result["skipped_shape_keys"])

if __name__ == "__main__":
    unittest.main()
