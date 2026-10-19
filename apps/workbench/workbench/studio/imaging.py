"""Full-image measurements of aligned LR sRGB renders, not an aesthetic score."""
import hashlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pixels(path):
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode != 'RGB':
            raise ValueError('Expected an RGB Lightroom render')
        return np.asarray(image).astype(np.float32)


def compare(current, reference):
    a, b = pixels(current), pixels(reference)
    if a.shape != b.shape:
        raise ValueError('同图评价需要相同尺寸与裁切；不会自动拉伸对齐')
    error = np.abs(a - b)
    per_pixel = error.mean(axis=2)
    weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    la, lb = a @ weights, b @ weights
    # Encoded-sRGB luma statistics, explicitly not scene luminance or Delta E.
    result = {
        'metric_version': 'srgb-full-v1', 'dimensions': [a.shape[1], a.shape[0]],
        'mae_rgb': float(error.mean()), 'p95_rgb': float(np.percentile(per_pixel, 95)),
        'mae_luma': float(np.abs(la-lb).mean()),
        'channel_bias': [float(x) for x in (a-b).mean(axis=(0, 1))],
        'current_quantiles': [float(x) for x in np.percentile(la, [5, 25, 50, 75, 95])],
        'target_quantiles': [float(x) for x in np.percentile(lb, [5, 25, 50, 75, 95])],
        'highlight_clip': float(np.any(a >= 254, axis=2).mean()),
        'shadow_clip': float(np.all(a <= 1, axis=2).mean()),
        'target_highlight_clip': float(np.any(b >= 254, axis=2).mean()),
        'target_shadow_clip': float(np.all(b <= 1, axis=2).mean()),
        'current_sha256': sha(current), 'target_sha256': sha(reference),
    }
    tiles = []
    for row, ys in enumerate(np.array_split(np.arange(a.shape[0]), 4)):
        for col, xs in enumerate(np.array_split(np.arange(a.shape[1]), 4)):
            if len(ys) and len(xs):
                tiles.append({'row': row, 'col': col, 'mae_rgb': float(per_pixel[np.ix_(ys, xs)].mean())})
    result['tiles'] = sorted(tiles, key=lambda x: x['mae_rgb'], reverse=True)
    return result
