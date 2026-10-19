import json
import uuid
from pathlib import Path

from test_lr_ops_server import ServerTestBase
from lr_ops.sidecar import CRS, RDF


class SidecarHttpTests(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.raw = Path(self.tmp.name) / 'image.ARW'; self.raw.write_bytes(b'RAW')
        self.xmp = self.raw.with_suffix('.xmp')
        self.xmp.write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="' + RDF + '" xmlns:crs="' + CRS + '">'
                            '<rdf:RDF><rdf:Description rdf:about="" crs:Exposure2012="0"/></rdf:RDF></x:xmpmeta>')
        self.context = {'photo_uuid': 'master', 'source_path': str(self.raw), 'is_virtual_copy': False, 'develop_settings': {'Exposure2012': 0}}

    def finish(self, command, params):
        status, result = self.call('POST', '/jobs', {'command': command, 'params': params})
        self.assertEqual(status, 200)
        job = result['job_id']
        self.assertEqual(self.call('GET', '/jobs/next')[1]['job_id'], job)
        self.assertEqual(self.call('POST', '/jobs/' + job + '/result', {'ok': True, 'data': {'_sidecar_context': self.context}})[0], 200)
        return job, self.call('GET', '/jobs/' + job)[1]['result']['data']

    def test_uuid_resolved_write_and_duplicate_http_result_are_safe(self):
        _, inspected = self.finish('update_xmp_sidecar', {'photo_uuid': 'master'})
        params = {'photo_uuid': 'master', 'action': 'update', 'confirm_xmp_mode': True, 'request_id': str(uuid.uuid4()),
                  'expected_token': inspected['expected_token'], 'settings': {'Exposure2012': .5}}
        job, result = self.finish('update_xmp_sidecar', params)
        self.assertTrue(result['sidecar_verified']); self.assertFalse(result['catalog_applied'])
        self.assertNotIn(str(self.raw), json.dumps(result))
        output = self.xmp.read_bytes()
        duplicate = self.call('POST', '/jobs/' + job + '/result', {'ok': True, 'data': {'_sidecar_context': self.context}})
        self.assertEqual(duplicate[0], 409); self.assertEqual(output, self.xmp.read_bytes())
        self.assertEqual(self.call('POST', '/jobs', {'command': 'update_xmp_sidecar', 'params': params})[1]['job_id'], job)
        self.assertIsNone(self.call('GET', '/jobs/next')[1])

    def test_path_arguments_and_batches_refused_before_plugin_dispatch(self):
        self.assertEqual(self.call('POST', '/jobs', {'command': 'apply_mask', 'params': {'photo_uuid': 'master', 'path': str(self.raw)}})[0], 400)
        self.assertEqual(self.call('POST', '/jobs', {'command': 'command_batch', 'params': {'commands': [
            {'command': 'apply_mask', 'params': {'photo_uuid': 'master'}}]}})[0], 400)
        self.assertIsNone(self.call('GET', '/jobs/next')[1])
