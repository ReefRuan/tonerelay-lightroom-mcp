import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from workbench.catalog_folders import CatalogFolders, TTL

A, B = 'folder-md5-v1:aa', 'folder-md5-v1:bb'


def photo(number):
    return dict(uuid=f'p{number}', file_name=f'p{number}.jpg', copy_name='',
                is_virtual_copy=False, width=6000, height=4000, rating=0,
                folder_id=A, flag='pick', label_text='green')


class FolderQueries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope = {'catalog_identity': 'catalog-a', 'access_mode': 'full'}
        self.rows = [photo(i) for i in range(5)]
        self.calls = []
        self.tools = CatalogFolders(Path(self.tmp.name), self.connect)

    def connect(self):
        return self.fake_run, self.scope

    def fake_run(self, command, args):
        self.calls.append((command, args))
        if command == 'list_folder_photos':
            return dict(complete=True, count=len(self.rows), photos=self.rows,
                        folders=[dict(id=A, label='2026', path_labels=['2026'])])
        return dict(folders=[dict(id=A, label='2026', path_labels=['2026'],
                                 direct_photo_count=0, subtree_photo_count=5, child_count=1)],
                    count=1, total=1, limit=args['limit'], offset=0, has_more=False)

    def test_ratings_and_selection_changes_do_not_shrink_later_pages(self):
        first = self.tools.photos(folder_ids=[A], include_subfolders=True, rating=0, limit=2)
        self.rows[:] = []  # Every candidate can disappear from a live rating=0 query.
        second = self.tools.photos(cursor=first['next_cursor'], limit=2)
        third = self.tools.photos(cursor=second['next_cursor'], limit=2)
        self.assertEqual(['p0','p1','p2','p3','p4'],
                         [p['uuid'] for page in (first,second,third) for p in page['photos']])
        self.assertEqual(1, len(self.calls))
        self.assertIsNone(third['next_cursor'])
        self.assertEqual('pick', second['photos'][0]['flag'])
        self.assertEqual('green', second['photos'][0]['label_text'])

    def test_scope_change_and_expiry_reject_cursor(self):
        first = self.tools.photos(folder_ids=[A], limit=1)
        self.scope = {'catalog_identity': 'catalog-b', 'access_mode': 'full'}
        with self.assertRaisesRegex(ValueError, 'another Target'):
            self.tools.photos(cursor=first['next_cursor'])
        self.scope = {'catalog_identity': 'catalog-a', 'access_mode': 'full'}
        with patch('workbench.catalog_folders.time.time', return_value=first['created_at']+TTL+1):
            with self.assertRaisesRegex(ValueError, 'expired'):
                self.tools.photos(cursor=first['next_cursor'])

    def test_input_validation_precedes_catalog_access(self):
        for kwargs in ({'folder_ids':[]}, {'folder_ids':['/private/folder']},
                       {'folder_ids':[A], 'rating':True}, {'cursor':'bad'},
                       {'folder_ids':[A], 'include_subfolders':'yes'}):
            with self.assertRaises(ValueError):
                self.tools.photos(**kwargs)
        self.assertEqual([], self.calls)

    def test_no_filter_changes_on_continuation_and_bounded_snapshots(self):
        first=self.tools.photos(folder_ids=[A], limit=1)
        with self.assertRaisesRegex(ValueError, 'only cursor'):
            self.tools.photos(cursor=first['next_cursor'], rating=0)
        with patch('workbench.catalog_folders.MAX_SNAPSHOTS', 1):
            self.tools.photos(folder_ids=[A])
        with self.assertRaisesRegex(ValueError, 'evicted'):
            self.tools.photos(cursor=first['next_cursor'])

    def test_clean_folder_counts_and_no_paths(self):
        page=self.tools.list(name_contains='2026')
        self.assertEqual(0,page['folders'][0]['direct_photo_count'])
        self.assertEqual(5,page['folders'][0]['subtree_photo_count'])
        self.assertEqual('catalog_photo_records',page['folders'][0]['count_scope'])
        self.rows[0]['file_path']='/private/secret.jpg'
        result=self.tools.photos(folder_ids=[A])
        self.assertNotIn('file_path', result['photos'][0])

    def test_duplicate_or_partial_inventory_is_refused(self):
        self.rows.append(self.rows[0])
        with self.assertRaisesRegex(ValueError,'duplicate'):
            self.tools.photos(folder_ids=[A])

    def test_backend_cannot_return_photos_from_another_folder(self):
        with self.assertRaisesRegex(ValueError,'requested scope'):
            self.tools.photos(folder_ids=[B])

    def test_capacity_eviction_keeps_the_new_query_when_timestamps_tie(self):
        with patch('workbench.catalog_folders.time.time',return_value=100), patch('workbench.catalog_folders.MAX_SNAPSHOTS',1):
            old=self.tools.photos(folder_ids=[A],limit=1)
            new=self.tools.photos(folder_ids=[A],limit=1)
            self.assertEqual('p1',self.tools.photos(cursor=new['next_cursor'])['photos'][0]['uuid'])
            with self.assertRaisesRegex(ValueError,'evicted'):
                self.tools.photos(cursor=old['next_cursor'])
