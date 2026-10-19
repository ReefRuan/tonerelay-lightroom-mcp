"""Immutable, identity-scoped storage for fast photo previews.

One published preview is three files under ``<root>/photo-previews``:

``blobs/<key>.jpg``
    The published bytes. ``key`` hashes the source identity *and* the content,
    so two different photos that happen to produce identical JPEG bytes still
    get their own file and their own metadata.

``manifests/<key>.json``
    Immutable facts about that artifact: hash, byte count, pixel size, the
    source identity and the Lightroom method that produced it. Readers trust a
    blob only while it still matches its manifest.

``requests/<request>.json``
    A pointer from "this source at this requested size" to ``key``. Small and
    per-request, so two workbench processes publishing different requests never
    rewrite each other's pointer -- the thing one shared ``index.json`` could
    not do without cross-process locking. Each file is replaced atomically, and
    the pointer is written last, so a visible pointer always resolves.

Nothing here holds a lock: every file is immutable or written whole via
``os.replace``. That is what makes separate protocol-adapter processes safe to
publish concurrently.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

from PIL import Image, ImageCms

MANIFEST_VERSION = "photo-preview-v1"
MAX_BYTES = 64 * 1024 * 1024
KEY_PATTERN = re.compile(r"\A(?:[0-9a-f]{24}|[0-9a-f]{64})\Z")


def _canonical(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _write_atomic(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path`` in one visible step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def inspect_jpeg(content: bytes) -> dict:
    """Return describable facts about ``content``, or raise ``ValueError``.

    A preview is only ever published from bytes that decode end to end as a
    JPEG. ``verify`` catches a lying header, the second ``load`` pass decodes
    every scan, and the format check rejects a PNG that was merely renamed
    ``.jpg`` -- a file Lightroom never produced but a cache directory could
    still be handed.
    """
    if not content:
        raise ValueError("photo preview is empty")
    if len(content) > MAX_BYTES:
        raise ValueError("photo preview exceeds 64 MiB")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image_format = image.format
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            actual_size = [image.width, image.height]
            icc_bytes = image.info.get("icc_profile")
            exif_color_space_code = image.getexif().get(0xA001)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("photo preview is not a complete JPEG") from exc
    if image_format != "JPEG":
        raise ValueError(f"photo preview must be JPEG, not {image_format or 'unknown'}")
    icc_name = None
    icc_status = "absent"
    if icc_bytes:
        try:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
            icc_name = ImageCms.getProfileName(profile).strip() or None
            icc_status = "readable"
        except (OSError, ValueError, ImageCms.PyCMSError):
            icc_status = "invalid"
    return {
        "actual_size": actual_size,
        # A profile name or EXIF declaration is source metadata, not proof that
        # Lightroom's cached thumbnail matches a controlled sRGB export.
        "color_space": "unknown",
        "icc_profile": {
            "status": icc_status,
            "name": icc_name,
            "sha256": hashlib.sha256(icc_bytes).hexdigest() if icc_bytes else None,
        },
        "exif_color_space_code": exif_color_space_code,
    }


class PreviewStore:
    """Publishes fast previews and resolves them back to readable files."""

    def __init__(self, root: Path):
        self.root = Path(root) / "photo-previews"
        self.blobs = self.root / "blobs"
        self.manifests = self.root / "manifests"
        self.requests = self.root / "requests"

    # ---- identities --------------------------------------------------------

    @staticmethod
    def request_id(ref: dict, size: int) -> str:
        """The one request this identity describes: this source, this size."""
        return hashlib.sha256(_canonical({**ref, "size": size})).hexdigest()[:24]

    @staticmethod
    def artifact_key(ref: dict, content: bytes) -> str:
        """Identify published bytes by their source identity and their content.

        Folding the source in is what keeps two catalogs -- or one catalog
        holding the same photo twice -- from sharing a URL whose metadata
        describes the other one.
        """
        digest = hashlib.sha256()
        digest.update(_canonical(ref))
        digest.update(b"\x00")
        digest.update(content)
        return digest.hexdigest()[:24]

    # ---- reads -------------------------------------------------------------

    def current(self, ref: dict, size: int):
        """The published artifact for this request: ``(key, path, manifest)``.

        ``None`` means "nothing usable is published": either no request was
        ever answered, or the artifact behind it no longer matches its
        manifest. Both cases let the caller re-render and repair.
        """
        pointer = self._read_json(self._request_path(ref, size))
        if pointer is None:
            return None
        key = pointer.get("key")
        if not isinstance(key, str):
            return None
        try:
            path, manifest = self._verified(key)
        except (OSError, ValueError):
            return None
        return key, path, manifest

    def read(self, key: str) -> Path:
        """The blob for ``key``, re-verified against its manifest."""
        return self._verified(key)[0]

    def _verified(self, key: str):
        if not isinstance(key, str) or not KEY_PATTERN.match(key):
            raise ValueError("invalid photo preview key")
        manifest = self._read_json(self.manifests / f"{key}.json")
        if manifest is None or manifest.get("key") != key:
            # No manifest means these bytes were never published by this
            # version; an old file is left in place rather than served.
            raise FileNotFoundError(key)
        path = self.blobs / f"{key}.jpg"
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest.get("sha256"):
            raise ValueError("photo preview hash mismatch")
        if len(content) != manifest.get("bytes"):
            raise ValueError("photo preview size mismatch")
        # Derive colour declarations from the verified bytes even for previews
        # published before these fields existed in the immutable manifest.
        inspected = inspect_jpeg(content)
        manifest = {**manifest, **inspected}
        return path, manifest

    # ---- writes ------------------------------------------------------------

    def publish(self, content: bytes, ref: dict, size: int,
                *, method: str = "requestJpegThumbnail",
                orientation: str | None = None) -> dict:
        """Publish ``content`` for ``ref`` and return its manifest.

        The bytes are validated first, so a bad payload leaves the store
        untouched. The request pointer is written last, so anything a reader
        can find is already complete. Re-publishing identical bytes under the
        same identity is a no-op that also repairs a damaged blob or manifest.
        """
        info = inspect_jpeg(content)
        key = self.artifact_key(ref, content)
        manifest = {
            "schema_version": MANIFEST_VERSION,
            "key": key,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "source": ref,
            "size": size,
            "method": method,
            "orientation": orientation,
            "actual_size": info["actual_size"],
            "color_space": info["color_space"],
            "icc_profile": info["icc_profile"],
            "exif_color_space_code": info["exif_color_space_code"],
        }
        self._store_blob(key, content, manifest["sha256"])
        self._store_manifest(key, manifest)
        _write_atomic(self._request_path(ref, size),
                      _canonical({"request": self.request_id(ref, size), "key": key}))
        return manifest

    def _store_blob(self, key: str, content: bytes, sha256: str) -> None:
        path = self.blobs / f"{key}.jpg"
        if _file_sha256(path) == sha256:
            return
        _write_atomic(path, content)

    def _store_manifest(self, key: str, manifest: dict) -> None:
        path = self.manifests / f"{key}.json"
        existing = self._read_json(path)
        if existing is not None and existing.get("sha256") == manifest["sha256"]:
            return
        _write_atomic(path, _canonical(manifest))

    def _request_path(self, ref: dict, size: int) -> Path:
        return self.requests / f"{self.request_id(ref, size)}.json"

    @staticmethod
    def _read_json(path: Path):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None
