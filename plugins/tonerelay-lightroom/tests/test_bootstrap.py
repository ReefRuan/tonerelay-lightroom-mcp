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
import venv
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "tonerelay_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("tonerelay_bootstrap", SCRIPT)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapStatusTests(unittest.TestCase):
    def runtime_root(self, root: Path) -> tuple[Path, Path]:
        version = root / "versions" / "source"
        commands = version / "source" / "plugins" / "lightroom" / "lr-xmp-mimic.lrdevplugin" / "Commands.lua"
        commands.parent.mkdir(parents=True)
        commands.write_text("build_id = 'selection-gallery-current'", encoding="utf-8")
        (version / "lightroom" / "lr-xmp-mimic.lrdevplugin").mkdir(parents=True)
        launcher = bootstrap.launcher_path(root)
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("@echo off" if bootstrap.IS_WINDOWS else "#!/bin/sh",
                            encoding="utf-8")
        if not bootstrap.IS_WINDOWS:
            launcher.chmod(0o755)
        bootstrap._point_at(root / "active", version)
        return root, version

    def test_old_loaded_plugin_is_not_reported_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
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

    def test_unavailable_probe_does_not_infer_reload(self):
        cases = (
            None,
            {},
            {"error": "plugin did not answer probe_client before timeout"},
            {"error": "timeout", "build_id": "selection-gallery-old"},
            {"build_id": "", "capabilities": {"current_folder_selection": True}},
            {"build_id": 123, "capabilities": {"current_folder_selection": True}},
            {"build_id": "selection-gallery-current"},
            {"capabilities": {"current_folder_selection": True}},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            for probe in cases:
                with self.subTest(probe=probe), \
                     mock.patch.object(bootstrap, "bridge_reachable", return_value=True), \
                     mock.patch.object(bootstrap, "probe_loaded_plugin", return_value=probe):
                    result = bootstrap.status(root)
                diagnostic = result["diagnostic"]
                self.assertEqual(result["status"], "repair_needed")
                self.assertFalse(result["checks"]["lightroom_plugin_loaded"])
                self.assertEqual(diagnostic["code"], "bootstrap.plugin_probe_unavailable")
                self.assertEqual(diagnostic["expected_build"], "selection-gallery-current")
                self.assertEqual(diagnostic["loaded_build"],
                                 probe.get("build_id") if isinstance(probe, dict) else None)
                self.assertEqual(diagnostic["probe_error"],
                                 probe.get("error") if isinstance(probe, dict) else None)
                self.assertFalse(diagnostic["safe_to_retry"])
                self.assertIn("Unlock", diagnostic["next"])
                self.assertIn("open menus, dialogs", diagnostic["next"])
                self.assertIn("Do not infer", diagnostic["next"])
                self.assertIn("do not replay unknown writes", diagnostic["next"])
                self.assertNotIn("Quit and reopen", diagnostic["next"])
                self.assertNotIn("lightroom-plugin install", diagnostic["next"])

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

    def test_unregistered_plugin_has_actionable_registration_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.runtime_root(Path(temporary))
            with mock.patch.object(bootstrap, "bridge_reachable", return_value=False):
                result = bootstrap.status(root)

        self.assertEqual(result["status"], "repair_needed")
        self.assertEqual(result["diagnostic"]["code"],
                         "bootstrap.lightroom_plugin_registration_required")
        # The stable path through `active`, so one registration survives upgrades.
        self.assertEqual(result["diagnostic"]["lightroom_plugin"],
                         str(root / "active" / "lightroom" / "lr-xmp-mimic.lrdevplugin"))


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
            if not bootstrap.IS_WINDOWS:
                self.assertEqual((version / "lightroom").resolve(),
                                 (version / "source/plugins/lightroom").resolve())

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

    def test_upgrade_changes_the_plugin_behind_stable_active_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            first = self.source_tree(root, "first-build")
            second = self.source_tree(root / "second", "second-build")
            with mock.patch.object(bootstrap, "create_virtualenv"), \
                 mock.patch.object(bootstrap, "install_dependencies"), \
                 mock.patch.object(bootstrap, "start_lr_ops", return_value=True), \
                 mock.patch.object(bootstrap, "status", return_value={"status": "repair_needed"}):
                self.run_install(runtime_root, first, "v1")
                self.run_install(runtime_root, second, "v2")

            commands = runtime_root / "active/lightroom/lr-xmp-mimic.lrdevplugin/Commands.lua"
            self.assertIn("second-build", commands.read_text(encoding="utf-8"))

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

    def test_launcher_and_venv_layout_follow_platform(self):
        root = Path("/tmp/runtime")
        with mock.patch.object(bootstrap, "IS_WINDOWS", True):
            self.assertEqual(bootstrap.launcher_path(root).name, "tonerelay-mcp.cmd")
            self.assertEqual(bootstrap.venv_python(root).parts[-3:],
                             (".venv", "Scripts", "python.exe"))
        with mock.patch.object(bootstrap, "IS_WINDOWS", False):
            self.assertEqual(bootstrap.launcher_path(root).name, "tonerelay-mcp")
            self.assertEqual(bootstrap.venv_python(root).parts[-3:],
                             (".venv", "bin", "python"))

    def test_runtime_root_follows_platform(self):
        with mock.patch.object(bootstrap, "IS_WINDOWS", True), \
             mock.patch.dict(bootstrap.os.environ, {"SystemDrive": "D:"}):
            self.assertEqual(bootstrap.default_runtime_root(), Path("D:/ToneRelay"))
        with mock.patch.object(bootstrap, "IS_WINDOWS", True), \
             mock.patch.dict(bootstrap.os.environ, {"LOCALAPPDATA": "/tmp/local"}):
            self.assertEqual(bootstrap.fallback_runtime_root(),
                             Path("/tmp/local") / "ToneRelay")
        with mock.patch.object(bootstrap, "IS_WINDOWS", False), \
             mock.patch.object(bootstrap.sys, "platform", "linux"), \
             mock.patch.dict(bootstrap.os.environ, {"XDG_DATA_HOME": "/tmp/xdg"}):
            self.assertEqual(bootstrap.default_runtime_root(),
                             Path("/tmp/xdg") / "ToneRelay")

    def test_control_script_name_follows_platform(self):
        root = Path("/tmp/runtime")
        with mock.patch.object(bootstrap, "IS_WINDOWS", True):
            self.assertEqual(bootstrap.control_path(root).name, "tonerelay.cmd")
        with mock.patch.object(bootstrap, "IS_WINDOWS", False):
            self.assertEqual(bootstrap.control_path(root).name, "tonerelay")

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
        root = Path("/tmp/runtime")
        self.assertEqual(bootstrap.data_root(root), root / "data")

    def test_mcp_config_uses_the_stable_active_link(self):
        root = Path("/tmp/runtime")
        with mock.patch.object(bootstrap, "IS_WINDOWS", False):
            config = bootstrap.mcp_server_config(root)["tonerelay-lightroom"]
        self.assertEqual(config["args"], ["-m", "workbench_mcp"])
        self.assertIn("active", config["command"])
        self.assertEqual(
            config["env"]["PYTHONPATH"].split(bootstrap.os.pathsep),
            [str(root / "active" / "source" / relative)
             for relative in bootstrap.RUNTIME_PYTHONPATH_ENTRIES])

    def test_local_images_default_on_for_all_platforms_and_client_labels(self):
        root = Path("/tmp/预览 runtime")
        for platform in ("win32", "darwin", "linux"):
            with self.subTest(platform=platform), \
                 mock.patch.object(bootstrap, "IS_WINDOWS", platform == "win32"), \
                 mock.patch.object(bootstrap.sys, "platform", platform), \
                 mock.patch.dict(bootstrap.os.environ, {}):
                bootstrap.os.environ.pop("LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES", None)
                generic = bootstrap.mcp_server_config(root)["tonerelay-lightroom"]
                codex = bootstrap.mcp_server_config(root, client="codex")["tonerelay-lightroom"]
                self.assertEqual(generic["env"]["LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES"], "1")
                self.assertEqual(codex, generic)
                with mock.patch.dict(bootstrap.os.environ, {"LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES": "0"}):
                    disabled = bootstrap.mcp_server_config(root)["tonerelay-lightroom"]
                self.assertEqual(disabled["env"]["LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES"], "0")

    @unittest.skipUnless(os.name == "nt", "requires a real Windows child process")
    def test_windows_default_config_reaches_the_process_with_unicode_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "预览 runtime"
            active = root / "active"
            venv.EnvBuilder(with_pip=False).create(active / ".venv")
            module = active / "source/apps/workbench-mcp/workbench_mcp"
            module.mkdir(parents=True)
            (module / "__main__.py").write_text(
                "import json, os\n"
                "print(json.dumps({key: os.environ.get(key) for key in "
                "['LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES', 'LR_XMP_MIMIC_RUNTIME_DATA_ROOT', "
                "'PYTHONPATH']}, ensure_ascii=True))\n", encoding="utf-8")
            config = bootstrap.mcp_server_config(root)["tonerelay-lightroom"]
            env = os.environ.copy()
            env.update(config["env"])
            process = subprocess.run([config["command"], *config["args"]],
                                     env=env, capture_output=True, text=True,
                                     encoding="utf-8", check=True)
            observed = json.loads(process.stdout)
            self.assertEqual(observed, config["env"])

    @unittest.skipUnless(os.name == "nt", "requires a real Windows CMD launcher")
    def test_windows_launcher_preserves_default_and_explicit_zero_with_unicode_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "预览 runtime"
            active = root / "active"
            venv.EnvBuilder(with_pip=False).create(active / ".venv")
            module = active / "source/apps/workbench-mcp/workbench_mcp"
            module.mkdir(parents=True)
            (module / "__main__.py").write_text(
                "import json, os\n"
                "print(json.dumps({key: os.environ.get(key) for key in "
                "['LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES', 'LR_XMP_MIMIC_RUNTIME_DATA_ROOT', "
                "'PYTHONPATH']}, ensure_ascii=True))\n", encoding="utf-8")
            bootstrap.write_bin_scripts(root)
            env = os.environ.copy()
            for opt_in in (None, "0", "1"):
                with self.subTest(opt_in=opt_in):
                    env.pop("LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES", None)
                    if opt_in is not None:
                        env["LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES"] = opt_in
                    process = subprocess.run(["cmd", "/d", "/c", str(bootstrap.launcher_path(root))],
                                             env=env, capture_output=True, text=True,
                                             encoding="utf-8", errors="replace")
                    self.assertEqual(process.returncode, 0, process.stderr)
                    observed = json.loads(process.stdout)
                    self.assertEqual(observed["LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES"], opt_in)
                    self.assertEqual(observed["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"], str(root / "data"))
                    self.assertEqual(observed["PYTHONPATH"], bootstrap.runtime_pythonpath(active / "source"))

    def test_windows_install_copies_plugin_instead_of_linking(self):
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            info = (staging / "source" / "plugins" / "lightroom" /
                    "lr-xmp-mimic.lrdevplugin" / "Info.lua")
            info.parent.mkdir(parents=True)
            info.write_text("return {}", encoding="utf-8")
            with mock.patch.object(bootstrap, "IS_WINDOWS", True):
                bootstrap.link_runtime_plugin(staging)

            copied = (staging / "lightroom" / "lr-xmp-mimic.lrdevplugin" / "Info.lua")
            self.assertTrue(copied.is_file())
            self.assertFalse((staging / "lightroom").is_symlink())


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

    def test_windows_default_cli_outputs_client_env_for_install_and_mcp_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            source = self.source_tree(Path(temporary) / "source")
            with mock.patch.object(bootstrap, "IS_WINDOWS", True), \
                 mock.patch.object(bootstrap, "create_virtualenv"), \
                 mock.patch.object(bootstrap, "install_dependencies"), \
                 mock.patch.object(bootstrap, "start_lr_ops", return_value=True), \
                 mock.patch.object(bootstrap, "status", return_value={"status": "ready"}):
                for command in (["install"], ["runtime", "install"], ["mcp", "status"]):
                    with self.subTest(command=command):
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            exit_code = bootstrap.main([
                                *command, "--runtime-root", str(root),
                                "--source-root", str(source), "--version", "v1"])
                        result = json.loads(output.getvalue())
                        self.assertEqual(exit_code, 0)
                        self.assertEqual(result["mcp_server"]["tonerelay-lightroom"]["env"][
                            "LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES"], "1")

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
            self.assertIn("https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/faq.md",
                          result["diagnostic"]["next"])
            self.assertIn("PIP_INDEX_URL", result["diagnostic"]["next"])

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
            self.assertIn("tonerelay_bootstrap.py", text)
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
