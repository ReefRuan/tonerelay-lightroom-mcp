"""Export failures preserve truthful Episode provenance, without a real Catalog."""
import copy
import tempfile
import unittest
from pathlib import Path

from test_studio import FakeLR
from workbench.studio.engine import Studio


class FailingExportLR(FakeLR):
    def __init__(self, fail_on):
        super().__init__()
        self.export_count = 0
        self.fail_on = fail_on

    def run(self, command, params=None):
        if command == 'export_preview':
            self.export_count += 1
            if self.export_count == self.fail_on:
                self.calls.append((command, copy.deepcopy(params)))
                raise RuntimeError('export_preview: render_failed: disk full')
        return super().run(command, params)


class StudioExportFailureTests(unittest.TestCase):
    def test_reference_export_failure_creates_no_catalog_copy_or_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            lr = FailingExportLR(fail_on=1)
            before = copy.deepcopy(lr.settings['source'])
            studio = Studio(Path(directory), lr)
            with self.assertRaisesRegex(RuntimeError, 'render_failed: disk full'):
                studio.create('source', mode='source_edit', long_edge=256)
            episode = studio.raw(studio.list()[0]['id'])
            self.assertEqual(episode['status'], 'failed')
            self.assertEqual(episode['versions'], [])
            self.assertNotIn('pending_copy_uuid', episode)
            self.assertEqual(lr.copies, 0)
            self.assertEqual(lr.export_count, 1)
            self.assertFalse((studio.directory(episode['id']) / 'reference.jpg').exists())
            self.assertEqual(lr.settings['source'], before)

    def test_version_export_failure_keeps_single_copy_and_reference_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            lr = FailingExportLR(fail_on=2)
            before = copy.deepcopy(lr.settings['source'])
            studio = Studio(Path(directory), lr)
            with self.assertRaisesRegex(RuntimeError, 'render_failed: disk full'):
                studio.create('source', mode='source_edit', long_edge=256)
            episode = studio.raw(studio.list()[0]['id'])
            self.assertEqual(episode['status'], 'failed')
            self.assertEqual(episode['versions'], [])
            self.assertEqual(episode['pending_copy_uuid'], 'copy1')
            self.assertIn('copy1', lr.settings)
            self.assertEqual(lr.copies, 1)
            self.assertEqual(lr.export_count, 2)
            self.assertTrue((studio.directory(episode['id']) / 'reference.jpg').is_file())
            self.assertFalse((studio.directory(episode['id']) / 'v0.jpg').exists())
            self.assertEqual(lr.settings['source'], before)
