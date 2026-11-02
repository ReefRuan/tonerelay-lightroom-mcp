import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from lr_ops import console_info, server


class ConsoleInfoTests(unittest.TestCase):
    def test_local_scope_is_accurate_and_does_not_disclose_environment_secrets(self):
        with mock.patch.object(console_info, "active_store", return_value=None), \
                mock.patch.dict(os.environ, {"LR_XMP_MIMIC_LOCAL_TOKEN": "secret-token",
                                            "LROPS_BASE": "http://user:secret@remote/?token=hidden"}):
            for host in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
                with self.subTest(host=host):
                    text = console_info.bridge_summary(host, 17777, Path("data"), Path("logs"))
                    self.assertIn("纯本地", text)
                    self.assertIn("启动摘要", text)
                    self.assertIn("AI 模型由客户端提供", text)
                    for secret in ("secret-token", "secret@", "hidden"):
                        self.assertNotIn(secret, text)
            for host in ("0.0.0.0", "192.168.1.2", "::", "example.test"):
                with self.subTest(host=host):
                    text = console_info.bridge_summary(host, 17777, Path("data"), Path("logs"))
                    self.assertNotIn("纯本地", text)
                    self.assertIn("包含本机以外", text)

    def test_configured_paths_and_values_cannot_inject_console_controls(self):
        store = mock.Mock(path=Path("managed/config.json"))
        store.paths.return_value = {"tmp": Path("managed/tmp")}
        with mock.patch.object(console_info, "active_store", return_value=store), \
                mock.patch.dict(os.environ, {"TEMP": "wrong-default"}):
            text = console_info.bridge_summary("127.0.0.1", 17777, Path("data\x1b[2J\nspoof"), Path("logs"))
        self.assertIn(str(store.path), text)
        self.assertIn(str(store.paths()["tmp"]), text)
        self.assertNotIn("wrong-default", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\nspoof", text)

    def test_missing_console_is_nonfatal_and_never_writes_stdio(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        data, logs = Path("data"), Path("logs")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                mock.patch.object(console_info.os, "name", "nt"), \
                mock.patch.object(console_info, "bridge_summary", return_value="summary"), \
                mock.patch.object(console_info, "write_existing_console", side_effect=OSError("no console")):
            self.assertFalse(console_info.show_bridge_startup("127.0.0.1", 17777, data, logs))
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

    def test_non_windows_does_not_build_summary_or_allocate_console(self):
        data, logs = Path("data"), Path("logs")
        with mock.patch.object(console_info.os, "name", "posix"), \
                mock.patch.object(console_info, "bridge_summary") as build:
            self.assertFalse(console_info.show_bridge_startup("127.0.0.1", 17777, data, logs))
            self.assertFalse(console_info.write_existing_console("unused"))
        build.assert_not_called()

    def test_server_summary_uses_actual_bound_address_and_ephemeral_port(self):
        http = mock.Mock(server_address=("127.0.0.1", 24567))
        http.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(server, "make_server", return_value=http), \
                mock.patch.object(server, "show_bridge_startup") as show, \
                contextlib.redirect_stdout(io.StringIO()):
            server._serve(0, "localhost")
        show.assert_called_once_with("127.0.0.1", 24567, server.DATA_ROOT, server.LOG_DIR)
        http.server_close.assert_called_once_with()

    @unittest.skipUnless(os.name == "nt", "Windows console API")
    def test_process_without_console_keeps_stdio_clean_without_creating_one(self):
        script = (
            "import ctypes,json; ctypes.WinDLL('kernel32').FreeConsole(); "
            "from lr_ops.console_info import write_existing_console; "
            "print(json.dumps({'console':write_existing_console('ToneRelay 中文')}))"
        )
        # CREATE_NO_WINDOW can leave a headless console; a venv redirector can
        # also attach its child to a console. Explicitly detach only this test
        # child so the helper must handle a genuinely console-free process.
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
                                startupinfo=startup, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"console": False})
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(os.name == "nt", "Windows console API")
    def test_existing_hidden_console_receives_unicode_with_redirected_stdio(self):
        # Only this test owns a console, hidden from the user's desktop.
        script = r'''
import ctypes, json
from ctypes import wintypes
from lr_ops.console_info import write_existing_console
ok = write_existing_console('ToneRelay 中文配置\n')
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
class COORD(ctypes.Structure):
    _fields_ = [('X', wintypes.SHORT), ('Y', wintypes.SHORT)]
kernel.ReadConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                              wintypes.DWORD, COORD, ctypes.POINTER(wintypes.DWORD)]
kernel.ReadConsoleOutputCharacterW.restype = wintypes.BOOL
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
handle = kernel.CreateFileW('CONOUT$', 0xC0000000, 3, None, 3, 0, None)
buffer = ctypes.create_unicode_buffer(4097)
read = wintypes.DWORD()
read_ok = kernel.ReadConsoleOutputCharacterW(handle, buffer, 4096, COORD(0, 0), ctypes.byref(read))
kernel.CloseHandle(handle)
print(json.dumps({'ok':ok, 'read_ok':bool(read_ok), 'text':buffer.value}, ensure_ascii=True))
'''
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                encoding="utf-8", creationflags=subprocess.CREATE_NEW_CONSOLE,
                                startupinfo=startup, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["read_ok"], data)
        self.assertIn("ToneRelay 中文配置", data["text"])
        self.assertEqual(result.stderr, "")
