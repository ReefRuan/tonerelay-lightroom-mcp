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
from workbench.runtime import Workbench
from workbench.store import Store
from workbench_mcp.server import McpServer


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
        self.assertEqual(image['content'][1]['type'],'image')
        s=self.data('verify_episode_xmp',episode_id=sid,base_revision=s['revision'],request_id=str(uuid.uuid4()),version=1)
        xmp=self.call('episode_artifact',episode_id=sid,name='v1.xmp')['content'][0]['text']
        self.assertIn('Exposure2012',xmp)
        s=self.data('restore_episode',episode_id=sid,base_revision=s['revision'],request_id=str(uuid.uuid4()),version=0)
        self.assertEqual(s['current_version'],0)
        self.assertEqual(len(self.data('list_episodes')),1)

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


if __name__=='__main__':unittest.main()
