"""Installation failures must retain prior links, user data and process ownership."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_bootstrap_macos as bootstrap_tests

bootstrap = bootstrap_tests.bootstrap


class BootstrapSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for patcher in (
            mock.patch.object(bootstrap.Path, "home", return_value=self.root / "home"),
            mock.patch.dict(bootstrap.os.environ, {
                "LR_XMP_MIMIC_RUNTIME_DATA_ROOT": "", "LROPS_BASE": bootstrap.DEFAULT_BRIDGE_BASE}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, root=None):
        root = root or self.root / "runtime"
        source = bootstrap_tests.BootstrapInstallTests().source_tree(self.root / "source", "test-build")
        with mock.patch.object(bootstrap, "create_virtualenv"), \
             mock.patch.object(bootstrap, "install_dependencies"), \
             mock.patch.object(bootstrap, "start_lr_ops", return_value=True), \
             mock.patch.object(bootstrap, "status", return_value={"status": "repair_needed"}):
            code, _ = bootstrap_tests.BootstrapInstallTests().run_install(root, source, "v1")
        self.assertEqual(code, 0)
        return root

    def emit_result(self, function, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = function(*args)
        return code, json.loads(output.getvalue())

    def test_posix_launchers_execute_and_preserve_literal_paths_and_env_override(self):
        root = self.install(self.root / 'runtime space 中文 \'"$x`')
        selected = self.root / 'data space 中文 \'"$x`'
        with mock.patch.dict(os.environ, {"LR_XMP_MIMIC_RUNTIME_DATA_ROOT": str(selected)}):
            bootstrap.write_bin_scripts(root)
        python = bootstrap.venv_python(bootstrap.active_root(root))
        python.parent.mkdir(parents=True)
        python.write_text('#!/usr/bin/env python3\nimport os,json,sys\nprint(json.dumps({"data":os.environ["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"],"path":os.environ.get("PYTHONPATH"),"args":sys.argv}))\n')
        python.chmod(0o755)
        env = os.environ.copy()
        env.pop("LR_XMP_MIMIC_RUNTIME_DATA_ROOT", None)
        for launcher in (bootstrap.launcher_path(root), bootstrap.control_path(root)):
            self.assertNotIn(b"\r", launcher.read_bytes())
            result = subprocess.run([str(launcher), "status"], env=env, capture_output=True,
                                    text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["data"], str(selected))
        result = subprocess.run([str(bootstrap.launcher_path(root))], env=env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["path"], bootstrap.runtime_pythonpath(root / "active/source"))
        env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"] = str(self.root / "explicit")
        result = subprocess.run([str(bootstrap.launcher_path(root))], env=env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["data"], env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"])

    def test_legacy_data_is_selected_and_remembered_without_moving(self):
        legacy = Path.home() / "lr-xmp-mimic-runtime-data"
        legacy.mkdir(parents=True)
        (legacy / "episode.json").write_text("keep")
        root = self.install()
        self.assertEqual(bootstrap.data_root(root), legacy)
        self.assertTrue((legacy / "episode.json").exists())
        other = root / "data"
        other.mkdir(); (other / "new.json").write_text("new")
        self.assertEqual(bootstrap.data_root(root), legacy)
        version = bootstrap.active_root(root)
        self.assertEqual(bootstrap.runtime_env(version)["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"], str(legacy))
        self.assertEqual(bootstrap.mcp_server_config(root)["tonerelay-lightroom"]["env"]["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"], str(legacy))
        code, result = self.emit_result(bootstrap.runtime_uninstall, root, "all")
        self.assertEqual(code, 0)
        self.assertTrue(result["data_preserved"])
        self.assertTrue((legacy / "episode.json").exists())
        self.assertTrue((root / "data-selection.json").exists())

    def test_explicit_data_overrides_saved_choice_and_persists(self):
        root = self.install()
        explicit = self.root / "existing-data"
        with mock.patch.dict(os.environ, {"LR_XMP_MIMIC_RUNTIME_DATA_ROOT": str(explicit)}):
            bootstrap.write_bin_scripts(root)
        self.assertEqual(bootstrap.data_root(root), explicit)

    def test_invalid_saved_selection_is_not_silently_reset(self):
        root = self.root / "runtime"
        root.mkdir()
        (root / "data-selection.json").write_text('{"data_root":"relative"}')
        with self.assertRaises(bootstrap.InstallError):
            bootstrap.data_root(root)

    def test_uninstall_preserves_local_data_and_refuses_data_inside_versions(self):
        root = self.install()
        data = root / "data"
        data.mkdir(); (data / "targets.json").write_text("keep")
        code, result = self.emit_result(bootstrap.runtime_uninstall, root, "all")
        self.assertEqual(code, 0)
        self.assertTrue((data / "targets.json").exists())
        # Reinstall from the same verified source after removing runtime files.
        with mock.patch.object(bootstrap, "create_virtualenv"), mock.patch.object(bootstrap, "install_dependencies"), \
             mock.patch.object(bootstrap, "start_lr_ops", return_value=True), mock.patch.object(bootstrap, "status", return_value={}):
            bootstrap_tests.BootstrapInstallTests().run_install(root, self.root / "source/verified-source", "v2")
        inside = root / "versions/v2/data"
        with mock.patch.dict(os.environ, {"LR_XMP_MIMIC_RUNTIME_DATA_ROOT": str(inside)}):
            code, _ = self.emit_result(bootstrap.runtime_uninstall, root, "all")
        self.assertEqual(code, 1)
        self.assertTrue((root / "versions/v2").exists())
        self.assertTrue(bootstrap.link_present(root / "active"))

    def test_uninstall_refuses_unmanaged_directory_and_system_root(self):
        directory = self.root / "other-project"
        (directory / "bin").mkdir(parents=True)
        (directory / "bin/file").write_text("keep")
        for root in (directory, Path(directory.anchor)):
            code, result = self.emit_result(bootstrap.runtime_uninstall, root, "all")
            self.assertEqual(code, 2)
            self.assertEqual(result["diagnostic"]["code"], "runtime.unmanaged_root")
        self.assertTrue((directory / "bin/file").exists())

    def test_plugin_install_refuses_foreign_link_and_ordinary_directory(self):
        root = self.install()
        modules = self.root / "Modules"
        modules.mkdir()
        entry = modules / "lr-xmp-mimic.lrdevplugin"
        foreign = self.root / "foreign"; foreign.mkdir()
        with mock.patch.object(bootstrap, "lightroom_modules_dir", return_value=modules):
            entry.symlink_to(foreign)
            code, _ = self.emit_result(bootstrap.plugin_install, root)
            self.assertEqual(code, 2)
            self.assertEqual(entry.resolve(), foreign)
            entry.unlink(); entry.mkdir()
            code, _ = self.emit_result(bootstrap.plugin_install, root)
            self.assertEqual(code, 2)
            self.assertTrue(entry.is_dir())

    def test_legacy_live_pid_refuses_termination(self):
        root = self.install()
        path = bootstrap.bridge_pid_path(root)
        path.parent.mkdir(); path.write_text(str(os.getpid()))
        with mock.patch.object(bootstrap, "terminate_process") as terminate:
            code, result = self.emit_result(bootstrap.bridge_stop, root)
        self.assertEqual(code, 2)
        self.assertEqual(result["diagnostic"]["code"], "bridge.process_identity_unknown")
        terminate.assert_not_called()
        self.assertTrue(path.exists())

    def test_nonlocal_stop_refuses_even_a_live_local_pid(self):
        root = self.install()
        with mock.patch.dict(os.environ, {"LROPS_BASE": "https://example.invalid:17777/path"}), \
             mock.patch.object(bootstrap, "terminate_process") as terminate:
            code, result = self.emit_result(bootstrap.bridge_stop, root)
        self.assertEqual(code, 2)
        self.assertEqual(result["diagnostic"]["code"], "bridge.non_loopback_target")
        terminate.assert_not_called()

    def record(self, root, process, identity):
        version = bootstrap.active_root(root)
        path = bootstrap.bridge_pid_path(root); path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"pid": process.pid, "identity": identity,
            "runtime_root": str(root.resolve()), "version_root": str(version),
            "base": bootstrap.bridge_base(), "command": [str(bootstrap.venv_python(version)), "-m", "lr_ops", "serve"]}))

    def test_changed_pid_identity_does_not_stop_disposable_process(self):
        root = self.install()
        process = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                                   stdout=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        try:
            identity = bootstrap.process_identity(process.pid)
            self.assertIsNotNone(identity)
            self.record(root, process, {**identity, "created": "obsolete"})
            code, _ = self.emit_result(bootstrap.bridge_stop, root)
            self.assertEqual(code, 2)
            self.assertIsNone(process.poll())
        finally:
            process.terminate(); process.wait(timeout=5)
            process.stdout.close()

    def test_matching_managed_disposable_process_can_stop(self):
        root = self.install()
        process = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                                   stdout=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        try:
            identity = bootstrap.process_identity(process.pid)
            self.assertIsNotNone(identity)
            self.record(root, process, identity)
            # Reap only this owned subprocess so kill(pid, 0) doesn't observe a zombie.
            alive = bootstrap.process_alive
            def reaped(pid):
                return process.poll() is None and alive(pid)
            with mock.patch.object(bootstrap, "process_alive", side_effect=reaped):
                code, result = self.emit_result(bootstrap.bridge_stop, root)
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "stopped")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_http_request_preserves_https_base_path_and_ipv6(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        for base in ("https://example.invalid:443/bridge", "http://[::1]:20000/base"):
            with mock.patch.dict(os.environ, {"LROPS_BASE": base}), \
                 mock.patch.object(bootstrap.urllib.request, "urlopen", return_value=response) as open_url:
                self.assertTrue(bootstrap._lr_ops_request("GET", "/health")["ok"])
            self.assertEqual(open_url.call_args.args[0].full_url, base + "/health")

    def test_mcp_tools_error_and_empty_result_fail_and_reap_owned_child(self):
        root = self.install()
        python = bootstrap.venv_python(bootstrap.active_root(root))
        python.parent.mkdir(parents=True); python.touch()
        for tools in ({"id": 2, "error": {"message": "bad"}}, {"id": 2, "result": {"tools": []}}, {"error": "timeout"}):
            process = mock.Mock()
            process.stdout = io.StringIO()
            process.stderr = io.StringIO()
            process.stdin = io.StringIO()
            with mock.patch.object(bootstrap.subprocess, "Popen", return_value=process), \
                 mock.patch.object(bootstrap, "_mcp_receive", side_effect=[
                    {"id": 1, "result": {"serverInfo": {"name": "test"}}}, tools]):
                code, result = self.emit_result(bootstrap.mcp_start, root)
            self.assertEqual(code, 1)
            self.assertEqual(result["status"], "start_failed")
            process.wait.assert_called_once_with(timeout=3)
            self.assertTrue(process.stdin.closed)


if __name__ == "__main__":
    unittest.main()
