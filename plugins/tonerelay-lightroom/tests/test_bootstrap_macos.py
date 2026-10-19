"""Bootstrap status must verify the plugin Lightroom actually loaded."""

from __future__ import annotations

import contextlib
import io
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "tonerelay_bootstrap_macos.py"
SPEC = importlib.util.spec_from_file_location("tonerelay_bootstrap_macos", SCRIPT)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapStatusTests(unittest.TestCase):
    def setUp(self):
        self.source_sha256 = None
        patcher = mock.patch.object(bootstrap, "_lr_ops_request", side_effect=self.health)
        patcher.start()
        self.addCleanup(patcher.stop)
        identity = mock.patch.object(bootstrap, "process_identity", return_value={"created": "test", "command": "test"})
        identity.start()
        self.addCleanup(identity.stop)

    def health(self, method, path, body=None):
        self.assertEqual((method, path), ("GET", "/health"))
        return {"ok": True, "source_sha256": self.source_sha256}

    def runtime_root(self, root: Path,
                     catalog_confirmation: bool = True) -> tuple[Path, Path]:
        version = root / "versions" / "source"
        commands = version / "source" / "plugins" / "lightroom" / "lr-xmp-mimic.lrdevplugin" / "Commands.lua"
        commands.parent.mkdir(parents=True)
        commands.write_text("build_id = 'selection-gallery-current'", encoding="utf-8")
        (version / "lightroom" / "lr-xmp-mimic.lrdevplugin").mkdir(parents=True)
        server = version / "source" / "apps" / "workbench-mcp" / "workbench_mcp" / "server.py"
        runtime = version / "source" / "apps" / "workbench" / "workbench" / "runtime.py"
        server.parent.mkdir(parents=True)
        runtime.parent.mkdir(parents=True)
        server.write_text(
            '"name": "trlr_confirm_catalog"' if catalog_confirmation else "",
            encoding="utf-8")
        runtime.write_text(
            "def confirm_catalog(" if catalog_confirmation else "",
            encoding="utf-8")
        package = version / "source/services/lr-ops/lr_ops"
        package.mkdir(parents=True)
        (package / "server.py").write_text("# test bridge source\n", encoding="utf-8")
        self.source_sha256 = bootstrap.bridge_source_fingerprint(package)
        launcher = bootstrap.launcher_path(root)
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh", encoding="utf-8")
        launcher.chmod(0o755)
        active = root / "active"
        bootstrap._point_at(active, version)
        return root, version

    def test_old_loaded_plugin_is_not_reported_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            plugin = bootstrap.lightroom_plugin_path(root)
            resolved_plugin = str(plugin.resolve())
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "probe_loaded_plugin", return_value={
                     "build_id": "selection-gallery-old",
                     "capabilities": {"current_folder_selection": False},
                 }):
                result = bootstrap.status(root)

        self.assertEqual(result["status"], "repair_needed")
        self.assertFalse(result["checks"]["lightroom_plugin_loaded"])
        self.assertEqual(result["diagnostic"]["code"],
                         "bootstrap.plugin_reload_required")
        self.assertEqual(result["diagnostic"]["lightroom_plugin"], str(plugin))
        self.assertEqual(result["diagnostic"]["resolved_lightroom_plugin"],
                         resolved_plugin)
        self.assertIn("File > Plug-in Manager", result["diagnostic"]["next"])
        self.assertIn("If it points to an older version", result["diagnostic"]["next"])

    def test_failed_plugin_probe_does_not_claim_missing_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "probe_loaded_plugin", return_value={
                     "error": "timeout",
                 }):
                result = bootstrap.status(root)
        diagnostic = result["diagnostic"]
        self.assertEqual(diagnostic["code"], "bootstrap.plugin_probe_unavailable")
        self.assertEqual(diagnostic["probe_error"], "timeout")
        self.assertIn("Unlock", diagnostic["next"])
        self.assertIn("Do not infer", diagnostic["next"])
        self.assertFalse(diagnostic["safe_to_retry"])
        self.assertNotIn("not registered", diagnostic["next"])

    def test_empty_plugin_probe_keeps_loaded_build_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "probe_loaded_plugin", return_value={}):
                result = bootstrap.status(root)
        self.assertEqual(result["status"], "repair_needed")
        self.assertFalse(result["checks"]["lightroom_plugin_loaded"])
        self.assertEqual(result["diagnostic"]["code"],
                         "bootstrap.plugin_probe_unavailable")
        self.assertIsNone(result["diagnostic"]["loaded_build"])

    def test_probe_without_capabilities_does_not_infer_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "probe_loaded_plugin", return_value={
                     "build_id": "selection-gallery-current",
                 }):
                result = bootstrap.status(root)
        self.assertEqual(result["diagnostic"]["code"],
                         "bootstrap.plugin_probe_unavailable")

    def test_matching_loaded_plugin_is_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "probe_loaded_plugin", return_value={
                     "build_id": "selection-gallery-current",
                     "capabilities": {"current_folder_selection": True},
                 }):
                result = bootstrap.status(root)

        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["checks"]["lightroom_plugin_loaded"])

    def test_runtime_without_catalog_confirmation_requires_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary), catalog_confirmation=False)
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "probe_loaded_plugin", return_value={
                     "build_id": "selection-gallery-current",
                     "capabilities": {"current_folder_selection": True},
                 }):
                result = bootstrap.status(root)

        self.assertEqual(result["status"], "repair_needed")
        self.assertFalse(result["checks"]["catalog_confirmation_supported"])
        self.assertEqual(result["diagnostic"]["code"],
                         "bootstrap.runtime_upgrade_required")
        self.assertIn("Do not edit the Target mode", result["diagnostic"]["next"])

    def test_unreachable_bridge_does_not_infer_plugin_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=False):
                result = bootstrap.status(root)

        self.assertEqual(result["status"], "repair_needed")
        self.assertEqual(result["diagnostic"]["code"],
                         "bootstrap.bridge_unavailable")
        self.assertIn("Run repair", result["diagnostic"]["next"])

    def test_reachable_legacy_bridge_cannot_prove_current_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "_lr_ops_request", return_value={
                     "ok": True, "version": "0.1.0", "queued": 0,
                 }), \
                 mock.patch.object(bootstrap, "probe_loaded_plugin", return_value={
                     "build_id": "selection-gallery-current",
                     "capabilities": {"current_folder_selection": True},
                 }):
                result = bootstrap.status(root)

        self.assertEqual(result["status"], "repair_needed")
        self.assertEqual(result["diagnostic"]["code"],
                         "bootstrap.bridge_restart_required")

    def test_different_bridge_source_requires_restart_without_plugin_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "_lr_ops_request", return_value={
                     "ok": True, "source_sha256": "0" * 64,
                 }), mock.patch.object(bootstrap, "probe_loaded_plugin") as probe:
                result = bootstrap.status(root)
        self.assertFalse(result["checks"]["lr_ops_build_matches_active"])
        self.assertEqual(result["bridge_build"]["loaded_source_sha256"], "0" * 64)
        self.assertEqual(result["diagnostic"]["code"], "bootstrap.bridge_restart_required")
        probe.assert_not_called()

    def test_bridge_health_error_is_actionable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "_lr_ops_request", side_effect=OSError("unavailable")):
                result = bootstrap.status(root)
        self.assertEqual(result["status"], "repair_needed")
        self.assertEqual(result["diagnostic"]["code"], "bootstrap.bridge_restart_required")

    def test_reachable_old_bridge_is_not_reused_or_terminated(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, version = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap, "_lr_ops_request", return_value={"ok": True}), \
                 mock.patch.object(bootstrap.subprocess, "Popen") as launch:
                self.assertFalse(bootstrap.start_lr_ops(version.parent.parent, version))
        launch.assert_not_called()

    def test_matching_bridge_is_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, version = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap.subprocess, "Popen") as launch:
                self.assertTrue(bootstrap.start_lr_ops(version.parent.parent, version))
        launch.assert_not_called()

    def test_new_bridge_waits_for_matching_health(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, version = self.runtime_root(Path(temporary))
            python = version / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            process = mock.Mock(pid=999999999)
            process.poll.return_value = None
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=False), \
                 mock.patch.object(bootstrap.subprocess, "Popen", return_value=process), \
                 mock.patch.object(bootstrap, "bridge_build_status", side_effect=[
                     {"matches_active_source": False}, {"matches_active_source": True},
                 ]) as health, mock.patch.object(bootstrap.time, "sleep") as sleep:
                self.assertTrue(bootstrap.start_lr_ops(version.parent.parent, version))
            self.assertEqual(health.call_count, 2)
            sleep.assert_called_once_with(0.1)
            process.terminate.assert_not_called()

    def test_new_bridge_early_exit_is_not_reported_started(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, version = self.runtime_root(Path(temporary))
            python = version / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            process = mock.Mock(pid=999999999)
            process.poll.return_value = 1
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=False), \
                 mock.patch.object(bootstrap.subprocess, "Popen", return_value=process), \
                 mock.patch.object(bootstrap, "bridge_build_status") as health:
                self.assertFalse(bootstrap.start_lr_ops(version.parent.parent, version))
            health.assert_not_called()
            process.terminate.assert_not_called()

    def test_new_bridge_timeout_is_bounded_without_termination(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, version = self.runtime_root(Path(temporary))
            python = version / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            process = mock.Mock(pid=999999999)
            process.poll.return_value = None
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=False), \
                 mock.patch.object(bootstrap.subprocess, "Popen", return_value=process), \
                 mock.patch.object(bootstrap.time, "monotonic", side_effect=[0, 0, 4]), \
                 mock.patch.object(bootstrap.time, "sleep"), \
                 mock.patch.object(bootstrap, "bridge_build_status", return_value={
                     "matches_active_source": False}):
                self.assertFalse(bootstrap.start_lr_ops(version.parent.parent, version))
            process.terminate.assert_not_called()


class BootstrapInstallTests(unittest.TestCase):
    def source_tree(self, root: Path, build_id: str) -> Path:
        source = root / "verified-source"
        for relative in bootstrap.RUNTIME_SOURCE_DIRECTORIES:
            (source / relative).mkdir(parents=True, exist_ok=True)
        (source / "apps/workbench/requirements.txt").write_text("", encoding="utf-8")
        commands = source / "plugins/lightroom/lr-xmp-mimic.lrdevplugin/Commands.lua"
        commands.parent.mkdir(parents=True)
        commands.write_text(
            f"build_id = '{build_id}'", encoding="utf-8")
        (source / "docs").mkdir()
        (source / "docs/private-note.md").write_text("must not install", encoding="utf-8")
        return source

    def run_install(self, runtime_root: Path, source: Path, version: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = bootstrap.install(runtime_root, source, version)
        return exit_code, json.loads(output.getvalue())

    def test_installs_explicit_source_without_docs_or_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source_tree(root, "first-build")
            runtime_root = root / "runtime"
            with mock.patch.object(bootstrap, "create_virtualenv"), \
                 mock.patch.object(bootstrap, "install_dependencies"), \
                 mock.patch.object(bootstrap, "start_lr_ops", return_value=True), \
                 mock.patch.object(bootstrap, "status", return_value={"status": "repair_needed"}):
                exit_code, result = self.run_install(runtime_root, source, "v1")

            version = runtime_root / "versions" / "v1"
            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "installed")
            self.assertTrue((version / "source/apps/workbench/requirements.txt").is_file())
            self.assertFalse((version / "source/docs/private-note.md").exists())
            self.assertEqual((runtime_root / "active").resolve(), version.resolve())
            self.assertTrue(
                (version / "lightroom" / "lr-xmp-mimic.lrdevplugin").is_dir())
            self.assertEqual((version / "lightroom").resolve(),
                                 (version / "source/plugins/lightroom").resolve())

    def test_virtualenv_uses_symlinked_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            version_root = Path(temporary) / "version"
            builder = mock.Mock()
            with mock.patch.object(
                    bootstrap.venv, "EnvBuilder", return_value=builder) as env_builder:
                bootstrap.create_virtualenv(version_root)

        env_builder.assert_called_once_with(with_pip=True, clear=True, symlinks=True)
        builder.create.assert_called_once_with(version_root / ".venv")

    def test_repeat_install_is_idempotent_and_reactivates_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source_tree(root, "repeat-build")
            runtime_root = root / "runtime"
            with mock.patch.object(bootstrap, "create_virtualenv") as create_venv, \
                 mock.patch.object(bootstrap, "install_dependencies") as dependencies, \
                 mock.patch.object(bootstrap, "start_lr_ops", return_value=True), \
                 mock.patch.object(bootstrap, "status", return_value={"status": "ready"}):
                self.run_install(runtime_root, source, "v1")
                exit_code, result = self.run_install(runtime_root, source, "v1")

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "already_installed")
            self.assertEqual(create_venv.call_count, 1)
            self.assertEqual(dependencies.call_count, 1)

    def test_upgrade_changes_bundle_behind_active_link_not_lr_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            first = self.source_tree(root, "first-build")
            second = self.source_tree(root / "second", "second-build")
            with mock.patch.object(bootstrap, "create_virtualenv"), \
                 mock.patch.object(bootstrap, "install_dependencies"), \
                 mock.patch.object(bootstrap, "start_lr_ops", return_value=True), \
                 mock.patch.object(bootstrap, "status", return_value={"status": "repair_needed"}):
                _, first_result = self.run_install(runtime_root, first, "v1")
                _, second_result = self.run_install(runtime_root, second, "v2")

            commands = runtime_root / "active/lightroom/lr-xmp-mimic.lrdevplugin/Commands.lua"
            self.assertIn("second-build", commands.read_text(encoding="utf-8"))
            expected_registration = str(
                runtime_root / "active/lightroom/lr-xmp-mimic.lrdevplugin")
            self.assertEqual(first_result["lightroom_plugin"], expected_registration)
            self.assertEqual(second_result["lightroom_plugin"], expected_registration)

    def test_rejects_incomplete_explicit_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = bootstrap.install(Path(temporary) / "runtime",
                                              Path(temporary) / "missing", "v1")
            result = json.loads(output.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertEqual(result["diagnostic"]["code"], "bootstrap.invalid_source")


class BootstrapPlatformTests(unittest.TestCase):
    """Layout must follow the host platform, never the developer's machine."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        patchers = (
            mock.patch.dict(bootstrap.os.environ, {"LR_XMP_MIMIC_RUNTIME_DATA_ROOT": ""}),
            mock.patch.object(bootstrap.Path, "home", return_value=Path(self.temporary.name) / "home"),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_macos_launcher_interpreter_and_default_root(self):
        root = Path(self.temporary.name) / "runtime"
        self.assertEqual(bootstrap.launcher_path(root).name, "tonerelay-mcp")
        self.assertEqual(bootstrap.control_path(root).name, "tonerelay")
        self.assertEqual(bootstrap.venv_python(root).parts[-3:], (".venv", "bin", "python"))
        self.assertEqual(bootstrap.default_runtime_root(),
                         Path.home() / "Library/Application Support/ToneRelay")

    def test_cli_refuses_non_macos_before_any_install_write(self):
        root = Path(self.temporary.name) / "must-not-create"
        for platform in ("win32", "linux"):
            with mock.patch.object(bootstrap.sys, "platform", platform), \
                 mock.patch.object(bootstrap, "install") as install:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = bootstrap.main(["install", "--runtime-root", str(root)])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output.getvalue())["diagnostic"]["code"], "bootstrap.macos_required")
                install.assert_not_called()
                self.assertFalse(root.exists())

    def test_help_remains_available_on_non_macos(self):
        with mock.patch.object(bootstrap.sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            bootstrap.main(["--help"])
        self.assertEqual(stopped.exception.code, 0)

    def test_bridge_endpoint_comes_from_the_configured_base(self):
        for base, expected in (
            ("http://127.0.0.1:17777", ("127.0.0.1", 17777)),
            ("127.0.0.1:20000", ("127.0.0.1", 20000)),
            ("http://127.0.0.1", ("127.0.0.1", 80)),
        ):
            with self.subTest(base=base):
                self.assertEqual(bootstrap.bridge_endpoint(base), expected)
        with mock.patch.dict(bootstrap.os.environ, {"LROPS_BASE": "http://10.0.0.5:17777"}):
            self.assertFalse(bootstrap.bridge_is_local())
            self.assertEqual(bootstrap.bridge_endpoint(), ("10.0.0.5", 17777))

    def test_data_root_sits_inside_the_install_root(self):
        root = Path(self.temporary.name) / "runtime"
        self.assertEqual(bootstrap.data_root(root), (root / "data").resolve())

    def test_explicit_data_override_precedes_saved_choice(self):
        root = Path(self.temporary.name) / "runtime"
        root.mkdir()
        saved = Path(self.temporary.name) / "saved-data"
        (root / "data-selection.json").write_text(json.dumps({"data_root": str(saved.resolve())}))
        explicit = Path(self.temporary.name) / "explicit-data"
        with mock.patch.dict(bootstrap.os.environ, {"LR_XMP_MIMIC_RUNTIME_DATA_ROOT": str(explicit)}):
            self.assertEqual(bootstrap.data_root(root), explicit.resolve())
        self.assertEqual(bootstrap.data_root(root), saved.resolve())

    def test_mcp_config_uses_the_stable_active_link(self):
        root = Path("/tmp/runtime")
        config = bootstrap.mcp_server_config(root)["tonerelay-lightroom"]
        self.assertEqual(config["args"], ["-m", "workbench_mcp"])
        self.assertIn("active", config["command"])
        self.assertEqual(
            config["env"]["PYTHONPATH"].split(bootstrap.os.pathsep),
            [str(root / "active" / "source" / relative)
             for relative in bootstrap.RUNTIME_PYTHONPATH_ENTRIES])



class ComponentOperationTests(unittest.TestCase):
    """Every component exposes the operations an agent needs to drive it."""

    def source_tree(self, root: Path) -> Path:
        source = root / "verified-source"
        for relative in bootstrap.RUNTIME_SOURCE_DIRECTORIES:
            (source / relative).mkdir(parents=True, exist_ok=True)
        (source / "apps/workbench/requirements.txt").write_text("", encoding="utf-8")
        commands = source / "plugins/lightroom/lr-xmp-mimic.lrdevplugin/Commands.lua"
        commands.parent.mkdir(parents=True)
        commands.write_text("build_id = 'component-build'", encoding="utf-8")
        return source

    def install(self, runtime_root: Path, version: str) -> None:
        source = self.source_tree(runtime_root.parent / f"src-{version}")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             mock.patch.object(bootstrap, "create_virtualenv"), \
             mock.patch.object(bootstrap, "install_dependencies"), \
             mock.patch.object(bootstrap, "start_lr_ops", return_value=True), \
             mock.patch.object(bootstrap, "status", return_value={"status": "ready"}):
            exit_code = bootstrap.install(runtime_root, source, version)
        self.assertEqual(exit_code, 0)

    def test_install_refuses_a_source_that_is_inside_the_runtime_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")
            nested = root / "versions" / "v1" / "source"

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = bootstrap.main(["runtime", "install",
                                            "--runtime-root", str(root),
                                            "--source-root", str(nested)])
            result = json.loads(output.getvalue())

            self.assertEqual(exit_code, 2)
            self.assertEqual(result["diagnostic"]["code"],
                             "runtime.install_needs_source")

    def test_every_component_answers_every_verb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")
            source = self.source_tree(Path(temporary) / "matrix")
            for component in bootstrap.COMPONENTS:
                for action in bootstrap.ACTIONS:
                    with self.subTest(component=component, action=action):
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output), \
                             mock.patch.object(bootstrap, "create_virtualenv"), \
                             mock.patch.object(bootstrap, "install_dependencies"), \
                             mock.patch.object(bootstrap, "start_lr_ops", return_value=True), \
                             mock.patch.object(bootstrap, "bridge_reachable", return_value=False), \
                             mock.patch.object(bootstrap, "lightroom_modules_dir",
                                               return_value=Path(temporary) / "Modules"):
                            bootstrap.main([component, action,
                                            "--runtime-root", str(root),
                                            "--source-root", str(source),
                                            "--version", "matrix"])
                        result = json.loads(output.getvalue())
                        self.assertNotEqual(
                            (result.get("diagnostic") or {}).get("code"),
                            "cli.unknown_action")

    def test_inapplicable_verbs_name_the_command_that_does_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")
            for component, action, needle in (
                    ("runtime", "start", "bridge start"),
                    ("bridge", "install", "runtime install"),
                    ("lightroom-plugin", "start", "lightroom-plugin install"),
                    ("mcp", "stop", "close the client session")):
                with self.subTest(component=component, action=action):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        exit_code = bootstrap.main([component, action,
                                                    "--runtime-root", str(root)])
                    result = json.loads(output.getvalue())
                    self.assertEqual(exit_code, 0)
                    self.assertEqual(result["status"], "not_applicable")
                    self.assertIn(needle, result["next"])
                    self.assertTrue(result["reason"])

    def test_dependency_failure_points_at_the_mirror_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source_tree(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), \
                 mock.patch.object(bootstrap, "create_virtualenv"), \
                 mock.patch.object(bootstrap, "install_dependencies",
                                   side_effect=subprocess.CalledProcessError(1, "pip")):
                exit_code = bootstrap.install(root / "runtime", source, "v1")
            result = json.loads(output.getvalue())

            self.assertEqual(exit_code, 1)
            self.assertEqual(result["status"], "repair_needed")
            self.assertIn("README.md#dependency-download-help", result["diagnostic"]["next"])
            self.assertIn("PIP_INDEX_URL", result["diagnostic"]["next"])

    def test_uninstall_without_target_returns_supported_version_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = bootstrap.runtime_uninstall(root, None)
            result = json.loads(output.getvalue())

            self.assertEqual(exit_code, 2)
            self.assertEqual(result["diagnostic"]["code"], "runtime.uninstall_needs_target")
            control = bootstrap.control_command(root)
            self.assertEqual(result["diagnostic"]["next"],
                             f"{control} runtime uninstall --version <label>, or "
                             f"{control} runtime uninstall --version all for all managed Runtime files")
            self.assertNotIn("--all", result["diagnostic"]["next"])
            self.assertTrue((root / "versions" / "v1").is_dir())

    def test_uninstall_reports_a_locked_root_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")

            output = io.StringIO()
            with contextlib.redirect_stdout(output), \
                 mock.patch.object(bootstrap.shutil, "rmtree",
                                   side_effect=OSError("in use")):
                exit_code = bootstrap.runtime_uninstall(root, "all")
            result = json.loads(output.getvalue())

            self.assertEqual(exit_code, 1)
            self.assertEqual(result["status"], "uninstall_failed")
            self.assertIn("Stop the bridge", result["next"])

    def test_control_script_pins_its_own_runtime_root(self):
        """A control script must drive the install it belongs to, not the default."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "elsewhere" / "runtime"
            self.install(root, "v1")

            text = bootstrap.control_path(root).read_text(encoding="utf-8")

            self.assertIn("--runtime-root", text)
            self.assertIn(str(root), text)

    def test_control_and_launcher_scripts_are_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")

            control = bootstrap.control_path(root)
            self.assertTrue(control.is_file())
            self.assertTrue(bootstrap.launcher_path(root).is_file())
            text = control.read_text(encoding="utf-8")
            self.assertIn("tonerelay_bootstrap_macos.py", text)
            self.assertIn(str(bootstrap.data_root(root)), text)

    def test_uninstall_removes_one_version_and_clears_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")
            self.install(root, "v2")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = bootstrap.runtime_uninstall(root, "v2")
            result = json.loads(output.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertFalse((root / "versions" / "v2").exists())
            self.assertTrue((root / "versions" / "v1").is_dir())
            self.assertTrue(result["active_cleared"])
            self.assertIsNone(bootstrap.active_root(root))

    def test_uninstall_refuses_a_version_that_is_not_a_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = bootstrap.runtime_uninstall(root, "../escape")

            self.assertEqual(exit_code, 2)
            self.assertTrue((root / "versions" / "v1").is_dir())

    def test_plugin_install_then_uninstall_manages_one_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            modules = Path(temporary) / "Modules"
            self.install(root, "v1")
            bundle = (root / "versions" / "v1" / "lightroom" /
                      "lr-xmp-mimic.lrdevplugin").resolve()

            with mock.patch.object(bootstrap, "lightroom_modules_dir",
                                   return_value=modules):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exit_code = bootstrap.plugin_install(root)
                entry = Path(json.loads(output.getvalue())["entry"])
                self.assertEqual(exit_code, 0)
                self.assertEqual(entry.resolve(), bundle)

                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exit_code = bootstrap.plugin_uninstall(root)
                self.assertEqual(exit_code, 0)

            self.assertFalse(entry.exists() or entry.is_symlink())

    def test_process_alive_distinguishes_a_live_pid_from_a_dead_one(self):
        self.assertTrue(bootstrap.process_alive(os.getpid()))
        self.assertFalse(bootstrap.process_alive(999999999))

    def test_bridge_stop_clears_a_record_for_a_process_that_already_exited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")
            pid_file = bootstrap.bridge_pid_path(root)
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text("999999999", encoding="utf-8")

            with mock.patch.object(bootstrap, "bridge_reachable", return_value=False), \
                 mock.patch.object(bootstrap, "terminate_process") as terminate:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exit_code = bootstrap.bridge_stop(root)
            result = json.loads(output.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "not_running")
            self.assertFalse(pid_file.exists())
            terminate.assert_not_called()

    def test_plugin_entry_points_through_active_so_upgrades_follow(self):
        """Registering a version path would pin Lightroom to that version forever."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            modules = Path(temporary) / "Modules"
            self.install(root, "v1")

            with mock.patch.object(bootstrap, "lightroom_modules_dir",
                                   return_value=modules):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    bootstrap.plugin_install(root)
                entry = Path(json.loads(output.getvalue())["entry"])

            self.assertIn("active", str(os.readlink(entry)))

    def test_bridge_stop_without_a_pid_file_kills_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self.install(root, "v1")
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                 mock.patch.object(bootstrap.os, "kill") as kill:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exit_code = bootstrap.bridge_stop(root)
            result = json.loads(output.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "not_started_by_installer")
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
