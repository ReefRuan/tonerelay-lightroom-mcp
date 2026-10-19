"""Read-only current-Lightroom-selection and folder-page contract.

The Lightroom plugin owns the volatile UI query: it identifies its current
target photo, determines that photo's leaf folder, sorts the folder's catalog
photos, and returns one page.  This module deliberately does *not* derive a
folder from a filesystem path or try to re-page the result locally.  Its job is
to validate that one bridge response, remove non-public fields, and hand a
stable, path-free page to protocol adapters.

``SelectionTools`` has no knowledge of MCP or image bytes.  A caller that
wants a gallery can invoke :meth:`current_folder`, then obtain a read-only
thumbnail for each returned UUID through the existing fast-preview path.
Keeping those concerns separate means the gallery never needs an export
session, and avoids the lr-ops batch output ambiguity (one batch job currently
has one file handle).
"""

from __future__ import annotations

import re
from typing import Callable


DEFAULT_LIMIT = 10
MAX_LIMIT = 20

# These are intentionally narrower than CatalogTools' list fields.  A folder
# gallery is a presentation-oriented page; rating is not needed to identify or
# render a thumbnail, and raw file paths are never part of this contract.
PHOTO_FIELDS = (
    "uuid",
    "file_name",
    "copy_name",
    "is_virtual_copy",
    "width",
    "height",
)

# Lightroom computes this from its normalized Catalog/folder data.  The
# Runtime only accepts the opaque digest-shaped identifier, so a plugin bug
# cannot turn a public folder field into an absolute path leak.
FOLDER_ID_PATTERN = re.compile(r"\Afolder-(?:md5|hash)-v1:[0-9a-f]+\Z")


class SelectionTools:
    """Validate and expose one page of the current target's leaf folder.

    ``runner`` follows the same small abstraction as :class:`CatalogTools`:
    ``runner(command, params)`` supplies target/catalog envelope fields at the
    Runtime boundary.  This class only sends the read-only command name and
    its pagination arguments.

    The plugin's success shape is::

        {
          "target": {<public photo fields>},
          "folder": {"id": "folder-md5-v1:<digest>", "label": "leaf"},
          "count": 10, "total": 100, "limit": 10, "offset": 0,
          "has_more": true, "photos": [{<public photo fields>}, ...]
        }

    With no Lightroom target, the plugin omits ``target`` and ``folder`` and
    returns an otherwise valid empty page.  The normalized Python result makes
    those two fields explicit ``None`` values so adapters can present an
    actionable empty-selection state without treating it as a transport error.
    """

    def __init__(self, runner: Callable):
        self.runner = runner

    def current_folder(self, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
        """Return one validated, path-free page for Lightroom's current target.

        The page is not cached here: current selection is UI state, and a
        future gallery caller can independently reuse immutable per-photo
        thumbnail cache entries after this method has supplied the UUIDs.
        """
        self._validate_request(limit, offset)
        result = self.runner("get_current_folder", {"limit": limit, "offset": offset})
        return self._page(result, limit=limit, offset=offset)

    @staticmethod
    def _validate_request(limit: int, offset: int) -> None:
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")

    @classmethod
    def _page(cls, result, *, limit: int, offset: int) -> dict:
        if not isinstance(result, dict):
            raise ValueError("Lightroom current-folder result must be an object")

        required = ("count", "total", "limit", "offset", "has_more", "photos")
        missing = [key for key in required if key not in result]
        if missing:
            raise ValueError(
                "Lightroom current-folder result is missing " + " and ".join(missing))

        photos = result["photos"]
        # The repository's Lua JSON encoder represents an empty Lua array as
        # {}, so normalize only that established wire quirk.  A non-empty
        # object must still be rejected instead of silently becoming a list.
        if photos == {}:
            photos = []
        if not isinstance(photos, list):
            raise ValueError("Lightroom current-folder result field photos must be an array")

        for key in ("count", "total", "limit", "offset"):
            if type(result[key]) is not int:
                raise ValueError(
                    f"Lightroom current-folder result field {key} must be an integer")
        if type(result["has_more"]) is not bool:
            raise ValueError(
                "Lightroom current-folder result field has_more must be a boolean")

        count = result["count"]
        total = result["total"]
        echoed_limit = result["limit"]
        echoed_offset = result["offset"]
        has_more = result["has_more"]
        if count != len(photos):
            raise ValueError(
                "Lightroom current-folder result count does not match photos length")
        if total < count or total < 0:
            raise ValueError("Lightroom current-folder result total is invalid")
        if echoed_limit != limit or echoed_offset != offset:
            raise ValueError(
                "Lightroom current-folder result pagination does not echo the request")
        expected_count = min(limit, max(0, total - offset))
        if count != expected_count:
            raise ValueError(
                "Lightroom current-folder result count does not match the page size")
        if has_more != (offset + count < total):
            raise ValueError(
                "Lightroom current-folder result has_more is inconsistent with total")

        target = result.get("target")
        folder = result.get("folder")
        if target is None:
            if folder is not None:
                raise ValueError("Lightroom returned a folder without a current target")
            if total != 0 or count != 0 or photos or has_more:
                raise ValueError(
                    "Lightroom returned current-folder photos without a current target")
            return {
                "target": None,
                "folder": None,
                "count": count,
                "total": total,
                "limit": echoed_limit,
                "offset": echoed_offset,
                "has_more": has_more,
                "photos": [],
            }

        if folder is None:
            raise ValueError("Lightroom returned a current target without a folder")
        return {
            "target": cls._photo(target, "target"),
            "folder": cls._folder(folder),
            "count": count,
            "total": total,
            "limit": echoed_limit,
            "offset": echoed_offset,
            "has_more": has_more,
            "photos": [cls._photo(photo, "photo") for photo in photos],
        }

    @staticmethod
    def _photo(photo, label: str) -> dict:
        if not isinstance(photo, dict):
            raise ValueError(f"Lightroom current-folder {label} must be an object")
        missing = [key for key in PHOTO_FIELDS if key not in photo]
        if missing:
            raise ValueError(
                f"Lightroom current-folder {label} is missing {' and '.join(missing)}")
        clean = {key: photo[key] for key in PHOTO_FIELDS}
        if not isinstance(clean["uuid"], str) or not clean["uuid"]:
            raise ValueError(f"Lightroom current-folder {label} uuid must be a non-empty string")
        if not isinstance(clean["file_name"], str):
            raise ValueError(f"Lightroom current-folder {label} file_name must be a string")
        if clean["copy_name"] is not None and not isinstance(clean["copy_name"], str):
            raise ValueError(
                f"Lightroom current-folder {label} copy_name must be a string or null")
        if type(clean["is_virtual_copy"]) is not bool:
            raise ValueError(
                f"Lightroom current-folder {label} is_virtual_copy must be a boolean")
        for key in ("width", "height"):
            if type(clean[key]) is not int or clean[key] < 0:
                raise ValueError(
                    f"Lightroom current-folder {label} {key} must be a non-negative integer")
        return clean

    @staticmethod
    def _folder(folder) -> dict:
        if not isinstance(folder, dict):
            raise ValueError("Lightroom current-folder folder must be an object")
        folder_id = folder.get("id")
        label = folder.get("label")
        if not isinstance(folder_id, str) or not FOLDER_ID_PATTERN.fullmatch(folder_id):
            raise ValueError("Lightroom current-folder folder id must be an opaque folder digest")
        if (not isinstance(label, str) or not label
                or "\x00" in label or "/" in label or "\\" in label):
            raise ValueError(
                "Lightroom current-folder folder label must be a non-empty leaf name")
        # Rebuild, rather than copy, so accidental path-like plugin metadata
        # cannot cross the Runtime/MCP boundary as a future extra field.
        return {"id": folder_id, "label": label}
