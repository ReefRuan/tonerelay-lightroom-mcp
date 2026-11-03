"""Conservative user-name protection for existing Lightroom working copies."""
from __future__ import annotations

import re


_CHINESE_MARKERS = ('保护', '锁定', '勿改', '只读', '留档')
_ENGLISH_MARKERS = re.compile(
    r'(?<![a-z0-9])(?:protected|locked|readonly|read[\s_-]+only|do[\s_-]+not[\s_-]+edit)(?![a-z0-9])',
    re.IGNORECASE,
)


def is_protected_copy_name(name: str) -> bool:
    """Names restrict edits; their other contents never grant authority."""
    if not isinstance(name, str):
        raise ValueError('Invalid virtual-copy name')
    return any(marker in name for marker in _CHINESE_MARKERS) or bool(_ENGLISH_MARKERS.search(name))


def require_editable_copy(photo: dict) -> str:
    name = photo.get('copy_name', '')
    if not isinstance(name, str):
        raise ValueError('Invalid virtual-copy name; read the photo again')
    if is_protected_copy_name(name):
        raise ValueError('Virtual copy is protected by its name; leave this copy unchanged')
    return name
