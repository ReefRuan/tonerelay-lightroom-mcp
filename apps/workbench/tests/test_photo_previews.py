"""Fast photo previews: immutable publication, identity isolation, pinning.

Two properties are load-bearing here and are pinned down separately:

* a published artifact key never changes meaning -- a later render, a concurrent render
  or a second workbench process must not overwrite the bytes or the metadata
  behind a key that was already handed out;
* the metadata on a cached answer is the metadata of the render that produced
  it, including which Lightroom answered.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

from workbench.runtime import Workbench
from workbench.preview import MAX_BYTES as MAX_PREVIEW_BYTES
from workbench.preview import PreviewStore
from workbench.store import Store, StoreError
from workbench.targets import LrOpsRouter, TargetRegistry, is_loopback_base

WORKBENCH_APP = Path(__file__).resolve().parents[1]
LOCAL_CATALOG_ID = "test.lrcat"
LOCAL_CATALOG_IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"
REMOTE_CATALOG_IDENTITY = "catalog-md5-v1:fedcba9876543210fedcba9876543210"

# Runs in a separate interpreter: the point is that no lock, cache or object
# graph is shared with the publishing process.
PUBLISH_IN_CHILD = """
import io, json, sys
from pathlib import Path
from PIL import Image
from workbench.preview import PreviewStore

root, ref, color = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
buffer = io.BytesIO()
Image.new("RGB", (24, 16), tuple(int(part) for part in color.split(","))).save(
    buffer, format="JPEG")
manifest = PreviewStore(Path(root)).publish(buffer.getvalue(), ref, 640)
print(json.dumps({"key": manifest["key"], "sha256": manifest["sha256"]}))
"""


def jpeg(color=(200, 40, 40), size=(24, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def photo_source(target_id="a", catalog_id="one", fingerprint="aaa", photo_uuid="same",
                 catalog_identity=LOCAL_CATALOG_IDENTITY):
    return {"kind": "catalog_photo", "target_id": target_id,
            "catalog_identity": catalog_identity, "catalog_id": catalog_id,
            "endpoint_fingerprint": fingerprint, "photo_uuid": photo_uuid}


class PreviewBridge:
    """A bridge fake that renders one JPEG per thumbnail request."""

    def __init__(self, base=None, catalog_id=None, color=(200, 40, 40), content=None,
                 orientation=None, gate=None, reached=None, credential=None,
                 catalog_identity=LOCAL_CATALOG_IDENTITY):
        self.base = base
        self.catalog_id = catalog_id
        self.reported_catalog_identity = catalog_identity
        self.credential = credential
        self.color = color
        self.content = content
        self.orientation = orientation
        self.gate = gate          # held until the test releases the fetch
        self.reached = reached    # set as soon as the fetch starts
        self.calls = []
        self.outputs = {}
        self._serial = 0

    @property
    def remote(self):
        return bool(self.base) and not is_loopback_base(self.base)

    def rendered(self):
        return list(self.outputs.values())

    def reachable(self):
        return True

    def run(self, command, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((command, params))
        if command == "probe_client":
            return {"access_mode": "full",
                    "catalog_identity": self.reported_catalog_identity,
                    "catalog_id": self.catalog_id or LOCAL_CATALOG_ID,
                    "sandbox": False,
                    "catalog_authorized": True,
                    "capabilities": {"expected_catalog_identity": True,
                                     "expected_access_mode": True,
                                     "develop_snapshot": True}}
        if command == "request_jpeg_thumbnail":
            assert params.get("expected_catalog_identity") == self.reported_catalog_identity, params
            assert params.get("expected_access_mode") == "full", params
            ref = f"thumb-{self._serial}"
            self._serial += 1
            self.outputs[ref] = self.content if self.content is not None else jpeg(self.color)
            return {"output_ref": ref, "method": "requestJpegThumbnail",
                    "purpose": "fast", "freshness": "unverified",
                    "orientation": self.orientation}
        raise AssertionError(f"unexpected Lightroom command {command}")

    def fetch_output(self, output_ref, destination):
        if self.reached is not None:
            self.reached.set()
        if self.gate is not None:
            assert self.gate.wait(5), "the preview fetch was never released"
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.outputs[output_ref])
        return destination

    def commands(self):
        return [name for name, _ in self.calls]


class PreviewStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = PreviewStore(self.root)
        self.a = photo_source()
        self.b = photo_source(target_id="b", catalog_id="two", fingerprint="bbb")


class TestPreviewPublication(PreviewStoreTestCase):
    def test_published_artifact_resolves_and_carries_its_metadata(self):
        content = jpeg()
        manifest = self.store.publish(content, self.a, 640, orientation="AB")
        self.assertEqual(manifest["key"], self.store.artifact_key(self.a, content))
        self.assertEqual(manifest["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(manifest["bytes"], len(content))
        self.assertEqual(manifest["actual_size"], [24, 16])
        self.assertEqual(manifest["orientation"], "AB")
        self.assertEqual(manifest["source"], self.a)
        key, path, loaded = self.store.current(self.a, 640)
        self.assertEqual(key, manifest["key"])
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(loaded["source"], self.a)

    def test_current_is_per_request_not_per_source(self):
        self.store.publish(jpeg(), self.a, 640)
        self.assertIsNone(self.store.current(self.a, 1024))
        self.assertIsNone(self.store.current(self.b, 640))

    def test_force_keeps_the_previous_url_readable(self):
        first = self.store.publish(jpeg((255, 0, 0)), self.a, 640)
        second = self.store.publish(jpeg((0, 0, 255)), self.a, 640)
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(self.store.current(self.a, 640)[0], second["key"])
        self.assertEqual(self.store.read(first["key"]).read_bytes(), jpeg((255, 0, 0)))
        self.assertEqual(self.store.read(second["key"]).read_bytes(), jpeg((0, 0, 255)))

    def test_identical_bytes_from_different_sources_stay_isolated(self):
        content = jpeg()
        first = self.store.publish(content, self.a, 640)
        second = self.store.publish(content, self.b, 640)
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(self.store.current(self.a, 640)[2]["source"], self.a)
        self.assertEqual(self.store.current(self.b, 640)[2]["source"], self.b)
        for key in (first["key"], second["key"]):
            self.assertEqual(self.store.read(key).read_bytes(), content)

    def test_republishing_identical_bytes_is_a_noop_that_repairs_the_blob(self):
        content = jpeg((1, 2, 3))
        first = self.store.publish(content, self.a, 640)
        (self.store.blobs / f"{first['key']}.jpg").write_bytes(b"damaged")
        self.assertIsNone(self.store.current(self.a, 640))  # not served while damaged
        repaired = self.store.publish(content, self.a, 640)
        self.assertEqual(repaired["key"], first["key"])
        self.assertEqual(self.store.read(first["key"]).read_bytes(), content)
        self.assertEqual(self.store.current(self.a, 640)[0], first["key"])

    def test_republishing_repairs_a_damaged_manifest(self):
        content = jpeg((9, 9, 9))
        first = self.store.publish(content, self.a, 640)
        (self.store.manifests / f"{first['key']}.json").write_text("{ not json")
        with self.assertRaises(FileNotFoundError):
            self.store.read(first["key"])
        self.store.publish(content, self.a, 640)
        self.assertEqual(self.store.read(first["key"]).read_bytes(), content)

    def test_tampered_blob_is_rejected_by_hash(self):
        key = self.store.publish(jpeg((1, 2, 3)), self.a, 640)["key"]
        (self.store.blobs / f"{key}.jpg").write_bytes(jpeg((3, 2, 1)))
        with self.assertRaises(ValueError):
            self.store.read(key)

    def test_invalid_payloads_never_replace_an_existing_pointer(self):
        published = self.store.publish(jpeg((5, 5, 5)), self.a, 640)
        png = io.BytesIO()
        Image.new("RGB", (8, 8), (0, 0, 0)).save(png, format="PNG")
        whole = jpeg((7, 7, 7))
        oversize = b"\xff\xd8\xff" + b"0" * (MAX_PREVIEW_BYTES + 1)
        bad_payloads = [b"", b"not an image at all", png.getvalue(),
                        whole[: int(len(whole) * 0.6)], oversize]
        for content in bad_payloads:
            with self.subTest(content=content[:8]):
                with self.assertRaises(ValueError):
                    self.store.publish(content, self.a, 640)
        self.assertEqual(self.store.current(self.a, 640)[0], published["key"])
        self.assertEqual(self.store.read(published["key"]).read_bytes(), jpeg((5, 5, 5)))
        self.assertEqual(len(list(self.store.blobs.glob("*.jpg"))), 1)

    def test_legacy_uuid_named_file_is_not_served(self):
        legacy = self.store.blobs / "6f1d0e2a-3c4b-4a5d-8e6f-7a8b9c0d1e2f.jpg"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(jpeg())
        with self.assertRaises(ValueError):
            self.store.read(legacy.stem)
        with self.assertRaises(FileNotFoundError):
            self.store.read("a" * 24)  # well-formed key, never published here

    def test_read_rejects_keys_outside_the_published_format(self):
        for key in ("../etc/passwd", "A" * 24, "a" * 23, "a" * 25, "", None):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.store.read(key)


class TestConcurrentPublication(PreviewStoreTestCase):
    def test_independent_instances_do_not_erase_each_others_requests(self):
        other = PreviewStore(self.root)  # a second Runtime instance, same root
        payloads = {self.a["target_id"]: jpeg((255, 0, 0)),
                    self.b["target_id"]: jpeg((0, 0, 255))}
        results = {}
        barrier = threading.Barrier(2)

        def publish(store, ref):
            barrier.wait(5)
            results[ref["target_id"]] = store.publish(payloads[ref["target_id"]], ref, 640)

        threads = [threading.Thread(target=publish, args=(store, ref))
                   for store, ref in ((self.store, self.a), (other, self.b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(set(results), {"a", "b"})
        self.assertEqual(self.store.current(self.a, 640)[0], results["a"]["key"])
        self.assertEqual(other.current(self.b, 640)[0], results["b"]["key"])

    def test_a_separate_process_publishes_into_the_same_store(self):
        env = {**os.environ,
               "PYTHONPATH": str(WORKBENCH_APP) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        completed = subprocess.run(
            [sys.executable, "-c", PUBLISH_IN_CHILD, str(self.root),
             json.dumps(self.b), "10,200,10"],
            capture_output=True, text=True, env=env, timeout=120, check=True)
        child = json.loads(completed.stdout.strip().splitlines()[-1])
        published = self.store.publish(jpeg((250, 250, 0)), self.a, 640)
        self.assertNotEqual(child["key"], published["key"])
        self.assertEqual(self.store.current(self.b, 640)[0], child["key"])
        self.assertEqual(self.store.current(self.a, 640)[0], published["key"])
        self.assertEqual(self.store.current(self.b, 640)[2]["source"], self.b)
        self.assertTrue((self.store.blobs / f"{child['key']}.jpg").is_file())


class TestPreviewIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def workbench(self, bridge):
        return Workbench(Store(self.root), bridge)

    def test_local_fallback_names_the_local_target_and_catalog(self):
        wb = self.workbench(PreviewBridge(base="http://127.0.0.1:17777"))
        record = wb.preview_photo("photo-1", size=640)
        source = record["artifact"]["source"]
        self.assertEqual(source["target_id"], "local")
        self.assertEqual(source["catalog_identity"], LOCAL_CATALOG_IDENTITY)
        self.assertEqual(source["catalog_id"], LOCAL_CATALOG_ID)
        self.assertEqual(source["photo_uuid"], "photo-1")
        self.assertNotIn("127.0.0.1", json.dumps(record))
        self.assertNotIn("17777", json.dumps(record))

    def test_remote_target_keeps_its_own_catalog_id(self):
        wb = self.workbench(PreviewBridge(base="http://10.0.0.4:17777",
                                          catalog_id="studio.lrcat",
                                          catalog_identity=REMOTE_CATALOG_IDENTITY))
        record = wb.preview_photo("photo-1", size=640)
        source = record["artifact"]["source"]
        self.assertEqual(source["catalog_id"], "studio.lrcat")
        self.assertNotEqual(source["catalog_id"], LOCAL_CATALOG_ID)
        self.assertNotIn("10.0.0.4", json.dumps(record))

    def test_endpoint_identity_is_hashed_and_never_carries_credentials(self):
        base = "http://operator:s3cret@10.0.0.4:17777"
        wb = self.workbench(PreviewBridge(base=base, catalog_id="studio.lrcat",
                                          credential="s3cret",
                                          catalog_identity=REMOTE_CATALOG_IDENTITY))
        record = wb.preview_photo("photo-1", size=640)
        serialized = json.dumps(record)
        self.assertNotIn("s3cret", serialized)
        self.assertNotIn("operator", serialized)
        self.assertNotIn("10.0.0.4", serialized)
        fingerprint = record["artifact"]["source"]["endpoint_fingerprint"]
        self.assertNotIn(base, fingerprint)
        self.assertEqual(len(fingerprint), 16)

    def test_target_without_catalog_identity_is_refused(self):
        wb = self.workbench(PreviewBridge(base="http://10.0.0.4:17777",
                                          catalog_identity=None))
        with self.assertRaises(StoreError):
            wb.preview_photo("photo-1", size=640)
        source = photo_source(target_id="local", catalog_id=LOCAL_CATALOG_ID)
        self.assertIsNone(wb._previews.current(source, 640))

    def test_same_uuid_and_bytes_on_two_endpoints_stay_isolated(self):
        content = jpeg((33, 33, 33))
        local = PreviewBridge(base="http://127.0.0.1:17777", content=content)
        remote = PreviewBridge(base="http://10.0.0.4:17777",
                               catalog_id="studio.lrcat", content=content,
                               catalog_identity=REMOTE_CATALOG_IDENTITY)
        first = self.workbench(local).preview_photo("photo-1", size=640)
        second = self.workbench(remote).preview_photo("photo-1", size=640)
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(first["artifact"]["sha256"], second["artifact"]["sha256"])
        # No target was ever selected on either workbench, so both fall back to
        # the single-target id; the hashed endpoint and catalog identity are
        # what keep the two records -- and their caches -- apart.
        self.assertEqual(first["artifact"]["source"]["target_id"], "local")
        self.assertEqual(second["artifact"]["source"]["target_id"], "local")
        self.assertNotEqual(first["artifact"]["source"]["endpoint_fingerprint"],
                            second["artifact"]["source"]["endpoint_fingerprint"])
        self.assertNotEqual(first["artifact"]["source"]["catalog_identity"],
                            second["artifact"]["source"]["catalog_identity"])
        self.assertEqual(first["artifact"]["source"]["catalog_id"], LOCAL_CATALOG_ID)
        self.assertEqual(second["artifact"]["source"]["catalog_id"], "studio.lrcat")
        for record in (first, second):
            self.assertEqual(Path(record["preview_path"]).read_bytes(), content)


class TestPreviewCaching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bridge = PreviewBridge(base="http://127.0.0.1:17777", orientation="AB")
        self.wb = Workbench(Store(self.root), self.bridge)

    def test_cache_hit_reuses_the_render_metadata(self):
        rendered = self.wb.preview_photo("photo-1", size=640)
        cached = self.wb.preview_photo("photo-1", size=640)
        self.assertFalse(rendered["cached"])
        self.assertTrue(cached["cached"])
        self.assertEqual(rendered["freshness"], "unverified")
        self.assertEqual(cached["freshness"], "cached_unverified")
        self.assertEqual(cached["artifact"]["evidence_status"], "unverified")
        for field in ("key", "requested_size", "actual_size", "bytes",
                      "method", "purpose", "color_space", "orientation", "photo_uuid"):
            self.assertEqual(cached[field], rendered[field], field)
        self.assertNotIn("preview_url", rendered)
        self.assertNotIn("preview_url", rendered["artifact"])
        self.assertEqual(cached["artifact"]["sha256"], rendered["artifact"]["sha256"])
        self.assertEqual(cached["artifact"]["dimensions"], rendered["artifact"]["dimensions"])
        self.assertEqual(cached["artifact"]["orientation"], "AB")
        # A cache hit still probes the current Catalog identity before looking
        # up its namespace, but it does not request thumbnail bytes again.
        self.assertEqual(self.bridge.commands(),
                         ["probe_client", "request_jpeg_thumbnail", "probe_client"])

    def test_force_renders_again_and_keeps_the_old_artifact(self):
        first = self.wb.preview_photo("photo-1", size=640)
        first_path = Path(first["preview_path"])
        first_bytes = first_path.read_bytes()
        self.bridge.color = (0, 0, 255)
        forced = self.wb.preview_photo("photo-1", size=640, force=True)
        forced_path = Path(forced["preview_path"])
        self.assertNotEqual(forced["key"], first["key"])
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertNotEqual(forced_path.read_bytes(), first_bytes)

    def test_invalid_arguments_are_refused_before_any_render(self):
        bad = [("size", True), ("size", 1024.5), ("size", 63), ("size", 2049),
               ("size", None), ("force", "true"), ("force", 1), ("force", None)]
        for field, value in bad:
            with self.subTest(field=field, value=value):
                arguments = {"size": 640, "force": False, field: value}
                with self.assertRaises(StoreError):
                    self.wb.preview_photo("photo-1", **arguments)
        self.assertEqual(self.bridge.calls, [])


class TestPreviewPinning(unittest.TestCase):
    """The target that answered is the target recorded, even across a switch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = TargetRegistry(self.root / "targets.json")
        self.remote = self.registry.add(label="remote", base="http://10.0.0.4:17777",
                                        catalog_id="studio.lrcat", access_mode="full",
                                        catalog_identity=REMOTE_CATALOG_IDENTITY)
        self.reached = threading.Event()
        self.release = threading.Event()
        self.bridges = {}

        def factory(base=None, token=None, **kwargs):
            remote = "10.0.0.4" in str(base)
            bridge = PreviewBridge(base=base,
                                   catalog_id="studio.lrcat" if remote else None,
                                   catalog_identity=(REMOTE_CATALOG_IDENTITY if remote
                                                     else LOCAL_CATALOG_IDENTITY),
                                   color=(255, 0, 0) if remote else (0, 0, 255),
                                   gate=self.release if remote else None,
                                   reached=self.reached if remote else None)
            self.bridges[base] = bridge
            return bridge

        self.router = LrOpsRouter(self.registry, client_factory=factory)
        self.wb = Workbench(Store(self.root), self.router)

    def test_a_switch_during_the_fetch_cannot_change_the_answer(self):
        self.registry.select(self.remote.id)
        results = []
        worker = threading.Thread(
            target=lambda: results.append(self.wb.preview_photo("photo-1", size=640)))
        worker.start()
        self.assertTrue(self.reached.wait(5), "the render never reached the fetch")
        try:
            self.registry.select("local")  # the user switches Lightroom mid-request
        finally:
            self.release.set()
        worker.join(timeout=10)
        self.assertEqual(len(results), 1)
        record = results[0]
        source = record["artifact"]["source"]
        self.assertEqual(source["target_id"], "remote")
        self.assertEqual(source["catalog_id"], "studio.lrcat")
        remote_bridge = self.bridges["http://10.0.0.4:17777"]
        self.assertEqual(remote_bridge.commands().count("request_jpeg_thumbnail"), 1)
        self.assertEqual(Path(record["preview_path"]).read_bytes(),
                         remote_bridge.rendered()[0])
        # The switch happened before the fetch: the newly selected bridge was
        # never even built, while the selection itself did change.
        self.assertNotIn("http://127.0.0.1:17777", self.bridges)
        self.assertEqual(self.router.target_id, "local")


if __name__ == "__main__":
    unittest.main()
