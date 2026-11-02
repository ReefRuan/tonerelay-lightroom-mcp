"""Startup information for an existing Windows console, separate from stdio.

Never allocate a console: managed/background services remain in the background.
The MCP JSON-RPC streams are not used for this presentation.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import sys

from . import __version__
from .configuration import active_store


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _line(value) -> str:
    # Paths and hostnames are data, never terminal control sequences.
    return "".join(c if c.isprintable() else "?" for c in str(value))


def bridge_summary(host: str, port: int, data_root: Path, logs_root: Path) -> str:
    store = active_store()
    config = store.path if store else "环境变量（未使用统一配置文件）"
    paths = store.paths() if store else {}
    source = Path(__file__).resolve().parents[3]
    address = f"[{host}]" if ":" in host else host
    scope = ("纯本地 MCP 配套服务：仅监听本机回环地址。" if is_loopback(host)
             else "MCP 配套服务：监听范围包含本机以外的地址。")
    fields = [
        ("监听地址", f"http://{address}:{port}"),
        ("Python", sys.executable),
        ("运行源码", source),
        ("配置来源", config),
        ("数据目录", data_root),
        ("日志目录", logs_root),
        ("Python 缓存", os.environ.get("PYTHONPYCACHEPREFIX") or "解释器默认"),
        ("临时目录", paths.get("tmp") or os.environ.get("TEMP") or os.environ.get("TMPDIR")
         or os.environ.get("TMP") or "系统默认"),
    ]
    return "\n".join([
        "", f"ToneRelay for Lightroom Classic | Bridge v{__version__}", scope,
        *(f"  {label}: {_line(value)}" for label, value in fields),
        "这是服务启动摘要；Lightroom 连接与 Catalog 授权状态请通过 Agent 查询。",
        "AI 模型由客户端提供；本地服务不代表 AI 离线运行。", "",
    ])


def write_existing_console(message: str) -> bool:
    """Use CONOUT$ even when stdout/stderr are redirected to a log file."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetConsoleMode.restype = wintypes.BOOL
    kernel.WriteConsoleW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.WriteConsoleW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        return False
    try:
        mode = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        message = message.replace("\n", "\r\n")
        written = wintypes.DWORD()
        length = len(message.encode("utf-16-le")) // 2
        return bool(kernel.WriteConsoleW(handle, message, length, ctypes.byref(written), None)
                    and written.value == length)
    finally:
        kernel.CloseHandle(handle)


def show_bridge_startup(host: str, port: int, data_root: Path, logs_root: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        return write_existing_console(bridge_summary(host, port, data_root, logs_root))
    except (OSError, ValueError, AttributeError):
        # Presentation must not prevent the service from starting.
        return False
