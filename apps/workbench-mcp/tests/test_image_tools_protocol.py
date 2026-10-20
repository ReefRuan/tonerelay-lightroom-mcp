"""MCP image-tools protocol tests over the real shared module.

Synthetic published episodes live under a resolved temporary root, the
Lightroom client raises on any call, and every tool goes through
McpServer.handle as a JSON-RPC tools/call.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workbench"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from workbench.runtime import Workbench  # noqa: E402
from workbench.image_tools import ImageTools  # noqa: E402
from workbench.state import RuntimeState  # noqa: E402
from workbench_mcp.server import McpServer  # noqa: E402

CATALOG_IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BombClient:
    """Any Lightroom access is a test failure, not a fallback."""

    def __getattr__(self, name):
        raise AssertionError("Lightroom client called: " + name)


class ThumbnailClient:
    def __init__(self):
        self.thumbnail_sizes = []

    def reachable(self):
        return True

    def run(self, command, params=None, timeout=None):
        params = params or {}
        if command == "probe_client":
            return {"access_mode": "full", "catalog_identity": CATALOG_IDENTITY,
                    "catalog_id": "test.lrcat", "sandbox": False,
                    "catalog_authorized": True,
                    "capabilities": {"expected_catalog_identity": True,
                                     "expected_access_mode": True,
                                     "develop_snapshot": True}}
        if command == "request_jpeg_thumbnail":
            assert params.get("expected_catalog_identity") == CATALOG_IDENTITY
            assert params.get("expected_access_mode") == "full"
            self.thumbnail_sizes.append(params.get("size"))
            return {"output_ref": "thumb-1", "method": "requestJpegThumbnail",
                    "purpose": "fast", "freshness": "unverified"}
        raise AssertionError(f"unexpected Lightroom command {command}")

    def fetch_output(self, output_ref, destination):
        assert output_ref == "thumb-1"
        Image.new("RGB", (256, 192), (120, 80, 40)).save(destination, format="JPEG")
        return Path(destination)


def publish_episode(root, colors=((90, 70, 60), (140, 60, 40), (40, 120, 200)),
                    size=(48, 32)):
    """Write reference.jpg + v0.jpg.. and a matching episode.json; return its id."""
    episode_id = str(uuid.uuid4())
    directory = root / episode_id
    directory.mkdir(parents=True)
    Image.new("RGB", size, colors[0]).save(directory / "reference.jpg")
    target = digest(directory / "reference.jpg")
    versions = []
    for index, color in enumerate(colors):
        name = f"v{index}.jpg"
        Image.new("RGB", size, color).save(directory / name)
        versions.append({"sha256": digest(directory / name)})
    (directory / "episode.json").write_text(json.dumps({
        "reference_sha256": target,
        "versions": versions,
    }))
    return episode_id


class ImageToolsProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.store = RuntimeState(self.base)
        self.episodes = self.base / "episodes"
        self.episodes.mkdir()
        self.episode_id = publish_episode(self.episodes)
        self.client = BombClient()
        self.workbench = Workbench(self.store, self.client)
        self.server = McpServer(self.workbench)

    def call(self, name, arguments):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})
        return response["result"]

    @staticmethod
    def metadata(result):
        return json.loads(result["content"][0]["text"])

    @property
    def source(self):
        return {"episode_id": self.episode_id, "name": "v1.jpg"}

    def test_new_tools_are_listed_with_explicit_schemas(self):
        tools = {tool["name"]: tool for tool in self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        for name in ("trlr_capabilities", "trlr_image_measure", "trlr_image_compare"):
            self.assertIn(name, tools)
        self.assertNotIn("trlr_image_inspect", tools)
        self.assertNotIn("trlr_image_stats", tools)
        preview = tools["trlr_get_photo_preview"]["inputSchema"]
        self.assertEqual(preview["required"], ["photo_uuid"])
        self.assertEqual((preview["properties"]["size"]["minimum"],
                          preview["properties"]["size"]["maximum"]), (64, 2048))
        self.assertFalse(preview["additionalProperties"])

        presentation = tools["trlr_set_presentation_config"]["inputSchema"]
        self.assertFalse(presentation["additionalProperties"])
        self.assertEqual(presentation["properties"]["codex_preview_max_edge"]["minimum"], 64)
        self.assertEqual(presentation["properties"]["edit_render_max_edge"]["minimum"], 256)

        measure = tools["trlr_image_measure"]["inputSchema"]
        self.assertEqual(measure["required"], ["source"])
        self.assertFalse(measure["additionalProperties"])
        source = measure["properties"]["source"]
        self.assertEqual(len(source["oneOf"]), 3)
        self.assertTrue(all(not variant["additionalProperties"]
                            for variant in source["oneOf"]))
        self.assertEqual(measure["properties"]["roi"]["minItems"], 4)
        self.assertEqual(measure["properties"]["roi"]["maxItems"], 4)
        self.assertEqual(measure["properties"]["roi"]["items"]["minimum"], 0)
        self.assertEqual(measure["properties"]["roi"]["items"]["maximum"], 1)
        self.assertEqual(measure["properties"]["points"]["maxItems"], 32)
        self.assertEqual(measure["properties"]["points"]["items"]["minItems"], 2)
        point_item = measure["properties"]["points"]["items"]["items"]
        self.assertEqual((point_item["minimum"], point_item["maximum"]), (0, 1))
        self.assertEqual(measure["properties"]["sample_radius"]["default"], 2)
        self.assertEqual((measure["properties"]["sample_radius"]["minimum"],
                          measure["properties"]["sample_radius"]["maximum"]), (0, 32))

        compare = tools["trlr_image_compare"]["inputSchema"]
        self.assertEqual(set(compare["required"]), {"left", "right"})
        compare_source = compare["properties"]["left"]
        self.assertEqual(len(compare_source["oneOf"]), 2)
        self.assertEqual(
            [set(variant["required"]) for variant in compare_source["oneOf"]],
            [{"episode_id", "name"}, {"episode_version"}],
        )
        self.assertTrue(all(not variant["additionalProperties"]
                            for variant in compare_source["oneOf"]))
        for variant in compare_source["oneOf"]:
            if "episode_version" in variant["properties"]:
                address = variant["properties"]["episode_version"]
                self.assertEqual(set(address["required"]), {"episode_id", "version"})
                self.assertFalse(address["additionalProperties"])
                self.assertEqual(address["properties"]["version"],
                                 {"type": "integer", "minimum": 0})
            else:
                address = variant
            self.assertIn("pattern", address["properties"]["episode_id"])
        self.assertIn("two versions of the same Episode", tools["trlr_image_compare"]["description"])
        self.assertEqual(compare["properties"]["right"], compare_source)
        self.assertEqual(compare["properties"]["mode"]["enum"], ["distribution", "aligned"])
        self.assertEqual(compare["properties"]["mode"]["default"], "distribution")
        self.assertEqual(compare["properties"]["max_edge"]["default"], 512)
        self.assertEqual((compare["properties"]["max_edge"]["minimum"],
                          compare["properties"]["max_edge"]["maximum"]), (64, 1024))
        self.assertEqual((compare["properties"]["roi"]["items"]["minimum"],
                          compare["properties"]["roi"]["items"]["maximum"]),
                         (0, 1))

    def test_capabilities_returns_engine_metadata_without_lightroom(self):
        result = self.call("trlr_capabilities", {})
        self.assertFalse(result["isError"])
        payload = self.metadata(result)
        self.assertIn("writable_parameters", payload)
        self.assertIn("unsupported", payload)
        self.assertEqual(len(result["content"]), 1)
        self.assertIsNone(self.workbench._image_tools)

    def test_presentation_config_persists_and_caps_native_image_payload(self):
        updated = self.call("trlr_set_presentation_config", {"codex_preview_max_edge": 64})
        self.assertFalse(updated["isError"])
        self.assertEqual(self.metadata(updated)["codex_preview_max_edge"], 64)

        current = self.call("trlr_get_presentation_config", {})
        self.assertEqual(self.metadata(current)["codex_preview_max_edge"], 64)

        client = ThumbnailClient()
        self.workbench.lrops = client
        self.workbench._studio = None
        result = self.call("trlr_get_photo_preview", {
            "photo_uuid": "catalog-photo", "size": 1024, "max_edge": 1024})
        self.assertFalse(result["isError"])
        self.assertEqual(client.thumbnail_sizes, [64])
        block = next(block for block in result["content"] if block["type"] == "image")
        with Image.open(io.BytesIO(base64.b64decode(block["data"]))) as image:
            self.assertEqual(max(image.size), 64)

    def test_photo_preview_requests_no_more_than_its_effective_codex_edge(self):
        self.call("trlr_set_presentation_config", {"codex_preview_max_edge": 512})
        client = ThumbnailClient()
        self.workbench.lrops = client
        self.workbench._studio = None

        result = self.call("trlr_get_photo_preview", {
            "photo_uuid": "catalog-photo", "size": 2048, "max_edge": 64})

        self.assertFalse(result["isError"])
        self.assertEqual(client.thumbnail_sizes, [64])
        block = next(block for block in result["content"] if block["type"] == "image")
        with Image.open(io.BytesIO(base64.b64decode(block["data"]))) as image:
            self.assertEqual(max(image.size), 64)

    def test_presentation_config_caps_each_native_image_transform(self):
        self.call("trlr_set_presentation_config", {"codex_preview_max_edge": 64})
        large = self.base / "large.jpg"
        Image.new("RGB", (256, 192), (80, 100, 120)).save(large, format="JPEG")
        testcase = self

        class ArtifactStudio:
            def artifact_payload(self, episode_id, name):
                testcase.assertEqual(episode_id, "episode")
                testcase.assertEqual(name, "v1.jpg")
                return large, large.read_bytes()

            def raw(self, episode_id):
                testcase.assertEqual(episode_id, "episode")
                return {"versions": [{}, {}]}

        self.workbench._studio = ArtifactStudio()
        artifact = self.call("trlr_episode_artifact", {
            "episode_id": "episode", "name": "v1.jpg", "max_edge": 1024})
        crop = self.call("trlr_episode_artifact", {
            "episode_id": "episode", "name": "v1.jpg", "crop": [0, 0, 1, 1],
            "max_edge": 1024})

        # Comparison reads published files through ImageTools rather than the
        # Episode artifact adapter, so give it a real published fixture too.
        episode_id = publish_episode(self.episodes, size=(256, 192))
        comparison = self.call("trlr_image_compare", {
            "left": {"episode_id": episode_id, "name": "v0.jpg"},
            "right": {"episode_id": episode_id, "name": "v1.jpg"},
            "max_edge": 1024})

        for result in (artifact, crop, comparison):
            with self.subTest(result=result["content"][0]["type"]):
                self.assertFalse(result["isError"])
                block = next(block for block in result["content"]
                             if block["type"] == "image")
                with Image.open(io.BytesIO(base64.b64decode(block["data"]))) as image:
                    self.assertLessEqual(max(image.size), 64)

    def test_fast_photo_preview_returns_native_image_and_truthful_metadata(self):
        self.workbench.lrops = ThumbnailClient()
        self.workbench._studio = None
        result = self.call("trlr_get_photo_preview", {"photo_uuid": "catalog-photo",
                                                        "size": 640, "max_edge": 64})
        self.assertFalse(result["isError"])
        self.assertEqual(len(result["content"]), 3)
        metadata = self.metadata(result)
        self.assertEqual(metadata["method"], "requestJpegThumbnail")
        self.assertEqual(metadata["freshness"], "unverified")
        self.assertEqual(metadata["artifact"]["schema_version"], "artifact-ref-v1")
        self.assertEqual(metadata["artifact"]["evidence_status"], "unverified")
        self.assertNotIn("preview_path", metadata)
        self.assertEqual(json.loads(result["content"][1]["text"])["display"]["kind"], "local_file")
        self.assertEqual(result["content"][2]["type"], "image")

    def test_measure_matches_shared_module_and_stays_path_free(self):
        arguments = {"source": self.source, "roi": [0.25, 0.25, 0.5, 0.5],
                     "points": [[0.5, 0.5]], "sample_radius": 1}
        result = self.call("trlr_image_measure", arguments)
        self.assertFalse(result["isError"])
        payload = self.metadata(result)
        self.assertEqual(payload, ImageTools(self.episodes).measure(**arguments))
        self.assertEqual(payload["schema_version"], "image-measurement-v1")
        self.assertEqual(payload["samples"][0]["pixel"], [24, 16])
        self.assertNotIn(str(self.base), json.dumps(payload))
        self.assertEqual(len(result["content"]), 1)

    def test_compare_returns_metadata_and_native_jpeg(self):
        left, right = {**self.source, "name": "v0.jpg"}, self.source
        arguments = {"left": left, "right": right, "roi": [0.0, 0.0, 0.5, 1.0], "max_edge": 64}
        result = self.call("trlr_image_compare", arguments)
        self.assertFalse(result["isError"])
        self.assertEqual(len(result["content"]), 3)
        metadata = self.metadata(result)
        expected = ImageTools(self.episodes).compare(**arguments)
        expected.pop("preview")
        actual = dict(metadata)
        actual.pop("preview")
        self.assertEqual(actual, expected)
        self.assertNotIn("base64", json.dumps(metadata))
        self.assertEqual(json.loads(result["content"][1]["text"])["display"]["kind"], "local_file")
        block = result["content"][2]
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["mimeType"], "image/jpeg")
        with Image.open(io.BytesIO(base64.b64decode(block["data"]))) as image:
            self.assertEqual(metadata["preview"],
                             {"mime_type": "image/jpeg", "dimensions": list(image.size)})
            self.assertLessEqual(max(image.size), 64)

    def test_aligned_mode_reports_caller_asserted_alignment(self):
        source = self.source
        result = self.call("trlr_image_compare",
                           {"left": source, "right": source, "mode": "aligned"})
        self.assertFalse(result["isError"])
        metadata = self.metadata(result)
        self.assertEqual(metadata["mode"], "aligned")
        self.assertEqual(metadata["schema_version"], "image-comparison-v1")
        self.assertEqual(metadata["comparison_basis"]["resolution"], "decoded_native")
        self.assertEqual(metadata["comparison_basis"]["resampling"], "none")
        self.assertEqual(metadata["aligned_error"]["alignment"], "caller_asserted_not_verified")
        self.assertEqual(metadata["aligned_error"]["mae_rgb"], 0.0)

    def test_episode_version_shortcuts_resolve_hash_verified_saved_renders(self):
        left = {"episode_version": {"episode_id": self.episode_id, "version": 0}}
        right = {"episode_version": {"episode_id": self.episode_id, "version": 1}}
        for mode in ("distribution", "aligned"):
            with self.subTest(mode=mode):
                result = self.call("trlr_image_compare", {"left": left, "right": right,
                                                           "mode": mode})
                self.assertFalse(result["isError"])
                actual = self.metadata(result)
                expected = self.metadata(self.call("trlr_image_compare", {
                    "left": {"episode_id": self.episode_id, "name": "v0.jpg"},
                    "right": self.source, "mode": mode}))
                self.assertEqual(actual, expected)
                self.assertEqual(actual["left"]["name"], "v0.jpg")
                self.assertEqual(actual["right"]["name"], "v1.jpg")
                self.assertEqual(actual["right"]["sha256"],
                                 digest(self.episodes / self.episode_id / "v1.jpg"))
                self.assertNotIn(str(self.base), json.dumps(actual))
                if mode == "aligned":
                    self.assertEqual(actual["aligned_error"]["alignment"],
                                     "caller_asserted_not_verified")
                else:
                    self.assertNotIn("aligned_error", actual)

    def test_episode_version_shortcut_preserves_geometry_rejection(self):
        directory = self.episodes / self.episode_id
        Image.new("RGB", (24, 32), "red").save(directory / "v1.jpg")
        state = json.loads((directory / "episode.json").read_text())
        state["versions"][1]["sha256"] = digest(directory / "v1.jpg")
        (directory / "episode.json").write_text(json.dumps(state))
        arguments = {
            "left": {"episode_version": {"episode_id": self.episode_id, "version": 0}},
            "right": {"episode_version": {"episode_id": self.episode_id, "version": 1}},
        }
        self.assertFalse(self.call("trlr_image_compare", arguments)["isError"])
        result = self.call("trlr_image_compare", {**arguments, "mode": "aligned"})
        self.assertTrue(result["isError"])
        self.assertIn("identical dimensions and crop", result["content"][0]["text"])

    def test_episode_version_shortcut_rejects_bad_sources_and_tampered_render(self):
        address = {"episode_id": self.episode_id, "version": 1}
        for source in (
                {"episode_version": None},
                {"episode_version": {"version": 1}},
                {"episode_version": {**address, "name": "v1.jpg"}},
                {"episode_version": address, "photo_uuid": "catalog-photo"},
                {"episode_version": {**address, "episode_id": "../escape"}},
                *({"episode_version": {**address, "version": value}}
                  for value in (True, -1, 1.0, "1", 9)),
        ):
            with self.subTest(source=source):
                result = self.call("trlr_image_compare", {"left": self.source, "right": source})
                self.assertTrue(result["isError"])
        (self.episodes / self.episode_id / "v1.jpg").write_bytes(b"tampered")
        result = self.call("trlr_image_compare", {
            "left": {"episode_version": address}, "right": self.source})
        self.assertTrue(result["isError"])
        self.assertIn("hash mismatch", result["content"][0]["text"])

    def test_error_results_reject_nan_roi_and_unpublished_artifacts(self):
        source = self.source
        cases = [
            ("trlr_image_measure", {"source": source, "roi": [float("nan"), 0.0, 0.5, 0.5]}),
            ("trlr_image_measure", {"source": source, "roi": [0.8, 0.0, 0.5, 0.5]}),
            ("trlr_image_measure", {"source": source, "points": [[float("inf"), 0.5]]}),
            ("trlr_image_measure", {"source": source, "sample_radius": 33}),
            ("trlr_image_measure", {"source": {**source, "name": "v9.jpg"}}),
            ("trlr_image_measure", {"source": {**source, "name": "v١.jpg"}}),
            ("trlr_image_compare", {"left": source, "right": source, "mode": "stretch"}),
            ("trlr_image_compare", {"left": {"photo_uuid": "catalog-photo"},
                                    "right": source}),
            ("trlr_image_compare", {"left": {"root": "abcdef012345",
                                               "name": "IMG_01.JPG"},
                                    "right": source}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                result = self.call(name, arguments)
                self.assertTrue(result["isError"])
                self.assertIn("error:", result["content"][0]["text"])

        empty = self.episodes / str(uuid.uuid4())
        empty.mkdir()
        (empty / "episode.json").write_text(json.dumps({"versions": []}))
        result = self.call("trlr_image_measure",
                           {"source": {"episode_id": empty.name, "name": "reference.jpg"}})
        self.assertTrue(result["isError"])
        self.assertIn("No published version", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
