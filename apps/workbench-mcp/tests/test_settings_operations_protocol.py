"""Same-copy iteration and durable failure contracts, through actual MCP dispatch."""
import copy
import json
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

WB = Path(__file__).resolve().parents[2] / 'workbench'
sys.path.insert(0, str(WB))
sys.path.insert(0, str(WB / 'tests'))
from test_studio import FakeLR
from workbench.runtime import Workbench
from workbench.state import RuntimeState
from workbench_mcp.server import McpServer, TOOLS


class GuardedLR(FakeLR):
    def __init__(self):
        super().__init__()
        self.settings['copy9'] = copy.deepcopy(self.settings['source'])
        self.inject = None
        self.caps = True
        self.clamp = False

    def run(self, command, params=None):
        params = params or {}
        if command == 'apply_candidate':
            if self.inject:
                callback, self.inject = self.inject, None
                callback()
            expected = params.get('expected_settings_snapshot')
            if params.get('require_in_place_guard') and expected != self.settings[params['photo_uuid']]:
                return {'status': 'conflict', 'code': 'settings_changed', 'applied': False}
        result = super().run(command, params)
        if command == 'probe_client':
            result['capabilities']['in_place_settings_v1'] = self.caps
        if command == 'apply_candidate' and self.clamp:
            self.settings[params['photo_uuid']]['GrainAmount'] = 100
        return result


class SettingsOperationProtocolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lr = GuardedLR()
        self.wb = Workbench(RuntimeState(self.root), self.lr)
        self.server = McpServer(self.wb)

    def rpc(self, name, **args):
        return self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': args}})['result']

    def data(self, name, **args):
        result = self.rpc(name, **args)
        self.assertFalse(result['isError'], result)
        return json.loads(result['content'][0]['text'])

    def token(self, photo='copy9'):
        return self.data('trlr_get_photo_settings', photo_uuid=photo, format='json')['settings_token']

    def apply(self, settings=None, **kwargs):
        params = {'photo_uuid': 'copy9', 'settings': settings or {'Exposure2012': .3},
                  'expected_settings_token': self.token(), 'request_id': str(uuid.uuid4()),
                  'render': False}
        params.update(kwargs)
        return self.data('trlr_apply_settings', **params)

    def test_twenty_rounds_modify_one_copy_without_session_or_new_copy(self):
        original = copy.deepcopy(self.lr.settings['source'])
        for index in range(20):
            result = self.apply({'Exposure2012': index / 10})
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['photo_uuid'], 'copy9')
            self.assertEqual(result['new_virtual_copies'], 0)
            self.assertEqual(result['readback_mismatches'], [])
        self.assertEqual(self.lr.copies, 0)
        self.assertEqual(self.lr.settings['source'], original)
        self.assertEqual(self.lr.settings['copy9']['Exposure2012'], 1.9)
        self.assertFalse((self.root / 'sessions').exists())

    def test_registry_keeps_sessions_retired_and_restores_edit_and_reset(self):
        names = {tool['name'] for tool in TOOLS}
        self.assertIn('trlr_apply_settings', names)
        self.assertIn('trlr_reset', names)
        self.assertIn('trlr_get_settings_operation', names)
        self.assertNotIn('trlr_create_session', names)
        self.assertNotIn('trlr_get_session', names)
        for required in ('trlr_get_photo_settings', 'trlr_export_image',
                         'trlr_episode_artifact', 'trlr_episode_diff',
                         'trlr_replay_episode', 'trlr_native_mask',
                         'trlr_parameter_reference'):
            self.assertIn(required, names)

    def test_concurrent_runtime_instances_cannot_edit_the_same_photo(self):
        entered, release = threading.Event(), threading.Event()
        self.lr.entered, self.lr.release = entered, release
        results = []
        def first():
            results.append(self.apply(render=True))
        worker = threading.Thread(target=first)
        worker.start()
        self.assertTrue(entered.wait(3))
        try:
            another = McpServer(Workbench(RuntimeState(self.root), self.lr))
            response = another.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'trlr_apply_settings', 'arguments': {
                    'photo_uuid': 'copy9', 'settings': {'Exposure2012': .8},
                    'expected_settings_token': self.token(), 'request_id': str(uuid.uuid4()),
                    'render': False}}})['result']
            self.assertTrue(response['isError'])
            self.assertIn('running or unknown', response['content'][0]['text'])
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['status'], 'completed')
        self.assertEqual(self.lr.settings['copy9']['Exposure2012'], .3)
        self.assertEqual(self.lr.copies, 0)

    def test_master_and_missing_guard_capability_refused_before_write(self):
        token = self.token('source')
        result = self.rpc('trlr_apply_settings', photo_uuid='source', settings={'Exposure2012': .3},
                          expected_settings_token=token, request_id=str(uuid.uuid4()))
        self.assertTrue(result['isError'])
        self.lr.caps = False
        result = self.rpc('trlr_apply_settings', photo_uuid='copy9', settings={'Exposure2012': .3},
                          expected_settings_token=self.token(), request_id=str(uuid.uuid4()))
        self.assertTrue(result['isError'])
        self.assertFalse(any(c == 'apply_candidate' for c, _ in self.lr.calls))

    def test_stale_wrong_photo_and_catalog_tokens_fail_without_write(self):
        old = self.token()
        self.lr.settings['copy9']['Exposure2012'] = .8
        with self.subTest('settings drift'):
            self.assertTrue(self.rpc('trlr_apply_settings', photo_uuid='copy9',
                settings={'Exposure2012': .3}, expected_settings_token=old,
                request_id=str(uuid.uuid4()))['isError'])
        old = self.token()
        self.lr.active_catalog_identity = 'catalog-md5-v1:ffffffffffffffffffffffffffffffff'
        self.assertTrue(self.rpc('trlr_apply_settings', photo_uuid='copy9',
            settings={'Exposure2012': .3}, expected_settings_token=old,
            request_id=str(uuid.uuid4()))['isError'])
        self.assertFalse(any(c == 'apply_candidate' for c, _ in self.lr.calls))

    def test_duplicate_across_restart_is_offline_and_different_payload_refused(self):
        request = str(uuid.uuid4())
        token = self.token()
        first = self.apply(request_id=request, expected_settings_token=token)
        self.server = McpServer(Workbench(RuntimeState(self.root), self.lr))
        calls = len(self.lr.calls)
        duplicate = self.data('trlr_apply_settings', photo_uuid='copy9',
            settings={'Exposure2012': .3}, expected_settings_token=token,
            request_id=request, render=False)
        self.assertEqual(first, duplicate)
        self.assertEqual(len(self.lr.calls), calls)
        self.assertTrue(self.rpc('trlr_apply_settings', photo_uuid='copy9',
            settings={'Exposure2012': .4}, expected_settings_token=token,
            request_id=request, render=False)['isError'])

    def test_unknown_write_not_replayed_and_blocks_other_requests(self):
        self.lr.fail = 'apply_candidate'
        token, request = self.token(), str(uuid.uuid4())
        first = self.apply(expected_settings_token=token, request_id=request)
        self.assertEqual(first['status'], 'unknown')
        calls = len(self.lr.calls)
        self.assertEqual(self.data('trlr_get_settings_operation', operation_id=request)['status'], 'unknown')
        self.assertEqual(self.data('trlr_apply_settings', photo_uuid='copy9',
            settings={'Exposure2012': .3}, expected_settings_token=token,
            request_id=request, render=False)['status'], 'unknown')
        self.assertEqual(len(self.lr.calls), calls)
        self.lr.fail = None
        self.assertTrue(self.rpc('trlr_apply_settings', photo_uuid='copy9',
            settings={'Exposure2012': .4}, expected_settings_token=token,
            request_id=str(uuid.uuid4()), render=False)['isError'])
        self.assertEqual(sum(c == 'apply_candidate' for c, _ in self.lr.calls), 1)

    def test_user_change_inside_write_gate_is_not_overwritten(self):
        self.lr.inject = lambda: self.lr.settings['copy9'].update(Exposure2012=1.1)
        result = self.apply()
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.lr.settings['copy9']['Exposure2012'], 1.1)
        self.assertEqual(self.apply({'Contrast2012': 4})['status'], 'completed')

    def test_reset_uses_captured_values_and_refuses_later_changes(self):
        first = self.apply({'Exposure2012': .7, 'Contrast2012': 9})
        reset = self.data('trlr_reset', operation_id=first['operation_id'],
            expected_settings_token=self.token(), request_id=str(uuid.uuid4()), render=False)
        self.assertEqual(reset['status'], 'completed')
        self.assertEqual(self.lr.settings['copy9']['Exposure2012'], 0)
        self.assertEqual(self.lr.copies, 0)
        second = self.apply()
        self.lr.settings['copy9']['Contrast2012'] = 3
        self.assertTrue(self.rpc('trlr_reset', operation_id=second['operation_id'],
            expected_settings_token=self.token(), request_id=str(uuid.uuid4()), render=False)['isError'])

    def test_render_is_controlled_and_historical_image_is_hash_checked(self):
        result = self.apply(render=True)
        self.assertEqual(result['preview']['evidence_status'], 'verified_render')
        self.assertEqual(self.lr.copies, 0)
        calls = len(self.lr.calls)
        preview = self.rpc('trlr_get_settings_operation', operation_id=result['operation_id'],
                           include_preview=True)
        self.assertFalse(preview['isError'])
        self.assertTrue(any(c['type'] == 'image' for c in preview['content']))
        self.assertEqual(len(self.lr.calls), calls)
        (self.root / 'settings-operations' / result['operation_id'] / 'preview.jpg').write_bytes(b'bad')
        self.assertTrue(self.rpc('trlr_get_settings_operation', operation_id=result['operation_id'],
                                include_preview=True)['isError'])

    def test_readback_clamping_is_reported(self):
        self.lr.clamp = True
        result = self.apply({'GrainAmount': 1000})
        self.assertEqual(result['status'], 'completed_with_mismatches')
        self.assertEqual(result['readback_mismatches'][0]['applied'], 100)

    def test_preview_failure_does_not_replay_a_verified_edit_or_lock_the_photo(self):
        for stage in ('export', 'download', 'drift_during_render'):
            with self.subTest(stage=stage):
                base_run = self.lr.run

                def run(command, params=None):
                    if command == 'export_preview' and stage == 'export':
                        raise OSError('export unavailable')
                    result = base_run(command, params)
                    if command == 'export_preview' and stage == 'drift_during_render':
                        self.lr.settings['copy9']['Exposure2012'] = 2
                    return result

                download = (mock.patch.object(self.lr, 'fetch_output',
                            side_effect=OSError('download unavailable'))
                            if stage == 'download' else nullcontext())
                token, request = self.token(), str(uuid.uuid4())
                with mock.patch.object(self.lr, 'run', side_effect=run), download:
                    first = self.apply(expected_settings_token=token, request_id=request, render=True)
                self.assertEqual(first['status'], 'completed')
                self.assertIn('preview_error', first)
                self.assertNotIn('preview', first)
                calls = len(self.lr.calls)
                duplicate = self.data('trlr_apply_settings', photo_uuid='copy9',
                    settings={'Exposure2012': .3}, expected_settings_token=token,
                    request_id=request, render=True)
                self.assertEqual(first, duplicate)
                self.assertEqual(len(self.lr.calls), calls)
                self.assertEqual(self.apply({'Contrast2012': 8})['status'], 'completed')
                self.assertEqual(self.lr.copies, 0)

    def test_white_balance_other_fields_and_curve_survive_later_edits(self):
        self.lr.settings['copy9'].update(Temperature=4500, Tint=7, GrainAmount=25)
        curve = [0, 4, 128, 139, 255, 255]
        self.apply({'ToneCurvePV2012': curve})
        self.apply({'Exposure2012': .4})
        self.assertEqual(self.lr.settings['copy9']['ToneCurvePV2012'], curve)
        self.assertEqual(self.lr.settings['copy9']['Temperature'], 4500)
        self.assertEqual(self.lr.settings['copy9']['Tint'], 7)
        self.assertEqual(self.lr.settings['copy9']['GrainAmount'], 25)


if __name__ == '__main__':
    unittest.main()
