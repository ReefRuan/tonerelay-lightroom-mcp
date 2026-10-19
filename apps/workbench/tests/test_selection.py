"""Unit contract for the current-Lightroom-folder query seam.

These tests intentionally stop before MCP image blocks.  The selection page is
volatile Lightroom state; the future gallery adapter combines this validated
page with the established per-photo fast-preview cache.
"""

from __future__ import annotations

import copy
import unittest

from workbench.selection import DEFAULT_LIMIT, MAX_LIMIT, SelectionTools


def photo(uuid="photo-1", file_name="IMG_0001.RAF", **extra):
    value = {
        "uuid": uuid,
        "file_name": file_name,
        "copy_name": "",
        "is_virtual_copy": False,
        "width": 6000,
        "height": 4000,
    }
    value.update(extra)
    return value


def page(*, limit=10, offset=0, photos=None, total=None, target=None, folder=None,
         has_more=None, count=None):
    photos = [photo()] if photos is None else photos
    total = offset + len(photos) if total is None else total
    target = photo("target-1", "TARGET.RAF") if target is None else target
    folder = {"id": "folder-md5-v1:0123456789abcdef", "label": "2026-trip"} \
        if folder is None else folder
    count = len(photos) if count is None else count
    has_more = offset + count < total if has_more is None else has_more
    return {
        "target": target,
        "folder": folder,
        "count": count,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "photos": photos,
    }


class Runner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, command, params):
        self.calls.append((command, copy.deepcopy(params)))
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)


class SelectionToolsTests(unittest.TestCase):
    def test_current_folder_sends_exact_read_only_page_query_and_sanitizes(self):
        raw_target = photo("target", "TARGET.RAF", path="/private/target.RAF")
        raw_photo = photo("item", "IMG_0002.RAF", rating=5, path="/private/item.RAF")
        raw_folder = {
            "id": "folder-md5-v1:0123456789abcdef",
            "label": "2026-trip",
            "path": "/private/photos/2026-trip",
        }
        runner = Runner(page(limit=3, offset=2, total=12, target=raw_target,
                             folder=raw_folder, photos=[raw_photo,
                                                         photo("item-2", "IMG_0003.RAF"),
                                                         photo("item-3", "IMG_0004.RAF")]))

        result = SelectionTools(runner).current_folder(limit=3, offset=2)

        self.assertEqual(runner.calls, [("get_current_folder", {"limit": 3, "offset": 2})])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["total"], 12)
        self.assertTrue(result["has_more"])
        self.assertEqual(set(result["target"]),
                         {"uuid", "file_name", "copy_name", "is_virtual_copy",
                          "width", "height"})
        self.assertEqual(set(result["photos"][0]), set(result["target"]))
        self.assertEqual(result["folder"],
                         {"id": "folder-md5-v1:0123456789abcdef", "label": "2026-trip"})
        self.assertNotIn("path", str(result))
        self.assertNotIn("rating", result["photos"][0])

    def test_default_page_arguments_are_bounded_and_forwarded(self):
        runner = Runner(page(limit=DEFAULT_LIMIT, offset=0,
                             photos=[photo()], total=1, has_more=False))
        result = SelectionTools(runner).current_folder()
        self.assertEqual(result["limit"], DEFAULT_LIMIT)
        self.assertEqual(runner.calls,
                         [("get_current_folder", {"limit": DEFAULT_LIMIT, "offset": 0})])

    def test_no_target_is_an_actionable_empty_page_not_a_malformed_reply(self):
        runner = Runner({
            "count": 0,
            "total": 0,
            "limit": 10,
            "offset": 0,
            "has_more": False,
            # Lua's JSON encoder represents empty tables as objects.
            "photos": {},
        })
        result = SelectionTools(runner).current_folder()
        self.assertEqual(result, {
            "target": None,
            "folder": None,
            "count": 0,
            "total": 0,
            "limit": 10,
            "offset": 0,
            "has_more": False,
            "photos": [],
        })

    def test_invalid_requests_do_not_call_lightroom(self):
        runner = Runner(page())
        bad = [
            {"limit": True},
            {"limit": 0},
            {"limit": MAX_LIMIT + 1},
            {"limit": 1.5},
            {"offset": True},
            {"offset": -1},
            {"offset": 1.5},
        ]
        tools = SelectionTools(runner)
        for arguments in bad:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    tools.current_folder(**arguments)
        self.assertEqual(runner.calls, [])

    def test_rejects_invalid_pagination_before_sanitizing(self):
        cases = [
            page(limit=9),
            page(offset=1),
            page(total=-1, photos=[]),
            page(count=2, photos=[photo()]),
            page(total=3, photos=[photo()], has_more=False),
            page(total=3, photos=[], offset=0),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                runner = Runner(raw)
                with self.assertRaises(ValueError):
                    SelectionTools(runner).current_folder(limit=10, offset=0)

    def test_rejects_target_and_folder_boundary_violations(self):
        no_target_with_photos = page(total=1, target=None, folder=None)
        no_target_with_photos.pop("target")
        no_target_with_photos.pop("folder")
        cases = [
            page(folder={"id": "/private/name/photos", "label": "photos"}),
            page(folder={"id": "folder-md5-v1:abc", "label": "parent/leaf"}),
            page(folder={"id": "folder-md5-v1:abc", "label": ""}),
            page(target={"uuid": "target"}),
            {**page(), "target": None},
            no_target_with_photos,
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    SelectionTools(Runner(raw)).current_folder()

    def test_photo_contract_has_no_rating_requirement_but_rejects_bad_public_fields(self):
        accepted = page(photos=[photo("p", "P.RAF", rating=4)], total=1,
                        has_more=False)
        result = SelectionTools(Runner(accepted)).current_folder()
        self.assertNotIn("rating", result["photos"][0])

        bad_photos = [
            {key: value for key, value in photo().items() if key != "uuid"},
            photo(uuid=""),
            photo(width=True),
            photo(height=-1),
            photo(is_virtual_copy="false"),
            photo(copy_name=5),
        ]
        for bad_photo in bad_photos:
            with self.subTest(bad_photo=bad_photo):
                raw = page(photos=[bad_photo], total=1, has_more=False)
                with self.assertRaises(ValueError):
                    SelectionTools(Runner(raw)).current_folder()

    def test_runner_errors_are_not_rewritten(self):
        runner = Runner(RuntimeError("bridge down"))
        with self.assertRaisesRegex(RuntimeError, "bridge down"):
            SelectionTools(runner).current_folder()


if __name__ == "__main__":
    unittest.main()
