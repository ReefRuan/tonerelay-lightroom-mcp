"""Ask the local operating system to open a Lightroom Catalog file.

The Lightroom Classic SDK exposes the active Catalog but not an API to switch
Catalogs. Opening a discovered ``.lrcat`` through the OS file association asks
Lightroom to perform its normal catalog switch flow, including any UI prompt.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class CatalogSwitchError(RuntimeError):
    pass


def is_supported_host() -> bool:
    return sys.platform == "darwin" or os.name == "nt"


def open_catalog_file(path: Path) -> None:
    """Open one already-discovered Catalog via the host's file association."""
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
        if (candidate.is_symlink() or not resolved.is_file()
                or resolved.suffix.lower() != ".lrcat"):
            raise CatalogSwitchError("目标不是可切换的 Lightroom Catalog 文件。")
    except CatalogSwitchError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise CatalogSwitchError("Lightroom Catalog 文件当前不可访问。") from None

    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["open", str(candidate.absolute())], check=True, timeout=15,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif os.name == "nt":
            os.startfile(str(candidate.absolute()))  # type: ignore[attr-defined]
        else:
            raise CatalogSwitchError(
                "Catalog 自动切换目前支持 macOS 和 Windows Runtime 主机。")
    except CatalogSwitchError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise CatalogSwitchError(
            "操作系统未能把该 Catalog 交给 Lightroom；请在 Lightroom 中手动打开。") from None
