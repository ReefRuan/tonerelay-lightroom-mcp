"""Replacement contracts for live reads, saved diffs and isolated replay."""
import copy
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from workbench.studio.engine import Studio, Conflict, atomic, fingerprint
from test_studio import FakeLR, CATALOG_IDENTITY


class PhotoSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lr = FakeLR()
        self.studio = Studio(self.root, self.lr)

    def test_full_live_read_includes_structured_values_without_rendering(self):
        settings = self.lr.settings['source']
        settings.update(GrainAmount=34, MaskGroupBasedCorrections=[{'Amount': .4}])
        result = self.studio.photo_settings('source')
        self.assertEqual(result['settings'], settings)
        self.assertEqual(result['settings_sha256'], fingerprint(settings))
        self.assertEqual(result['source']['catalog_identity'], CATALOG_IDENTITY)
        self.assertEqual(result['evidence']['evidence_status'], 'stable_settings_readback')
        self.assertEqual(result['evidence']['color_evidence'], 'not_applicable')
        self.assertEqual(result['evidence']['freshness'], 'verified')
        self.assertEqual(result['xmp_verification'], 'unverified_serialization')
        self.assertIn('MaskGroupBasedCorrections', result['skipped_structured_keys'])
        self.assertIn('GrainAmount', result['written_keys'])
        self.assertEqual([c for c, _ in self.lr.calls],
                         ['probe_client', 'get_develop_snapshot', 'get_develop_snapshot'])
        for command, params in self.lr.calls[1:]:
            self.assertEqual(params['expected_catalog_identity'], CATALOG_IDENTITY)
        self.assertEqual(self.lr.copies, 0)
        self.assertEqual(list((self.root / 'episodes').iterdir()), [])

    def test_json_and_xmp_selection(self):
        self.assertNotIn('xmp', self.studio.photo_settings('source', 'json'))
        self.assertNotIn('settings', self.studio.photo_settings('source', 'xmp'))

    def test_pins_router_once_for_entire_observation(self):
        outer = self
        class Router:
            pins = 0
            def pin(self):
                self.pins += 1
                return outer.lr
            def run(self, *args, **kwargs):
                raise AssertionError('Unpinned router call')
        router = Router()
        result = Studio(self.root, router).photo_settings('source', 'json')
        self.assertEqual(router.pins, 1)
        self.assertEqual(result['settings'], self.lr.settings['source'])

    def test_invalid_requests_rejected_before_lightroom(self):
        for photo, format in [('', 'json'), (None, 'json'), ('source', 'invalid')]:
            with self.subTest(photo=photo, format=format), self.assertRaises(ValueError):
                self.studio.photo_settings(photo, format)
        self.assertEqual(self.lr.calls, [])

    def test_settings_drift_is_not_reported_as_verified(self):
        original = self.lr.run
        def changing(command, params=None):
            result = original(command, params)
            if command == 'get_develop_snapshot':
                self.lr.settings['source']['Exposure2012'] += .1
            return result
        self.lr.run = changing
        with self.assertRaisesRegex(Conflict, 'changed during readback'):
            self.studio.photo_settings('source')

    def test_catalog_drift_between_reads_is_rejected(self):
        original = self.lr.run
        def changing(command, params=None):
            result = original(command, params)
            if command == 'get_develop_snapshot':
                self.lr.active_catalog_identity = 'catalog-md5-v1:' + 'f' * 32
            return result
        self.lr.run = changing
        with self.assertRaisesRegex(RuntimeError, 'catalog identity mismatch'):
            self.studio.photo_settings('source')

    def test_wrong_photo_or_missing_full_settings_fails_closed(self):
        original = self.lr.run
        for invalid in ({'photo': {'uuid': 'source'}},
                        {'photo': {'uuid': 'other'}, 'settings': {}},
                        {'photo': {'uuid': 'source'}, 'settings': []}):
            def malformed(command, params=None):
                return invalid if command == 'get_develop_snapshot' else original(command, params)
            self.lr.run = malformed
            with self.assertRaisesRegex(ValueError, 'Invalid full settings'):
                self.studio.photo_settings('source')

    def test_catalog_authorization_failure_prevents_reads(self):
        original = self.lr.run
        def denied(command, params=None):
            result = original(command, params)
            if command == 'probe_client':
                result['catalog_authorized'] = False
            return result
        self.lr.run = denied
        with self.assertRaises(ValueError):
            self.studio.photo_settings('source')
        self.assertEqual([c for c, _ in self.lr.calls], ['probe_client'])


class EpisodeReplacementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lr = FakeLR()
        self.studio = Studio(self.root, self.lr)
        self.state = self.studio.create('source', mode='source_edit', long_edge=256)

    def action(self, action, **options):
        self.state = self.studio.action(self.state['id'], action,
                                       self.state['revision'], str(uuid.uuid4()), **options)
        return self.state

    def test_diff_uses_saved_submitted_and_actual_values_offline(self):
        original = self.lr.run
        def clamped(command, params=None):
            result = original(command, params)
            if command == 'apply_candidate':
                self.lr.settings[params['photo_uuid']]['GrainAmount'] = 20
            return result
        self.lr.run = clamped
        self.action('edit', settings={'GrainAmount': 50, 'Exposure2012': .2})
        before = (self.studio.directory(self.state['id']) / 'episode.json').read_bytes()
        calls = len(self.lr.calls)
        result = self.studio.version_diff(self.state['id'], 0, 1)
        self.assertEqual(result['observed_revision'], self.state['revision'])
        self.assertEqual(result['revision'], result['observed_revision'])
        self.assertEqual(len(self.lr.calls), calls)
        self.assertEqual(result['submitted_settings_diff']['added']['GrainAmount'], 50)
        self.assertEqual(result['after_readback_diff']['added']['GrainAmount'], 20)
        self.assertEqual(result['after_readback_diff']['changed']['Exposure2012'], [0, .2])
        self.assertEqual(result['readback_mismatches_b'][0]['requested'], 50)
        self.assertEqual(result['readback_mismatches_b'][0]['applied'], 20)
        self.assertEqual((self.studio.directory(self.state['id']) / 'episode.json').read_bytes(), before)
        reverse = self.studio.version_diff(self.state['id'], 1, 0)
        self.assertEqual(reverse['submitted_settings_diff']['removed']['GrainAmount'], 50)
        self.assertEqual(reverse['after_readback_diff']['changed']['Exposure2012'], [.2, 0])

    def test_diff_handles_nested_values_and_missing_historical_readback(self):
        raw = self.studio.raw(self.state['id'])
        other = copy.deepcopy(raw['versions'][0])
        other.update(version=1, patch={'Mask': [{'Amount': .3}]},
                     snapshot={'Mask': [{'Amount': .2}]})
        raw['versions'].append(other)
        atomic(self.studio.directory(raw['id']) / 'episode.json', raw)
        result = self.studio.version_diff(raw['id'], 0, 1)
        self.assertEqual(result['submitted_settings_diff']['added']['Mask'], [{'Amount': .3}])
        other.pop('snapshot')
        atomic(self.studio.directory(raw['id']) / 'episode.json', raw)
        missing = self.studio.version_diff(raw['id'], 0, 1)
        self.assertIsNone(missing['after_readback_diff'])
        self.assertEqual(missing['readback_available'], {'a': True, 'b': False})

    def test_diff_version_boundaries_and_busy_lock(self):
        for version in (-1, 1, True, 0.0, '0', None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.studio.version_diff(self.state['id'], 0, version)
        with self.studio.lock(self.state['id']):
            before = self.studio.raw(self.state['id'])
            result = self.studio.version_diff(self.state['id'], 0, 0)
            self.assertEqual(result['observed_revision'], before['revision'])
            self.assertEqual(result['submitted_settings_diff'],
                             {'changed': {}, 'added': {}, 'removed': {}})
            self.assertEqual(self.studio.raw(self.state['id']), before)

    def test_diff_reads_complete_persisted_snapshot_during_active_edit(self):
        self.lr.entered, self.lr.release = threading.Event(), threading.Event()
        self.addCleanup(self.lr.release.set)
        failures = []

        def edit():
            try:
                self.action('edit', settings={'Exposure2012': .5})
            except Exception as exc:
                failures.append(exc)

        writer = threading.Thread(target=edit)
        writer.start()
        try:
            self.assertTrue(self.lr.entered.wait(3), 'writer did not reach render')
            path = self.studio.directory(self.state['id']) / 'episode.json'
            persisted = path.read_bytes()
            observed = self.studio.raw(self.state['id'])
            self.assertEqual(observed['status'], 'editing')
            calls = len(self.lr.calls)
            result = Studio(self.root, self.lr).version_diff(self.state['id'], 0, 0)
            self.assertEqual(result['observed_revision'], observed['revision'])
            self.assertEqual(result['version_a'], 0)
            self.assertEqual(len(self.lr.calls), calls)
            self.assertEqual(path.read_bytes(), persisted)
            # No incomplete pending version is exposed by the read snapshot.
            with self.assertRaisesRegex(ValueError, 'Unknown version'):
                self.studio.version_diff(self.state['id'], 0, 1)
        finally:
            self.lr.release.set()
            writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        latest = self.studio.version_diff(self.state['id'], 0, 1)
        self.assertGreater(latest['observed_revision'], result['observed_revision'])
        self.assertEqual(latest['submitted_settings_diff']['added']['Exposure2012'], .5)

    def test_replay_replaces_cumulative_patch_and_preserves_all_old_copies(self):
        master = copy.deepcopy(self.lr.settings['source'])
        self.action('edit', settings={'Exposure2012': .4})
        replay_source = copy.deepcopy(self.state['versions'][1])
        self.action('edit', settings={'Contrast2012': 20})
        old = copy.deepcopy(self.state['versions'])
        start = len(self.lr.calls)
        self.action('replay', version=1, note='repeat saved parameters')
        replay = self.state['versions'][-1]
        self.assertEqual(replay['patch'], {'Exposure2012': .4})
        self.assertEqual(replay['snapshot'], replay_source['snapshot'])
        self.assertEqual(replay['parent_version'], 2)
        self.assertEqual(replay['replayed_from_version'], 1)
        self.assertEqual(self.state['versions'][:-1], old)
        self.assertEqual(self.lr.settings['source'], master)
        self.assertEqual(len({v['photo_uuid'] for v in self.state['versions']}), 4)
        for command, params in self.lr.calls[start:]:
            self.assertEqual(params['expected_catalog_identity'], CATALOG_IDENTITY)
        for command, params in self.lr.calls:
            if command == 'apply_candidate':
                self.assertTrue(params['photo_uuid'].startswith('copy'))
        self.action('replay', version=0)
        self.assertEqual(self.state['versions'][-1]['patch'], {})
        self.assertEqual(self.state['versions'][-1]['snapshot'], master)

    def test_replay_idempotency_conflict_and_stale_revision(self):
        revision, request = self.state['revision'], str(uuid.uuid4())
        state = self.studio.action(self.state['id'], 'replay', revision, request, version=0)
        calls = len(self.lr.calls)
        repeated = self.studio.action(self.state['id'], 'replay', revision, request, version=0)
        self.assertEqual(state, repeated)
        self.assertEqual(len(self.lr.calls), calls)
        with self.assertRaises(Conflict):
            self.studio.action(self.state['id'], 'replay', revision, request, version=1)
        with self.assertRaises(Conflict):
            self.studio.action(self.state['id'], 'replay', revision, str(uuid.uuid4()), version=0)

    def test_replay_retains_readback_mismatches_as_facts(self):
        original = self.lr.run
        def clamped(command, params=None):
            result = original(command, params)
            if command == 'apply_candidate':
                self.lr.settings[params['photo_uuid']]['GrainAmount'] = 20
            return result
        self.lr.run = clamped
        self.action('edit', settings={'GrainAmount': 50})
        self.action('replay', version=1)
        replay = self.state['versions'][-1]
        self.assertEqual(replay['patch']['GrainAmount'], 50)
        self.assertEqual(replay['snapshot']['GrainAmount'], 20)
        self.assertEqual(replay['readback_mismatches'][0]['applied'], 20)

    def test_replay_uses_episode_lock_and_rejects_target_or_renderer_changes(self):
        with self.studio.lock(self.state['id']):
            with self.assertRaises(Conflict):
                self.action('replay', version=0)
        state = self.studio.raw(self.state['id'])
        state['target_id'] = 'original'
        atomic(self.studio.directory(state['id']) / 'episode.json', state)
        self.lr.target_id = 'other'
        copies = self.lr.copies
        with self.assertRaisesRegex(ValueError, '另一台'):
            self.action('replay', version=0)
        self.lr.target_id = 'original'
        state['renderer']['lr_version'] = 'another-renderer'
        atomic(self.studio.directory(state['id']) / 'episode.json', state)
        with self.assertRaisesRegex(Conflict, 'Renderer version changed'):
            self.action('replay', version=0)
        self.assertEqual(self.lr.copies, copies)

    def test_persisted_baseline_render_replays_without_lightroom(self):
        calls = len(self.lr.calls)
        self.lr.fail = 'probe_client'
        path = self.studio.artifact(self.state['id'], 'v0.jpg')
        self.assertEqual(len(self.lr.calls), calls)
        self.assertTrue(path.is_file())
        path.write_bytes(b'tampered baseline')
        with self.assertRaisesRegex(Conflict, 'render was modified'):
            self.studio.artifact(self.state['id'], 'v0.jpg')
        self.assertEqual(len(self.lr.calls), calls)

    def test_artifact_payload_rechecks_the_exact_bytes_for_delivery(self):
        original = self.studio.artifact
        def replace_after_initial_check(sid, name):
            path = original(sid, name)
            path.write_bytes(b'changed after path validation')
            return path
        with patch.object(self.studio, 'artifact', replace_after_initial_check):
            with self.assertRaisesRegex(Conflict, 'changed before delivery'):
                self.studio.artifact_payload(self.state['id'], 'v0.jpg')

    def test_artifact_payload_is_a_stable_snapshot_and_rejects_symlinks(self):
        path, content = self.studio.artifact_payload(self.state['id'], 'v0.jpg')
        path.write_bytes(b'changed after byte capture')
        self.assertNotEqual(path.read_bytes(), content)
        path.unlink()
        path.symlink_to('reference.jpg')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.studio.artifact_payload(self.state['id'], 'v0.jpg')

    def test_replay_invalid_version_or_note_does_not_poison_state(self):
        before = self.studio.raw(self.state['id'])
        for options in ({}, {'version': True}, {'version': -1}, {'version': 99},
                        {'version': 0.0}, {'version': 0, 'note': None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.action('replay', **options)
        self.assertEqual(self.studio.raw(self.state['id']), before)

    def test_replay_failed_write_is_not_replayed_or_published(self):
        self.action('edit', settings={'Exposure2012': .4})
        revision, request = self.state['revision'], str(uuid.uuid4())
        self.lr.fail = 'apply_candidate'
        with self.assertRaises(RuntimeError):
            self.studio.action(self.state['id'], 'replay', revision, request, version=1)
        failed = self.studio.get(self.state['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(len(failed['versions']), 2)
        self.assertIn('pending_copy_uuid', failed)
        calls = len(self.lr.calls)
        duplicate = self.studio.action(self.state['id'], 'replay', revision, request, version=1)
        self.assertEqual(duplicate, failed)
        self.assertEqual(len(self.lr.calls), calls)
        with self.assertRaises(Conflict):
            self.studio.action(self.state['id'], 'replay', failed['revision'],
                               str(uuid.uuid4()), version=1)

    def test_replay_detects_source_version_and_preview_drift_before_copy(self):
        for drift in ('source', 'version', 'preview', 'catalog'):
            with self.subTest(drift=drift):
                lr = FakeLR()
                studio = Studio(self.root / drift, lr)
                state = studio.create('source', mode='source_edit', long_edge=256)
                copies = lr.copies
                if drift == 'source':
                    lr.settings['source']['Exposure2012'] = 1
                elif drift == 'version':
                    lr.settings[state['versions'][0]['photo_uuid']]['Exposure2012'] = 1
                elif drift == 'preview':
                    (studio.directory(state['id']) / 'v0.jpg').write_bytes(b'tampered')
                else:
                    lr.active_catalog_identity = 'catalog-md5-v1:' + 'f' * 32
                with self.assertRaises((ValueError, RuntimeError)):
                    studio.action(state['id'], 'replay', state['revision'],
                                  str(uuid.uuid4()), version=0)
                self.assertEqual(lr.copies, copies)

    def test_interrupted_replay_is_detected_and_not_restarted(self):
        self.state['status'] = 'replaying'
        self.studio.save(self.state)
        calls = len(self.lr.calls)
        result = Studio(self.root, self.lr).get(self.state['id'])
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(len(self.lr.calls), calls)

    def test_orphaned_active_write_cannot_resume_through_offline_diff_revision(self):
        for status in ('replaying', 'editing', 'rendering_parameter_variants'):
            with self.subTest(status=status):
                old_request = str(uuid.uuid4())
                state = self.studio.raw(self.state['id'])
                state['status'] = status
                state['operations'][old_request] = {
                    'input': {'action': 'replay', 'version': 0}, 'status': 'running'}
                self.studio.save(state)
                revision = self.studio.version_diff(state['id'], 0, 0)['revision']
                calls = len(self.lr.calls)
                with self.assertRaises(Conflict):
                    self.studio.action(state['id'], 'replay', revision,
                                       str(uuid.uuid4()), version=0)
                duplicate = self.studio.action(state['id'], 'replay', revision,
                                              old_request, version=0)
                self.assertEqual(duplicate['status'], status)
                self.assertEqual(len(self.lr.calls), calls)


if __name__ == '__main__':
    unittest.main()
