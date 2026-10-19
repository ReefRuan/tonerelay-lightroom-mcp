"""Catalog search tests: argument validation and result normalization.

The fake bridge answers ``search_photos`` the way the plugin will: it filters,
sorts and paginates, and echoes the requested limit/offset. Tests therefore
never let Python do the filtering -- anything that fetches a small page and
then filters it locally would drop matches behind the page window.
"""

import copy
import unittest

from workbench.catalog import CatalogTools
from workbench.lrops import LrOpsError


def photo(uuid, file_name, rating=0, copy_name="", virtual=False, **extra):
    return {"uuid": uuid, "file_name": file_name, "copy_name": copy_name,
            "is_virtual_copy": virtual, "width": 6000, "height": 4000,
            "rating": rating, **extra}


def page(limit, offset, total, photos):
    return {"count": len(photos), "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(photos) < total, "photos": photos}


PAGE = page(2, 1, 5, [photo("u3", "DSC_0003.NEF", rating=4),
                      photo("u4", "DSC_0004.NEF", rating=5,
                            copy_name="edit", virtual=True)])


class CatalogToolsTests(unittest.TestCase):
    def recording(self, result=PAGE):
        calls = []

        def runner(command, params):
            calls.append((command, copy.deepcopy(params)))
            return copy.deepcopy(result)

        return calls, CatalogTools(runner)

    def test_each_search_calls_the_runner_once_with_validated_params(self):
        empty = page(100, 0, 0, [])
        calls, tools = self.recording(empty)
        self.assertEqual(tools.search(), empty)
        self.assertEqual(calls, [("search_photos", {
            "limit": 100, "offset": 0, "name_contains": ""})])

        calls, tools = self.recording(PAGE)
        self.assertEqual(tools.search(limit=2, offset=1,
                                      name_contains="dsc", rating=0), PAGE)
        self.assertEqual(calls, [("search_photos", {
            "limit": 2, "offset": 1, "name_contains": "dsc", "rating": 0})])

        # None means "no filter", and an omitted rating must not be invented.
        calls, tools = self.recording(empty)
        tools.search(name_contains=None)
        self.assertEqual(calls, [("search_photos", {
            "limit": 100, "offset": 0, "name_contains": ""})])

        # Boundaries are inclusive.
        calls, tools = self.recording(page(1, 0, 0, []))
        tools.search(limit=1, rating=0)
        self.assertEqual(calls[0][1]["limit"], 1)
        calls, tools = self.recording(page(2000, 0, 0, []))
        tools.search(limit=2000, offset=0, rating=5, name_contains="x" * 256)
        self.assertEqual(calls[0][1]["limit"], 2000)

    def test_invalid_arguments_fail_before_the_runner(self):
        calls, tools = self.recording(page(100, 0, 0, []))
        bad_calls = [
            lambda: tools.search(limit=True),
            lambda: tools.search(limit=0),
            lambda: tools.search(limit=2001),
            lambda: tools.search(limit=1.5),
            lambda: tools.search(limit="100"),
            lambda: tools.search(limit=None),
            lambda: tools.search(offset=True),
            lambda: tools.search(offset=-1),
            lambda: tools.search(offset=1.5),
            lambda: tools.search(offset="0"),
            lambda: tools.search(offset=None),
            lambda: tools.search(name_contains=3),
            lambda: tools.search(name_contains=["a"]),
            lambda: tools.search(name_contains="x" * 257),
            lambda: tools.search(rating=True),
            lambda: tools.search(rating=-1),
            lambda: tools.search(rating=6),
            lambda: tools.search(rating=1.5),
            lambda: tools.search(rating="5"),
            lambda: tools.search(rating={}),
        ]
        for call in bad_calls:
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(calls, [])
        # Validation failures do not poison later valid searches.
        tools.search()
        self.assertEqual(len(calls), 1)

    def test_legacy_empty_arrays_are_normalized_but_other_shapes_error(self):
        calls, tools = self.recording(
            {"count": 0, "total": 0, "limit": 100, "offset": 0,
             "has_more": False, "photos": {}})
        self.assertEqual(tools.search()["photos"], [])

        malformed = [
            [],
            "bad",
            {"count": 0, "total": 0, "limit": 100, "offset": 0, "has_more": False},
            {"photos": {}},
            {"count": 0, "total": 0, "limit": 100, "offset": 0, "has_more": False,
             "photos": {"0": photo("u1", "a.jpg")}},
            {"count": 0, "total": 0, "limit": 100, "offset": 0, "has_more": False,
             "photos": "bad"},
            {"count": 0, "total": 0, "limit": 100, "offset": 0, "has_more": False,
             "photos": [3]},
        ]
        for result in malformed:
            _, tools = self.recording(result)
            with self.assertRaises(ValueError):
                tools.search()

    def test_pagination_fields_are_checked_for_consistency(self):
        base = {"count": 2, "total": 5, "limit": 2, "offset": 1, "has_more": True,
                "photos": PAGE["photos"]}
        inconsistent = [
            {**base, "count": 1},                                # count != len
            {**base, "count": True},                             # bool is not int
            {**base, "total": 2.0},
            {**base, "total": 1},                                # total < count
            {**base, "limit": 50},                               # no echo of request
            {**base, "offset": 0},
            {**base, "has_more": 1},                             # bool is not int
            {**base, "has_more": False},                         # inconsistent
            {k: v for k, v in base.items() if k != "total"},
            {k: v for k, v in base.items() if k != "has_more"},
            # An empty page before the filtered end is always malformed.
            {**base, "count": 0, "photos": [], "has_more": True},
        ]
        for result in inconsistent:
            _, tools = self.recording(result)
            with self.assertRaises(ValueError):
                tools.search(limit=2, offset=1)

    def test_short_nonempty_page_before_the_end_is_rejected(self):
        # One row returned where the window has room for two is a dropped row,
        # not a page -- even though has_more is self-consistent.
        _, tools = self.recording(page(2, 1, 5, [photo("u3", "DSC_0003.NEF")]))
        with self.assertRaises(ValueError):
            tools.search(limit=2, offset=1)
        # A short page is legal only when it is the final page.
        final = page(3, 3, 5, [photo("u4", "DSC_0004.NEF"),
                               photo("u5", "DSC_0005.NEF")])
        _, tools = self.recording(final)
        payload = tools.search(limit=3, offset=3)
        self.assertEqual(payload["count"], 2)
        self.assertFalse(payload["has_more"])

    def test_pages_over_the_end_of_the_filtered_set_are_rejected(self):
        # Only one row remains after offset 4, so a two-row page claims rows that
        # do not exist.
        _, tools = self.recording(page(2, 4, 5, [photo("u5", "DSC_0005.NEF"),
                                                 photo("u6", "DSC_0006.NEF")]))
        with self.assertRaises(ValueError):
            tools.search(limit=2, offset=4)
        # An offset past the end admits only an empty page.
        _, tools = self.recording(page(100, 7, 5, [photo("u8", "DSC_0008.NEF")]))
        with self.assertRaises(ValueError):
            tools.search(limit=100, offset=7)
        _, tools = self.recording(page(100, 7, 5, []))
        payload = tools.search(limit=100, offset=7)
        self.assertEqual(payload["count"], 0)
        self.assertFalse(payload["has_more"])

    def test_photos_are_validated_and_stripped_to_the_contract_fields(self):
        leaked = photo("u1", "a.jpg", rating=3,
                       path="/private/catalog/a.jpg", develop={"Exposure2012": 1.0})
        result = {**page(100, 0, 1, [leaked]), "catalog_path": "/private/catalog"}
        _, tools = self.recording(result)
        payload = tools.search()
        self.assertEqual(set(payload), {"count", "total", "limit", "offset",
                                        "has_more", "photos"})
        self.assertEqual(set(payload["photos"][0]), {
            "uuid", "file_name", "copy_name", "is_virtual_copy",
            "width", "height", "rating"})
        self.assertNotIn("path", payload["photos"][0])
        self.assertNotIn("develop", payload["photos"][0])

        _, tools = self.recording(page(100, 0, 1, [
            photo("u1", "a.jpg", copy_name=None)]))
        self.assertIsNone(tools.search()["photos"][0]["copy_name"])

        bad_photos = [
            photo(None, "a.jpg"),
            photo("", "a.jpg"),
            photo("u1", 3),
            photo("u1", "a.jpg", copy_name=3),
            photo("u1", "a.jpg", virtual=1),
            photo("u1", "a.jpg", width=True),
            photo("u1", "a.jpg", height=-1),
            photo("u1", "a.jpg", rating=6),
            photo("u1", "a.jpg", rating=True),
            {k: v for k, v in photo("u1", "a.jpg").items() if k != "rating"},
        ]
        for bad in bad_photos:
            _, tools = self.recording(page(100, 0, 1, [bad]))
            with self.assertRaises(ValueError):
                tools.search()

    def test_runner_errors_pass_through_once(self):
        calls = []

        def runner(command, params):
            calls.append(command)
            raise LrOpsError("sdk exploded")

        with self.assertRaises(LrOpsError):
            CatalogTools(runner).search()
        self.assertEqual(calls, ["search_photos"])


if __name__ == "__main__":
    unittest.main()
