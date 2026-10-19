"""Actual launcher processes and cleanup, with no Lightroom or network access."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts/tonerelay_bootstrap_macos.py"
SPEC = importlib.util.spec_from_file_location("tonerelay_lifecycle_bootstrap", SCRIPT)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)

PROBE = '''import json, os, sys, tempfile, subprocess
from . import cache_probe
with tempfile.NamedTemporaryFile(delete=False) as handle:
    temporary = handle.name
child = subprocess.run([sys.executable, "-c",
    "import tempfile; f=tempfile.NamedTemporaryFile(delete=False); print(f.name); f.close()"],
    capture_output=True, text=True, check=True)
print(json.dumps({"cwd": os.getcwd(), "tmp": temporary,
    "child_tmp": child.stdout.strip(), "pycache": sys.pycache_prefix,
    "data": os.environ["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"],
    "pip": os.environ["PIP_CACHE_DIR"], "xdg": os.environ["XDG_CACHE_HOME"]}), flush=True)
if os.environ.get("PROBE_WAIT") == "1":
    sys.stdin.read()
'''


@unittest.skipUnless(sys.platform == "darwin", "macOS shell launcher contract")
class ConfigurationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'Application Support/ToneRelay 中文 \'"$x`'
        bootstrap.prepare_installation(self.root)
        version = self.root / "versions/v1"
        source = version / "source"
        package = source / "services/lr-ops/lr_ops"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "configuration.py").write_text(bootstrap._config_path.read_text())
        scripts = source / "plugins/tonerelay-lightroom/scripts"
        scripts.mkdir(parents=True)
        for name in ("tonerelay_bootstrap_macos.py", "tonerelay_lifecycle.py"):
            shutil.copyfile(SCRIPT.with_name(name), scripts / name)
        mcp = source / "apps/workbench-mcp/workbench_mcp"
        mcp.mkdir(parents=True)
        (mcp / "__init__.py").write_text("")
        (mcp / "cache_probe.py").write_text("marker = 1\n")
        (mcp / "__main__.py").write_text(PROBE)
        python = version / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        bootstrap.activate_runtime(self.root, version)
        bootstrap.write_bin_scripts(self.root)
        bootstrap.lifecycle().Ledger(self.root).remember_tree(version)
        self.env = dict(os.environ)
        for key in ("TMPDIR", "TEMP", "TMP", "PIP_CACHE_DIR", "XDG_CACHE_HOME",
                    "PYTHONPYCACHEPREFIX", "LR_XMP_MIMIC_RUNTIME_DATA_ROOT", "TONERELAY_CONFIG"):
            self.env[key] = str(self.root.parent / "outside")

    def launch(self):
        result = subprocess.run([str(bootstrap.launcher_path(self.root))],
            cwd=self.root.parent, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def uninstall(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = bootstrap.runtime_uninstall(self.root, "all")
        return code, json.loads(output.getvalue())

    def test_real_launcher_pins_paths_and_new_process_reads_updated_config(self):
        first = self.launch()
        self.assertEqual(first["cwd"], str(self.root / "run"))
        self.assertEqual(first["data"], str(self.root / "data"))
        for field in ("tmp", "child_tmp"):
            self.assertEqual(Path(first[field]).parent, self.root / "tmp")
        self.assertEqual(first["pip"], str(self.root / "cache/pip"))
        self.assertEqual(first["xdg"], str(self.root / "cache/xdg"))
        self.assertEqual(first["pycache"], str(self.root / "cache/python"))
        self.assertTrue(list((self.root / "cache/python").rglob("cache_probe*.pyc")))
        bootstrap.config_store(self.root).update("paths", lambda value: value.update(tmp="scratch"))
        second = self.launch()
        self.assertEqual(Path(second["tmp"]).parent, self.root / "scratch")
        self.assertEqual(Path(second["child_tmp"]).parent, self.root / "scratch")
        marker = self.root / "data/keep.txt"
        marker.write_text("retained state")
        code, result = self.uninstall()
        self.assertEqual(code, 0, result)
        for directory in ("versions", "bin"):
            self.assertFalse((self.root / directory).exists(), directory)
        # Runtime-generated files were never adopted as installed software.
        # Exact-file cleanup preserves them for offline residual review.
        self.assertTrue(Path(first["tmp"]).is_file())
        self.assertTrue(Path(second["tmp"]).is_file())
        self.assertEqual(marker.read_text(), "retained state")
        self.assertTrue((self.root / "config.json").is_file())
        self.assertTrue((self.root / "backups/config-last-good.json").is_file())
        self.assertTrue((self.root / "install-manifest.json").is_file())
        self.assertEqual(result["status"], "runtime_files_removed")

    def test_live_client_process_blocks_cleanup_and_host_entry_is_retained(self):
        host = self.root.parent / "client.json"
        host.write_text('{"other_mcp": true}')
        bootstrap.record_installation(self.root, external={
            "kind": "client", "path": str(host), "owned": False,
            "remove": "only ToneRelay entry",
        })
        env = {**self.env, "PROBE_WAIT": "1"}
        process = subprocess.Popen([str(bootstrap.launcher_path(self.root))], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(15), "launcher did not become ready")
            line = process.stdout.readline()
            self.assertTrue(line, "launcher exited before readiness")
            code, result = self.uninstall()
            self.assertEqual(code, 1)
            self.assertIn("client-owned MCP", result["error"])
            self.assertTrue((self.root / "versions/v1").exists())
            process.communicate(timeout=10)
            code, result = self.uninstall()
            self.assertEqual(code, 0, result)
            self.assertEqual(host.read_text(), '{"other_mcp": true}')
            self.assertEqual(result["external"][0]["path"], str(host))
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_prepare_cli_and_uninstaller_share_one_ledger_and_recovery_config(self):
        fresh = self.root.parent / "fresh-install"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = bootstrap.main(["prepare", "--runtime-root", str(fresh)])
        self.assertEqual(code, 0, output.getvalue())
        manifest = json.loads((fresh / "install-manifest.json").read_text())
        self.assertEqual(manifest["schema"], "tonerelay-install-manifest-v1")
        self.assertNotIn("owned_paths", manifest)
        self.assertEqual(manifest["configuration"], str(fresh / "config.json"))

        personal = self.root / "cache/personal.txt"
        personal.write_text("keep unknown file")
        before = (self.root / "config.json").read_bytes()
        output = io.StringIO()
        with mock.patch.object(bootstrap.lifecycle(), "process_snapshot", return_value={
                "runtime": [], "lightroom": [], "unattributed": []}), \
             mock.patch.object(bootstrap, "bridge_stop", side_effect=lambda root: bootstrap.emit({"listening": False})), \
             mock.patch.object(bootstrap, "lightroom_entry", return_value=self.root.parent / "absent-plugin"), \
             mock.patch.object(bootstrap, "plugin_uninstall", side_effect=lambda root: bootstrap.emit({"status": "not_installed"})), \
             contextlib.redirect_stdout(output):
            code = bootstrap.main(["uninstall", "--runtime-root", str(self.root), "--apply", "--clients-reviewed"])
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "residual_review_required", result)
        self.assertEqual(code, 2)
        self.assertEqual(personal.read_text(), "keep unknown file")
        self.assertEqual((self.root / "config.json").read_bytes(), before)
        self.assertFalse((self.root / "versions").exists())
        runner = Path(result["resume_script"])
        self.assertTrue(runner.with_name("configuration.py").is_file())
        resumed = subprocess.run([sys.executable, str(runner), "inventory", "--runtime-root", str(self.root)],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["phase"], "software_removed")
        self.assertEqual((self.root / "config.json").read_bytes(), before)

    def test_new_configured_run_path_keeps_verified_process_tracking(self):
        bootstrap.config_store(self.root).update("paths", lambda value: value.update(run="processes", backups="saved"))
        value = self.launch()
        self.assertEqual(value["cwd"], str(self.root / "processes"))
        ledger = bootstrap.lifecycle().Ledger(self.root)
        self.assertEqual(ledger.run_directory(), self.root / "processes")
        self.assertEqual(ledger.backup_directory(), self.root / "saved")
        self.assertIn(str(self.root / "backups"), ledger.record["retained_paths"])

    def test_managed_download_install_keeps_provenance_and_exact_file_ownership(self):
        from test_bootstrap_macos import BootstrapInstallTests
        root = self.root.parent / "managed-download"
        bootstrap.prepare_installation(root)
        source = BootstrapInstallTests().source_tree(root / "downloads", "merge-test")
        destination = root / "downloads/source"
        source.rename(destination)
        output = io.StringIO()
        with mock.patch.object(bootstrap, "create_virtualenv"), \
             mock.patch.object(bootstrap, "install_dependencies"), \
             mock.patch.object(bootstrap, "start_lr_ops", return_value=True), \
             mock.patch.object(bootstrap, "status", return_value={"status": "pending"}), \
             contextlib.redirect_stdout(output):
            code = bootstrap.main(["install", "--runtime-root", str(root),
                "--source-root", str(destination), "--version", "fixed-merge-test", "--manage-source",
                "--source-origin", "https://example.invalid/verified", "--source-integrity", "checked-digest"])
        self.assertEqual(code, 0, output.getvalue())
        ledger = bootstrap.lifecycle().Ledger(root)
        self.assertEqual(ledger.record["sources"][-1]["integrity"], "checked-digest")
        self.assertEqual(ledger.record["sources"][-1]["origin"], "https://example.invalid/verified")
        self.assertEqual(ledger.record["managed_source"], str(destination))
        self.assertIn("downloads/source/services/lr-ops/lr_ops/configuration.py", ledger.record["files"])
        self.assertIn("bin/tonerelay-mcp", ledger.record["files"])
        self.assertEqual(ledger.record["phase"], "runtime_installed")

    def test_missing_persisted_configuration_is_not_recreated(self):
        (self.root / "config.json").unlink()
        with self.assertRaisesRegex(bootstrap.InstallError, "missing"):
            bootstrap.prepare_installation(self.root)
        self.assertFalse((self.root / "config.json").exists())

    def test_legacy_maintenance_does_not_implicitly_initialize_unified_config(self):
        root = self.root.parent / "legacy-layout"
        ledger = bootstrap.lifecycle().Ledger(root, create=True)
        ledger.checkpoint("runtime_installed", root / "data")
        before = ledger.path.read_bytes()
        with self.assertRaisesRegex(bootstrap.InstallError, "implicit migration"):
            bootstrap.prepare_installation(root)
        self.assertFalse((root / "config.json").exists())
        self.assertEqual(ledger.path.read_bytes(), before)
