"""Episode invariants and full-image metrics with a fake Lightroom.
The fake's tiny images exercise state and measurements only, not LR fidelity.
"""
import copy
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import numpy as np
from PIL import Image
from workbench.studio.engine import Studio, Conflict, atomic, equivalent_snapshot
from workbench.studio.imaging import compare
from workbench.studio.settings import BOUNDS, to_xmp, from_xmp, validate


CATALOG_IDENTITY = 'catalog-md5-v1:0123456789abcdef0123456789abcdef'


class FakeLR:
    def __init__(self):
        self.access_mode='full'
        self.catalog_identity=None
        self.active_catalog_identity=CATALOG_IDENTITY
        self.calls=[]
        self.initial={key: 0 for key in BOUNDS if key not in ('Temperature','Tint')}
        self.initial.update(CameraProfile='Embedded', ProcessVersion='15.0', ToneCurvePV2012=[0,0,255,255])
        self.settings={'source': copy.deepcopy(self.initial), 'reference': {**copy.deepcopy(self.initial), 'Exposure2012': .4}}
        self.copies=0
        self.fail=None
        self.selected=None
        self.entered=None
        self.release=None
        self.outputs={}
        self._handles=0

    def reachable(self): return True

    def fetch_output(self, ref, destination):
        """Hand back the bytes the bridge staged for an export handle."""
        destination=Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.outputs[ref])
        return destination

    def capture(self, image):
        import io
        buffer=io.BytesIO()
        image.save(buffer,format='JPEG',quality=95)
        ref=f'out{len(self.outputs)+1}'
        self.outputs[ref]=buffer.getvalue()
        return ref

    def run(self, command, params=None):
        params=params or {}
        self.calls.append((command,copy.deepcopy(params)))
        assert params.get('expected_access_mode') == self.access_mode
        expected_identity=params.get('expected_catalog_identity')
        if expected_identity is not None and expected_identity != self.active_catalog_identity:
            raise RuntimeError('catalog identity mismatch')
        if command == self.fail: raise RuntimeError('injected LR failure')
        if command == 'probe_client':
            return {'sandbox':False,'catalog_identity':self.active_catalog_identity,
                    'access_mode':self.access_mode,
                    'catalog_authorized': True,
                    'lr_version':'fake','plugin_version':'0.3.0',
                    'capabilities': {
                        'catalog_identity': True,
                        'expected_catalog_identity': True,
                        'expected_access_mode': True,
                        'develop_snapshot': True,
                        'hsl': True,
                    }}
        if command == 'list_photos':
            photos=[self.info(p) for p in self.settings]
            return {'count':len(photos),'photos':photos}
        photo=params['photo_uuid']
        if command in ('get_develop_snapshot','get_develop_state'):
            return {'settings':copy.deepcopy(self.settings[photo]),'photo':self.info(photo)}
        if command == 'create_virtual_copy':
            assert photo == 'source'
            self.copies+=1
            name=f'copy{self.copies}'
            self.settings[name]=copy.deepcopy(self.settings[photo])
            return {'copy':self.info(name)}
        if command == 'capture_baseline': return {'baseline':copy.deepcopy(self.settings[photo])}
        if command == 'apply_candidate':
            assert photo.startswith('copy')
            self.settings[photo].update(params['settings'])
            return {'after':copy.deepcopy(self.settings[photo])}
        if command == 'select_photo':
            self.selected=photo
            return {'photo':self.info(photo)}
        if command == 'export_preview':
            if self.entered:
                self.entered.set()
                if not self.release.wait(5): raise RuntimeError('test wait timeout')
            # No caller-supplied output_dir any more: the bridge owns the path
            # and answers with a handle (see Staging.inject_dispatch).
            assert 'output_dir' not in params, 'caller must not choose the export path'
            assert 'path' not in params and 'paths' not in params
            pixels=np.full((24,32,3),100,dtype=np.uint8)
            pixels[:12,:,0]=130
            pixels=np.clip(pixels.astype(float)+self.settings[photo]['Exposure2012']*40,0,255).astype(np.uint8)
            ref=self.capture(Image.fromarray(pixels))
            return {'output_ref':ref,'color_space':'sRGB'}
        raise AssertionError(command)

    def info(self,p):
        return {'uuid':p,'file_name':p+'.jpg','is_virtual_copy':p.startswith('copy'),'width':32,'height':24}


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lr=FakeLR()
        self.studio=Studio(Path(self.tmp.name),self.lr)
        self.s=self.studio.create('source','reference',True,'同图仿色',256)

    def action(self, action, **options):
        self.s=self.studio.action(self.s['id'],action,self.s['revision'],str(uuid.uuid4()),**options)
        return self.s

    def wait_parameter_variants(self):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            self.s=self.studio.get(self.s['id'])
            if self.s['status']!='rendering_parameter_variants': return self.s
            time.sleep(.02)
        self.fail('parameter variants timeout')

    def test_episode_binds_catalog_and_echoes_it_on_later_calls(self):
        self.assertEqual(self.s['catalog_identity'], CATALOG_IDENTITY)
        start=len(self.lr.calls)
        self.action('edit',settings={'Exposure2012':.2})
        for command, params in self.lr.calls[start:]:
            self.assertEqual(params['expected_access_mode'], 'full', command)
            self.assertEqual(params['expected_catalog_identity'], CATALOG_IDENTITY, command)

    def test_catalog_drift_is_rejected_before_copy(self):
        copies=self.lr.copies
        self.lr.active_catalog_identity='catalog-md5-v1:ffffffffffffffffffffffffffffffff'
        with self.assertRaises(RuntimeError):
            self.action('edit',settings={'Exposure2012':.2})
        self.assertEqual(self.lr.copies,copies)

    def test_legacy_episode_without_opaque_identity_is_read_only(self):
        raw=self.studio.raw(self.s['id'])
        raw.pop('catalog_identity',None)
        raw['renderer'].pop('catalog_identity',None)
        raw['renderer']['catalog_path']='/legacy/catalog/path.lrcat'
        with self.studio.lock(self.s['id']):
            atomic(self.studio.directory(self.s['id'])/'episode.json',raw)
        self.s=self.studio.get(self.s['id'])
        with self.assertRaisesRegex(Conflict, '只能读取'):
            self.action('edit',settings={'Exposure2012':.2})
        self.assertIsNone(self.studio.raw(self.s['id']).get('catalog_identity'))

    def test_fixed_master_independent_versions_and_replay(self):
        baseline=copy.deepcopy(self.lr.settings['source'])
        self.action('edit',settings={'Exposure2012':.4})
        original=self.s['versions'][1]
        self.assertNotIn('metrics', original)
        self.assertEqual(original['artifact']['purpose'],'verified')
        self.assertEqual(original['artifact']['evidence_status'],'verified_render')
        self.assertEqual(original['artifact']['method'],'LrExportSession')
        self.assertNotIn('path', original['artifact'])
        self.action('verify',version=1)
        self.assertTrue(self.s['versions'][1]['verification']['passed'])
        self.assertLess(self.s['versions'][1]['verification']['metrics']['mae_rgb'],.1)
        self.assertEqual(self.lr.settings['source'],baseline)
        self.assertEqual(len({v['photo_uuid'] for v in self.s['versions']}),3)
        self.assertEqual(from_xmp(self.studio.artifact(self.s['id'],'v1.xmp').read_text()),{'Exposure2012':.4})

    def test_lr_transient_incremental_wb_sentinel_only(self):
        self.lr.initial.update(IncrementalTemperature=0, IncrementalTint=0)
        self.lr.settings['source'].update(IncrementalTemperature=0, IncrementalTint=0)
        self.s=self.studio.create('source','reference',True,long_edge=256)
        original_run=self.lr.run
        def transient(command,params=None):
            photo=(params or {}).get('photo_uuid')
            if command=='export_preview' and self.lr.settings[photo].get('IncrementalTemperature') == -999999:
                self.lr.settings[photo]['IncrementalTemperature']=0
            result=original_run(command,params)
            if command=='apply_candidate':
                self.lr.settings[photo]['IncrementalTemperature']=-999999
            return result
        self.lr.run=transient
        self.action('edit',settings={'Exposure2012':.4})
        self.assertEqual(self.s['versions'][1]['snapshot']['IncrementalTemperature'],0)

    def test_restore_survives_new_engine_and_empty_plugin_memory(self):
        self.action('edit',settings={'Exposure2012':.4})
        self.studio=Studio(Path(self.tmp.name),self.lr)
        self.action('restore',version=0)
        self.assertEqual(self.s['current_version'],0)
        self.assertEqual(self.lr.selected,self.s['versions'][0]['photo_uuid'])
        self.assertNotIn('best_version',self.s)

    def test_stale_and_idempotent_request(self):
        revision=self.s['revision'];request=str(uuid.uuid4())
        self.s=self.studio.action(self.s['id'],'edit',revision,request,settings={'Exposure2012':.4})
        copies=self.lr.copies
        repeated=self.studio.action(self.s['id'],'edit',revision,request,settings={'Exposure2012':.4})
        self.assertEqual(repeated,self.s);self.assertEqual(copies,self.lr.copies)
        with self.assertRaises(Conflict):
            self.studio.action(self.s['id'],'edit',revision,request,settings={'Exposure2012':.8})
        with self.assertRaises(Conflict):
            self.studio.action(self.s['id'],'edit',revision,str(uuid.uuid4()),settings={'Exposure2012':.8})

    def test_failure_does_not_publish_candidate(self):
        self.lr.fail='apply_candidate'
        with self.assertRaises(RuntimeError): self.action('edit',settings={'Exposure2012':.4})
        s=self.studio.get(self.s['id'])
        self.assertEqual(s['status'],'failed');self.assertEqual(len(s['versions']),1)
        self.assertNotIn('best_version',s);self.assertIn('pending_copy_uuid',s)

    def test_target_change_is_rejected_before_copy(self):
        target=self.studio.directory(self.s['id'])/'reference.jpg'
        target.write_bytes(b'tampered')
        copies=self.lr.copies
        with self.assertRaises(Conflict): self.action('edit',settings={'Exposure2012':.4})
        self.assertEqual(self.lr.copies,copies)

    def test_real_profile_change_during_render_still_rejected(self):
        original_run=self.lr.run
        def mutate(command,params=None):
            result=original_run(command,params)
            if command=='export_preview':
                self.lr.settings[params['photo_uuid']]['CameraProfile']='unexpected'
            return result
        self.lr.run=mutate
        with self.assertRaises(Conflict): self.action('edit',settings={'Exposure2012':.4})
        self.assertEqual(len(self.studio.get(self.s['id'])['versions']),1)

    def test_invalid_message_does_not_poison_episode(self):
        with self.assertRaises(ValueError): self.action('message',text='')
        self.assertEqual(self.studio.get(self.s['id'])['status'],'ready')

    def test_master_change_is_rejected_before_copy(self):
        self.lr.settings['source']['CameraProfile']='Changed'
        copies=self.lr.copies
        with self.assertRaises(Conflict): self.action('edit',settings={'Exposure2012':.4})
        self.assertEqual(copies,self.lr.copies)

    def test_cross_instance_lock_and_cancellation(self):
        self.lr.entered=threading.Event();self.lr.release=threading.Event()
        self.s=self.studio.start_parameter_variants(
            self.s['id'],self.s['revision'],str(uuid.uuid4()),'Exposure2012',[.2,.4])
        self.assertTrue(self.lr.entered.wait(3))
        second=Studio(Path(self.tmp.name),self.lr)
        self.assertEqual(second.get(self.s['id'])['status'],'rendering_parameter_variants')
        with self.assertRaises(Conflict):
            second.action(self.s['id'],'restore',self.s['revision'],str(uuid.uuid4()),version=0)
        second.stop(self.s['id']);self.lr.release.set();self.wait_parameter_variants()
        self.assertEqual(self.s['parameter_variants']['used'],1)
        self.assertEqual(self.s['parameter_variants']['stop_reason'],'cancelled')

    def test_interrupted_work_is_not_replayed(self):
        self.s['status']='editing';self.studio.save(self.s)
        count=len(self.lr.calls)
        state=Studio(Path(self.tmp.name),self.lr).get(self.s['id'])
        self.assertEqual(state['status'],'interrupted');self.assertEqual(len(self.lr.calls),count)

    def test_no_same_capture_assertion_rejected(self):
        with self.assertRaises(ValueError): self.studio.create('source','reference',False)

    def test_unpublished_and_tampered_preview_cannot_be_read(self):
        directory=self.studio.directory(self.s['id'])
        (directory/'v99.jpg').write_bytes(b'orphan')
        with self.assertRaises(ValueError):self.studio.artifact(self.s['id'],'v99.jpg')
        (directory/'v0.jpg').write_bytes(b'tampered')
        with self.assertRaises(Conflict):self.studio.artifact(self.s['id'],'v0.jpg')

    def test_tampered_export_and_path_rejected(self):
        self.action('edit',settings={'Exposure2012':.4});self.action('verify',version=1)
        path=self.studio.artifact(self.s['id'],'v1.xmp');path.write_text('changed')
        with self.assertRaises(ValueError): self.studio.artifact(self.s['id'],'v1.xmp')
        with self.assertRaises(ValueError): self.studio.artifact(self.s['id'],'../../episode.json')

    def test_raw_lazy_defaults_preserve_resolved_baseline(self):
        self.lr.settings['source'].update(WhiteBalance='As Shot')
        original=self.lr.run
        def raw_copy(command,params=None):
            result=original(command,params)
            if command=='create_virtual_copy':
                self.lr.settings[result['copy']['uuid']].update(Temperature=5600,Tint=2,FilterList={})
            return result
        self.lr.run=raw_copy
        s=self.studio.create('source','reference',True,long_edge=256)
        self.assertNotIn('Temperature',s['source_snapshot'])
        self.assertEqual(s['baseline_snapshot']['Temperature'],5600)
        self.assertFalse(equivalent_snapshot({'WhiteBalance':'Custom','Temperature':5000},{'WhiteBalance':'Custom','Temperature':5100}))
        self.assertFalse(equivalent_snapshot({'Look':{'Name':'A'}},{'Look':{'Name':'B'}}))

    def test_curve_geometry_and_wb_semantics(self):
        for curve in ([0,0,0,100,255,255],[0,0,128,200,200,200],[0,0,128.5,100,255,255]):
            with self.assertRaises(ValueError):validate({'ToneCurvePV2012':curve})
        copies=self.lr.copies
        with self.assertRaises(ValueError):self.action('edit',settings={'Temperature':5000})
        self.assertEqual(self.lr.copies,copies)

    def test_input_validation_prevents_lr_writes(self):
        count=len(self.lr.calls)
        # 会在碰 Lightroom 之前被拒的三类：已知字段的坏值、任何字段的坏类型、
        # 以及不属于显影参数的键（LR 自己维护的元数据）。
        for patch in ({'Exposure2012':float('nan')},{'Exposure2012':True},{'Exposure2012':6},
                      {'NotASetting':'x'},{'NotASetting':[1,'a']},{'History':1},{'Bad-Key':1}):
            with self.assertRaises(ValueError): self.action('edit',settings=patch)
        self.assertEqual(len(self.lr.calls),count)

    def test_unknown_but_well_typed_settings_are_passed_through(self):
        """透传是有意的，不是漏了校验。

        没写下边界的字段不该因为「引擎不认识」就变成「用户不能改」——那是对照片的限制，
        不是安全属性。范围由 Lightroom 决定，写入后由回读如实报告实际值。
        """
        from workbench.studio.settings import validate
        for patch in ({'GrainAmount':40},{'ConvertToGrayscale':True},{'SplitToningShadowHue':220},
                      {'GrayMixerRed':-20},{'Texture':15},{'Dehaze':-10},
                      {'NotASetting':5000},{'GrainAmount':40,'GrainSize':25}):
            self.assertEqual(validate(patch), patch)
        # 已知字段仍然按范围拦，这条没有被透传放松
        with self.assertRaises(ValueError): validate({'Exposure2012':6})
        # 嵌套结构（蒙版）默认也放开，但可以按部署关掉
        mask = {'MaskGroupBasedCorrections': [{'CorrectionAmount': 0.5}]}
        self.assertEqual(validate(mask), mask)
        with self.assertRaises(ValueError): validate(mask, allow_structured=False)
        # 透传整体也能关，关掉之后已知字段照常
        with self.assertRaises(ValueError): validate({'GrainAmount': 40}, allow_passthrough=False)
        self.assertEqual(validate({'Exposure2012': 0.5}, allow_passthrough=False),
                         {'Exposure2012': 0.5})


class MeasurementTests(unittest.TestCase):
    def test_dense_measurement_detects_unsampled_patch(self):
        with tempfile.TemporaryDirectory() as folder:
            a=Path(folder)/'a.png';b=Path(folder)/'b.png'
            p=np.full((20,20,3),100,dtype=np.uint8);Image.fromarray(p).save(a)
            p[7:11,7:11]=200;Image.fromarray(p).save(b)
            result=compare(a,b)
            self.assertAlmostEqual(result['mae_rgb'],4)
            self.assertEqual(len(result['tiles']),16)
            self.assertEqual(compare(a,a)['mae_rgb'],0)
            Image.new('RGB',(21,20)).save(b)
            with self.assertRaises(ValueError):compare(a,b)

    def test_xmp_precision_and_escaping(self):
        patch={'Exposure2012':.123456,'HueAdjustmentBlue':-14.125,'ToneCurvePV2012':[0,0,128,140,255,255],'IncrementalTemperature':10}
        self.assertEqual(from_xmp(to_xmp(patch,'a < b & c')),patch)


if __name__=='__main__':unittest.main()
