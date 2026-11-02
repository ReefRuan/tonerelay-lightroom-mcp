from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from workbench.storage_usage import storage_usage


class StorageUsageTests(unittest.TestCase):
    def test_classification_no_deletion_and_partial_counts_are_not_zero_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cache=root/'cache';cache.mkdir();(cache/'x.pyc').write_bytes(b'abc')
            history=root/'episodes';history.mkdir();(history/'v0.jpg').write_bytes(b'photo')
            config=Mock();config.paths.return_value={'cache':cache,'data':root/'data'}
            result=storage_usage(root,config)
            self.assertEqual(3,result['categories'][0]['bytes'])
            self.assertTrue((history/'v0.jpg').is_file())
            self.assertEqual('durable_history',next(r for r in result['categories'] if r['category']=='episodes')['kind'])
            with patch('workbench.storage_usage.time.monotonic',side_effect=[0]+[10]*100):
                result=storage_usage(root,config,budget_seconds=1)
            self.assertFalse(result['complete'])
            self.assertTrue(all(r['bytes'] is None for r in result['categories']))
