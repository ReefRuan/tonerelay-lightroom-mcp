"""Read-only Lightroom develop preset tools for Runtime adapters."""
from __future__ import annotations

from typing import Callable


class PresetTools:
    def __init__(self, runner: Callable):
        self.runner = runner

    def _run(self, command: str, params: dict) -> dict:
        result = self.runner(command, params)
        if not isinstance(result, dict):
            raise ValueError("Lightroom preset result must be an object")
        # The plugin's legacy JSON encoder represents an empty Lua array as {}.
        # Normalize only fields whose contract is known to be arrays.
        for key in ("presets", "changes"):
            if key in result and result[key] == {}:
                result[key] = []
            elif key in result and not isinstance(result[key], list):
                raise ValueError(f"Lightroom preset result field {key} must be an array")
        return result

    @staticmethod
    def _selector(value, label="selector") -> dict:
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        allowed = {"preset_name", "preset_uuid", "preset_folder", "preset_scope"}
        if set(value) - allowed:
            raise ValueError(f"{label} contains unknown fields")
        if not any(isinstance(value.get(k), str) and value[k] for k in ("preset_name", "preset_uuid")):
            raise ValueError(f"{label} requires preset_name or preset_uuid")
        for key in ("preset_name", "preset_uuid", "preset_folder"):
            if key in value and (not isinstance(value[key], str) or not value[key]):
                raise ValueError(f"{label}.{key} must be a non-empty string")
        if value.get("preset_scope") not in (None, "lightroom", "plugin"):
            raise ValueError(f"{label}.preset_scope must be lightroom or plugin")
        return dict(value)

    def list(self, query="", limit=100, offset=0):
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        return self._run("list_develop_presets", {"query": query, "limit": limit, "offset": offset})

    def get(self, selector):
        return self._run("get_develop_preset", self._selector(selector))

    def compare(self, base, candidate):
        return self._run("compare_develop_presets", {
            "base": self._selector(base, "base"),
            "candidate": self._selector(candidate, "candidate"),
        })
