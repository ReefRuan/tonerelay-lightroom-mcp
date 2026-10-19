"""New MCP episode tools over the real engine with an offline LR double."""
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

WB=Path(__file__).resolve().parents[2]/'workbench'
sys.path.insert(0,str(WB));sys.path.insert(0,str(WB/'tests'))
from test_studio import FakeLR
from test_studio_ui_context import UiReportingLR, PARTIAL_UI
from workbench.runtime import Workbench
from workbench.store import Store
from workbench_mcp.server import McpServer, TOOLS


class EpisodeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.lr=FakeLR()
        self.server=McpServer(Workbench(Store(Path(self.tmp.name)),self.lr))

    def call(self,tool_name,**args):
        result=self.server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'trlr_'+tool_name,'arguments':args}})['result']
        self.assertFalse(result['isError'],result)
        return result

    def data(self,name,**args):
        return json.loads(self.call(name,**args)['content'][0]['text'])

    def test_conversation_edit_crop_replay_and_restore(self):
        s=self.data('create_episode',source_uuid='source',reference_uuid='reference',same_capture=True,long_edge=256)
        sid=s['id']
        s=self.data('message_episode',episode_id=sid,base_revision=s['revision'],request_id=str(uuid.uuid4()),text='曝光增加 0.4',role='user')
        self.assertEqual(s['messages'][-1]['text'],'曝光增加 0.4')
        s=self.data('edit_episode',episode_id=sid,base_revision=s['revision'],request_id=str(uuid.uuid4()),settings={'Exposure2012':.4})
        image=self.call('episode_artifact',episode_id=sid,name='v1.jpg',crop=[0,0,.5,.5],max_edge=128)
        self.assertEqual(sum(block['type']=='image' for block in image['content']),1)
        s=self.data('verify_episode_xmp',episode_id=sid,base_revision=s['revision'],request_id=str(uuid.uuid4()),version=1)
        xmp=self.call('episode_artifact',episode_id=sid,name='v1.xmp')['content'][0]['text']
        self.assertIn('Exposure2012',xmp)
        s=self.data('restore_episode',episode_id=sid,base_revision=s['revision'],request_id=str(uuid.uuid4()),version=0)
        self.assertEqual(s['current_version'],0)
        self.assertEqual(len(self.data('list_episodes')),1)

    def assert_snapshots(self, state, include):
        values = [state['source_snapshot'], state['baseline_snapshot']]
        values.extend(version['snapshot'] for version in state['versions'])
        for value in values:
            if include:
                self.assertIn('Exposure2012', value)
                self.assertNotIn('omitted', value)
            else:
                self.assertEqual(set(value), {'omitted', 'sha256', 'keys'})
                self.assertIs(value['omitted'], True)

    def test_five_episode_entrypoints_support_compact_and_complete_evidence(self):
        for include in (False, True):
            with self.subTest(include_snapshots=include):
                # Omitting the flag exercises the MCP defaults, rather than
                # duplicating the handler's explicit false argument.
                projection = {'include_snapshots': True} if include else {}
                state = self.data('create_episode', source_uuid='source', mode='source_edit',
                                  long_edge=256, **projection)
                self.assert_snapshots(state, include)
                state = self.data('get_episode', episode_id=state['id'], **projection)
                self.assert_snapshots(state, include)
                for tool, options in (
                    ('edit_episode', {'settings': {'Exposure2012': .4}}),
                    ('verify_episode_xmp', {'version': 1}),
                    ('restore_episode', {'version': 0}),
                ):
                    revision = state['revision']
                    request_id = str(uuid.uuid4())
                    state = self.data(tool, episode_id=state['id'], base_revision=revision,
                                      request_id=request_id, **options, **projection)
                    self.assert_snapshots(state, include)
                    calls = len(self.lr.calls)
                    replayed = self.data(tool, episode_id=state['id'], base_revision=revision,
                                         request_id=request_id, include_snapshots=not include,
                                         **options)
                    self.assert_snapshots(replayed, not include)
                    self.assertEqual(replayed['revision'], state['revision'])
                    self.assertEqual(len(self.lr.calls), calls)
                    raw = self.server.wb.studio.raw(state['id'])
                    self.assertNotIn('include_snapshots', raw['operations'][request_id]['input'])
                    self.assert_snapshots(raw, True)

    def test_episode_snapshot_schema_points_to_episode_evidence(self):
        tools = {tool['name']: tool for tool in TOOLS}
        for name in ('create_episode', 'get_episode', 'edit_episode',
                     'restore_episode', 'verify_episode_xmp'):
            field = tools['trlr_' + name]['inputSchema']['properties']['include_snapshots']
            self.assertIs(field['default'], False)
            self.assertIn('trlr_get_episode(include_snapshots=true)', field['description'])
            self.assertNotIn('trlr_get_full_settings', field['description'])
        self.assertIn('Session compatibility only', tools['trlr_get_full_settings']['description'])

    def test_compact_mismatch_can_be_retrieved_in_full_without_replaying_edit(self):
        requested = [{'CorrectionAmount': 1, 'MaskingName': 'request' * 60}]
        applied = [{'CorrectionAmount': .4, 'MaskingName': 'readback' * 60}]
        original_run = self.lr.run

        def clamp(command, params=None):
            result = original_run(command, params)
            if command == 'apply_candidate':
                self.lr.settings[params['photo_uuid']]['MaskGroupBasedCorrections'] = applied
            return result

        self.lr.run = clamp
        state = self.data('create_episode', source_uuid='source', mode='source_edit', long_edge=256)
        revision = state['revision']
        request_id = str(uuid.uuid4())
        options = {'episode_id': state['id'], 'base_revision': revision,
                   'request_id': request_id, 'settings': {'MaskGroupBasedCorrections': requested}}
        compact = self.data('edit_episode', **options)
        self.assertIs(compact['versions'][1]['readback_mismatches'][0]['requested']['omitted'], True)
        calls = len(self.lr.calls)
        for full in (self.data('get_episode', episode_id=state['id'], include_snapshots=True),
                     self.data('edit_episode', include_snapshots=True, **options)):
            mismatch = full['versions'][1]['readback_mismatches'][0]
            self.assertEqual(mismatch['requested'], requested)
            self.assertEqual(mismatch['applied'], applied)
            self.assertEqual(full['revision'], compact['revision'])
        self.assertEqual(len(self.lr.calls), calls)

    def test_invalid_crop_returns_tool_error(self):
        s=self.data('create_episode',source_uuid='source',reference_uuid='reference',same_capture=True,long_edge=256)
        r=self.server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'trlr_episode_artifact','arguments':{'episode_id':s['id'],'name':'v0.jpg','crop':[0,0,2,1]}}})
        self.assertTrue(r['result']['isError'])

    def test_presentation_budget_caps_new_episode_render(self):
        self.data('set_presentation_config',edit_render_max_edge=256)
        state=self.data('create_episode',source_uuid='source',reference_uuid='reference',same_capture=True,long_edge=2048)
        self.assertEqual(state['long_edge'],256)
        self.assertEqual(
            [params['long_edge'] for command, params in self.lr.calls
             if command == 'export_preview'],
            [256, 256],
        )

    def test_ui_restore_warning_is_public_without_repeating_the_copy(self):
        self.lr = UiReportingLR(PARTIAL_UI)
        self.server = McpServer(Workbench(Store(Path(self.tmp.name)), self.lr))
        state = self.data('create_episode', source_uuid='source', mode='source_edit', long_edge=256)
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['ui_context'], PARTIAL_UI)
        self.assertEqual(state['versions'][0]['ui_context'], PARTIAL_UI)
        readback = self.data('get_episode', episode_id=state['id'])
        self.assertEqual(readback['ui_context'], PARTIAL_UI)
        self.assertEqual(self.lr.copies, 1)

    def test_malformed_ui_report_does_not_turn_a_copy_into_tool_error(self):
        self.lr = UiReportingLR({'schema_version': 'lr-ui-context-v1', 'status': [], 'parts': {}})
        self.server = McpServer(Workbench(Store(Path(self.tmp.name)), self.lr))
        state = self.data('create_episode', source_uuid='source', mode='source_edit', long_edge=256)
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['versions'][0]['photo_uuid'], 'copy1')
        self.assertEqual(state['ui_context']['status'], 'unavailable')
        self.assertEqual(self.lr.copies, 1)


if __name__=='__main__':unittest.main()
