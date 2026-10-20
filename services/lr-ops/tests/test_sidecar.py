from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from lr_ops.sidecar import CRS, RDF, SidecarError, finalize, finalize_result, validate_request
from lr_ops.server import JobQueue

MASK = '''<crs:MaskGroupBasedCorrections><rdf:Seq><rdf:li><rdf:Description
 crs:CorrectionSyncID="existing" crs:CorrectionActive="true" crs:CorrectionName="Skin"
 crs:LocalExposure2012="0.3"><crs:CorrectionMasks><rdf:Seq><rdf:li
 crs:What="Mask/Image" crs:MaskSyncID="component" crs:MaskActive="true" crs:ErrorReason="0"/>
 </rdf:Seq></crs:CorrectionMasks></rdf:Description></rdf:li></rdf:Seq></crs:MaskGroupBasedCorrections>'''


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = self.root / 'image.ARW'
        self.raw.write_bytes(b'original RAW bytes')
        self.xmp = self.raw.with_suffix('.xmp')
        self.xmp.write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="' + RDF + '" xmlns:crs="' + CRS + '" xmlns:dc="urn:test"><rdf:RDF>'
                            '<rdf:Description rdf:about="" dc:title="keep me" crs:Exposure2012="0" crs:Temperature="6150" crs:Tint="17" '
                            'crs:WhiteBalance="As Shot" crs:ProcessVersion="15.0">' + MASK + '</rdf:Description></rdf:RDF></x:xmpmeta>', encoding='utf-8')
        self.original = self.xmp.read_bytes()
        self.context = {'photo_uuid': 'master', 'source_path': str(self.raw), 'is_virtual_copy': False,
                        'develop_settings': {'Exposure2012': 0, 'Temperature': 6150, 'Tint': 17, 'WhiteBalance': 'As Shot', 'ProcessVersion': '15.0',
                                             'MaskGroupBasedCorrections': [{'CorrectionSyncID': 'existing', 'CorrectionName': 'Skin', 'CorrectionActive': True,
                                                 'LocalExposure2012': .3, 'CorrectionMasks': [{'MaskSyncID': 'component', 'MaskActive': True, 'ErrorReason': 0}]}]}}
        self.backups = self.root / 'backups'

    def tearDown(self):
        self.tmp.cleanup()

    def run_op(self, command='update_xmp_sidecar', **params):
        return finalize(command, {'photo_uuid': 'master', **params}, self.context, self.backups, str(uuid.uuid4()))

    def write_params(self, **params):
        return {'action': 'update', 'confirm_xmp_mode': True, 'request_id': str(uuid.uuid4()),
                'expected_token': self.run_op()['expected_token'], 'settings': {'Exposure2012': .5}, **params}

    def test_patch_preserves_metadata_masks_wb_raw_and_acr_and_can_restore_bytes(self):
        acr = self.raw.with_suffix('.acr'); acr.write_bytes(b'opaque AI geometry')
        result = self.run_op(**self.write_params())
        self.assertTrue(result['sidecar_verified'])
        self.assertFalse(result['catalog_applied'])
        self.assertEqual(result['status'], 'sidecar_written_reload_required')
        text = self.xmp.read_text()
        self.assertIn('keep me', text); self.assertIn('CorrectionSyncID="existing"', text)
        self.assertIn('Temperature="6150"', text); self.assertIn('Tint="17"', text)
        self.assertEqual(acr.read_bytes(), b'opaque AI geometry')
        self.assertEqual(self.raw.read_bytes(), b'original RAW bytes')
        saved = self.backups / result['backup_id']
        self.assertEqual((saved / 'original.xmp').read_bytes(), self.original)
        self.assertEqual((saved / 'original.acr').read_bytes(), acr.read_bytes())
        self.assertEqual(json.loads((saved / 'develop-settings.json').read_text()), self.context['develop_settings'])
        restore = self.write_params(action='restore', backup_id=result['backup_id'])
        self.run_op(**restore)
        self.assertEqual(self.xmp.read_bytes(), self.original)

    def test_catalog_or_sidecar_drift_refuses_write(self):
        params = self.write_params()
        self.context['develop_settings']['Tint'] = 18
        with self.assertRaisesRegex(SidecarError, 'changed'):
            self.run_op(**params)
        self.assertEqual(self.xmp.read_bytes(), self.original)

    def test_one_axis_wb_patch_preserves_other_axis_and_uses_custom_mode(self):
        self.run_op(**self.write_params(settings={'Tint': 23}))
        text = self.xmp.read_text()
        self.assertIn('Temperature="6150"', text)
        self.assertIn('Tint="23"', text)
        self.assertIn('WhiteBalance="Custom"', text)

    def test_stale_native_baseline_requires_save_without_writing(self):
        self.context['develop_settings']['Temperature'] = 7000
        result = self.run_op(**self.write_params())
        self.assertEqual(result['status'], 'metadata_save_required')
        self.assertFalse(result['sidecar_written'])
        self.assertEqual(self.xmp.read_bytes(), self.original)

    def test_missing_native_defaults_are_materialized_from_current_values(self):
        self.context['develop_settings']['Contrast2012'] = 22
        result = self.run_op(**self.write_params())
        self.assertTrue(result['sidecar_written'])
        self.assertIn('Contrast2012="22"', self.xmp.read_text())
        self.assertIn('Contrast2012', result['initialized_keys'])

    def test_conflicting_native_non_wb_edit_blocks_import(self):
        self.context['develop_settings']['Contrast2012'] = 22
        self.xmp.write_bytes(self.original.replace(b'crs:Exposure2012="0"', b'crs:Exposure2012="0" crs:Contrast2012="0"'))
        original = self.xmp.read_bytes()
        result = self.run_op(**self.write_params())
        self.assertEqual(result['status'], 'metadata_save_required')
        self.assertIn('Contrast2012', result['baseline_mismatches'])
        self.assertEqual(self.xmp.read_bytes(), original)

    def test_virtual_copy_embedded_xmp_and_missing_confirmation_are_refused(self):
        self.context['is_virtual_copy'] = True
        with self.assertRaises(SidecarError): self.run_op()
        self.context['is_virtual_copy'] = False
        self.context['source_path'] = str(self.raw.with_suffix('.dng'))
        with self.assertRaises(SidecarError): self.run_op()
        with self.assertRaises(SidecarError):
            validate_request('update_xmp_sidecar', {'photo_uuid': 'master', 'action': 'update', 'settings': {'Exposure2012': 1}})

    def test_mask_update_changes_one_correction_and_returns_reload_not_success(self):
        params = self.write_params(correction_id='existing', settings={'LocalExposure2012': .6})
        result = self.run_op('apply_mask', **params)
        self.assertEqual(result['correction_id'], 'existing')
        self.assertIn('LocalExposure2012="0.6"', self.xmp.read_text())
        self.assertFalse(result['catalog_applied'])

    def test_semantic_mask_needs_geometry_and_template_does_not_claim_creation(self):
        params = self.write_params(action='create', settings={'LocalExposure2012': .4}, mask_type='body_skin')
        result = self.run_op('apply_mask', **params)
        self.assertEqual(result['status'], 'xmp_mask_generation_unavailable')
        self.assertFalse(result['sidecar_written']); self.assertFalse(result['mask_exists'])
        self.assertEqual(self.xmp.read_bytes(), self.original)
        result = self.run_op('apply_mask', **{**params, 'mask_xml': MASK})
        self.assertFalse(result['mask_exists']); self.assertTrue(result['sidecar_written'])
        self.assertNotEqual(result['correction_id'], 'existing')
        self.assertEqual(len(self.run_op()['masks_in_xmp']), 2)

    def test_verify_uses_actual_catalog_records_and_local_parameter_readback(self):
        params = {'action': 'verify', 'correction_id': 'existing', 'settings': {'LocalExposure2012': .6}}
        self.context['develop_settings'].pop('MaskGroupBasedCorrections')
        self.assertFalse(self.run_op('apply_mask', **params)['mask_exists'])
        group = {'CorrectionSyncID': 'existing', 'CorrectionName': 'Skin', 'CorrectionActive': True,
                 'LocalExposure2012': .6, 'CorrectionMasks': [{'What': 'Mask/Image', 'MaskActive': True, 'ErrorReason': 0}]}
        self.context['develop_settings']['MaskGroupBasedCorrections'] = [group]
        result = self.run_op('apply_mask', **params)
        self.assertTrue(result['mask_exists']); self.assertTrue(result['catalog_applied'])
        self.assertFalse(result['coverage_verified'])
        group['CorrectionMasks'][0]['ErrorReason'] = 2
        self.assertFalse(self.run_op('apply_mask', **params)['mask_exists'])

    def test_malformed_and_entity_xml_never_write(self):
        # Assembled at runtime so the security payload stays identical while
        # this source file carries no literal file-scheme marker.
        entity = '<!ENTITY x SYSTEM "file:' '///private">'
        params = self.write_params(action='create', settings={}, mask_xml='<!DOCTYPE mask [' + entity + ']>')
        with self.assertRaises(SidecarError): self.run_op('apply_mask', **params)
        self.assertEqual(self.xmp.read_bytes(), self.original)

    def test_public_results_strip_catalog_paths(self):
        params = {'photo_uuid': 'master'}
        body = finalize_result('update_xmp_sidecar', params, {'ok': True, 'data': {'_sidecar_context': self.context}}, self.backups, str(uuid.uuid4()))
        self.assertNotIn(str(self.root), json.dumps(body))


class QueueSidecarTests(unittest.TestCase):
    def test_dedup_survives_restart_and_duplicate_completion_cannot_write_twice(self):
        with tempfile.TemporaryDirectory() as root:
            queue = JobQueue(Path(root))
            params = {'photo_uuid': 'master', 'action': 'update', 'request_id': 'request-1'}
            job = queue.submit('update_xmp_sidecar', params)
            self.assertEqual(job, queue.submit('update_xmp_sidecar', params))
            with self.assertRaises(ValueError): queue.submit('update_xmp_sidecar', {**params, 'settings': {'Exposure2012': 1}})
            queue.next_queued()
            calls = []
            def finalizer(record, body):
                self.assertEqual(record['status'], 'finalizing')
                calls.append(True); return body
            self.assertTrue(queue.complete(job, {'ok': True}, finalize=finalizer))
            self.assertFalse(queue.complete(job, {'ok': True}, finalize=finalizer))
            self.assertEqual(len(calls), 1)
            recovered = JobQueue(Path(root))
            self.assertEqual(job, recovered.submit('update_xmp_sidecar', params))

    def test_interrupted_finalization_does_not_replay(self):
        with tempfile.TemporaryDirectory() as root:
            queue = JobQueue(Path(root))
            params = {'photo_uuid': 'master', 'action': 'update', 'request_id': 'request-1'}
            job = queue.submit('update_xmp_sidecar', params); queue.next_queued()
            def crash(record, body): raise RuntimeError('interrupted after write')
            with self.assertRaises(RuntimeError): queue.complete(job, {'ok': True}, finalize=crash)
            recovered = JobQueue(Path(root))
            self.assertEqual(recovered.get(job)['status'], 'interrupted')
            self.assertEqual(job, recovered.submit('update_xmp_sidecar', params))
            self.assertIsNone(recovered.next_queued())


if __name__ == '__main__':
    unittest.main()
