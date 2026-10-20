"""Deterministic, read-only image measurements shared by Runtime surfaces.

This module owns the canonical measurement schema.  Source adapters may load an
image elsewhere, but all measurements pass through :func:`measure_image` and
:func:`describe`.  The results are display-referred sRGB facts, never an
aesthetic score, recommendation, or inverse mapping to Lightroom sliders.
"""
import base64
import hashlib
import io
import json
import math
import re
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageOps

from .srgb_profile import SRGB_ICC

MEASUREMENT_VERSION = 'image-measurement-v1'
COMPARISON_VERSION = 'image-comparison-v1'
MAX_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 2048 * 2048
MIN_ANALYSIS_EDGE = 64
MAX_ANALYSIS_EDGE = 4096


def box_for(roi, size):
    if roi is None:
        return (0, 0, *size)
    if (not isinstance(roi, list) or len(roi) != 4 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in roi)):
        raise ValueError('roi must be finite [x,y,width,height]')
    x, y, w, h = roi
    if min(x, y) < 0 or min(w, h) <= 0 or x+w > 1 or y+h > 1:
        raise ValueError('roi outside normalized image bounds')
    width, height = size
    box = (round(x*width), round(y*height), round((x+w)*width), round((y+h)*height))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError('roi is smaller than one pixel')
    return box


def luminance(rgb):
    encoded = rgb.astype(np.float32) / 255
    linear = np.where(encoded <= .04045, encoded / 12.92,
                      ((encoded + .055) / 1.055) ** 2.4)
    return linear @ np.array([.2126, .7152, .0722], dtype=np.float32)


def decode_srgb(source, require_rgb=False):
    """Decode a trusted path or bounded byte buffer and normalize it to sRGB."""
    if isinstance(source, (bytes, bytearray)):
        if len(source) > MAX_BYTES:
            raise ValueError('Image exceeds 32 MiB analysis limit')
        opened_context = Image.open(io.BytesIO(source))
    else:
        opened_context = Image.open(source)
    with opened_context as opened:
        if opened.width * opened.height > MAX_PIXELS:
            raise ValueError('Image exceeds 2048*2048 analysis pixel budget')
        if require_rgb and opened.mode != 'RGB':
            raise ValueError('Expected an RGB Lightroom render')
        profile = opened.info.get('icc_profile')
        oriented = ImageOps.exif_transpose(opened)
        if profile:
            try:
                image = ImageCms.profileToProfile(
                    oriented, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                    ImageCms.createProfile('sRGB'), outputMode='RGB')
            except (OSError, ValueError, ImageCms.PyCMSError) as exc:
                raise ValueError('Invalid or unsupported embedded ICC profile') from exc
            policy = 'embedded_icc_to_srgb'
        else:
            image = oriented.convert('RGB')
            policy = 'assumed_srgb_from_source_contract'
        return image.copy(), policy


def _circular_hue_mean(hue_deg):
    if hue_deg.size == 0:
        return None
    radians = np.deg2rad(hue_deg.astype(np.float64))
    sin_mean = float(np.sin(radians).mean())
    cos_mean = float(np.cos(radians).mean())
    if math.hypot(sin_mean, cos_mean) < 1e-9:
        return None
    return float(math.degrees(math.atan2(sin_mean, cos_mean)) % 360.0)


def _band_metrics(lum, sat, hue, mask):
    total = int(mask.sum())
    if total == 0:
        return {'pixel_fraction': 0.0, 'chromatic_fraction': 0.0,
                'hue_mean_deg': None, 'saturation_mean': None,
                'relative_luminance_mean': None}
    chromatic = mask & (sat >= 26)  # HSV S >= approximately 0.10.
    chromatic_count = int(chromatic.sum())
    return {
        'pixel_fraction': total / lum.size,
        'chromatic_fraction': chromatic_count / total,
        'hue_mean_deg': _circular_hue_mean(hue[chromatic] * (360.0 / 255.0)),
        'saturation_mean': (float(sat[chromatic].mean() / 255.0)
                            if chromatic_count else None),
        'relative_luminance_mean': float(lum[mask].mean()),
    }


def _grid_luminance(lum):
    cells = []
    height, width = lum.shape
    for grid_y in range(8):
        row = []
        for grid_x in range(8):
            y0, y1 = grid_y * height // 8, (grid_y + 1) * height // 8
            x0, x1 = grid_x * width // 8, (grid_x + 1) * width // 8
            cell = lum[y0:y1, x0:x1]
            row.append(round(float(cell.mean()), 4) if cell.size else None)
        cells.append(row)
    return cells


def describe(image, grid=False):
    """Return the one canonical metrics object for an RGB image."""
    if image.mode != 'RGB':
        image = image.convert('RGB')
    rgb = np.asarray(image)
    lum = luminance(rgb)
    hsv = np.asarray(image.convert('HSV'))
    hue_channel, saturation = hsv[:, :, 0], hsv[:, :, 1]
    chromatic = saturation >= 26
    hue_histogram = np.histogram(hue_channel[chromatic], bins=12, range=(0, 256))[0]
    quantile_names = ('p0_5', 'p5', 'p20', 'p25', 'p50', 'p75', 'p80', 'p95', 'p99_5')
    quantile_values = np.percentile(lum, [.5, 5, 20, 25, 50, 75, 80, 95, 99.5])
    quantiles = dict(zip(quantile_names, (float(value) for value in quantile_values)))
    midtone = (lum >= quantiles['p25']) & (lum <= quantiles['p75'])
    if int(midtone.sum()):
        midtone_r_minus_b = (float(rgb[:, :, 0][midtone].mean())
                             - float(rgb[:, :, 2][midtone].mean()))
    else:
        midtone_r_minus_b = None
    result = {
        'dimensions': [image.width, image.height],
        'pixels': image.width * image.height,
        'mean_rgb': rgb.mean(axis=(0, 1)).tolist(),
        'rgb_histogram_16': [np.histogram(rgb[:, :, c], bins=16, range=(0, 256))[0].tolist() for c in range(3)],
        'relative_luminance_mean': float(lum.mean()),
        'relative_luminance_quantiles': quantiles,
        'relative_luminance_histogram_16': np.histogram(lum, bins=16, range=(0, 1))[0].tolist(),
        'hue_histogram_12': hue_histogram.tolist(),
        'achromatic_fraction': float((~chromatic).mean()),
        'saturation_mean': float(saturation.mean() / 255),
        'midtone_r_minus_b': midtone_r_minus_b,
        'shadow_20pct': _band_metrics(lum, saturation, hue_channel,
                                      lum <= quantiles['p20']),
        'highlight_20pct': _band_metrics(lum, saturation, hue_channel,
                                         lum >= quantiles['p80']),
        'highlight_clip_fraction': float(np.any(rgb >= 254, axis=2).mean()),
        'shadow_clip_fraction': float(np.all(rgb <= 1, axis=2).mean()),
    }
    if grid:
        result['grid_8x8_relative_luminance'] = _grid_luminance(lum)
    return result


def diff_metrics(left, right):
    """Return deterministic numeric deltas (right minus left).

    Circular hue means are deliberately not subtracted: that would imply a
    meaningful linear direction across the 0/360-degree boundary.
    """
    result = {
        'mean_rgb': (np.asarray(right['mean_rgb'])
                     - np.asarray(left['mean_rgb'])).tolist(),
    }
    for key in ('relative_luminance_mean', 'achromatic_fraction', 'saturation_mean',
                'midtone_r_minus_b', 'highlight_clip_fraction',
                'shadow_clip_fraction'):
        a, b = left.get(key), right.get(key)
        result[key] = (b - a if isinstance(a, (int, float))
                       and isinstance(b, (int, float)) else None)
    left_quantiles = left.get('relative_luminance_quantiles') or {}
    right_quantiles = right.get('relative_luminance_quantiles') or {}
    result['relative_luminance_quantiles'] = {
        key: right_quantiles[key] - left_quantiles[key]
        for key in left_quantiles if key in right_quantiles
    }
    for band in ('shadow_20pct', 'highlight_20pct'):
        a, b = left.get(band) or {}, right.get(band) or {}
        result[band] = {
            key: (b[key] - a[key] if isinstance(a.get(key), (int, float))
                  and isinstance(b.get(key), (int, float)) else None)
            for key in ('pixel_fraction', 'chromatic_fraction',
                        'saturation_mean', 'relative_luminance_mean')
        }
    return result


def resize_for_analysis(image, analysis_edge):
    if type(analysis_edge) is not int or not MIN_ANALYSIS_EDGE <= analysis_edge <= MAX_ANALYSIS_EDGE:
        raise ValueError(f'analysis_edge must be an integer in {MIN_ANALYSIS_EDGE}..{MAX_ANALYSIS_EDGE}')
    if max(image.size) <= analysis_edge:
        return image
    scale = analysis_edge / max(image.size)
    return image.resize((max(1, round(image.width * scale)),
                         max(1, round(image.height * scale))), Image.Resampling.LANCZOS)


def measure_image(image, source, roi=None, points=None, sample_radius=2,
                  grid=False, analysis_edge=1024):
    """Measure an already-loaded sRGB image without interpreting the result."""
    if type(grid) is not bool:
        raise ValueError('grid must be a boolean')
    if type(sample_radius) is not int or not 0 <= sample_radius <= 32:
        raise ValueError('sample_radius must be an integer in 0..32 pixels')
    if points is None:
        points = []
    if not isinstance(points, list) or len(points) > 32:
        raise ValueError('points must be a list of at most 32 normalized [x,y] coordinates')
    for point in points:
        if (not isinstance(point, list) or len(point) != 2 or
                any(type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 <= value <= 1 for value in point)):
            raise ValueError('points must contain normalized finite [x,y] coordinates')
    image = resize_for_analysis(image.convert('RGB'), analysis_edge)
    box = box_for(roi, image.size)
    samples = []
    for x, y in points:
        pixel_x = min(int(x * image.width), image.width - 1)
        pixel_y = min(int(y * image.height), image.height - 1)
        region = (max(0, pixel_x - sample_radius), max(0, pixel_y - sample_radius),
                  min(image.width, pixel_x + sample_radius + 1),
                  min(image.height, pixel_y + sample_radius + 1))
        pixels = np.asarray(image.crop(region))
        samples.append({
            'point': [x, y],
            'pixel': [pixel_x, pixel_y],
            'box_pixels': list(region),
            'mean_rgb': pixels.mean(axis=(0, 1)).tolist(),
            'relative_luminance_mean': float(luminance(pixels).mean()),
        })
    return {
        'schema_version': MEASUREMENT_VERSION,
        'source': source,
        'dimensions': [image.width, image.height],
        'analysis_edge': max(image.size),
        'color_space': 'sRGB',
        'roi_pixels': list(box),
        'metrics': describe(image.crop(box), grid=grid),
        'samples': samples,
    }


def compare_images(left_image, left_meta, right_image, right_meta,
                   mode='distribution', roi=None, max_edge=512):
    """Compare two already-decoded, display-referred sRGB images.

    Comparison deliberately operates on each image's decoded/native
    resolution after applying the requested ROI.  It does not use
    :func:`measure_image` and does not apply that function's analysis-edge
    downsampling.  Source adapters are responsible for loading trustworthy
    bytes and providing their provenance in ``left_meta``/``right_meta``.
    """
    if mode not in ('distribution', 'aligned'):
        raise ValueError('mode must be distribution or aligned')
    if type(max_edge) is not int or not 64 <= max_edge <= 1024:
        raise ValueError('max_edge must be an integer in 64..1024')

    left_box = box_for(roi, left_image.size)
    right_box = box_for(roi, right_image.size)
    if (mode == 'aligned' and
            (left_image.size != right_image.size or left_box != right_box)):
        raise ValueError(
            'Aligned comparison requires identical dimensions and crop; '
            'no automatic stretching')

    left_crop = left_image.crop(left_box).convert('RGB')
    right_crop = right_image.crop(right_box).convert('RGB')
    left_metrics = describe(left_crop)
    right_metrics = describe(right_crop)
    result = {
        'schema_version': COMPARISON_VERSION,
        'comparison_basis': {
            'color_space': 'sRGB',
            'resolution': 'decoded_native',
            'resampling': 'none',
        },
        'mode': mode,
        'left': left_meta,
        'right': right_meta,
        'left_roi_pixels': list(left_box),
        'right_roi_pixels': list(right_box),
        'left_metrics': left_metrics,
        'right_metrics': right_metrics,
        'metric_delta_right_minus_left': diff_metrics(left_metrics, right_metrics),
    }
    if mode == 'aligned':
        error = np.abs(np.asarray(left_crop).astype(np.float32)
                       - np.asarray(right_crop).astype(np.float32))
        result['aligned_error'] = {
            'alignment': 'caller_asserted_not_verified',
            'unit': 'encoded_srgb_8bit_channel_value',
            'value_range': [0.0, 255.0],
            'mae_rgb': float(error.mean()),
            'p95_pixel_mae_rgb': float(np.percentile(error.mean(axis=2), 95)),
        }

    # Preview sizing is presentation-only and never changes the metrics above.
    left_preview = left_crop.copy()
    right_preview = right_crop.copy()
    left_preview.thumbnail((max_edge, max_edge))
    right_preview.thumbnail((max_edge, max_edge))
    board = Image.new(
        'RGB',
        (left_preview.width + right_preview.width + 12,
         max(left_preview.height, right_preview.height) + 24),
        '#202020')
    board.paste(left_preview, (0, 24))
    board.paste(right_preview, (left_preview.width + 12, 24))
    draw = ImageDraw.Draw(board)
    draw.text((4, 5), 'LEFT', fill='white')
    draw.text((left_preview.width + 16, 5), 'RIGHT', fill='white')
    buffer = io.BytesIO()
    board.save(buffer, format='JPEG', quality=90, icc_profile=SRGB_ICC)
    result['preview'] = {
        'mime_type': 'image/jpeg',
        'dimensions': list(board.size),
        'base64': base64.b64encode(buffer.getvalue()).decode('ascii'),
    }
    return result


class ImageTools:
    def __init__(self, episodes_root):
        self.root = Path(episodes_root).resolve()

    def _load(self, source):
        if not isinstance(source, dict) or set(source) != {'episode_id', 'name'}:
            raise ValueError('source requires only episode_id and name')
        sid, name = source['episode_id'], source['name']
        if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:
            raise ValueError('Invalid episode ID')
        if not isinstance(name, str) or not re.fullmatch(r'reference\.jpg|v[0-9]+\.jpg', name):
            raise ValueError('Only published reference.jpg or vN.jpg renders are accepted')
        directory = self.root / sid
        if directory.resolve() != directory:
            raise ValueError('Episode symlinks are not accepted')
        state_path, path = directory / 'episode.json', directory / name
        if state_path.is_symlink() or path.is_symlink():
            raise ValueError('Artifact symlinks are not accepted')
        state = json.loads(state_path.read_text(encoding='utf-8'))  # Deliberately no Studio.get recovery/write.
        versions = state.get('versions') or []
        if not versions:
            raise ValueError('No published version yet')
        if name == 'reference.jpg':
            expected = state.get('reference_sha256')
            if not expected:
                expected = versions[0].get('metrics', {}).get('target_sha256')
            wb_readback = None
        else:
            index = int(name[1:-4])
            if index >= len(versions):
                raise ValueError('Version has not been published')
            version = versions[index]
            expected = version.get('sha256')
            snapshot = version.get('snapshot') or {}
            wb_keys = ('WhiteBalance', 'Temperature', 'Tint',
                       'IncrementalTemperature', 'IncrementalTint')
            wb_readback = ({key: snapshot[key] for key in wb_keys if key in snapshot}
                           if isinstance(snapshot, dict) else {}) or None
        with path.open('rb') as handle:
            content = handle.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError('Render exceeds 32 MiB analysis limit')
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected:
            raise ValueError('Published render hash mismatch')
        # Decode the exact verified bytes, avoiding a second path read.
        image, color_policy = decode_srgb(content, require_rgb=True)
        provenance = {**source, 'sha256': digest, 'dimensions': list(image.size),
                      'kind': 'episode_artifact',
                      'color_space': 'sRGB',
                      'color_policy': (color_policy if color_policy == 'embedded_icc_to_srgb'
                                       else 'assumed_srgb_from_lr_export_contract')}
        if wb_readback is not None:
            provenance['white_balance_readback'] = wb_readback
            provenance['white_balance_meaning'] = (
                'Lightroom develop setting, not a measured scene illuminant CCT; '
                'IncrementalTemperature on rendered files is not kelvin')
        return image, provenance

    def load(self, source):
        """Load one hash-verified published Episode artifact."""
        return self._load(source)

    def measure(self, source, roi=None, points=None, sample_radius=2, grid=False,
                analysis_edge=1024):
        image, meta = self._load(source)
        return measure_image(image, meta, roi=roi, points=points,
                             sample_radius=sample_radius, grid=grid,
                             analysis_edge=analysis_edge)

    def compare(self, left, right, mode='distribution', roi=None, max_edge=512):
        # Normalize both addresses before reading either render.  The shortcut
        # changes addressing only, never the persisted-source or alignment rules.
        left = self._comparison_source(left)
        right = self._comparison_source(right)
        left_image, left_meta = self._load(left)
        right_image, right_meta = self._load(right)
        return compare_images(
            left_image, left_meta, right_image, right_meta,
            mode=mode, roi=roi, max_edge=max_edge)

    @staticmethod
    def _comparison_source(source):
        """Resolve a version shortcut to the existing published-render address."""
        if not isinstance(source, dict):
            raise ValueError('comparison source must be a nonempty object')
        if set(source) == {'episode_id', 'name'}:
            return source
        if set(source) != {'episode_version'}:
            raise ValueError('comparison source must be a published episode render '
                             'or an episode_version address')
        version = source['episode_version']
        if not isinstance(version, dict) or set(version) != {'episode_id', 'version'}:
            raise ValueError('episode_version requires only episode_id and version')
        number = version['version']
        if type(number) is not int or number < 0:
            raise ValueError('episode_version version must be a nonnegative integer')
        return {'episode_id': version['episode_id'], 'name': f'v{number}.jpg'}
