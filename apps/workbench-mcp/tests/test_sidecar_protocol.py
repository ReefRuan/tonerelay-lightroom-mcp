import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from workbench.runtime import Workbench
from workbench.store import Store
from workbench_mcp.server import McpServer, TOOLS


class SidecarProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wb = Workbench(Store(Path(self.tmp.name)), Mock())
        self.wb.run = Mock(return_value={'status': 'inspected', 'photo_uuid': 'master', 'expected_token': 'a' * 64})
        self.server = McpServer(self.wb)

    def tearDown(self): self.tmp.cleanup()

    def test_tools_have_valid_schemas_and_uuid_only_inspection(self):
        for name, command in [('trlr_update_xmp_sidecar', 'update_xmp_sidecar'), ('trlr_apply_mask', 'apply_mask')]:
            tool = next(t for t in TOOLS if t['name'] == name)
            self.assertEqual(tool['inputSchema']['required'], ['photo_uuid'])
            self.assertFalse(tool['inputSchema']['additionalProperties'])
            result = self.server.call_tool(name, {'photo_uuid': 'master'})
            self.assertEqual(json.loads(result['content'][0]['text'])['status'], 'inspected')
            self.wb.run.assert_called_with(command, {'photo_uuid': 'master'})

    def test_unapproved_writes_and_path_requests_never_reach_lightroom(self):
        for name in ['trlr_update_xmp_sidecar', 'trlr_apply_mask']:
            with self.assertRaises(ValueError):
                self.server.call_tool(name, {'photo_uuid': 'master', 'action': 'update', 'settings': {'LocalExposure2012': .5}})
            with self.assertRaises(ValueError):
                self.server.call_tool(name, {'photo_uuid': 'master', 'path': '/private/photo.ARW'})
        self.wb.run.assert_not_called()

    def test_jsonrpc_exposes_tools_and_returns_errors_for_unapproved_writes(self):
        listed = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        names = {tool['name'] for tool in listed['result']['tools']}
        self.assertTrue({'trlr_apply_mask', 'trlr_update_xmp_sidecar'}.issubset(names))
        response = self.server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                      'params': {'name': 'trlr_apply_mask', 'arguments': {
                                          'photo_uuid': 'master', 'action': 'create', 'mask_type': 'subject'}}})
        self.assertTrue(response['result']['isError'])
        self.wb.run.assert_not_called()

    def test_create_fallback_is_structured_and_not_reported_as_created(self):
        self.wb.run.return_value = {'status': 'xmp_mask_generation_unavailable', 'mask_exists': False,
                                   'sidecar_written': False, 'fallback': {'required': True, 'shortcut': 'Shift+W'}}
        params = {'photo_uuid': 'master', 'action': 'create', 'confirm_xmp_mode': True,
                  'expected_token': 'a' * 64, 'request_id': 'create-1', 'mask_type': 'body_skin'}
        result = json.loads(self.server.call_tool('trlr_apply_mask', params)['content'][0]['text'])
        self.assertFalse(result['mask_exists']); self.assertTrue(result['fallback']['required'])


if __name__ == '__main__': unittest.main()
