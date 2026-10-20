"""Episode labels use the existing metadata transaction and never migrate reads."""
import copy
import tempfile
import unittest
import uuid
from pathlib import Path

from workbench.studio.engine import Studio, Conflict, atomic
from test_studio import FakeLR


class EpisodeLabelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lr = FakeLR()
        self.studio = Studio(self.root, self.lr)
        self.state = self.studio.create('source', mode='source_edit', long_edge=256,
                                        label='Portrait · 色彩')

    def message(self, **options):
        self.state = self.studio.action(self.state['id'], 'message', self.state['revision'],
                                       str(uuid.uuid4()), **options)
        return self.state

    def test_create_and_read_label_in_full_compact_and_summary(self):
        for include in (True, False):
            self.assertEqual(self.studio.get(self.state['id'], include_snapshots=include)['label'],
                             'Portrait · 色彩')
        self.assertEqual(self.studio.list()[0]['label'], 'Portrait · 色彩')
        boundary = self.studio.create('source', mode='source_edit', long_edge=256, label='色' * 120)
        self.assertEqual(boundary['label'], '色' * 120)
        default = self.studio.create('source', mode='source_edit', long_edge=256)
        self.assertEqual(default['label'], '')

    def test_invalid_create_label_has_no_lightroom_or_storage_side_effect(self):
        before = list(self.studio.root.iterdir())
        calls = copy.deepcopy(self.lr.calls)
        for label in ('x' * 121, None, 1, [], True):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, 'label'):
                self.studio.create('source', mode='source_edit', label=label)
        self.assertEqual(list(self.studio.root.iterdir()), before)
        self.assertEqual(self.lr.calls, calls)

    def test_label_only_update_and_clear_preserve_messages_versions_and_photos(self):
        before = copy.deepcopy(self.state)
        calls = copy.deepcopy(self.lr.calls)
        photos = copy.deepcopy(self.lr.settings)
        self.message(label='Updated')
        self.assertEqual(self.state['label'], 'Updated')
        self.assertEqual(self.state['messages'], before['messages'])
        self.assertEqual(self.state['versions'], before['versions'])
        self.assertGreater(self.state['revision'], before['revision'])
        self.message(label='')
        self.assertEqual(self.state['label'], '')
        self.assertEqual(self.studio.list()[0]['label'], '')
        self.assertEqual(self.lr.calls, calls)
        self.assertEqual(self.lr.settings, photos)

    def test_label_and_message_update_share_one_idempotent_operation(self):
        revision, request_id = self.state['revision'], str(uuid.uuid4())
        options = {'text': 'Keep this context', 'role': 'assistant', 'label': 'Saved'}
        state = self.studio.action(self.state['id'], 'message', revision, request_id, **options)
        self.assertEqual(state['label'], 'Saved')
        self.assertEqual(state['messages'][-1]['text'], options['text'])
        duplicate = self.studio.action(self.state['id'], 'message', revision, request_id, **options)
        self.assertEqual(duplicate, state)
        self.assertEqual(len(duplicate['messages']), len(self.state['messages']) + 1)
        with self.assertRaises(Conflict):
            self.studio.action(self.state['id'], 'message', revision, request_id, label='Other')
        with self.assertRaises(Conflict):
            self.studio.action(self.state['id'], 'message', revision, str(uuid.uuid4()), label='Other')
        self.assertEqual(self.studio.get(self.state['id']), state)

    def test_invalid_label_or_message_does_not_change_persisted_state(self):
        path = self.studio.directory(self.state['id']) / 'episode.json'
        before = path.read_bytes()
        for options in ({'label': 'x' * 121}, {'label': None}, {'label': 1}, {},
                        {'label': 'Valid', 'text': ''}, {'label': 'Valid', 'role': 'invalid'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.message(**options)
        self.assertEqual(path.read_bytes(), before)

    def test_update_retains_existing_exclusive_lock(self):
        before = self.studio.raw(self.state['id'])
        with self.studio.lock(self.state['id']):
            with self.assertRaises(Conflict):
                self.message(label='Busy')
        self.assertEqual(self.studio.raw(self.state['id']), before)

    def test_old_episode_label_fallback_does_not_migrate_storage(self):
        raw = self.studio.raw(self.state['id'])
        raw.pop('label')
        path = self.studio.directory(raw['id']) / 'episode.json'
        atomic(path, raw)
        before = path.read_bytes()
        self.assertEqual(self.studio.get(raw['id'])['label'], '')
        self.assertEqual(self.studio.get(raw['id'], include_snapshots=False)['label'], '')
        self.assertEqual(self.studio.list()[0]['label'], '')
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('label', self.studio.raw(raw['id']))
        self.message(label='Explicit update')
        self.assertEqual(self.studio.raw(raw['id'])['label'], 'Explicit update')


if __name__ == '__main__':
    unittest.main()
