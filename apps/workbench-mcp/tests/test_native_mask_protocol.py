import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from workbench.runtime import Workbench
from workbench.state import RuntimeState
from workbench_mcp.server import McpServer, TOOLS


class NativeMaskProtocolTests(unittest.TestCase):
    def test_schema_dispatch_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            wb = Workbench(RuntimeState(Path(tmp)), Mock())
            wb.run = Mock(return_value={'status': 'created', 'segmentation_data_present': True})
            server = McpServer(wb)
            tool = next(t for t in TOOLS if t['name'] == 'trlr_native_mask')
            self.assertFalse(tool['inputSchema']['additionalProperties'])
            args = {'photo_uuid': 'copy', 'action': 'create', 'mask_type': 'subject',
                    'expected_token': 'native-mask-md5-v1:' + 'b' * 32, 'request_id': 'test',
                    'settings': {'LocalExposure2012': .1}}
            result = server.call_tool('trlr_native_mask', args)
            self.assertEqual(json.loads(result['content'][0]['text'])['status'], 'created')
            wb.run.assert_called_once_with('native_mask', args)
            wb.run.reset_mock()
            with self.assertRaises(ValueError): server.call_tool('trlr_native_mask', {'photo_uuid': 'copy', 'action': 'create'})
            wb.run.assert_not_called()


if __name__ == '__main__': unittest.main()
