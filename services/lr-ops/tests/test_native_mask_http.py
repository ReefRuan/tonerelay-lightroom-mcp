from test_lr_ops_server import ServerTestBase


class NativeMaskHttpTests(ServerTestBase):
    def test_invalid_native_requests_and_batches_never_dispatch(self):
        self.assertEqual(self.call('POST', '/jobs', {'command': 'native_mask', 'params': {
            'photo_uuid': 'copy', 'action': 'create', 'mask_type': 'subject'}})[0], 400)
        self.assertEqual(self.call('POST', '/jobs', {'command': 'command_batch', 'params': {'commands': [
            {'command': 'native_mask', 'params': {'photo_uuid': 'copy'}}]}})[0], 400)
        self.assertIsNone(self.call('GET', '/jobs/next')[1])

    def test_native_result_is_preserved_without_sidecar_finalization(self):
        params = {'photo_uuid': 'copy', 'action': 'create', 'mask_type': 'subject',
                  'expected_token': 'native-mask-md5-v1:' + 'a' * 32, 'request_id': 'native-1'}
        status, result = self.call('POST', '/jobs', {'command': 'native_mask', 'params': params})
        self.assertEqual(status, 200)
        job = result['job_id']
        self.assertEqual(self.call('GET', '/jobs/next')[1]['job_id'], job)
        payload = {'ok': True, 'data': {'status': 'created', 'segmentation_data_present': True}}
        self.assertEqual(self.call('POST', '/jobs/' + job + '/result', payload)[0], 200)
        self.assertEqual(self.call('GET', '/jobs/' + job)[1]['result'], payload)
        self.assertEqual(self.call('POST', '/jobs', {'command': 'native_mask', 'params': params})[1]['job_id'], job)
        self.assertIsNone(self.call('GET', '/jobs/next')[1])
