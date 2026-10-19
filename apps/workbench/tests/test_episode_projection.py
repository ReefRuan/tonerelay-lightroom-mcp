"""Compact Episode projections preserve the original execution evidence."""
import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from test_studio import FakeLR
from workbench.studio.engine import Studio, fingerprint, mismatch_value, public_state


class EpisodeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lr = FakeLR()
        self.studio = Studio(Path(self.tmp.name), self.lr)
        self.state = self.studio.create('source', mode='source_edit', long_edge=256)

    def test_compact_mismatches_hash_original_values_and_are_idempotent(self):
        requested = [{'CorrectionAmount': 1, 'MaskingName': 'request' * 60}]
        applied = [{'CorrectionAmount': .4, 'MaskingName': 'readback' * 60}]
        state = copy.deepcopy(self.state)
        state['versions'][0]['readback_mismatches'] = [
            {'key': 'MaskGroupBasedCorrections', 'requested': requested,
             'applied': applied, 'reason': 'clamped'},
            {'key': 'Texture', 'requested': 1, 'applied': None, 'reason': 'ignored'},
        ]
        original = copy.deepcopy(state)

        compact = public_state(state, include_snapshots=False)
        mismatch = compact['versions'][0]['readback_mismatches'][0]
        self.assertEqual(mismatch['requested']['sha256'], fingerprint(requested))
        self.assertEqual(mismatch['applied']['sha256'], fingerprint(applied))
        self.assertEqual(mismatch['requested']['preview'],
                         json.dumps(requested, ensure_ascii=False)[:200])
        self.assertEqual(compact['versions'][0]['readback_mismatches'][1],
                         state['versions'][0]['readback_mismatches'][1])
        self.assertEqual(public_state(compact, include_snapshots=False), compact)
        self.assertEqual(public_state(state, include_snapshots=True), original)
        self.assertEqual(state, original)

    def test_historical_summaries_are_preserved_without_claiming_recovery(self):
        original = [{'MaskingName': 'historical value' * 40}]
        summary = mismatch_value(original)
        self.assertEqual(mismatch_value(summary), summary)
        state = copy.deepcopy(self.state)
        state['versions'][0]['readback_mismatches'] = [
            {'key': 'MaskGroupBasedCorrections', 'requested': summary,
             'applied': None, 'reason': 'ignored'}]
        self.studio.save(state)
        episode = self.studio.directory(state['id']) / 'episode.json'
        before = episode.read_bytes()
        for include in (False, True, False):
            with self.subTest(include_snapshots=include):
                readback = self.studio.get(state['id'], include_snapshots=include)
                self.assertEqual(readback['versions'][0]['readback_mismatches'][0]
                                 ['requested'], summary)
        self.assertEqual(episode.read_bytes(), before)

    def test_render_persists_raw_mismatch_and_complete_reads_retrieve_it(self):
        requested = [{'CorrectionAmount': 1, 'MaskingName': 'request' * 60}]
        applied = [{'CorrectionAmount': .4, 'MaskingName': 'readback' * 60}]
        original_run = self.lr.run

        def clamp(command, params=None):
            result = original_run(command, params)
            if command == 'apply_candidate':
                self.lr.settings[params['photo_uuid']]['MaskGroupBasedCorrections'] = (
                    copy.deepcopy(applied))
            return result

        self.lr.run = clamp
        compact = self.studio.action(
            self.state['id'], 'edit', self.state['revision'], str(uuid.uuid4()),
            settings={'MaskGroupBasedCorrections': requested}, include_snapshots=False)
        expected = {'key': 'MaskGroupBasedCorrections', 'requested': requested,
                    'applied': applied, 'reason': 'Lightroom 没有原样回读（夹取或忽略）'}
        raw = self.studio.raw(self.state['id'])
        self.assertEqual(raw['versions'][1]['readback_mismatches'], [expected])
        self.assertEqual(raw['versions'][1]['snapshot']['MaskGroupBasedCorrections'],
                         applied)
        self.assertEqual(compact['versions'][1]['readback_mismatches'][0]
                         ['requested']['sha256'], fingerprint(requested))
        self.assertEqual(compact['versions'][1]['readback_mismatches'][0]
                         ['applied']['sha256'], fingerprint(applied))

        episode = self.studio.directory(self.state['id']) / 'episode.json'
        before = episode.read_bytes()
        calls = len(self.lr.calls)
        full = self.studio.get(self.state['id'], include_snapshots=True)
        self.assertEqual(full['versions'][1]['readback_mismatches'], [expected])
        self.assertEqual(full['versions'][1]['snapshot'], raw['versions'][1]['snapshot'])
        self.assertEqual(self.studio.get(self.state['id'], include_snapshots=False), compact)
        self.assertEqual(episode.read_bytes(), before)
        self.assertEqual(len(self.lr.calls), calls)

    def test_legacy_parameter_probe_uses_readback_before_snapshot_omission(self):
        state = copy.deepcopy(self.state)
        version = state['versions'][0]
        version['snapshot']['Exposure2012'] = .4
        version['parameter_probe'] = {'parameter': 'Exposure2012', 'value': 1,
                                      'base_version': 0}
        original = copy.deepcopy(state)
        for include in (False, True):
            with self.subTest(include_snapshots=include):
                projected = public_state(state, include_snapshots=include)
                variant = projected['versions'][0]['parameter_variant']
                self.assertEqual(variant['requested_value'], 1)
                self.assertEqual(variant['applied_value'], .4)
        self.assertEqual(state, original)

    def test_legacy_missing_readback_never_substitutes_requested_value(self):
        state = copy.deepcopy(self.state)
        version = state['versions'][0]
        version['snapshot'].pop('Exposure2012')
        version['parameter_probe'] = {'parameter': 'Exposure2012', 'value': 1,
                                      'base_version': 0}
        self.studio.save(state)
        for include in (False, True):
            with self.subTest(include_snapshots=include):
                projected = self.studio.get(state['id'], include_snapshots=include)
                self.assertNotIn('applied_value', projected['versions'][0]['parameter_variant'])
        result = self.studio.list_parameter_variant_results()['results'][0]
        self.assertEqual(result['requested_value'], 1)
        self.assertIsNone(result['applied_value'])


if __name__ == '__main__':
    unittest.main()
