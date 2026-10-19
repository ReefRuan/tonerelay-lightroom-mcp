"""Small, path-free metadata records for bytes produced by the workbench.

An ``ArtifactRef`` is the boundary object passed to an agent.  The local path
is deliberately never part of the record: callers receive identity, source,
provenance, freshness and enough byte metadata to decide whether an image can
be inspected or used as acceptance evidence.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
from pathlib import Path

from PIL import Image


SCHEMA_VERSION = "artifact-ref-v1"
MAX_BYTES = 64 * 1024 * 1024


def _read(path: Path) -> bytes:
    original = Path(path)
    if original.is_symlink():
        raise ValueError("artifact symlinks are not accepted")
    resolved = original.resolve()
    if not resolved.is_file():
        raise ValueError("artifact is not a regular file")
    with resolved.open("rb") as handle:
        content = handle.read(MAX_BYTES + 1)
    if not content:
        raise ValueError("artifact is empty")
    if len(content) > MAX_BYTES:
        raise ValueError("artifact exceeds 64 MiB")
    return content


def _build_artifact_ref(
    content: bytes,
    filename: str,
    *,
    artifact_id: str,
    source: dict,
    purpose: str,
    method: str,
    freshness: str,
    evidence_status: str,
    color_space: str,
    orientation: str | None = None,
) -> dict:
    """Build one path-free record from an already captured byte snapshot."""
    fields = {
        "artifact_id": artifact_id,
        "source": source,
        "purpose": purpose,
        "method": method,
        "freshness": freshness,
        "evidence_status": evidence_status,
        "color_space": color_space,
    }
    if not isinstance(content, bytes):
        raise ValueError("artifact content must be bytes")
    if not content:
        raise ValueError("artifact is empty")
    if len(content) > MAX_BYTES:
        raise ValueError("artifact exceeds 64 MiB")
    if not isinstance(filename, str) or not filename:
        raise ValueError("filename is required")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("artifact_id is required")
    if not isinstance(source, dict) or not source:
        raise ValueError("source is required")
    for key in ("purpose", "method", "freshness", "evidence_status", "color_space"):
        if not isinstance(fields[key], str) or not fields[key]:
            raise ValueError(f"{key} is required")

    try:
        with Image.open(io.BytesIO(content)) as image:
            dimensions = [image.width, image.height]
            image_format = image.format
            mode = image.mode
    except Exception as exc:  # keep this boundary error actionable for agents
        raise ValueError("artifact is not a readable image") from exc

    record = {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "source": source,
        "purpose": purpose,
        "method": method,
        "freshness": freshness,
        "evidence_status": evidence_status,
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "format": image_format or "unknown",
        "mode": mode,
        "dimensions": dimensions,
        "orientation": orientation,
        "color_space": color_space,
    }
    return record


def build_artifact_ref_from_bytes(
    content: bytes,
    *,
    filename: str,
    artifact_id: str,
    source: dict,
    purpose: str,
    method: str,
    freshness: str,
    evidence_status: str,
    color_space: str,
    orientation: str | None = None,
) -> dict:
    """Describe exact caller-provided bytes without reopening a filesystem path."""
    return _build_artifact_ref(
        content, filename, artifact_id=artifact_id, source=source,
        purpose=purpose, method=method, freshness=freshness,
        evidence_status=evidence_status, color_space=color_space,
        orientation=orientation)


def build_artifact_ref(
    path: Path,
    *,
    artifact_id: str,
    source: dict,
    purpose: str,
    method: str,
    freshness: str,
    evidence_status: str,
    color_space: str,
    orientation: str | None = None,
) -> dict:
    """Describe one published file without exposing its host path."""
    original = Path(path)
    return _build_artifact_ref(
        _read(original), original.name,
        artifact_id=artifact_id, source=source, purpose=purpose,
        method=method, freshness=freshness,
        evidence_status=evidence_status, color_space=color_space,
        orientation=orientation)
