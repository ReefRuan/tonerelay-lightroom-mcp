"""Known-color, provenance and read-only contract tests; no LR test double needed."""
import base64
import hashlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms

from workbench.image_tools import (
    COMPARISON_VERSION,
    MEASUREMENT_VERSION,
    ImageTools,
    compare_images,
    describe,
    luminance,
    measure_image,
)
from workbench.srgb_profile import SRGB_ICC


class ImageToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tools = ImageTools(self.root)
        self.sid = str(uuid.uuid4())
        self.directory = self.root / self.sid
        self.directory.mkdir()
        self.state = {'status': 'editing', 'revision': 5, 'versions': []}

    def publish(self, image, **save_args):
        name = f'v{len(self.state["versions"])}.jpg'
        path = self.directory / name
        # Lossless encoded RGB fixture permits exact expected numeric checks.
        image.save(path, format='PNG', **save_args)
        self.state['versions'].append({'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        (self.directory / 'episode.json').write_text(json.dumps(self.state))
        return {'episode_id': self.sid, 'name': name}

    def test_known_srgb_luminance_and_gray(self):
        values = np.array([[[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255], [0, 0, 0], [128, 128, 128]]], dtype=np.uint8)
        np.testing.assert_allclose(luminance(values)[0], [.2126, .7152, .0722, 1, 0, .2158605], atol=1e-6)

    def test_canonical_quantiles_are_ordered_for_gray_gradient(self):
        gradient = np.tile(np.linspace(0, 255, 256, dtype=np.uint8), (64, 1))
        metrics = describe(Image.fromarray(np.stack([gradient] * 3, axis=-1)))
        quantiles = metrics['relative_luminance_quantiles']
        names = ('p0_5', 'p5', 'p20', 'p25', 'p50', 'p75', 'p80', 'p95', 'p99_5')
        self.assertEqual(sorted(quantiles[name] for name in names),
                         [quantiles[name] for name in names])
        self.assertAlmostEqual(metrics['saturation_mean'], 0.0, places=3)

    def test_roi_and_sampling_use_full_image_coordinates(self):
        image = Image.new('RGB', (10, 10), 'red')
        image.paste('blue', (5, 0, 10, 10))
        ref = self.publish(image)
        result = self.tools.measure(ref, roi=[.5, 0, .5, 1], points=[[0, 0], [1, 1]], sample_radius=0)
        self.assertEqual(result['metrics']['mean_rgb'], [0, 0, 255])
        self.assertEqual(result['metrics']['pixels'], 50)
        self.assertEqual(result['samples'][0]['mean_rgb'], [255, 0, 0])
        self.assertEqual(result['samples'][1]['pixel'], [9, 9])
        self.assertEqual(sum(result['metrics']['hue_histogram_12']), 50)
        self.assertEqual(sum(result['metrics']['relative_luminance_histogram_16']), 50)

    def test_bounded_sampling_clips_at_edges(self):
        ref = self.publish(Image.new('RGB', (10, 10), 'white'))
        report = self.tools.measure(ref, points=[[0, 0]], sample_radius=2)
        self.assertEqual(report['samples'][0]['box_pixels'], [0, 0, 3, 3])
        self.assertEqual(report['metrics']['highlight_clip_fraction'], 1)
        self.assertEqual(report['metrics']['achromatic_fraction'], 1)

    def test_measure_uses_canonical_schema_and_analysis_bound(self):
        ref = self.publish(Image.new('RGB', (128, 96), (120, 80, 40)))
        report = self.tools.measure(ref, grid=True, analysis_edge=64)
        self.assertEqual(report['schema_version'], MEASUREMENT_VERSION)
        self.assertEqual(report['dimensions'], [64, 48])
        self.assertEqual(report['analysis_edge'], 64)
        self.assertEqual(report['color_space'], 'sRGB')
        self.assertEqual(report['source']['kind'], 'episode_artifact')
        self.assertEqual(report['metrics']['dimensions'], [64, 48])
        self.assertIn('p0_5', report['metrics']['relative_luminance_quantiles'])
        self.assertIn('p99_5', report['metrics']['relative_luminance_quantiles'])
        self.assertEqual(len(report['metrics']['grid_8x8_relative_luminance']), 8)
        self.assertNotIn('interpretation', report)

    def test_read_only_does_not_recover_active_episode_or_touch_files(self):
        ref = self.publish(Image.new('RGB', (8, 8), 'gray'))
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()}
        self.tools.measure(ref)
        self.tools.compare(ref, ref)
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()}
        self.assertEqual(before, after)

    def test_aligned_delta_and_preview(self):
        a = self.publish(Image.new('RGB', (80, 40), (0, 0, 0)))
        b = self.publish(Image.new('RGB', (80, 40), (255, 255, 255)))
        report = self.tools.compare(a, b, mode='aligned', max_edge=64)
        self.assertEqual(report['schema_version'], COMPARISON_VERSION)
        self.assertEqual(report['comparison_basis'], {
            'color_space': 'sRGB',
            'resolution': 'decoded_native',
            'resampling': 'none',
        })
        self.assertEqual(report['aligned_error']['mae_rgb'], 255)
        self.assertEqual(report['aligned_error']['unit'],
                         'encoded_srgb_8bit_channel_value')
        self.assertEqual(report['aligned_error']['value_range'], [0.0, 255.0])
        self.assertEqual(report['metric_delta_right_minus_left']['mean_rgb'], [255]*3)
        with Image.open(io.BytesIO(base64.b64decode(report['preview']['base64']))) as preview:
            self.assertEqual(preview.size, (140, 56))
            self.assertTrue(preview.info['icc_profile'])
        same = self.tools.compare(a, a, mode='aligned')
        self.assertEqual(same['aligned_error']['mae_rgb'], 0)

    def test_episode_version_shortcut_matches_saved_render_and_stays_read_only(self):
        left = self.publish(Image.new('RGB', (80, 40), (20, 30, 40)))
        right = self.publish(Image.new('RGB', (80, 40), (40, 50, 60)))
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.directory.iterdir()}
        shortcut_left = {'episode_version': {'episode_id': self.sid, 'version': 0}}
        shortcut_right = {'episode_version': {'episode_id': self.sid, 'version': 1}}

        expected = self.tools.compare(left, right, mode='aligned', max_edge=64)
        actual = self.tools.compare(shortcut_left, shortcut_right,
                                    mode='aligned', max_edge=64)

        self.assertEqual(actual, expected)
        self.assertEqual(actual['left']['name'], 'v0.jpg')
        self.assertEqual(actual['right']['name'], 'v1.jpg')
        self.assertEqual(actual['aligned_error']['alignment'], 'caller_asserted_not_verified')
        self.assertEqual(self.tools.compare(left, shortcut_right),
                         self.tools.compare(left, right))
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.directory.iterdir()}
        self.assertEqual(before, after)

    def test_episode_version_shortcut_keeps_geometry_and_mode_rules(self):
        self.publish(Image.new('RGB', (10, 20), 'red'))
        self.publish(Image.new('RGB', (20, 10), 'red'))
        left = {'episode_version': {'episode_id': self.sid, 'version': 0}}
        right = {'episode_version': {'episode_id': self.sid, 'version': 1}}
        report = self.tools.compare(left, right)
        self.assertEqual(report['mode'], 'distribution')
        self.assertNotIn('aligned_error', report)
        with self.assertRaisesRegex(ValueError, 'identical dimensions and crop'):
            self.tools.compare(left, right, mode='aligned')

    def test_episode_version_shortcut_rejects_bad_addresses_and_unpublished_versions(self):
        ref = self.publish(Image.new('RGB', (8, 8)))
        address = {'episode_id': self.sid, 'version': 0}
        for source in (
                {'episode_version': None},
                {'episode_version': {}},
                {'episode_version': {**address, 'path': '/tmp/secret'}},
                {'episode_version': address, 'name': 'v0.jpg'},
                {'episode_version': {**address, 'episode_id': '../escape'}},
                *({'episode_version': {**address, 'version': value}}
                  for value in (True, False, -1, 0.0, '0', None, 1)),
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.tools.compare(ref, source)
        # The convenience form does not widen single-image measurement sources.
        with self.assertRaises(ValueError):
            self.tools.measure({'episode_version': address})

    def test_episode_version_shortcut_retains_hash_and_symlink_checks(self):
        ref = self.publish(Image.new('RGB', (8, 8)))
        source = {'episode_version': {'episode_id': self.sid, 'version': 0}}
        path = self.directory / ref['name']
        content = path.read_bytes()
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.tools.compare(source, source)
        path.write_bytes(content)
        target = self.root / 'outside.jpg'
        path.replace(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.tools.compare(source, source)

    def test_loaded_image_compare_keeps_native_resolution_separate_from_measure(self):
        stripes = np.zeros((64, 128, 3), dtype=np.uint8)
        stripes[:, 1::2] = 255
        image = Image.fromarray(stripes)

        measurement = measure_image(
            image, {'kind': 'memory'}, analysis_edge=64)
        comparison = compare_images(
            image, {'kind': 'left-memory'}, image, {'kind': 'right-memory'},
            mode='aligned', max_edge=64)

        self.assertEqual(measurement['schema_version'], MEASUREMENT_VERSION)
        self.assertEqual(measurement['metrics']['dimensions'], [64, 32])
        self.assertEqual(comparison['schema_version'], COMPARISON_VERSION)
        self.assertEqual(comparison['left_metrics']['dimensions'], [128, 64])
        self.assertEqual(comparison['right_metrics']['dimensions'], [128, 64])
        self.assertEqual(comparison['comparison_basis']['resolution'],
                         'decoded_native')
        self.assertEqual(comparison['comparison_basis']['resampling'], 'none')
        self.assertEqual(comparison['left'], {'kind': 'left-memory'})
        self.assertEqual(comparison['right'], {'kind': 'right-memory'})
        self.assertEqual(comparison['aligned_error']['mae_rgb'], 0)
        self.assertLess(
            measurement['metrics']['relative_luminance_mean'],
            comparison['left_metrics']['relative_luminance_mean'])
        # Neither metric calculation nor preview generation mutates inputs.
        self.assertEqual(image.size, (128, 64))

    def test_distribution_accepts_different_geometry_without_pixel_score(self):
        a = self.publish(Image.new('RGB', (10, 20), 'red'))
        b = self.publish(Image.new('RGB', (20, 10), 'red'))
        report = self.tools.compare(a, b)
        self.assertNotIn('aligned_error', report)
        self.assertEqual(report['metric_delta_right_minus_left']['mean_rgb'], [0, 0, 0])
        with self.assertRaises(ValueError):
            self.tools.compare(a, b, mode='aligned')

    def test_invalid_inputs_rejected(self):
        ref = self.publish(Image.new('RGB', (10, 10)))
        for args in ({'roi': [0, 0, 2, 1]}, {'roi': [0, 0, float('nan'), 1]},
                     {'roi': [False, 0, 1, 1]}, {'roi': [0, 0, .0001, 1]},
                     {'points': [[1.1, 0]]}, {'points': [[float('inf'), 0]]},
                     {'points': [[0, 0]] * 33}, {'sample_radius': True}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.tools.measure(ref, **args)
        for edge in (True, 63, 1025):
            with self.assertRaises(ValueError):
                self.tools.compare(ref, ref, max_edge=edge)

    def test_hash_unpublished_and_path_rejected(self):
        ref = self.publish(Image.new('RGB', (8, 8)))
        for name in ('../../secret.jpg', 'diff0.png', 'v1.jpg', 'v١.jpg'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.tools.measure({**ref, 'name': name})
        with self.assertRaises(ValueError):
            self.tools.measure({**ref, 'path': '/tmp/secret'})
        (self.directory / ref['name']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.tools.measure(ref)

    def test_symlink_escape_rejected(self):
        ref = self.publish(Image.new('RGB', (8, 8)))
        path = self.directory / ref['name']
        target = self.root / 'outside.jpg'
        path.replace(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.tools.measure(ref)

    def test_reference_hash_and_pixel_budget(self):
        ref = self.publish(Image.new('RGB', (8, 8)))
        raw = (self.directory / ref['name']).read_bytes()
        (self.directory / 'reference.jpg').write_bytes(raw)
        self.state['reference_sha256'] = hashlib.sha256(raw).hexdigest()
        (self.directory / 'episode.json').write_text(json.dumps(self.state))
        self.assertEqual(self.tools.measure({**ref, 'name': 'reference.jpg'})['metrics']['pixels'], 64)
        huge = self.publish(Image.new('RGB', (2049, 2048)))
        with self.assertRaisesRegex(ValueError, 'pixel budget'):
            self.tools.measure(huge)

    def test_icc_and_exif_orientation(self):
        image = Image.new('RGB', (10, 20), (127, 127, 127))
        exif = Image.Exif()
        exif[274] = 6
        ref = self.publish(image, exif=exif, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
        report = self.tools.measure(ref)
        self.assertEqual(report['source']['dimensions'], [20, 10])
        self.assertEqual(report['source']['color_policy'], 'embedded_icc_to_srgb')
        self.assertEqual(report['metrics']['mean_rgb'], [127]*3)
        bad = self.publish(image, icc_profile=b'not a profile')
        with self.assertRaisesRegex(ValueError, 'ICC'):
            self.tools.measure(bad)

    def test_preview_icc_is_frozen_srgb_and_reproducible(self):
        a = self.publish(Image.new('RGB', (80, 40), (0, 0, 0)))
        b = self.publish(Image.new('RGB', (80, 40), (255, 255, 255)))
        first = self.tools.compare(a, b, mode='aligned', max_edge=64)
        second = self.tools.compare(a, b, mode='aligned', max_edge=64)
        # Board ICC must be the frozen module bytes served verbatim, so two
        # independent calls produce byte-identical previews (createProfile
        # previously embedded a per-call creation-date header at byte 35).
        self.assertEqual(first['preview']['base64'], second['preview']['base64'])
        with Image.open(io.BytesIO(base64.b64decode(first['preview']['base64']))) as preview:
            self.assertEqual(preview.size, (140, 56))
            self.assertEqual(preview.info['icc_profile'], SRGB_ICC)
            profile = ImageCms.ImageCmsProfile(io.BytesIO(preview.info['icc_profile']))
            self.assertIn('srgb', ImageCms.getProfileDescription(profile).lower())
            decoded = np.asarray(preview.convert('RGB'), dtype=np.int16)
        self.assertLessEqual(np.abs(decoded[24:, :64]).max(), 2)  # LEFT paste: black.
        self.assertGreaterEqual(decoded[24:, 76:].min(), 253)  # RIGHT paste: white.


if __name__ == '__main__':
    unittest.main()
