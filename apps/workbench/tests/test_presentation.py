from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workbench.presentation import PresentationSettings
from workbench.state import StoreError


class PresentationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = PresentationSettings(Path(self.temp.name))

    def test_defaults_do_not_need_a_file(self):
        self.assertEqual(self.settings.get(), {
            "schema_version": 1,
            "codex_preview_max_edge": 1024,
            "edit_render_max_edge": 2048,
        })

    def test_update_is_persistent_and_partial(self):
        updated = self.settings.update(codex_preview_max_edge=512)
        self.assertEqual(updated["codex_preview_max_edge"], 512)
        self.assertEqual(updated["edit_render_max_edge"], 2048)
        self.assertEqual(PresentationSettings(Path(self.temp.name)).get(), updated)

    def test_rejects_empty_or_out_of_range_updates(self):
        with self.assertRaisesRegex(StoreError, "at least one"):
            self.settings.update()
        with self.assertRaisesRegex(StoreError, "codex_preview_max_edge"):
            self.settings.update(codex_preview_max_edge=32)
        with self.assertRaisesRegex(StoreError, "edit_render_max_edge"):
            self.settings.update(edit_render_max_edge=4096)
