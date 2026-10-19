"""Parameter-reference and fixed-base parameter-variant execution contracts."""
import copy
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from test_studio import FakeLR
from workbench.studio.engine import Conflict, Studio
from workbench.studio.parameter_specs import parameter_records
from workbench.studio.settings import BOUNDS, CURVES


class ParameterReferenceTests(unittest.TestCase):
    def test_reference_matches_execution_contract(self):
        items = parameter_records()
        self.assertEqual({row['id'] for row in items}, set(BOUNDS) | set(CURVES))
        self.assertEqual(len(items), len({row['id'] for row in items}))
        for row in items:
            if row['id'] in BOUNDS:
                self.assertEqual(row['bounds'], list(BOUNDS[row['id']]))
            self.assertNotIn('review_status', row)
            self.assertEqual(row['evidence_status'], 'project_contract')


class ParameterVariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lr = FakeLR()
        self.studio = Studio(Path(self.tmp.name), self.lr)
        self.state = self.studio.create('source', mode='source_edit', long_edge=256)

    def wait_done(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.state = self.studio.get(self.state['id'])
            if self.state['status'] != 'rendering_parameter_variants':
                return self.state
            time.sleep(.02)
        self.fail('parameter variants timeout')

    def variants(self, parameter='Exposure2012', values=None):
        self.studio.start_parameter_variants(
            self.state['id'], self.state['revision'], str(uuid.uuid4()),
            parameter, values if values is not None else [-.2, .2])
        return self.wait_done()

    def action(self, name, **options):
        self.state = self.studio.action(
            self.state['id'], name, self.state['revision'], str(uuid.uuid4()), **options)
        return self.state

    def test_fixed_base_variants_are_neutral_execution_records(self):
        source = copy.deepcopy(self.lr.settings['source'])
        revision = self.state['revision']
        request_id = str(uuid.uuid4())
        started = self.studio.start_parameter_variants(
            self.state['id'], revision, request_id, 'Exposure2012', [-.2, .2])
        self.assertEqual(started['status'], 'rendering_parameter_variants')
        self.wait_done()

        self.assertEqual(self.state['status'], 'ready')
        self.assertEqual(self.state['current_version'], 0)
        self.assertNotIn('best_version', self.state)
        self.assertEqual(self.state['parameter_variants']['versions'], [1, 2])
        self.assertEqual(self.lr.selected, self.state['versions'][0]['photo_uuid'])
        self.assertEqual(self.lr.settings['source'], source)

        prohibited = {
            'technique_id', 'technique', 'technique_review_status',
            'experiment_only', 'technique_controls_match', 'knowledge_hash',
            'summary', 'response', 'rationale', 'recommendation',
        }
        for number, requested in zip((1, 2), (-.2, .2)):
            version = self.state['versions'][number]
            record = version['parameter_variant']
            self.assertNotIn('metrics', version)
            self.assertNotIn('response', version)
            self.assertFalse((self.studio.directory(self.state['id']) /
                              f'diff{number}.png').exists())
            self.assertEqual(record['base_version'], 0)
            self.assertEqual(record['requested_value'], requested)
            self.assertEqual(record['applied_value'], requested)
            self.assertEqual(record['scope'], 'fixed_base_parameter_variant')
            self.assertTrue(prohibited.isdisjoint(record))

        listed = self.studio.list_parameter_variant_results('曝光')
        self.assertEqual(listed['count'], 2)
        self.assertEqual([row['requested_value'] for row in listed['results']], [-.2, .2])
        self.assertTrue(all(row['candidate_artifact'] for row in listed['results']))
        self.assertTrue(all(prohibited.isdisjoint(row) for row in listed['results']))

        copies = self.lr.copies
        replay = self.studio.start_parameter_variants(
            self.state['id'], revision, request_id, 'Exposure2012', [-.2, .2])
        self.assertEqual(replay, self.state)
        self.assertEqual(self.lr.copies, copies)
        with self.assertRaises(Conflict):
            self.studio.start_parameter_variants(
                self.state['id'], revision, str(uuid.uuid4()), 'Exposure2012', [-.2, .2])

    def test_variants_use_current_version_as_fixed_base(self):
        self.action('edit', settings={'Exposure2012': .4})
        self.assertNotIn('metrics', self.state['versions'][1])
        self.assertFalse((self.studio.directory(self.state['id']) / 'diff1.png').exists())
        self.variants('Contrast2012', [-10, 10])
        self.assertEqual(self.state['current_version'], 1)
        for version in self.state['versions'][2:]:
            self.assertEqual(version['parent_version'], 1)
            self.assertEqual(version['patch']['Exposure2012'], .4)
            self.assertEqual(version['parameter_variant']['base_version'], 1)
            self.assertNotIn('response', version['parameter_variant'])
        self.action('restore', version=2)
        self.action('verify', version=2)
        self.assertTrue(self.state['versions'][2]['verification']['passed'])

    def test_variants_are_available_in_same_capture_mode(self):
        self.state = self.studio.create(
            'source', 'reference', True, mode='same_capture_match', long_edge=256)
        self.variants('Exposure2012', [.1])
        self.assertEqual(self.state['parameter_variants']['used'], 1)
        self.assertEqual(self.state['current_version'], 0)
        self.assertNotIn('best_version', self.state)

    def test_invalid_request_has_no_side_effect(self):
        before = copy.deepcopy(self.state)
        copies = self.lr.copies
        cases = [
            ('Exposure2012', []),
            ('Exposure2012', [1, 1]),
            ('Exposure2012', [True]),
            ('Exposure2012', [6]),
            ('Temperature', [5500]),
            ('Unknown', [1]),
            ('Exposure2012', [1, 2, 3, 4, 5, 0]),
        ]
        for parameter, values in cases:
            with self.subTest(parameter=parameter, values=values):
                with self.assertRaises(ValueError):
                    self.studio.start_parameter_variants(
                        self.state['id'], self.state['revision'], str(uuid.uuid4()),
                        parameter, values)
        with self.assertRaises(TypeError):
            self.studio.start_parameter_variants(
                self.state['id'], self.state['revision'], str(uuid.uuid4()),
                'Exposure2012', [.1], technique_id='removed')
        self.assertEqual(self.studio.get(self.state['id']), before)
        self.assertEqual(self.lr.copies, copies)

    def test_cancel_keeps_only_completed_variants(self):
        self.lr.entered = threading.Event()
        self.lr.release = threading.Event()
        self.addCleanup(self.lr.release.set)
        self.studio.start_parameter_variants(
            self.state['id'], self.state['revision'], str(uuid.uuid4()),
            'Exposure2012', [-.2, .2])
        self.assertTrue(self.lr.entered.wait(2))
        with self.assertRaises(Conflict):
            self.action('restore', version=0)
        self.studio.stop(self.state['id'])
        self.lr.release.set()
        self.wait_done()
        self.assertEqual(self.state['parameter_variants']['stop_reason'], 'cancelled')
        self.assertEqual(self.state['parameter_variants']['used'], 1)
        self.assertEqual(self.studio.list_parameter_variant_results()['count'], 1)

    def test_failure_and_tampered_artifact_are_not_listed(self):
        self.variants(values=[.2])
        self.assertEqual(self.studio.list_parameter_variant_results()['count'], 1)
        (self.studio.directory(self.state['id']) / 'v0.jpg').write_bytes(b'tampered')
        self.assertEqual(self.studio.list_parameter_variant_results()['results'], [])
        with self.assertRaises(Conflict):
            self.variants()

        self.state = self.studio.create('source', mode='source_edit', long_edge=256)
        self.lr.fail = 'apply_candidate'
        self.variants(values=[.2])
        self.assertEqual(self.state['status'], 'failed')
        self.assertIn('pending_copy_uuid', self.state)
        self.assertEqual(len(self.state['versions']), 1)

    def test_curve_values_are_copied_and_applied(self):
        values = [[0, 0, 128, 135, 255, 255]]
        self.variants('ToneCurvePV2012', values)
        record = self.state['versions'][1]['parameter_variant']
        self.assertEqual(record['base_value'], [0, 0, 255, 255])
        self.assertEqual(record['requested_value'], values[0])
        self.assertEqual(record['applied_value'], values[0])
        self.assertEqual(record['parameter_spec']['value_type'], 'curve')

    def test_old_parameter_probe_remains_readable_but_is_neutrally_projected(self):
        self.variants(values=[.2])
        state = self.studio.raw(self.state['id'])
        version = state['versions'][1]
        old = version.pop('parameter_variant')
        old['value'] = old.pop('requested_value')
        old.pop('applied_value')
        old.update({
            'technique_id': 'old-technique',
            'technique': {'id': 'old-technique'},
            'knowledge_hash': 'old-knowledge',
            'summary': 'legacy interpretation',
            'response': {'mae_rgb': 1.0},
        })
        version['parameter_probe'] = old
        version['style_candidate'] = {'recipe_id': 'old-recipe'}
        version['technical_review'] = {'status': 'approved', 'summary': 'legacy review'}
        version['accepted_by_search'] = True
        state['best_version'] = 1
        state['search'] = {'used': 1, 'parameters': ['Exposure2012']}
        state['exploration'] = {'recipe_ids': ['old-recipe']}
        state['probe'] = {'technique_id': 'old-technique'}
        state['operations'].update({
            'old-explore': {'input': {'action': 'explore'}, 'status': 'complete'},
            'old-probe': {'input': {'action': 'probe'}, 'status': 'complete'},
            'old-search': {'input': {'action': 'search'}, 'status': 'complete'},
        })
        self.studio.save(state)

        reopened = Studio(Path(self.tmp.name), self.lr)
        disk = reopened.raw(state['id'])
        self.assertEqual(disk['best_version'], 1)
        self.assertEqual(disk['search']['used'], 1)
        self.assertEqual(disk['exploration']['recipe_ids'], ['old-recipe'])
        self.assertEqual(disk['probe']['technique_id'], 'old-technique')
        self.assertTrue(disk['versions'][1]['accepted_by_search'])
        self.assertEqual(disk['versions'][1]['parameter_probe']['summary'],
                         'legacy interpretation')
        self.assertEqual(disk['versions'][1]['style_candidate']['recipe_id'], 'old-recipe')
        self.assertEqual(disk['versions'][1]['technical_review']['status'], 'approved')
        self.assertTrue({'old-explore', 'old-probe', 'old-search'} <= set(disk['operations']))

        public = reopened.get(state['id'])
        for hidden in ('best_version', 'search', 'exploration', 'probe'):
            self.assertNotIn(hidden, public)
        for hidden in ('accepted_by_search', 'parameter_probe', 'style_candidate',
                       'technical_review'):
            self.assertNotIn(hidden, public['versions'][1])
        self.assertTrue({'old-explore', 'old-probe', 'old-search'}.isdisjoint(
            public['operations']))
        self.assertNotIn('best_version', reopened.list()[0])
        neutral = public['versions'][1]['parameter_variant']
        self.assertEqual(neutral['parameter'], 'Exposure2012')
        self.assertEqual(neutral['requested_value'], .2)
        self.assertEqual(neutral['applied_value'], .2)
        for hidden in ('technique_id', 'technique', 'knowledge_hash', 'summary', 'response'):
            self.assertNotIn(hidden, neutral)

        result = reopened.list_parameter_variant_results()['results'][0]
        self.assertEqual(result['requested_value'], .2)
        for hidden in ('technique_id', 'technique', 'knowledge_hash', 'summary', 'response'):
            self.assertNotIn(hidden, result)

    def test_query_validation(self):
        with self.assertRaises(ValueError):
            self.studio.list_parameter_variant_results([])
        with self.assertRaises(ValueError):
            self.studio.list_parameter_variant_results('x' * 201)


if __name__ == '__main__':
    unittest.main()
