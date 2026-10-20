"""Persistent presentation budgets for images returned by the Runtime.

These settings deliberately do not describe Lightroom develop settings or a
final export.  They bound two different costs: image bytes sent to an MCP
client, and the Lightroom render size used for *new* editing contexts.
"""

from __future__ import annotations

from pathlib import Path

from .state import StoreError
from lr_ops.configuration import ConfigError, for_workbench


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
        self.config = for_workbench(root)
        self.path = self.config.path

    @staticmethod
    def _edge(value, name: str, minimum: int, maximum: int) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise StoreError(f"{name} must be an integer from {minimum} to {maximum}")
        return value

    def _read(self) -> dict:
        try:
            raw = self.config.read()["presentation"]
        except ConfigError as exc:
            raise StoreError(str(exc)) from exc
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
        changes = {}
        if codex_preview_max_edge is not None:
            changes["codex_preview_max_edge"] = self._edge(
                codex_preview_max_edge, "codex_preview_max_edge", CODEX_EDGE_MIN, CODEX_EDGE_MAX)
        if edit_render_max_edge is not None:
            changes["edit_render_max_edge"] = self._edge(
                edit_render_max_edge, "edit_render_max_edge", EDIT_EDGE_MIN, EDIT_EDGE_MAX)
        try:
            value = self.config.update("presentation", lambda current: current.update(changes))
            return {"schema_version": SCHEMA_VERSION, **value["presentation"]}
        except ConfigError as exc:
            raise StoreError(str(exc)) from exc
