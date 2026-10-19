import tempfile
import unittest
from pathlib import Path
from lr_ops.native_mask import validate_request
from lr_ops.server import JobQueue


class NativeMaskTests(unittest.TestCase):
    def params(self, **patch):
        return {'photo_uuid': 'copy', 'action': 'create', 'mask_type': 'subject',
                'request_id': 'native-1', 'expected_token': 'native-mask-md5-v1:' + 'a' * 32, **patch}

    def test_validation(self):
        validate_request({'photo_uuid': 'copy'})
        validate_request(self.params(settings={'LocalExposure2012': .1}))
        validate_request(self.params(action='update', correction_id='mask', settings={'LocalTint': .1}))
        for patch in [{'settings': {'Exposure2012': 1}}, {'settings': {'LocalExposure2012': True}},
                      {'settings': {'LocalTint': float('inf')}}, {'mask_type': 'objects'},
                      {'expected_token': 'old'}, {'action': 'update'}, {'path': '/private.ARW'}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError): validate_request(self.params(**patch))

    def test_durable_dedup_and_unknown_dispatch_are_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp)
            queue = JobQueue(log_dir=journal)
            params = self.params()
            job = queue.submit('native_mask', params)
            self.assertEqual(queue.submit('native_mask', params), job)
            with self.assertRaises(ValueError): queue.submit('native_mask', self.params(mask_type='sky'))
            queue.next_queued()
            recovered = JobQueue(log_dir=journal)
            self.assertEqual(recovered.submit('native_mask', params), job)
            self.assertIsNone(recovered.next_queued())


if __name__ == '__main__': unittest.main()
