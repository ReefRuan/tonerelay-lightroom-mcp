"""Opaque Catalog/mode bindings shared by direct lr-ops clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


ACCESS_MODES = frozenset({"safe", "full"})


@dataclass(frozen=True)
class CatalogBinding:
    """The authorization values returned by one ``probe_client`` call."""

    access_mode: str
    catalog_identity: str

    @classmethod
    def from_probe(cls, probe: Mapping) -> "CatalogBinding":
        mode = probe.get("access_mode")
        identity = probe.get("catalog_identity")
        capabilities = probe.get("capabilities")
        if mode not in ACCESS_MODES:
            raise ValueError("probe_client did not return access_mode safe or full")
        if not isinstance(identity, str) or not identity:
            raise ValueError("probe_client did not return a non-empty catalog_identity")
        if (not isinstance(capabilities, Mapping)
                or capabilities.get("expected_access_mode") is not True
                or capabilities.get("expected_catalog_identity") is not True):
            raise ValueError("plugin does not advertise the required Catalog binding contract")
        if probe.get("catalog_authorized") is not True:
            raise ValueError("the active Lightroom Catalog is not authorized")
        return cls(access_mode=mode, catalog_identity=identity)

    def apply(self, params: Mapping | None = None) -> dict:
        """Copy params and attach the binding, rejecting contradictory input."""
        bound = dict(params or {})
        expected = {
            "expected_access_mode": self.access_mode,
            "expected_catalog_identity": self.catalog_identity,
        }
        for key, value in expected.items():
            supplied = bound.get(key)
            if supplied is not None and supplied != value:
                raise ValueError(f"{key} conflicts with the probed Lightroom Catalog")
            bound[key] = value
        return bound
