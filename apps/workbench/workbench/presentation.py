"""Persistent presentation budgets for images returned by the Runtime.

These settings deliberately do not describe Lightroom develop settings or a
final export.  They bound two different costs: image bytes sent to an MCP
client, and the Lightroom render size used for *new* editing contexts.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .store import StoreError


SCHEMA_VERSION = 1
CODEX_EDGE_MIN = 64
CODEX_EDGE_MAX = 2048
EDIT_EDGE_MIN = 256
EDIT_EDGE_MAX = 2048
DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "codex_preview_max_edge": 1024,
    "edit_render_max_edge": 2048,
}


class PresentationSettings:
    """Small JSON-backed settings store, scoped to one Runtime data root."""

    def __init__(self, root: Path):
        self.path = Path(root) / "presentation-settings.json"

    @staticmethod
    def _edge(value, name: str, minimum: int, maximum: int) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise StoreError(f"{name} must be an integer from {minimum} to {maximum}")
        return value

    def _read(self) -> dict:
        if not self.path.exists():
            return dict(DEFAULTS)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreError("presentation settings are unreadable; repair or remove presentation-settings.json") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise StoreError("presentation settings have an unsupported schema version")
        return {
            "schema_version": SCHEMA_VERSION,
            "codex_preview_max_edge": self._edge(
                raw.get("codex_preview_max_edge"), "codex_preview_max_edge",
                CODEX_EDGE_MIN, CODEX_EDGE_MAX),
            "edit_render_max_edge": self._edge(
                raw.get("edit_render_max_edge"), "edit_render_max_edge",
                EDIT_EDGE_MIN, EDIT_EDGE_MAX),
        }

    def get(self) -> dict:
        return self._read()

    def update(self, *, codex_preview_max_edge=None, edit_render_max_edge=None) -> dict:
        if codex_preview_max_edge is None and edit_render_max_edge is None:
            raise StoreError("set_presentation_config requires at least one setting")
        current = self._read()
        if codex_preview_max_edge is not None:
            current["codex_preview_max_edge"] = self._edge(
                codex_preview_max_edge, "codex_preview_max_edge", CODEX_EDGE_MIN, CODEX_EDGE_MAX)
        if edit_render_max_edge is not None:
            current["edit_render_max_edge"] = self._edge(
                edit_render_max_edge, "edit_render_max_edge", EDIT_EDGE_MIN, EDIT_EDGE_MAX)
        self._write(current)
        return current

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".presentation-settings-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
