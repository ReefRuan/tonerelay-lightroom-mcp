import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from lr_ops.bytecode_cache import GenerationCache, MARKER


class BytecodeCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_exact_generation_close_preserves_neighbor_and_legacy(self):
        cache=GenerationCache(self.root, 'a'*32)
        (cache.path/'module.pyc').write_bytes(b'compiled')
        legacy=self.root/('b'*32);legacy.mkdir();(legacy/'keep').write_bytes(b'old')
        self.assertTrue(cache.close())
        self.assertTrue((legacy/'keep').is_file())
        self.assertFalse(cache.close())

    def test_prune_preserves_live_worker_even_if_supervisor_has_exited(self):
        cache=GenerationCache(self.root,'c'*32);cache.attach(12345)
        cache.owner['created_at']=1;cache._save()
        with patch('lr_ops.bytecode_cache.pid_alive',side_effect=lambda pid:pid==12345):
            self.assertEqual(0,GenerationCache.prune(self.root,now=1000000,max_bytes=0))
        self.assertTrue(cache.path.exists())

    def test_age_and_capacity_reclaim_only_owned_inactive_directories(self):
        cache=GenerationCache(self.root,'d'*32)
        (cache.path/'module.pyc').write_bytes(b'x'*64)
        with patch('lr_ops.bytecode_cache.pid_alive',return_value=False):
            self.assertEqual(1,GenerationCache.prune(self.root,max_bytes=0))
        legacy=self.root/('e'*32);legacy.mkdir()
        with patch('lr_ops.bytecode_cache.pid_alive',return_value=False):
            self.assertEqual(0,GenerationCache.prune(self.root,now=1000000,max_bytes=0))

    def test_invalid_ownership_and_live_supervisor_are_preserved(self):
        cache=GenerationCache(self.root,'f'*32)
        with patch('lr_ops.bytecode_cache.pid_alive',return_value=True):
            self.assertEqual(0,GenerationCache.prune(self.root,now=10**12,max_bytes=0))
        (cache.path/MARKER).write_text('{}')
        self.assertFalse(cache.close())

    def test_symlink_escape_is_not_removed(self):
        cache=GenerationCache(self.root,'1'*32)
        outside=self.root/'outside';outside.mkdir();(outside/'keep').write_text('keep')
        try:
            (cache.path/'escape').symlink_to(outside,target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        self.assertFalse(cache.close())
        self.assertEqual('keep',(outside/'keep').read_text())
