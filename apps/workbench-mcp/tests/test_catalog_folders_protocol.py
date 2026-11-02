import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from workbench.runtime import Workbench
from workbench.state import RuntimeState
from workbench_mcp.server import McpServer, TOOLS

IDENTITY='catalog-md5-v1:0123456789abcdef0123456789abcdef'
FOLDER='folder-md5-v1:aa'


class Bridge:
    base='http://localhost:17777'
    target_id='local'
    catalog_identity=IDENTITY
    access_mode='full'
    def __init__(self):
        self.calls=[]
        self.capable=True
    def run(self,command,params=None,**kwargs):
        self.calls.append((command,params))
        if command=='probe_client':
            return dict(access_mode='full',catalog_identity=IDENTITY,catalog_authorized=True,
                        capabilities=dict(expected_catalog_identity=True,expected_access_mode=True,
                                          develop_snapshot=True,catalog_folders_v1=self.capable))
        assert params['expected_catalog_identity']==IDENTITY
        assert params['expected_access_mode']=='full'
        if command=='list_catalog_folders':
            return dict(folders=[dict(id=FOLDER,label='2026',path_labels=['2026'],
                                     child_count=1,direct_photo_count=0,subtree_photo_count=2)],
                        count=1,total=1,offset=0,limit=params['limit'],has_more=False)
        if command=='list_folder_photos':
            rows=[dict(uuid=f'p{i}',file_name=f'p{i}.jpg',copy_name='',is_virtual_copy=False,
                       width=100,height=100,rating=0,folder_id=FOLDER,flag='pick',label_text='green') for i in range(2)]
            return dict(photos=rows,count=2,complete=True,folders=[dict(id=FOLDER,label='2026',path_labels=['2026'])])
        raise AssertionError(command)


class FolderProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.bridge=Bridge();self.wb=Workbench(RuntimeState(Path(self.tmp.name)),self.bridge)
        self.server=McpServer(self.wb)
    def call(self,name,args):
        return self.server.handle(dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name=name,arguments=args)))['result']
    def payload(self,result):
        self.assertFalse(result.get('isError'),result)
        return json.loads(result['content'][0]['text'])
    def test_real_facade_folder_query_and_cross_instance_cursor(self):
        tree=self.payload(self.call('trlr_list_folders',{'name_contains':'2026'}))
        self.assertEqual(2,tree['folders'][0]['subtree_photo_count'])
        first=self.payload(self.call('trlr_list_folder_photos',dict(folder_ids=[FOLDER],rating=0,limit=1)))
        self.server=McpServer(Workbench(RuntimeState(Path(self.tmp.name)),self.bridge))
        second=self.payload(self.call('trlr_list_folder_photos',dict(cursor=first['next_cursor'])))
        self.assertEqual('p1',second['photos'][0]['uuid'])
        self.assertEqual(1,sum(cmd=='list_folder_photos' for cmd,_ in self.bridge.calls))
    def test_old_plugin_and_invalid_input_are_actionable_errors(self):
        result=self.call('trlr_list_folder_photos',dict(folder_ids=['/private/path']))
        self.assertTrue(result['isError']);self.assertEqual([],self.bridge.calls)
        self.bridge.capable=False
        result=self.call('trlr_list_folders',{})
        self.assertTrue(result['isError'])
        self.assertNotIn('list_catalog_folders',[cmd for cmd,_ in self.bridge.calls])
    def test_batch_preview_keeps_order_and_missing_item_without_export(self):
        path=Path(self.tmp.name)/'thumbnail.jpg';Image.new('RGB',(100,100)).save(path)
        def preview(client,studio,probe,photo_uuid,**kwargs):
            from workbench.lrops import FastThumbnailUnavailable
            if photo_uuid=='missing':
                raise FastThumbnailUnavailable('missing')
            return dict(photo_uuid=photo_uuid,preview_path=str(path),method='requestJpegThumbnail')
        with patch.object(self.wb,'_preview_photo_on_client',side_effect=preview):
            result=self.call('trlr_get_photo_preview',dict(photo_uuids=['p1','missing','p0'],size=512,max_edge=512))
        self.assertFalse(result.get('isError'),result)
        metadata=[json.loads(c['text']) for c in result['content'] if c['type']=='text']
        ids=[m['photo_uuid'] for m in metadata if 'photo_uuid' in m]
        self.assertEqual(['p1','missing','p0'],ids)
        self.assertEqual(2,len([c for c in result['content'] if c['type']=='image']))
        self.assertEqual(['probe_client'],[cmd for cmd,_ in self.bridge.calls])
    def test_preview_validation_preserves_old_single_photo_contract(self):
        for args in (dict(photo_uuid='p',photo_uuids=['p']),dict(photo_uuids=['p','p']),dict(photo_uuids=['p'],size=4096)):
            self.assertTrue(self.call('trlr_get_photo_preview',args)['isError'])
        self.assertEqual([],self.bridge.calls)
        names={t['name'] for t in TOOLS}
        self.assertTrue({'trlr_list_folders','trlr_list_folder_photos','trlr_get_storage_usage'}<=names)
    def test_storage_usage_reads_no_catalog(self):
        result=self.payload(self.call('trlr_get_storage_usage',{}))
        self.assertTrue(result['read_only']);self.assertEqual([],self.bridge.calls)
        self.assertNotIn(str(Path(self.tmp.name)),json.dumps(result))
