"""The Catalog switch launcher only requests an OS file association."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workbench.catalog_switch import CatalogSwitchError, open_catalog_file


class CatalogSwitchLauncherTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Path(self.temp.name) / "Studio Catalog.lrcat"
        self.catalog.write_bytes(b"metadata fixture")

    def test_macos_uses_the_os_file_association_without_waiting_for_lightroom(self):
        with patch("workbench.catalog_switch.sys.platform", "darwin"), \
             patch("workbench.catalog_switch.subprocess.run") as run:
            open_catalog_file(self.catalog)

        run.assert_called_once_with(
            ["open", str(self.catalog.absolute())],
            check=True,
            timeout=15,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_non_catalog_path_is_refused_before_starting_an_application(self):
        image = Path(self.temp.name) / "photo.jpg"
        image.write_bytes(b"not a catalog")
        with patch("workbench.catalog_switch.sys.platform", "darwin"), \
             patch("workbench.catalog_switch.subprocess.run") as run:
            with self.assertRaises(CatalogSwitchError):
                open_catalog_file(image)

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
