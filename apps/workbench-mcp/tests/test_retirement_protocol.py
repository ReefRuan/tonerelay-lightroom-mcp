"""Session retirement and replacement tools over JSON-RPC with offline LR."""
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

WB = Path(__file__).resolve().parents[2] / 'workbench'
sys.path.insert(0, str(WB))
sys.path.insert(0, str(WB / 'tests'))
from test_studio import FakeLR, CATALOG_IDENTITY
from workbench.runtime import Workbench
from workbench.state import RuntimeState
from workbench_mcp.server import McpServer, TOOLS


class RetirementProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lr = FakeLR()
        self.wb = Workbench(RuntimeState(self.root), self.lr)
        self.server = McpServer(self.wb)

    def rpc(self, tool_name, **args):
        return self.server.handle({'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call', 'params': {'name': tool_name, 'arguments': args}})['result']

    def data(self, name, **args):
        result = self.rpc(name, **args)
        self.assertFalse(result['isError'], result)
        return json.loads(result['content'][0]['text'])

    def test_registry_and_dispatch_retire_entire_family(self):
        names = {tool['name'] for tool in TOOLS}
        for name in ('create_session', 'get_session', 'apply_settings', 'get_preview',
                     'get_xmp', 'reset', 'get_full_settings', 'round_diff',
                     'replay_round', 'export_session_context'):
            with self.subTest(name=name):
                self.assertNotIn('trlr_' + name, names)
                self.assertTrue(self.rpc('trlr_' + name)['isError'])
        self.assertEqual(self.lr.calls, [])
        self.assertFalse((self.root / 'sessions').exists())
        self.assertFalse(hasattr(self.wb, 'create_session'))

    def test_live_read_reports_current_full_settings_and_distinct_evidence(self):
        self.lr.settings['source']['GrainAmount'] = 31
        result = self.data('trlr_get_photo_settings', photo_uuid='source')
        self.assertEqual(result['settings']['GrainAmount'], 31)
        self.assertEqual(result['evidence']['evidence_status'], 'stable_settings_readback')
        self.assertEqual(result['evidence']['color_evidence'], 'not_applicable')
        self.assertEqual(result['xmp_verification'], 'unverified_serialization')
        self.assertNotIn('episode_id', result)
        self.assertIn('GrainAmount', result['written_keys'])
        self.assertEqual([command for command, _ in self.lr.calls],
                         ['probe_client', 'get_develop_snapshot', 'get_develop_snapshot'])
        self.assertTrue(all(params['expected_catalog_identity'] == CATALOG_IDENTITY
                            for command, params in self.lr.calls if command != 'probe_client'))
        self.assertEqual(self.lr.copies, 0)

    def test_live_read_formats_and_invalid_inputs(self):
        self.assertNotIn('xmp', self.data('trlr_get_photo_settings', photo_uuid='source', format='json'))
        self.assertNotIn('settings', self.data('trlr_get_photo_settings', photo_uuid='source', format='xmp'))
        calls = len(self.lr.calls)
        for options in ({}, {'photo_uuid': ''}, {'photo_uuid': 'source', 'format': 'jpeg'},
                        {'session_id': 'retired'}):
            with self.subTest(options=options):
                self.assertTrue(self.rpc('trlr_get_photo_settings', **options)['isError'])
        self.assertEqual(len(self.lr.calls), calls)
        self.assertTrue(self.rpc('trlr_get_photo_settings', photo_uuid='missing')['isError'])

    def create(self):
        return self.data('trlr_create_episode', source_uuid='source', mode='source_edit', long_edge=256)

    def test_diff_offline_and_replay_as_new_immutable_version(self):
        state = self.create()
        sid = state['id']
        state = self.data('trlr_edit_episode', episode_id=sid,
                          base_revision=state['revision'], request_id=str(uuid.uuid4()),
                          settings={'Exposure2012': .4})
        calls = len(self.lr.calls)
        diff = self.data('trlr_episode_diff', episode_id=sid, version_a=0., version_b=1.)
        self.assertEqual(diff['submitted_settings_diff']['added']['Exposure2012'], .4)
        self.assertEqual(diff['after_readback_diff']['changed']['Exposure2012'], [0, .4])
        self.assertEqual(diff['readback_mismatches_a'], [])
        self.assertEqual(diff['readback_mismatches_b'], [])
        self.assertEqual(len(self.lr.calls), calls)
        revision = state['revision']
        request = str(uuid.uuid4())
        result = self.data('trlr_replay_episode', episode_id=sid, version=1.,
                           base_revision=revision, request_id=request)
        self.assertEqual(result['current_version'], 2)
        self.assertEqual(result['versions'][2]['replayed_from_version'], 1)
        self.assertNotEqual(result['versions'][1]['photo_uuid'], result['versions'][2]['photo_uuid'])
        self.assertTrue(result['versions'][2]['snapshot']['omitted'])
        calls = len(self.lr.calls)
        duplicate = self.data('trlr_replay_episode', episode_id=sid, version=1,
                              base_revision=revision, request_id=request, include_snapshots=True)
        self.assertEqual(duplicate['revision'], result['revision'])
        self.assertEqual(duplicate['versions'][2]['snapshot']['Exposure2012'], .4)
        self.assertEqual(len(self.lr.calls), calls)
        self.assertEqual(self.lr.settings['source']['Exposure2012'], 0)

    def test_diff_and_replay_invalid_versions_and_concurrency_are_errors(self):
        state = self.create()
        sid = state['id']
        calls = len(self.lr.calls)
        for version in (-1, True, 1.5, 99):
            with self.subTest(version=version):
                self.assertTrue(self.rpc('trlr_episode_diff', episode_id=sid,
                                         version_a=0, version_b=version)['isError'])
                self.assertTrue(self.rpc('trlr_replay_episode', episode_id=sid,
                                         version=version, base_revision=state['revision'],
                                         request_id=str(uuid.uuid4()))['isError'])
        self.assertTrue(self.rpc('trlr_replay_episode', episode_id=sid, version=0)['isError'])
        self.assertTrue(self.rpc('trlr_replay_episode', episode_id=sid, version=0,
                                 base_revision=state['revision'] - 1,
                                 request_id=str(uuid.uuid4()))['isError'])
        self.assertEqual(self.lr.copies, 1)
        self.assertFalse(any(command == 'apply_candidate' for command, _ in self.lr.calls[calls:]))

    def test_offline_diff_revision_does_not_resume_an_unknown_active_write(self):
        state = self.create()
        sid = state['id']
        original_request = str(uuid.uuid4())
        raw = self.wb.studio.raw(sid)
        raw['status'] = 'replaying'
        raw['operations'][original_request] = {
            'input': {'action': 'replay', 'version': 0}, 'status': 'running'}
        self.wb.studio.save(raw)
        calls = len(self.lr.calls)
        diff = self.data('trlr_episode_diff', episode_id=sid, version_a=0, version_b=0)
        failed = self.rpc('trlr_replay_episode', episode_id=sid, version=0,
                          base_revision=diff['revision'], request_id=str(uuid.uuid4()))
        self.assertTrue(failed['isError'], failed)
        recovered = self.data('trlr_replay_episode', episode_id=sid, version=0,
                              base_revision=diff['revision'], request_id=original_request)
        self.assertEqual(recovered['status'], 'replaying')
        self.assertEqual(recovered['revision'], diff['revision'])
        self.assertEqual(recovered['operations'][original_request]['status'], 'running')
        self.assertEqual(len(self.lr.calls), calls)

    def test_artifact_serves_the_verified_capture_when_path_changes_after_capture(self):
        state = self.create()
        sid = state['id']
        original_payload = self.wb.studio.artifact_payload
        captured = {}
        def mutate_after_capture(episode_id, name):
            path, content = original_payload(episode_id, name)
            captured['content'] = content
            path.write_bytes(b'changed after the verified read')
            return path, content
        with patch.object(self.wb.studio, 'artifact_payload', side_effect=mutate_after_capture):
            result = self.rpc('trlr_episode_artifact', episode_id=sid, name='v0.jpg')
        self.assertFalse(result['isError'], result)
        metadata = json.loads(result['content'][0]['text'])
        import hashlib
        self.assertEqual(metadata['artifact_ref']['sha256'],
                         hashlib.sha256(captured['content']).hexdigest())
        self.assertTrue(any(block['type'] == 'image' for block in result['content']))

    def test_artifact_replay_is_offline_hash_checked_and_compare_stays_episode_only(self):
        state = self.create()
        sid = state['id']
        calls = len(self.lr.calls)
        result = self.rpc('trlr_episode_artifact', episode_id=sid, name='v0.jpg')
        self.assertFalse(result['isError'], result)
        self.assertTrue(any(block['type'] == 'image' for block in result['content']))
        stable = {'episode_id': sid, 'name': 'v0.jpg'}
        for source in ({'photo_uuid': 'source'}, {'root': '012345abcdef', 'name': 'a.jpg'},
                       {'session_id': 'retired', 'round': 1}):
            with self.subTest(source=source):
                self.assertTrue(self.rpc('trlr_image_compare', left=source, right=stable)['isError'])
        self.assertEqual(len(self.lr.calls), calls)
        (self.wb.studio.directory(sid) / 'v0.jpg').write_bytes(b'tampered')
        self.assertTrue(self.rpc('trlr_episode_artifact', episode_id=sid, name='v0.jpg')['isError'])
        self.assertTrue(self.rpc('trlr_image_compare', left=stable, right=stable)['isError'])
        self.assertEqual(len(self.lr.calls), calls)


if __name__ == '__main__':
    unittest.main()
