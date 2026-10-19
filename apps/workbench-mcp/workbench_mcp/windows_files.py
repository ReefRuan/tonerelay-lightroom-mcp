"""Windows handle-based reads of confined persisted Session previews.

Windows has no ``openat`` / ``dir_fd`` support. Keep each directory open without
delete sharing while walking from the resolved Store root, and inspect the
opened handles rather than trusting a separate path check.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path

from workbench.store import StoreError


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("access_time", wintypes.FILETIME),
        ("write_time", wintypes.FILETIME),
        ("volume_serial", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_FileInformation)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _open_handle(kernel, path, *, directory, allow_reparse=False):
    # GENERIC_READ is also required for directories: an attributes-only handle
    # does not participate in Windows sharing checks and cannot anchor a path.
    access = 0x80000000
    # Directory contents may change, but an anchored directory may not be moved
    # or replaced. The final file additionally denies writers until read closes.
    share = 0x1 | (0x2 if directory else 0)
    flags = 0x02000000 if directory else 0  # FILE_FLAG_BACKUP_SEMANTICS
    if not allow_reparse:
        flags |= 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    handle = kernel.CreateFileW(str(path), access, share, None, 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _FileInformation()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not allow_reparse and info.attributes & 0x400:
            raise StoreError("session preview symlinks are not accepted")
        is_directory = bool(info.attributes & 0x10)
        if is_directory != directory or kernel.GetFileType(handle) != 1:
            raise StoreError("session preview is not a regular file"
                             if not directory else
                             "session preview parent is not a directory")
        return handle
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def _final_path(kernel, handle):
    size = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value)


def read_preview(root: Path, session_id: str, filename: str, max_bytes: int) -> bytes:
    """Read one bounded byte snapshot without traversing child reparse points.

    ``root`` is the resolved configured Store root, whose ancestors and root may
    legitimately be links. All entries below it must be ordinary directories or
    the ordinary final file. The caller validates the Session address and hash.
    """
    import msvcrt

    # A persisted file address cannot select an NTFS alternate data stream.
    if Path(filename).name != filename or filename in ("", ".", "..") or ":" in filename:
        raise StoreError("session preview path escapes its preview directory")
    kernel = _kernel32()
    directories = []
    file_handle = None
    descriptor = None
    try:
        root_handle = _open_handle(kernel, root, directory=True, allow_reparse=True)
        directories.append(root_handle)
        path = _final_path(kernel, root_handle)
        for component in ("sessions", session_id, "previews"):
            path = path / component
            directories.append(_open_handle(kernel, path, directory=True))
        file_handle = _open_handle(kernel, path / filename, directory=False)
        descriptor = msvcrt.open_osfhandle(file_handle, os.O_RDONLY | os.O_BINARY)
        file_handle = None  # ownership transferred to the CRT descriptor
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return stream.read(max_bytes + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if file_handle is not None:
            kernel.CloseHandle(file_handle)
        for handle in reversed(directories):
            kernel.CloseHandle(handle)
