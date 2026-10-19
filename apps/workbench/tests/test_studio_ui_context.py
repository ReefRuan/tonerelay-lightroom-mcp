"""Copy success and restoration warnings remain independent in Episode state."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from test_studio import FakeLR
from workbench.studio.engine import Studio


PARTIAL_UI = {
    'schema_version': 'lr-ui-context-v1', 'status': 'partial',
    'parts': {'sources': {'status': 'not_restored'},
              'selection': {'status': 'skipped'}, 'module': {'status': 'restored'}},
    'warnings': [{'part': 'sources', 'code': 'restore_failed'}],
}


class UiReportingLR(FakeLR):
    def __init__(self, report):
        super().__init__()
        self.report = copy.deepcopy(report)

    def run(self, command, params=None):
        result = super().run(command, params)
        if command == 'create_virtual_copy':
            result['ui_context'] = copy.deepcopy(self.report)
        return result


class StudioUiContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lr = UiReportingLR(PARTIAL_UI)
        self.studio = Studio(Path(self.temp.name), self.lr)

    def test_partial_restore_does_not_fail_or_repeat_the_copy(self):
        state = self.studio.create('source', mode='source_edit', long_edge=256)
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(self.lr.copies, 1)
        self.assertEqual(state['ui_context'], PARTIAL_UI)
        self.assertEqual(state['versions'][0]['ui_context'], PARTIAL_UI)
        self.assertEqual(self.studio.get(state['id'])['ui_context'], PARTIAL_UI)
        self.assertEqual(self.studio.list()[0]['ui_context'], PARTIAL_UI)

    def test_verified_restore_keeps_empty_warnings_as_an_array(self):
        self.lr.report = {
            'schema_version': 'lr-ui-context-v1', 'status': 'restored',
            'parts': {part: {'status': 'restored'} for part in ('sources', 'selection', 'module')},
            'warnings': [],
        }
        state = self.studio.create('source', mode='source_edit', long_edge=256)
        self.assertEqual(state['ui_context'], self.lr.report)
        self.assertEqual(state['versions'][0]['ui_context'], self.lr.report)

    def test_pending_copy_and_warning_survive_a_later_failure(self):
        self.lr.fail = 'capture_baseline'
        with self.assertRaisesRegex(RuntimeError, 'injected LR failure'):
            self.studio.create('source', mode='source_edit', long_edge=256)
        state = self.studio.get(self.studio.list()[0]['id'])
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['pending_copy_uuid'], 'copy1')
        self.assertEqual(state['versions'], [])
        self.assertEqual(state['ui_context'], PARTIAL_UI)
        self.assertEqual(self.lr.copies, 1)

    def test_report_extras_never_persist_sdk_objects_or_private_paths(self):
        self.lr.report['sdk_object'] = object()
        self.lr.report['private_path'] = '/private/never-on-wire'
        self.lr.report['parts']['sources']['private_path'] = '/private/never-on-wire'
        self.lr.report['warnings'][0]['detail'] = '/private/never-on-wire'
        state = self.studio.create('source', mode='source_edit', long_edge=256)
        serialized = json.dumps(self.studio.raw(state['id']))
        self.assertNotIn('never-on-wire', serialized)
        self.assertNotIn('sdk_object', serialized)
        self.assertEqual(state['ui_context'], PARTIAL_UI)

    def test_unhashable_report_fields_preserve_copy_success(self):
        for report in (
                {'schema_version': 'lr-ui-context-v1', 'status': [], 'parts': {}},
                {'schema_version': 'lr-ui-context-v1', 'status': 'restored',
                 'parts': {'sources': {'status': {}}}},
                {**PARTIAL_UI, 'warnings': [{'part': ['sources'], 'code': {}}]},
                {'schema_version': []}):
            with self.subTest(report=report):
                self.lr.report = report
                before = self.lr.copies
                state = self.studio.create('source', mode='source_edit', long_edge=256)
                self.assertEqual(state['status'], 'ready')
                self.assertEqual(self.lr.copies, before + 1)
                self.assertEqual(state['ui_context']['status'], 'unavailable')
                self.assertIn({'part': 'all', 'code': 'invalid_report'}, state['ui_context']['warnings'])

    def test_public_projection_sanitizes_stored_report_extras(self):
        state = self.studio.create('source', mode='source_edit', long_edge=256)
        raw = self.studio.raw(state['id'])
        raw['ui_context']['private_path'] = '/private/never-on-wire'
        raw['versions'][0]['ui_context']['sdk_handle'] = {'path': '/private/never-on-wire'}
        self.studio.save(raw)
        serialized = json.dumps(self.studio.get(state['id']))
        self.assertNotIn('never-on-wire', serialized)
        self.assertNotIn('sdk_handle', serialized)
