"""参数能力、校验与 XMP 序列化。

FakeLR 只驱动状态与数值，不证明 LR 侧保真度；真机渲染不在本文件覆盖范围。
"""
import tempfile
import unittest
from pathlib import Path

from test_studio import FakeLR
from workbench.studio.engine import Studio
from workbench.studio.settings import from_xmp, to_xmp, validate


class XmpSerializationTests(unittest.TestCase):
    def test_xmp_roundtrip_keeps_scalar_bool_and_numbers(self):
        patch = {
            'Exposure2012': 0.25,
            'Contrast2012': -12,
            'ConvertToGrayscale': True,
            'AutoLateralCA': False,
        }
        self.assertEqual(from_xmp(to_xmp(patch, 'scalar roundtrip')), patch)

    def test_xmp_rejects_unknown_arrays_and_nested_masks(self):
        with self.assertRaisesRegex(ValueError, 'XMP serialization unsupported'):
            to_xmp({'UnknownArray': [1, 2, 3]}, 'unknown array')
        with self.assertRaisesRegex(ValueError, 'XMP serialization unsupported'):
            to_xmp({'MaskGroupBasedCorrections': [{'what': 'Mask'}]}, 'mask')

        old_repr = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/">'
            '<rdf:RDF><rdf:Description rdf:about="" '
            'crs:UnknownArray="[1, 2, 3]" /></rdf:RDF></x:xmpmeta>'
        )
        with self.assertRaisesRegex(ValueError, 'Unsupported structured XMP attribute'):
            from_xmp(old_repr)


class StudioExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lr = FakeLR()
        self.studio = Studio(Path(self.tmp.name), self.lr)
        self.s = self.studio.create('source', mode='source_edit', long_edge=256)

    def test_capabilities_reports_truthful_limits(self):
        caps = self.studio.capabilities()
        self.assertEqual(caps['limits']['parameter_variant_values_max'], 5)
        self.assertNotIn('explore_recipes_max', caps['limits'])
        self.assertNotIn('probe_values_max', caps['limits'])
        self.assertNotIn('search_budget_max', caps['limits'])
        self.assertIn('Exposure2012', caps['writable_parameters'])
        self.assertIn('ToneCurvePV2012Blue', caps['curves'])
        self.assertEqual(sorted(f['id'] for f in caps['families']), [
            'color', 'color_grade', 'curve', 'hsl', 'split_toning', 'tone',
            'vignette', 'wb',
        ])

    def test_color_family_parameters_survive_xmp_roundtrip(self):
        """Color-family fields are validated, grouped, and serialized end-to-end."""
        patch = {
            'SplitToningShadowHue': 220,
            'SplitToningShadowSaturation': 25,
            'SplitToningHighlightHue': 45,
            'SplitToningHighlightSaturation': 20,
            'SplitToningBalance': -10,
            'ColorGradeMidtoneHue': 200,
            'ColorGradeMidtoneSat': 20,
            'ColorGradeMidtoneLum': -10,
            'ColorGradeShadowLum': -15,
            'ColorGradeHighlightLum': 10,
            'ColorGradeGlobalHue': 0,
            'ColorGradeGlobalSat': 5,
            'ColorGradeGlobalLum': 0,
            'ColorGradeBlending': 60,
            'PostCropVignetteAmount': -35,
            'PostCropVignetteMidpoint': 40,
            'PostCropVignetteFeather': 60,
            'PostCropVignetteRoundness': 0,
            'PostCropVignetteStyle': 2,
            'PostCropVignetteHighlightContrast': 20,
        }
        self.assertEqual(from_xmp(to_xmp(patch, 'color family roundtrip')), patch)
        with self.assertRaises(ValueError):
            validate({'ColorGradeBlending': 101})

    def test_xmp_roundtrip_keeps_numeric_passthrough(self):
        # These four legacy color-grade names are valid scalar passthrough. LR
        # 15.4.1 ignores them at readback, but serialization must not erase them
        # before that result can be observed.
        patch = {
            'ColorGradeShadowHue': 240,
            'ColorGradeShadowSat': 15,
            'ColorGradeHighlightHue': 40,
            'ColorGradeHighlightSat': 15,
        }
        self.assertEqual(from_xmp(to_xmp(patch, 'passthrough roundtrip')), patch)

    def test_capabilities_do_not_turn_our_whitelist_into_a_photo_limit(self):
        """只有 SDK 真的没有入口的才叫「做不到」，而且必须给出去 Lightroom 的路径。

        2026-09-16 之前这里列的是 masks / geometry / profile / detail，理由写的是
        「不在可写参数白名单内」—— 那是自我设限，被当成 LR 的能力边界报给了 Agent。
        白名单改成类型检查之后那几个字段都能透传，所以这条断言改成了：
        列表里只允许出现 SDK 层面真的做不到的，且每条都要有 manual_path。
        """
        caps = self.studio.capabilities()
        ids = [item['id'] for item in caps['unsupported']]
        for gone in ('masks', 'detail', 'profile'):
            self.assertNotIn(gone, ids)
        for item in caps['unsupported']:
            self.assertTrue(item['manual_path'], f"{item['id']} 必须给出 LR 里的进入路径")
            self.assertNotIn('白名单', item['reason'], '别把自设白名单说成能力边界')
        tiers = caps['capability_tiers']
        self.assertIn('Exposure2012', tiers['structured']['keys'])
        self.assertIn('MaskGroupBasedCorrections', ' '.join(tiers['passthrough']['examples']))

if __name__ == '__main__':
    unittest.main()
