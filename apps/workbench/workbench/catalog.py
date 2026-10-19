"""Read-only paginated catalog photo queries for Runtime adapters.

The plugin owns filtering, sorting and pagination: it runs ``search_photos``
against the active authorized Catalog and answers with exactly one page. The Workbench
side must not filter or slice that page itself -- with a small ``limit`` and a
non-zero ``offset`` it never sees the matches that live outside the page, so a
local filter would silently drop them and a local page would double-paginate.

Arguments are validated here, before the runner (and therefore before any
Lightroom call) is touched.
"""
from __future__ import annotations

from typing import Callable

MAX_LIMIT = 2000
MAX_NAME_CONTAINS = 256
PHOTO_FIELDS = ("uuid", "file_name", "copy_name", "is_virtual_copy",
                "width", "height", "rating")


class CatalogTools:
    def __init__(self, runner: Callable):
        self.runner = runner

    def search(self, limit=100, offset=0, name_contains="", rating=None) -> dict:
        """Ask Lightroom for one page of photos matching the filters.

        ``rating=0`` keeps unrated photos (the plugin's filter semantics);
        ``rating=None`` means no rating filter at all. ``name_contains=None``
        is the same as an empty string here.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if name_contains is None:
            name_contains = ""
        if not isinstance(name_contains, str):
            raise ValueError("name_contains must be a string or null")
        if len(name_contains) > MAX_NAME_CONTAINS:
            raise ValueError(
                f"name_contains must be at most {MAX_NAME_CONTAINS} characters")
        if rating is not None and (type(rating) is not int or not 0 <= rating <= 5):
            raise ValueError("rating must be an integer from 0 to 5")

        params = {"limit": limit, "offset": offset, "name_contains": name_contains}
        if rating is not None:
            params["rating"] = rating
        return self._page(self.runner("search_photos", params),
                          limit=limit, offset=offset)

    @staticmethod
    def _page(result, limit: int, offset: int) -> dict:
        if not isinstance(result, dict):
            raise ValueError("Lightroom photo search result must be an object")
        photos = result.get("photos")
        # The plugin's legacy JSON encoder represents an empty Lua array as {}.
        # Only that one shape is normalized; anything else must be a real array.
        if photos == {}:
            photos = []
        if not isinstance(photos, list):
            raise ValueError("Lightroom photo search result field photos must be an array")

        required = ("count", "total", "limit", "offset", "has_more")
        missing = [key for key in required if key not in result]
        if missing:
            raise ValueError(
                f"Lightroom photo search result is missing {' and '.join(missing)}")
        for key in ("count", "total", "limit", "offset"):
            if type(result[key]) is not int:
                raise ValueError(
                    f"Lightroom photo search result field {key} must be an integer")
        if type(result["has_more"]) is not bool:
            raise ValueError(
                "Lightroom photo search result field has_more must be a boolean")

        count, total = result["count"], result["total"]
        out_limit, out_offset = result["limit"], result["offset"]
        if count != len(photos):
            raise ValueError(
                "Lightroom photo search result count does not match photos length")
        if total < count:
            raise ValueError(
                "Lightroom photo search result total is smaller than count")
        if out_limit != limit or out_offset != offset:
            raise ValueError(
                "Lightroom photo search result pagination does not echo the request")
        # The page must hold exactly the rows the request window allows: the
        # limit, capped by what remains after the offset. A short non-empty
        # page before the end, or photos reported past the end, would mean the
        # plugin dropped or invented rows.
        expected = min(out_limit, max(0, total - out_offset))
        if count != expected:
            raise ValueError(
                "Lightroom photo search result count does not match the page size")
        if result["has_more"] != (out_offset + count < total):
            raise ValueError(
                "Lightroom photo search result has_more is inconsistent with total")

        return {
            "count": count,
            "total": total,
            "limit": out_limit,
            "offset": out_offset,
            "has_more": result["has_more"],
            "photos": [CatalogTools._photo(photo) for photo in photos],
        }

    @staticmethod
    def _photo(photo) -> dict:
        if not isinstance(photo, dict):
            raise ValueError("Lightroom photo entry must be an object")
        missing = [key for key in PHOTO_FIELDS if key not in photo]
        if missing:
            raise ValueError(
                f"Lightroom photo entry is missing {' and '.join(missing)}")
        # Rebuild with exactly the contract fields: a raw path or develop
        # snapshot must never ride along to external clients.
        clean = {key: photo[key] for key in PHOTO_FIELDS}
        if not isinstance(clean["uuid"], str) or not clean["uuid"]:
            raise ValueError("Lightroom photo uuid must be a non-empty string")
        if not isinstance(clean["file_name"], str):
            raise ValueError("Lightroom photo file_name must be a string")
        if clean["copy_name"] is not None and not isinstance(clean["copy_name"], str):
            raise ValueError("Lightroom photo copy_name must be a string or null")
        if type(clean["is_virtual_copy"]) is not bool:
            raise ValueError("Lightroom photo is_virtual_copy must be a boolean")
        for key in ("width", "height"):
            if type(clean[key]) is not int or clean[key] < 0:
                raise ValueError(
                    f"Lightroom photo {key} must be a non-negative integer")
        if type(clean["rating"]) is not int or not 0 <= clean["rating"] <= 5:
            raise ValueError("Lightroom photo rating must be an integer from 0 to 5")
        return clean
