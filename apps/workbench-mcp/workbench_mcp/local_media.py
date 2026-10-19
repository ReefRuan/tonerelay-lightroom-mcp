"""Default local references to the exact native image bytes already returned.

This adapter only supplies references. The agent and client own reply rendering.
Clients without shared file access can disable references and keep native media.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path


_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/webp": ".webp", "image/gif": ".gif"}


def _publish(root: Path, content: bytes, suffix: str) -> tuple[Path, str]:
    digest = hashlib.sha256(content).hexdigest()
    root = root.absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"{digest}{suffix}"
    if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
        return path, digest
    descriptor, temporary = tempfile.mkstemp(prefix=".image-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path, digest


def add_local_image_references(result: dict, root: Path) -> dict:
    """Precede each native image with its reference, without changing the image.

    Reference failure must not turn a completed editing operation into a tool
    failure that the agent might retry. Native media stays available in all cases.
    """
    blocks = []
    index = 0
    for block in result.get("content", []):
        if block.get("type") == "image":
            try:
                mime_type = block["mimeType"]
                suffix = _EXTENSIONS[mime_type]
                content = base64.b64decode(block["data"], validate=True)
                if not content:
                    raise ValueError("empty image")
                path, digest = _publish(root, content, suffix)
                display = {"kind": "local_file", "path": str(path),
                           "mime_type": mime_type, "sha256": digest,
                           "image_index": index}
            except (OSError, ValueError, TypeError, KeyError):
                display = {"kind": "unavailable", "image_index": index,
                           "reason": "local_image_reference_unavailable"}
            blocks.append({"type": "text", "text": json.dumps(
                {"display": display}, ensure_ascii=False)})
            index += 1
        blocks.append(block)
    return {**result, "content": blocks}
