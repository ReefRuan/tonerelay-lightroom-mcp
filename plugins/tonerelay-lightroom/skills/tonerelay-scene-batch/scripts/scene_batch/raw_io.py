"""Fast seek-based EXIF and embedded JPEG preview extraction for RAW and standard images."""
from __future__ import annotations

import io
import re
import struct
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

RAW_TIFF_EXTS = frozenset({
    ".arw", ".dng", ".nef", ".cr2", ".orf", ".rw2", ".pef", ".srw",
})
SCAN_RAW_EXTS = frozenset({".cr3", ".raf"})
STANDARD_IMG_EXTS = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"})
SUPPORTED_EXTS = RAW_TIFF_EXTS | SCAN_RAW_EXTS | STANDARD_IMG_EXTS

_TIFF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
_EXIF_TAGS = {
    0x9003: "dt",
    0x8827: "iso",
    0x829A: "shutter",
    0x829D: "fnum",
    0x9203: "bv",
    0x9204: "ev",
    0x920A: "fl",
}
_MAX_FALLBACK_SCAN_BYTES = 6 * 1024 * 1024


def _parse_dt(dt_str: str | None) -> float | None:
    if not dt_str or not isinstance(dt_str, str):
        return None
    clean = dt_str.strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z"):
        try:
            return datetime.strptime(clean[:19], fmt[:17] if "%z" not in fmt else fmt).timestamp()
        except ValueError:
            continue
    return None


def _read_ifd(f, offset: int, endian: str, file_size: int) -> tuple[dict, int]:
    if offset <= 0 or offset + 2 > file_size:
        return {}, 0
    f.seek(offset)
    raw_cnt = f.read(2)
    if len(raw_cnt) < 2:
        return {}, 0
    count = struct.unpack(endian + "H", raw_cnt)[0]
    if count > 4096 or offset + 2 + count * 12 > file_size:
        return {}, 0
    buf = f.read(count * 12)
    entries = {}
    for i in range(count):
        tag, typ, cnt, val_bytes = struct.unpack_from(endian + "HHI4s", buf, i * 12)
        entries[tag] = (typ, cnt, val_bytes)
    nxt_bytes = f.read(4)
    nxt = struct.unpack(endian + "I", nxt_bytes)[0] if len(nxt_bytes) == 4 else 0
    return entries, nxt


def _decode_tag(f, endian: str, typ: int, cnt: int, val_bytes: bytes, file_size: int):
    unit = _TIFF_TYPE_SIZES.get(typ)
    if unit is None or cnt <= 0 or cnt > 65536:
        return None
    size = unit * cnt
    if size <= 4:
        raw = val_bytes[:size]
    else:
        off = struct.unpack(endian + "I", val_bytes)[0]
        if off + size > file_size:
            return None
        f.seek(off)
        raw = f.read(size)
        if len(raw) < size:
            return None
    if typ == 2:
        return raw.rstrip(b"\x00").decode("ascii", errors="replace")
    if typ == 3:
        vals = struct.unpack(endian + f"{cnt}H", raw)
        return vals[0] if cnt == 1 else list(vals)
    if typ == 4:
        vals = struct.unpack(endian + f"{cnt}I", raw)
        return vals[0] if cnt == 1 else list(vals)
    if typ == 5:
        a, b = struct.unpack(endian + "II", raw[:8])
        return a / b if b else 0.0
    if typ == 10:
        a, b = struct.unpack(endian + "ii", raw[:8])
        return a / b if b else 0.0
    return None


def _is_jpeg_at(f, offset: int, length: int, file_size: int) -> bool:
    if offset <= 0 or length < 128 or offset + length > file_size:
        return False
    f.seek(offset)
    if f.read(3) != b"\xff\xd8\xff":
        return False
    tail_len = min(32, length)
    f.seek(offset + length - tail_len)
    return b"\xff\xd9" in f.read(tail_len)


def _scan_largest_jpeg(f, file_size: int) -> tuple[int, int] | tuple[None, None]:
    f.seek(0)
    chunk = f.read(min(file_size, _MAX_FALLBACK_SCAN_BYTES))
    best_off, best_len = None, 0
    for match in re.finditer(b"\xff\xd8\xff", chunk):
        start = match.start()
        end_idx = chunk.find(b"\xff\xd9", start + 3)
        if end_idx != -1:
            length = (end_idx + 2) - start
            if length > best_len and length >= 4096:
                best_off, best_len = start, length
    return best_off, (best_len if best_off is not None else None)


def _read_tiff_raw_meta(path: Path, meta: dict) -> dict:
    file_size = path.stat().st_size
    with path.open("rb") as f:
        hdr = f.read(8)
        if len(hdr) < 8 or hdr[:2] not in (b"II", b"MM"):
            off, length = _scan_largest_jpeg(f, file_size)
            meta["preview_offset"] = off
            meta["preview_length"] = length
            return meta
        endian = "<" if hdr[:2] == b"II" else ">"
        ifd0_off = struct.unpack(endian + "I", hdr[4:8])[0]
        ifd0, ifd1_off = _read_ifd(f, ifd0_off, endian, file_size)

        if 0x0112 in ifd0:
            ori = _decode_tag(f, endian, *ifd0[0x0112], file_size)
            if isinstance(ori, int) and 1 <= ori <= 8:
                meta["orientation"] = ori

        if 0x8769 in ifd0:
            exif_off = _decode_tag(f, endian, *ifd0[0x8769], file_size)
            if isinstance(exif_off, int):
                eifd, _ = _read_ifd(f, exif_off, endian, file_size)
                for tag, key in _EXIF_TAGS.items():
                    if tag in eifd:
                        val = _decode_tag(f, endian, *eifd[tag], file_size)
                        if val is not None:
                            meta[key] = val

        # Collect candidate preview JPEGs across IFD0, SubIFDs, and IFD1; pick largest valid JPEG.
        ifds = [ifd0]
        if 0x014A in ifd0:
            sub_offs = _decode_tag(f, endian, *ifd0[0x014A], file_size)
            if isinstance(sub_offs, int):
                sub_offs = [sub_offs]
            if isinstance(sub_offs, list):
                for so in sub_offs[:4]:
                    sub_ifd, _ = _read_ifd(f, so, endian, file_size)
                    if sub_ifd:
                        ifds.append(sub_ifd)
        if ifd1_off:
            ifd1, _ = _read_ifd(f, ifd1_off, endian, file_size)
            if ifd1:
                ifds.append(ifd1)

        best_off, best_len = None, 0
        for ifd in ifds:
            if 0x0201 in ifd and 0x0202 in ifd:
                jo = _decode_tag(f, endian, *ifd[0x0201], file_size)
                jl = _decode_tag(f, endian, *ifd[0x0202], file_size)
                if isinstance(jo, int) and isinstance(jl, int) and jl > best_len:
                    if _is_jpeg_at(f, jo, jl, file_size):
                        best_off, best_len = jo, jl

        if best_off is None:
            for ifd in ifds:
                comp = _decode_tag(f, endian, *ifd[0x0103], file_size) if 0x0103 in ifd else None
                if comp in (6, 7) and 0x0111 in ifd and 0x0117 in ifd:
                    jo = _decode_tag(f, endian, *ifd[0x0111], file_size)
                    jl = _decode_tag(f, endian, *ifd[0x0117], file_size)
                    if isinstance(jo, int) and isinstance(jl, int) and jl <= 12 * 1024 * 1024 and jl > best_len:
                        if _is_jpeg_at(f, jo, jl, file_size):
                            best_off, best_len = jo, jl

        if best_off is None:
            best_off, best_len = _scan_largest_jpeg(f, file_size)
        meta["preview_offset"] = best_off
        meta["preview_length"] = best_len

    meta["timestamp"] = _parse_dt(meta.get("dt"))
    return meta


def _rat_to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _read_standard_image_meta(path: Path, meta: dict) -> dict:
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                ori = exif.get(0x0112)
                if isinstance(ori, int) and 1 <= ori <= 8:
                    meta["orientation"] = ori
                ifd = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
                dt = ifd.get(0x9003) or exif.get(0x9003) or exif.get(0x0132)
                if dt:
                    meta["dt"] = str(dt)
                for tag, key in ((0x8827, "iso"), (0x829A, "shutter"),
                                 (0x829D, "fnum"), (0x9203, "bv"),
                                 (0x9204, "ev"), (0x920A, "fl")):
                    raw_val = ifd.get(tag)
                    if raw_val is not None:
                        meta[key] = int(raw_val) if key == "iso" else _rat_to_float(raw_val)
    except OSError:
        pass
    meta["timestamp"] = _parse_dt(meta.get("dt"))
    return meta


def read_exif_and_preview_meta(path: Path) -> dict:
    """Read EXIF metadata and embedded JPEG preview offsets without decoding the sensor payload."""
    path = Path(path)
    ext = path.suffix.lower()
    meta: dict = {
        "file_name": path.name,
        "stem": path.stem.upper() if path.stem.upper().startswith("DSC") else path.stem,
        "path": str(path),
        "ext": ext,
        "orientation": 1,
        "dt": None,
        "timestamp": None,
        "preview_offset": None,
        "preview_length": None,
    }
    if ext in RAW_TIFF_EXTS:
        return _read_tiff_raw_meta(path, meta)
    if ext in SCAN_RAW_EXTS:
        with path.open("rb") as f:
            off, length = _scan_largest_jpeg(f, path.stat().st_size)
        meta["preview_offset"] = off
        meta["preview_length"] = length
        return meta
    return _read_standard_image_meta(path, meta)


def _apply_tiff_orientation(img: Image.Image, orientation: int) -> Image.Image:
    ops = {
        2: lambda im: im.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
        3: lambda im: im.transpose(Image.Transpose.ROTATE_180),
        4: lambda im: im.transpose(Image.Transpose.FLIP_TOP_BOTTOM),
        5: lambda im: im.transpose(Image.Transpose.TRANSPOSE),
        6: lambda im: im.transpose(Image.Transpose.ROTATE_270),
        7: lambda im: im.transpose(Image.Transpose.TRANSVERSE),
        8: lambda im: im.transpose(Image.Transpose.ROTATE_90),
    }
    fn = ops.get(orientation)
    return fn(img) if fn else img


def load_image_and_dimensions(
    path: Path,
    max_edge: int | None = None,
) -> tuple[Image.Image, int, int]:
    """Open a standard image once, record true upright dimensions, and decode downscaled via JPEG draft."""
    path = Path(path)
    with Image.open(path) as src:
        orig_w, orig_h = src.size
        exif = src.getexif()
        ori = exif.get(274) if exif else None
        if max_edge is not None and src.format == "JPEG":
            src.draft("RGB", (max_edge * 2, max_edge * 2))
        src.load()
        img = src.copy()
    if isinstance(ori, int) and 2 <= ori <= 8:
        img = _apply_tiff_orientation(img, ori)
        if ori in (5, 6, 7, 8):
            orig_w, orig_h = orig_h, orig_w
    if img.mode != "RGB":
        img = img.convert("RGB")
    if max_edge is not None and max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.BILINEAR)
    return img, orig_w, orig_h


def load_preview_image(
    path: Path,
    meta: dict | None = None,
    max_edge: int | None = None,
) -> Image.Image:
    """Load an upright RGB preview image quickly using embedded JPEG seek when available."""
    path = Path(path)
    if meta is None:
        meta = read_exif_and_preview_meta(path)
    off = meta.get("preview_offset")
    length = meta.get("preview_length")

    if isinstance(off, int) and isinstance(length, int) and length > 0:
        with path.open("rb") as f:
            f.seek(off)
            raw_bytes = f.read(length)
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
        # Apply embedded JPEG's own orientation if set, else fall back to RAW IFD0 orientation.
        jpg_ori = img.getexif().get(274) if img.getexif() else None
        if isinstance(jpg_ori, int) and jpg_ori != 1:
            img = ImageOps.exif_transpose(img)
        elif meta.get("orientation", 1) != 1:
            img = _apply_tiff_orientation(img, meta["orientation"])
    else:
        with Image.open(path) as src:
            exif = src.getexif()
            ori = exif.get(274) if exif else None
            if max_edge is not None and src.format == "JPEG":
                src.draft("RGB", (max_edge * 2, max_edge * 2))
            src.load()
            img = src.copy()
        if isinstance(ori, int) and 2 <= ori <= 8:
            img = _apply_tiff_orientation(img, ori)

    if img.mode != "RGB":
        img = img.convert("RGB")
    if max_edge is not None and max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.BILINEAR)
    return img


def compute_sharpness(rgb_u8: np.ndarray) -> float:
    """Compute 3x3 Laplacian variance on luminance as a fast focus/sharpness score."""
    if rgb_u8.ndim != 3 or rgb_u8.shape[0] < 4 or rgb_u8.shape[1] < 4:
        return 0.0
    rgb = rgb_u8.astype(np.float32)
    lum = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    lap = (
        -4.0 * lum[1:-1, 1:-1]
        + lum[:-2, 1:-1]
        + lum[2:, 1:-1]
        + lum[1:-1, :-2]
        + lum[1:-1, 2:]
    )
    return float(np.var(lap))
