"""Catalog discovery keeps active identities stable across path aliases."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workbench.catalog_discovery import catalog_identity_for_path, discover_catalogs


class CatalogDiscoveryPathAliasTestCase(unittest.TestCase):
    def test_overlapping_roots_visit_each_directory_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            (home / "Studio.lrcat").write_bytes(b"metadata")
            with patch("workbench.catalog_discovery._scan_roots", return_value=[
                    ("Home", home, "home"), ("Volume", root, "volume")]), \
                    patch("workbench.catalog_discovery.SCAN_DIRECTORIES", 2):
                result = discover_catalogs(active_identity=None, active_catalog_id=None)
            self.assertEqual(result["scan_status"], "complete")
            self.assertEqual(len(result["catalogs"]), 1)
            self.assertEqual(result["catalogs"][0]["location_hint"], "~")

    def test_directory_budget_returns_partial_and_preserves_active_item(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child"
            child.mkdir()
            (child / "Studio.lrcat").write_bytes(b"metadata")
            with patch("workbench.catalog_discovery._scan_roots", return_value=[
                    ("Home", root, "home")]), \
                    patch("workbench.catalog_discovery.SCAN_DIRECTORIES", 1):
                result = discover_catalogs(active_identity="active-identity",
                                           active_catalog_id="Open.lrcat")
            self.assertEqual(result["scan_status"], "partial")
            self.assertEqual(result["scan_limit_reason"], "directory_budget")
            self.assertEqual(result["scan_errors"], 0)
            self.assertEqual(result["catalogs"][0]["catalog_id"], "Open.lrcat")

    def test_elapsed_budget_reports_partial_without_disclosing_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("workbench.catalog_discovery._scan_roots", return_value=[
                    ("Home", root, "home")]), \
                    patch("workbench.catalog_discovery.time.monotonic", side_effect=[0, 6, 6]):
                result = discover_catalogs(active_identity=None, active_catalog_id=None)
            self.assertEqual(result["scan_status"], "partial")
            self.assertEqual(result["scan_limit_reason"], "time_budget")
            self.assertNotIn(temporary, str(result))

    def test_active_catalog_alias_is_attached_to_the_discovered_file(self):
        if os.name == "nt":
            self.skipTest("directory symlink creation is not available on every Windows host")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real_root = base / "real"
            real_root.mkdir()
            alias_root = base / "alias"
            try:
                alias_root.symlink_to(real_root, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink creation unavailable: {exc}")
            catalog = real_root / "Studio.lrcat"
            catalog.write_bytes(b"catalog metadata fixture")
            discovered_path = alias_root / catalog.name
            active_identity = catalog_identity_for_path(discovered_path.resolve())

            with patch("workbench.catalog_discovery._scan_roots",
                       return_value=[("Home", alias_root, "home")]):
                inventory = discover_catalogs(
                    active_identity=active_identity,
                    active_catalog_id=catalog.name,
                    scan_local=True,
                    include_paths=True,
                )

        self.assertEqual(inventory["scan_status"], "complete")
        self.assertEqual(len(inventory["catalogs"]), 1)
        row = inventory["catalogs"][0]
        self.assertEqual(row["catalog_identity"], active_identity)
        self.assertTrue(row["is_active"])
        self.assertEqual(row["_local_path"], str(discovered_path))


if __name__ == "__main__":
    unittest.main()
