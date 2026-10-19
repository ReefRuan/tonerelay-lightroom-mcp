"""Runtime-host folders: config, containment, scanning, previews, sidecars.

The tests are grouped by the promise each one pins down:

* ``ConfigTestCase``  -- one config file, a documented format, and a legacy file
  that must not re-import photos already in the catalog.
* ``SecurityTestCase`` -- containment before existence, no traversal, no
  symlink escape, and no absolute path anywhere in a response.
* ``ScanTestCase``    -- only configured extensions, recursion under a config
  flag, and Lightroom never being touched.
* ``PreviewTestCase`` -- the direct/Lightroom split and the cache decisions
  that decide whether Lightroom is called at all.
* ``WriteXmpTestCase`` -- the one place this module writes into a user's photo
  directory.
"""

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from workbench.folders import (CONFIG_ENV, DEFAULT_EXTENSIONS, DIRECT_EXTENSIONS,
                               FolderConflict, FolderError, FolderLibrary,
                               FolderNotFound, _parse_config_text, file_key_for,
                               image_header_facts, normalize_extensions, root_key_for)


def _image_bytes(fmt="JPEG", size=(48, 32), color=(20, 60, 120)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


JPEG_BYTES = _image_bytes("JPEG")
PNG_BYTES = _image_bytes("PNG")
TIFF_BYTES = _image_bytes("TIFF", size=(24, 12))


class FakeBridge:
    """Records every bridge call so "did this touch Lightroom?" is a fact."""

    def __init__(self):
        self.calls = []
        self.export_bytes = JPEG_BYTES
        self.uuid_seq = 0

    def run(self, command, params=None):
        self.calls.append((command, dict(params or {})))
        if command == "import_photo":
            self.uuid_seq += 1
            return {"photo": {"uuid": f"UUID-{self.uuid_seq}"}}
        if command == "export_preview":
            return {"output_ref": f"ref-{len(self.calls)}", "color_space": "sRGB"}
        return {}

    def upload(self, filename, content):
        self.calls.append(("upload", filename))
        return "stage-1"

    def fetch_output(self, ref, destination):
        self.calls.append(("fetch_output", ref))
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.export_bytes)
        return destination

    def discard_stage(self, stage_id):
        self.calls.append(("discard", stage_id))

    def reachable(self):
        return True

    # ---- assertions helpers ------------------------------------------------

    def commands(self):
        return [name for name, _ in self.calls]

    def reset(self):
        self.calls = []


class FolderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.data = self.base / "data"
        self.photos = self.base / "photos"
        self.photos.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.lib = FolderLibrary(self.data)
        self.bridge = FakeBridge()
        self.use_config()

    # ---- fixture helpers ---------------------------------------------------

    def use_config(self, *, name="folder-library.json", **overrides):
        payload = {
            "roots": [str(self.photos)],
            "extensions": list(DEFAULT_EXTENSIONS),
            "recursive": False,
        }
        payload.update(overrides)
        (self.data / name).write_text(json.dumps(payload, ensure_ascii=False),
                                      encoding="utf-8")
        return self.data / name

    def clear_config(self):
        for name in ("folder-library.json", "folder-library.yaml",
                     "folder-library.yml", "folder-roots.json"):
            (self.data / name).unlink(missing_ok=True)

    def put(self, name, content=JPEG_BYTES, directory=None):
        target = (directory or self.photos) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def scan(self, **kwargs):
        if "path" not in kwargs and "root" not in kwargs:
            kwargs["path"] = str(self.photos)
        return self.lib.scan(**kwargs)

    def payload(self, value):
        """The serialized public payload used by no-path-leak assertions."""
        return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------
# configuration


class ConfigTestCase(FolderTestCase):
    def test_defaults_when_nothing_is_configured(self):
        self.clear_config()
        config = self.lib.config()
        self.assertEqual(config.source, "default")
        self.assertTrue(config.roots)
        if os.name != "nt":
            self.assertEqual(config.roots, ("/",))
        self.assertEqual(config.extensions, DEFAULT_EXTENSIONS)
        self.assertFalse(config.recursive)
        self.assertIsNone(config.error)
        self.assertEqual(self.lib.public_state()["access_mode"], "full")
        self.assertEqual(self.lib.public_state()["roots"][0]["label"],
                         "整个 Runtime 主机")

    def test_default_full_access_can_scan_a_runtime_host_directory(self):
        self.clear_config()
        photo = self.put("full-access.jpg")
        root = self.lib.public_state()["roots"][0]
        if os.name == "nt":
            drive_root = Path(root["label"].split()[-1] + "\\")
            relative = photo.relative_to(drive_root).as_posix()
        else:
            relative = photo.relative_to(Path("/")).as_posix()
        result = self.lib.scan(root=root["key"], rel=relative.rsplit("/", 1)[0])
        self.assertEqual([item["name"] for item in result["files"]], [photo.name])

    def test_default_extensions_are_the_documented_set(self):
        self.assertEqual(
            set(DEFAULT_EXTENSIONS),
            {".jpg", ".jpeg", ".png", ".tif", ".tiff",
             ".cr2", ".cr3", ".nef", ".arw", ".raf", ".rw2", ".dng"})

    def test_explicitly_empty_roots_config_disables_folder_access(self):
        self.use_config(roots=[])
        with self.assertRaises(FolderError) as caught:
            self.scan()
        self.assertIn("没有允许访问的目录", str(caught.exception))
        self.assertEqual(self.lib.public_state()["access_mode"], "restricted")

    def test_extension_normalisation(self):
        self.assertEqual(normalize_extensions(["JPG", " .PNg ", "tif", ".jpg", "CR2"]),
                         (".jpg", ".png", ".tif", ".cr2"))
        self.assertEqual(normalize_extensions("jpeg"), (".jpeg",))

    def test_extension_normalisation_rejects_junk(self):
        for bad in ([""], ["  "], ["."], [".xmp"], [".lrcat"], ["a/b"], ["a\\b"],
                    [None], [7]):
            with self.subTest(bad=bad):
                with self.assertRaises(FolderError):
                    normalize_extensions(bad)

    def test_empty_extension_list_is_refused_loudly(self):
        self.use_config(extensions=[])
        config = self.lib.config()
        self.assertIn("extensions", config.error or "")
        with self.assertRaises(FolderError):
            self.scan()

    def test_absent_extensions_key_still_uses_the_defaults(self):
        path = self.data / "folder-library.json"
        path.write_text(json.dumps({"roots": [str(self.photos)], "recursive": False}),
                        encoding="utf-8")
        self.assertEqual(self.lib.config().extensions, DEFAULT_EXTENSIONS)

    def test_json_config_is_read(self):
        self.use_config()
        config = self.lib.config()
        self.assertEqual(config.source, "primary")
        self.assertEqual(config.roots, (str(self.photos.resolve()),))

    def test_yaml_config_is_read(self):
        self.use_config(name="folder-library.yaml")
        self.assertEqual(self.lib.config().source, "primary")

    def test_documented_yaml_example_shape_parses_without_pyyaml(self):
        """The built-in subset parser must read exactly the shipped example."""
        from workbench.folders import _minimal_yaml
        path = self.base / "example.yaml"
        path.write_text(
            "# comment\n"
            "roots:\n"
            "  - /var/tmp/tonerelay-fixtures/Pictures\n"
            "  - /var/tmp/tonerelay-fixtures/Photos\n"
            "extensions:\n"
            "  - .jpg\n"
            "  - .CR3\n"
            "recursive: false\n",
            encoding="utf-8")
        parsed = _minimal_yaml(path.read_text(encoding="utf-8"), path)
        self.assertEqual(parsed["roots"], ["/var/tmp/tonerelay-fixtures/Pictures", "/var/tmp/tonerelay-fixtures/Photos"])
        self.assertEqual(parsed["extensions"], [".jpg", ".CR3"])
        self.assertFalse(parsed["recursive"])

    def test_shipped_examples_parse_and_match_the_documented_contract(self):
        repo = Path(__file__).resolve().parents[1]
        for name in ("folder-library.example.json", "folder-library.example.yaml"):
            example = repo / name
            self.assertTrue(example.is_file(), f"{name} 缺失")
            parsed = _parse_config_text(example.read_text(encoding="utf-8"), example)
            self.assertEqual(set(parsed) - {"_comment"},
                             {"roots", "extensions", "recursive"})
            self.assertEqual(normalize_extensions(parsed["extensions"]),
                             DEFAULT_EXTENSIONS)
            self.assertTrue(all(entry.startswith("/") for entry in parsed["roots"]))
            self.assertIs(parsed["recursive"], False)

    def test_underscore_keys_are_treated_as_comments(self):
        path = self.data / "folder-library.json"
        path.write_text(json.dumps({
            "_comment": "hi", "roots": [str(self.photos)],
            "extensions": [".jpg"], "recursive": False}), encoding="utf-8")
        self.assertIsNone(self.lib.config().error)
        self.assertEqual(self.lib.config().extensions, (".jpg",))

    def test_unknown_config_key_is_an_error_not_a_silent_ignore(self):
        self.use_config(recursiv=False)
        with self.assertRaises(FolderError) as caught:
            self.scan()
        self.assertIn("未知配置项", str(caught.exception))

    def test_env_override_wins_over_the_default_path(self):
        self.use_config()
        other = self.base / "elsewhere" / "config.json"
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps({"roots": [], "extensions": [".jpg"],
                                     "recursive": False}), encoding="utf-8")
        with mock.patch.dict(os.environ, {CONFIG_ENV: str(other)}):
            config = FolderLibrary(self.data).config()
        self.assertEqual(config.source, "env")
        self.assertEqual(config.path, other)

    def test_env_pointing_at_a_missing_file_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {CONFIG_ENV: str(self.base / "nope.yaml")}):
            config = FolderLibrary(self.data).config()
        self.assertEqual(config.roots, ())
        self.assertIn("不存在", config.error or "")
        with self.assertRaises(FolderError):
            FolderLibrary(self.data).scan(path=str(self.photos))

    def test_legacy_folder_roots_is_read_with_default_extensions(self):
        self.clear_config()
        (self.data / "folder-roots.json").write_text(
            json.dumps({"roots": [str(self.photos)], "updated_at": 1}), encoding="utf-8")
        config = self.lib.config()
        self.assertEqual(config.source, "legacy")
        self.assertEqual(config.roots, (str(self.photos.resolve()),))
        self.assertEqual(config.extensions, DEFAULT_EXTENSIONS)
        self.assertFalse(config.recursive)

    def test_primary_config_wins_over_legacy(self):
        (self.data / "folder-roots.json").write_text(
            json.dumps({"roots": [str(self.photos)]}), encoding="utf-8")
        self.use_config(roots=[])
        config = self.lib.config()
        self.assertEqual(config.source, "primary")
        self.assertEqual(config.roots, ())

    def test_broken_config_does_not_fall_back_to_defaults_silently(self):
        (self.data / "folder-library.json").write_text("{not json or yaml: [",
                                                       encoding="utf-8")
        config = self.lib.config()
        self.assertIsNotNone(config.error)
        self.assertEqual(config.roots, ())

    def test_external_config_edit_is_picked_up_without_a_restart(self):
        self.use_config()
        self.assertEqual(len(self.lib.config().roots), 1)
        self.use_config(roots=[str(self.photos), str(self.base)])
        self.assertEqual(len(self.lib.config().roots), 2)

    def test_root_key_is_stable_and_distinct(self):
        self.assertEqual(root_key_for(self.photos), root_key_for(self.photos.resolve()))
        self.assertNotEqual(root_key_for(self.photos), root_key_for(self.base))
        self.assertEqual(len(root_key_for(self.photos)), 12)

    def test_public_state_uses_only_opaque_root_keys(self):
        self.use_config()
        state = self.lib.public_state()
        self.assertNotIn(str(self.photos), self.payload(state))
        self.assertEqual(set(state["roots"][0]), {"key", "label"})

    def test_capabilities_split_direct_from_lightroom(self):
        self.use_config()
        caps = self.lib.public_state()["capabilities"]
        self.assertEqual(set(caps["direct_preview"]),
                         {e for e in DIRECT_EXTENSIONS if e in DEFAULT_EXTENSIONS})
        self.assertIn(".cr2", caps["lightroom_preview"])
        self.assertNotIn(".jpg", caps["lightroom_preview"])
        self.assertTrue(caps["scan"])

    def test_nested_roots_use_the_most_specific_one(self):
        inner = self.photos / "inner"
        inner.mkdir()
        self.use_config(roots=[str(self.photos), str(inner)])
        self.put("inner/a.jpg")
        result = self.lib.scan(path=str(inner))
        self.assertEqual(result["root"], root_key_for(inner))


# --------------------------------------------------------------------------
# security


class SecurityTestCase(FolderTestCase):
    def setUp(self):
        super().setUp()
        self.use_config()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.jpg").write_bytes(JPEG_BYTES)

    def test_unlisted_path_is_refused_whether_or_not_it_exists(self):
        """The refusal must not double as an existence oracle."""
        with self.assertRaises(FolderNotFound) as existing:
            self.lib.scan(path=str(self.outside))
        with self.assertRaises(FolderNotFound) as missing:
            self.lib.scan(path=str(self.base / "does-not-exist"))
        self.assertEqual(str(existing.exception), str(missing.exception))

    def test_refusal_does_not_leak_the_resolved_path(self):
        with self.assertRaises(FolderNotFound) as caught:
            self.lib.scan(path=str(self.outside))
        self.assertNotIn(str(self.outside), str(caught.exception))

    def test_traversal_in_a_typed_path_is_refused(self):
        for raw in (f"{self.photos}/../outside", f"{self.photos}/../../etc",
                    "/var/tmp/tonerelay-fixtures/Pictures/../../etc"):
            with self.subTest(raw=raw):
                with self.assertRaises(FolderError):
                    self.lib.scan(path=raw)

    def test_traversal_in_the_relative_directory_is_refused(self):
        root = root_key_for(self.photos)
        for rel in ("..", "../outside", "sub/../../outside", "a/../../b", "/etc",
                    "sub\\..\\..", "sub\x00"):
            with self.subTest(rel=rel):
                with self.assertRaises(FolderError):
                    self.lib.scan(root=root, rel=rel)

    def test_symlinked_file_escaping_the_root_is_skipped(self):
        (self.photos / "escape.jpg").symlink_to(self.outside / "secret.jpg")
        self.put("kept.jpg")
        result = self.scan()
        self.assertEqual([f["name"] for f in result["files"]], ["kept.jpg"])
        self.assertEqual(result["skipped_reasons"]["escape"], 1)

    def test_symlinked_file_inside_the_root_is_listed_under_its_target_key(self):
        target = self.put("real.jpg")
        (self.photos / "alias.jpg").symlink_to(target)
        result = self.scan()
        names = sorted(f["name"] for f in result["files"])
        self.assertEqual(names, ["alias.jpg", "real.jpg"])
        keys = {f["name"]: f["key"] for f in result["files"]}
        # The same bytes must key the same way however they are reached.
        self.assertEqual(keys["alias.jpg"], file_key_for(target))

    def test_directory_symlinks_are_never_traversed_even_when_recursing(self):
        self.use_config(recursive=True)
        (self.photos / "linkdir").symlink_to(self.outside)
        self.put("kept.jpg")
        result = self.scan()
        self.assertEqual([f["name"] for f in result["files"]], ["kept.jpg"])
        self.assertEqual(result["skipped_reasons"]["symlink_dir"], 1)

    def test_tree_refuses_a_symlinked_directory_escaping_the_root(self):
        (self.photos / "linkdir").symlink_to(self.outside)
        (self.photos / "real").mkdir()
        result = self.lib.tree(root=root_key_for(self.photos))
        self.assertEqual([entry["name"] for entry in result["dirs"]], ["real"])

    def test_preview_of_a_path_outside_the_whitelist_is_refused(self):
        with self.assertRaises(FolderError):
            self.lib.preview(path=str(self.outside), name="secret.jpg")
        self.assertEqual(self.bridge.calls, [])

    def test_exposed_name_forms_are_refused(self):
        for name in ("../secret.jpg", "sub/secret.jpg", "..", "", ".hidden.jpg",
                     "a\\b.jpg", "x\x00.jpg"):
            with self.subTest(name=name):
                with self.assertRaises(FolderError):
                    self.lib.preview(path=str(self.photos), name=name)

    def test_non_image_extensions_are_refused_even_when_present(self):
        self.put("notes.txt", b"hello")
        self.put("side.xmp", b"<x/>")
        with self.assertRaises(FolderError) as caught:
            self.lib.preview(path=str(self.photos), name="side.xmp")
        self.assertIn(".xmp", str(caught.exception))

    def test_hidden_and_denied_entries_never_appear(self):
        self.put(".hidden.jpg")
        self.put("notes.txt", b"x")
        self.put("shot.xmp", b"x")
        (self.photos / ".lrdata").mkdir()
        self.put("inside.jpg", JPEG_BYTES, directory=self.photos / ".lrdata")
        self.put("kept.jpg")
        result = self.scan()
        self.assertEqual([f["name"] for f in result["files"]], ["kept.jpg"])

    def test_no_absolute_path_anywhere_in_a_scan_response(self):
        self.put("one.jpg")
        self.put("two.jpg", JPEG_BYTES, directory=self.photos / "sub")
        payload = self.payload(self.lib.scan(path=str(self.photos)))
        self.assertNotIn(str(self.photos), payload)
        self.assertNotIn(str(self.base), payload)

    def test_no_absolute_path_anywhere_in_a_tree_response(self):
        (self.photos / "sub").mkdir()
        payload = self.payload(self.lib.tree(root=root_key_for(self.photos)))
        self.assertNotIn(str(self.photos), payload)

    def test_no_absolute_path_anywhere_in_a_direct_preview(self):
        self.put("one.jpg")
        record = self.lib.preview(path=str(self.photos), name="one.jpg")
        self.assertNotIn(str(self.photos), self.payload(record))
        self.assertNotIn("resolved", record)

    def test_unknown_root_key_is_refused(self):
        with self.assertRaises(FolderError):
            self.lib.scan(root="0" * 12)
        with self.assertRaises(FolderError):
            self.lib.scan(root="not-a-key")


# --------------------------------------------------------------------------
# scanning and the tree


class ScanTestCase(FolderTestCase):
    def test_only_configured_extensions_are_listed(self):
        self.use_config(extensions=[".jpg", ".cr2"])
        self.put("one.jpg")
        self.put("two.JPG", JPEG_BYTES)
        self.put("three.cr2", b"raw")
        self.put("four.png", PNG_BYTES)
        result = self.scan()
        self.assertEqual(sorted(f["name"] for f in result["files"]),
                         ["one.jpg", "three.cr2", "two.JPG"])
        self.assertEqual(result["skipped_reasons"]["extension"], 1)

    def test_extension_matching_is_case_insensitive(self):
        self.put("UPPER.JPG")
        self.put("mixed.PnG", PNG_BYTES)
        result = self.scan()
        self.assertEqual(sorted(f["extension"] for f in result["files"]),
                         [".jpg", ".png"])

    def test_files_report_size_extension_key_and_preview_route(self):
        self.put("one.jpg")
        self.put("two.cr2", b"raw-bytes")
        result = self.scan()
        by_name = {f["name"]: f for f in result["files"]}
        self.assertEqual(by_name["one.jpg"]["preview"], "direct")
        self.assertEqual(by_name["two.cr2"]["preview"], "lightroom_export")
        self.assertEqual(by_name["two.cr2"]["size"], len(b"raw-bytes"))
        self.assertRegex(by_name["two.cr2"]["key"], r"\A[0-9a-f]{20}\Z")

    def test_tiff_defaults_to_direct_for_mcp_image_conversion(self):
        self.put("scan.tif", TIFF_BYTES)
        result = self.scan()
        entry = result["files"][0]
        self.assertEqual(entry["preview"], "direct")

    def test_has_sidecar_reports_existence_only(self):
        self.put("one.jpg")
        self.put("two.jpg")
        (self.photos / "two.xmp").write_text("<x:xmpmeta/>", encoding="utf-8")
        by_name = {f["name"]: f for f in self.scan()["files"]}
        self.assertFalse(by_name["one.jpg"]["has_sidecar"])
        self.assertTrue(by_name["two.jpg"]["has_sidecar"])
        self.assertNotIn("xmpmeta", self.payload(by_name["two.jpg"]))

    def test_scan_never_calls_lightroom(self):
        self.put("one.jpg")
        self.put("two.cr2", b"raw")
        result = self.scan()
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(result["count"], 2)

    def test_empty_directory_is_a_clear_404_naming_the_extensions(self):
        with self.assertRaises(FolderNotFound) as caught:
            self.scan()
        self.assertIn("没有符合已配置扩展名的图片", str(caught.exception))
        self.assertIn(".cr2", str(caught.exception))

    def test_missing_directory_is_reported_without_the_absolute_path(self):
        with self.assertRaises(FolderNotFound) as caught:
            self.lib.scan(root=root_key_for(self.photos), rel="nope")
        self.assertNotIn(str(self.photos), str(caught.exception))
        self.assertIn("nope", str(caught.exception))

    def test_recursive_off_lists_one_level_only(self):
        self.put("top.jpg")
        self.put("nested.jpg", JPEG_BYTES, directory=self.photos / "deep")
        self.assertEqual([f["name"] for f in self.scan()["files"]], ["top.jpg"])

    def test_recursive_on_lists_nested_files_with_their_relative_dir(self):
        self.use_config(recursive=True)
        self.put("top.jpg")
        self.put("nested.jpg", JPEG_BYTES, directory=self.photos / "deep" / "inner")
        result = self.scan()
        by_name = {f["name"]: f for f in result["files"]}
        self.assertEqual(set(by_name), {"top.jpg", "nested.jpg"})
        self.assertEqual(by_name["nested.jpg"]["rel_dir"], "deep/inner")
        self.assertEqual(by_name["top.jpg"]["rel_dir"], "")
        self.assertTrue(result["recursive"])

    def test_recursive_skips_lightroom_storage_directories(self):
        self.use_config(recursive=True)
        self.put("kept.jpg")
        for denied in ("catalog.lrdata", "sandbox.lrprev", ".git", "nested.lrcat"):
            self.put("inside.jpg", JPEG_BYTES, directory=self.photos / denied)
        self.assertEqual([f["name"] for f in self.scan()["files"]], ["kept.jpg"])

    def test_scan_request_cannot_turn_recursion_on(self):
        """``recursive`` is config-controlled; the API has no such argument."""
        self.put("top.jpg")
        with self.assertRaises(TypeError):
            self.lib.scan(path=str(self.photos), recursive=True)

    def test_scan_records_a_direct_address_so_the_thumb_resolves_before_a_post(self):
        self.put("one.jpg")
        result = self.scan()
        key = result["files"][0]["key"]
        entry = self.lib.entry(key)
        self.assertEqual(entry["method"], "direct")
        content, mime = self.lib.preview_bytes(key)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(content, JPEG_BYTES)

    def test_scan_does_not_downgrade_a_rendered_record(self):
        self.put("one.cr2", b"raw")
        rendered = self.lib.preview(path=str(self.photos), name="one.cr2",
                                    client=self.bridge, catalog_write=True)
        self.scan()
        self.assertEqual(self.lib.entry(rendered["key"])["method"], "lightroom_export")

    def test_truncation_is_reported_not_silent(self):
        for index in range(5):
            self.put(f"shot{index}.jpg")
        with mock.patch("workbench.folders.MAX_SCAN_FILES", 3):
            result = self.scan()
        self.assertTrue(result["truncated"])
        self.assertEqual(result["count"], 3)


class TreeTestCase(FolderTestCase):
    def test_tree_returns_one_level_with_relative_dirs(self):
        self.use_config()
        (self.photos / "alpha" / "beta").mkdir(parents=True)
        (self.photos / "gamma").mkdir()
        self.put("top.jpg")
        result = self.lib.tree(root=root_key_for(self.photos))
        self.assertEqual([entry["name"] for entry in result["dirs"]], ["alpha", "gamma"])
        self.assertEqual(result["dirs"][0]["dir"], "alpha")
        self.assertEqual(result["images"], 1)

    def test_tree_hides_denied_and_hidden_directories(self):
        self.use_config()
        (self.photos / ".lrdata").mkdir()
        (self.photos / ".hidden").mkdir()
        (self.photos / "shown").mkdir()
        result = self.lib.tree(root=root_key_for(self.photos))
        self.assertEqual([entry["name"] for entry in result["dirs"]], ["shown"])

    def test_tree_never_recurses_on_its_own(self):
        self.use_config()
        (self.photos / "alpha" / "beta").mkdir(parents=True)
        result = self.lib.tree(root=root_key_for(self.photos))
        self.assertEqual([entry["name"] for entry in result["dirs"]], ["alpha"])


class RecentTestCase(FolderTestCase):
    def test_scanning_a_directory_remembers_it(self):
        self.use_config()
        self.put("one.jpg")
        self.scan()
        recent = self.lib.recent()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["root"], root_key_for(self.photos))
        self.assertEqual(recent[0]["dir"], "")
        self.assertEqual(recent[0]["label"], self.photos.name)

    def test_recent_is_deduplicated_and_newest_first(self):
        self.use_config()
        sub = self.photos / "sub"
        sub.mkdir()
        self.put("one.jpg")
        self.put("two.jpg", JPEG_BYTES, directory=sub)
        self.scan()
        self.lib.scan(root=root_key_for(self.photos), rel="sub")
        self.scan()
        recent = self.lib.recent()
        self.assertEqual([entry["dir"] for entry in recent], ["", "sub"])

    def test_recent_forgets_roots_that_were_removed(self):
        self.use_config()
        self.put("one.jpg")
        self.scan()
        self.use_config(roots=[])
        self.assertEqual(self.lib.recent(), [])


# --------------------------------------------------------------------------
# previews


class PreviewTestCase(FolderTestCase):
    def setUp(self):
        super().setUp()
        self.use_config()

    def test_direct_jpeg_is_served_without_touching_lightroom(self):
        self.put("one.jpg")
        record = self.lib.preview(path=str(self.photos), name="one.jpg")
        self.assertEqual(record["method"], "direct")
        self.assertEqual(record["mime_type"], "image/jpeg")
        self.assertEqual(record["cache_status"], "direct")
        self.assertIsNone(record["uuid"])
        self.assertEqual(self.bridge.calls, [])
        content, mime = self.lib.preview_bytes(record["key"])
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(content, JPEG_BYTES)

    def test_direct_preview_reports_header_dimensions(self):
        self.put("one.png", PNG_BYTES)
        record = self.lib.preview(path=str(self.photos), name="one.png")
        self.assertEqual((record["width"], record["height"]), (48, 32))
        self.assertEqual(record["dimensions_source"], "header")

    def test_header_reader_handles_the_three_formats(self):
        self.assertEqual(image_header_facts(JPEG_BYTES, ".jpg")[:2], (48, 32))
        self.assertEqual(image_header_facts(PNG_BYTES, ".png")[:2], (48, 32))
        self.assertEqual(image_header_facts(TIFF_BYTES, ".tif")[:2], (24, 12))
        self.assertIsNone(image_header_facts(b"", ".jpg"))
        self.assertIsNone(image_header_facts(b"not an image", ".jpeg"))

    def test_tiff_can_be_forced_direct(self):
        self.put("scan.tif", TIFF_BYTES)
        record = self.lib.preview(path=str(self.photos), name="scan.tif",
                                  render="direct")
        self.assertEqual(record["method"], "direct")
        self.assertEqual(record["mime_type"], "image/tiff")

    def test_raw_cannot_be_forced_direct(self):
        self.put("one.cr2", b"raw")
        with self.assertRaises(FolderError) as caught:
            self.lib.preview(path=str(self.photos), name="one.cr2", render="direct")
        self.assertIn("Lightroom", str(caught.exception))
        self.assertEqual(self.bridge.calls, [])

    def test_raw_preview_requires_explicit_catalog_write(self):
        self.put("one.cr2", b"raw")
        with self.assertRaises(FolderError) as caught:
            self.lib.preview(path=str(self.photos), name="one.cr2", client=self.bridge)
        self.assertIn("catalog_write=allow", str(caught.exception))
        self.assertEqual(self.bridge.calls, [])

    def test_raw_preview_runs_the_documented_sequence(self):
        self.put("one.cr2", b"raw-bytes")
        record = self.lib.preview(path=str(self.photos), name="one.cr2",
                                  client=self.bridge, catalog_write=True)
        self.assertEqual(self.bridge.commands(),
                         ["upload", "import_photo", "export_preview", "fetch_output"])
        self.assertEqual(record["method"], "lightroom_export")
        self.assertEqual(record["cache_status"], "miss")
        self.assertTrue(record["rendered_now"])
        self.assertTrue(record["imported_now"])
        self.assertEqual(record["uuid"], "UUID-1")
        self.assertEqual((record["width"], record["height"]), (48, 32))
        self.assertEqual(record["mime_type"], "image/jpeg")
        self.assertEqual(self.lib.blob_path(record["key"]).read_bytes(), JPEG_BYTES)

    def test_second_raw_preview_is_a_hit_with_no_bridge_traffic(self):
        self.put("one.cr2", b"raw-bytes")
        first = self.lib.preview(path=str(self.photos), name="one.cr2",
                                 client=self.bridge, catalog_write=True)
        self.bridge.reset()
        second = self.lib.preview(path=str(self.photos), name="one.cr2",
                                  client=self.bridge, catalog_write=True)
        self.assertEqual(self.bridge.calls, [], "缓存命中不应再调用 Lightroom")
        self.assertEqual(second["cache_status"], "hit")
        self.assertFalse(second["rendered_now"])
        self.assertFalse(second["imported_now"])
        self.assertEqual(second["uuid"], first["uuid"])
        self.assertEqual(second["width"], first["width"])
        self.assertEqual(second["artifact"]["sha256"], first["artifact"]["sha256"])

    def test_touching_the_file_without_changing_it_re_exports_without_reimporting(self):
        target = self.put("one.cr2", b"raw-bytes")
        self.lib.preview(path=str(self.photos), name="one.cr2",
                         client=self.bridge, catalog_write=True)
        self.bridge.reset()
        info = target.stat()
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000))
        record = self.lib.preview(path=str(self.photos), name="one.cr2",
                                  client=self.bridge, catalog_write=True)
        self.assertEqual(self.bridge.commands(), ["export_preview", "fetch_output"])
        self.assertEqual(record["cache_status"], "reused_uuid")
        self.assertEqual(record["uuid"], "UUID-1")
        self.assertFalse(record["imported_now"])

    def test_changing_the_bytes_re_imports_and_re_renders(self):
        target = self.put("one.cr2", b"raw-bytes")
        first = self.lib.preview(path=str(self.photos), name="one.cr2",
                                 client=self.bridge, catalog_write=True)
        self.bridge.reset()
        target.write_bytes(b"different bytes entirely")
        second = self.lib.preview(path=str(self.photos), name="one.cr2",
                                  client=self.bridge, catalog_write=True)
        self.assertEqual(self.bridge.commands(),
                         ["upload", "import_photo", "export_preview", "fetch_output"])
        self.assertEqual(second["cache_status"], "stale")
        self.assertTrue(second["imported_now"])
        self.assertEqual(second["uuid"], "UUID-2")
        self.assertEqual(second["key"], first["key"])

    def test_force_re_renders_but_reuses_the_uuid(self):
        self.put("one.cr2", b"raw-bytes")
        self.lib.preview(path=str(self.photos), name="one.cr2",
                         client=self.bridge, catalog_write=True)
        self.bridge.reset()
        record = self.lib.preview(path=str(self.photos), name="one.cr2",
                                  client=self.bridge, catalog_write=True, force=True)
        self.assertEqual(self.bridge.commands(), ["export_preview", "fetch_output"])
        self.assertEqual(record["cache_status"], "reused_uuid")
        self.assertFalse(record["imported_now"])

    def test_stale_uuid_recovers_by_importing_again(self):
        self.put("one.cr2", b"raw-bytes")
        self.lib.preview(path=str(self.photos), name="one.cr2",
                         client=self.bridge, catalog_write=True)
        entry_path = self.lib._entry_path(
            file_key_for(self.photos / "one.cr2"))
        record = json.loads(entry_path.read_text())
        record["source"] = {"size": 1, "mtime_ns": 1}
        record["sha256"] = "deadbeef"
        entry_path.write_text(json.dumps(record), encoding="utf-8")
        self.bridge.reset()
        result = self.lib.preview(path=str(self.photos), name="one.cr2",
                                  client=self.bridge, catalog_write=True)
        self.assertEqual(result["cache_status"], "stale")
        self.assertTrue(result["imported_now"])

    def test_exported_bytes_must_be_a_real_jpeg(self):
        self.put("one.cr2", b"raw")
        self.bridge.export_bytes = b"not a jpeg at all"
        with self.assertRaises(FolderError) as caught:
            self.lib.preview(path=str(self.photos), name="one.cr2",
                             client=self.bridge, catalog_write=True)
        self.assertIn("JPEG", str(caught.exception))

    def test_failed_import_releases_the_staged_upload(self):
        self.put("one.cr2", b"raw")

        class Broken(FakeBridge):
            def run(self, command, params=None):
                self.calls.append((command, dict(params or {})))
                raise RuntimeError("bridge said no")

        broken = Broken()
        with self.assertRaises(RuntimeError):
            self.lib.preview(path=str(self.photos), name="one.cr2",
                             client=broken, catalog_write=True)
        self.assertIn(("discard", "stage-1"), broken.calls)

    def test_thumb_bytes_are_none_before_any_preview(self):
        self.put("one.jpg")
        self.assertIsNone(self.lib.preview_bytes(file_key_for(self.photos / "one.jpg")))
        self.assertIsNone(self.lib.preview_bytes("nope"))
        self.assertIsNone(self.lib.preview_bytes("../.."))

    def test_adopt_imports_once_and_keeps_a_direct_entry_direct(self):
        self.put("one.jpg")
        first = self.lib.adopt(path=str(self.photos), name="one.jpg", client=self.bridge)
        self.assertEqual(self.bridge.commands(), ["upload", "import_photo"])
        self.assertTrue(first["imported_now"])
        self.bridge.reset()
        second = self.lib.adopt(path=str(self.photos), name="one.jpg", client=self.bridge)
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(second["imported_now"])
        self.assertEqual(second["uuid"], first["uuid"])
        # Registering it must not pretend a JPEG needed Lightroom to be seen.
        self.assertEqual(self.lib.entry(first["key"])["method"], "direct")

    def test_key_construction_matches_the_cached_blobs_already_on_disk(self):
        """Pins the hash so a later "improvement" cannot orphan live blobs."""
        pinned = "/var/tmp/tonerelay-fixtures/IMG_0004.CR2"
        self.assertEqual(file_key_for(pinned), "7d5a4125b75d460c18f9")

    def test_legacy_cache_entry_is_reused_instead_of_re_importing(self):
        """Exactly the shape the live data root has today."""
        target = self.put("IMG_0004.CR2", b"raw-bytes")
        key = file_key_for(target)
        (self.data / "folder-cache").mkdir(parents=True, exist_ok=True)
        (self.data / "folder-cache" / f"{key}.jpg").write_bytes(JPEG_BYTES)
        (self.data / "folder-cache" / "index.json").write_text(json.dumps({
            str(target.resolve()): {"uuid": "LEGACY-UUID", "key": key,
                                    "name": "IMG_0004.CR2", "at": 1},
        }), encoding="utf-8")
        record = self.lib.preview(path=str(self.photos), name="IMG_0004.CR2",
                                  client=self.bridge, catalog_write=True)
        self.assertEqual(self.bridge.calls, [], "旧缓存应命中，不应重新导入")
        self.assertEqual(record["cache_status"], "hit")
        self.assertEqual(record["uuid"], "LEGACY-UUID")
        self.assertEqual(record["method"], "lightroom_export")

    def test_legacy_uuid_is_reused_even_when_the_blob_is_gone(self):
        target = self.put("IMG_0004.CR2", b"raw-bytes")
        key = file_key_for(target)
        (self.data / "folder-cache").mkdir(parents=True, exist_ok=True)
        (self.data / "folder-cache" / "index.json").write_text(json.dumps({
            str(target.resolve()): {"uuid": "LEGACY-UUID", "key": key,
                                    "name": "IMG_0004.CR2", "at": 1},
        }), encoding="utf-8")
        record = self.lib.preview(path=str(self.photos), name="IMG_0004.CR2",
                                  client=self.bridge, catalog_write=True)
        self.assertEqual(self.bridge.commands(), ["export_preview", "fetch_output"])
        self.assertEqual(record["uuid"], "LEGACY-UUID")
        self.assertFalse(record["imported_now"])

    def test_locate_finds_the_folder_a_catalog_photo_came_from(self):
        self.put("one.jpg")
        adopted = self.lib.adopt(path=str(self.photos), name="one.jpg",
                                 client=self.bridge)
        found = self.lib.locate(adopted["uuid"])
        self.assertTrue(found["found"])
        self.assertEqual(found["key"], adopted["key"])
        self.assertEqual(found["name"], "one.jpg")
        self.assertNotIn(str(self.photos), self.payload(found))

    def test_locate_refuses_a_photo_outside_every_whitelisted_root(self):
        self.assertEqual(self.lib.locate("SOME-UUID")["found"], False)


# --------------------------------------------------------------------------
# writing the verified XMP beside the original


class WriteXmpTestCase(FolderTestCase):
    def setUp(self):
        super().setUp()
        self.use_config()
        self.original = self.put("IMG_0004.CR2", b"raw-bytes")
        self.key = file_key_for(self.original)
        self.lib.adopt(path=str(self.photos), name="IMG_0004.CR2", client=self.bridge)

    def test_writes_a_sidecar_beside_the_original(self):
        result = self.lib.write_xmp(key=self.key, content=b"<x:xmpmeta/>")
        sidecar = self.photos / "IMG_0004.xmp"
        self.assertTrue(sidecar.is_file())
        self.assertEqual(sidecar.read_bytes(), b"<x:xmpmeta/>")
        self.assertEqual(result["sidecar_name"], "IMG_0004.xmp")
        self.assertIsNone(result["backed_up"])
        self.assertNotIn(str(self.photos), self.payload(result))

    def test_the_original_file_is_never_touched(self):
        before = self.original.read_bytes()
        stamp = self.original.stat().st_mtime_ns
        self.lib.write_xmp(key=self.key, content=b"<x:xmpmeta/>")
        self.assertEqual(self.original.read_bytes(), before)
        self.assertEqual(self.original.stat().st_mtime_ns, stamp)

    def test_an_existing_sidecar_is_refused_by_default(self):
        (self.photos / "IMG_0004.xmp").write_bytes(b"existing edits")
        with self.assertRaises(FolderConflict) as caught:
            self.lib.write_xmp(key=self.key, content=b"<x:xmpmeta/>")
        self.assertEqual((self.photos / "IMG_0004.xmp").read_bytes(), b"existing edits")
        self.assertEqual(caught.exception.detail.get("sidecar"), "IMG_0004.xmp")

    def test_overwriting_backs_the_previous_sidecar_up_first(self):
        (self.photos / "IMG_0004.xmp").write_bytes(b"existing edits")
        result = self.lib.write_xmp(key=self.key, content=b"<x:xmpmeta/>",
                                    overwrite=True)
        self.assertEqual((self.photos / "IMG_0004.xmp").read_bytes(), b"<x:xmpmeta/>")
        self.assertTrue(result["backed_up"].startswith("IMG_0004.xmp.bak-"))
        backup = self.photos / result["backed_up"]
        self.assertEqual(backup.read_bytes(), b"existing edits")

    def test_two_overwrites_do_not_clobber_the_first_backup(self):
        (self.photos / "IMG_0004.xmp").write_bytes(b"first")
        one = self.lib.write_xmp(key=self.key, content=b"<a/>", overwrite=True)
        (self.photos / "IMG_0004.xmp").write_bytes(b"second")
        two = self.lib.write_xmp(key=self.key, content=b"<b/>", overwrite=True)
        self.assertNotEqual(one["backed_up"], two["backed_up"])
        self.assertEqual((self.photos / one["backed_up"]).read_bytes(), b"first")
        self.assertEqual((self.photos / two["backed_up"]).read_bytes(), b"second")

    def test_writing_refuses_once_the_root_is_removed(self):
        self.use_config(roots=[])
        with self.assertRaises(FolderError):
            self.lib.write_xmp(key=self.key, content=b"<x/>")
        self.assertFalse((self.photos / "IMG_0004.xmp").exists())

    def test_unknown_key_and_empty_content_are_refused(self):
        with self.assertRaises(FolderNotFound):
            self.lib.write_xmp(key="0" * 20, content=b"<x/>")
        with self.assertRaises(FolderError):
            self.lib.write_xmp(key=self.key, content=b"")
        with self.assertRaises(FolderError):
            self.lib.write_xmp(key=self.key, content="")

    def test_write_leaves_no_partial_file_behind(self):
        self.lib.write_xmp(key=self.key, content=b"<x:xmpmeta/>" * 100)
        leftovers = [p.name for p in self.photos.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
