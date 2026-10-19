"""Removal contracts use isolated installations, client files and owned children."""
from __future__ import annotations

import contextlib
import io
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import test_bootstrap_macos as existing

bootstrap = existing.bootstrap
lifecycle = bootstrap.lifecycle() if os.name == "posix" else None


@unittest.skipUnless(os.name == "posix", "POSIX installation ledger and file locking")
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.base = self.directory / "ToneRelay"
        lifecycle.prepare(self.base)
        self.runtime = self.base / "runtime"
        self.ledger = lifecycle.Ledger(self.runtime)
        self.data = self.base / "data"
        self.data.mkdir()
        (self.data / "personal.json").write_text("keep", encoding="utf-8")
        self.software = self.runtime / "versions/v1/source/app.py"
        self.software.parent.mkdir(parents=True)
        self.software.write_text("software", encoding="utf-8")
        (self.runtime / "active").symlink_to(self.runtime / "versions/v1", target_is_directory=True)
        self.ledger.remember_tree(self.runtime)
        self.ledger.checkpoint("runtime_installed", self.data)
        self.entry = self.directory / "Modules/lr-xmp-mimic.lrdevplugin"
        self.entry.parent.mkdir()
        self.entry.symlink_to(self.runtime / "active/plugin", target_is_directory=True)
        self.scope = {"runtime": [], "lightroom": [], "unattributed": []}
        for patcher in (
            mock.patch.object(lifecycle, "process_snapshot", side_effect=lambda *args: self.scope),
            mock.patch.object(bootstrap, "lightroom_entry", return_value=self.entry),
            mock.patch.object(bootstrap, "bridge_reachable", return_value=False),
            mock.patch.object(bootstrap, "probe_loaded_plugin", side_effect=AssertionError("uninstall must not probe")),
            mock.patch.object(bootstrap, "_lr_ops_request", side_effect=AssertionError("uninstall must not use HTTP")),
            mock.patch.object(lifecycle.time, "sleep"),
            mock.patch.dict(os.environ, {"LR_XMP_MIMIC_RUNTIME_DATA_ROOT": "", "LROPS_BASE": bootstrap.DEFAULT_BRIDGE_BASE}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.api = types.SimpleNamespace(**vars(bootstrap))

    def apply(self):
        return lifecycle.uninstall(self.ledger, self.api, apply=True, clients_reviewed=True)

    def client(self):
        path = self.directory / "client.json"
        entry = {"command": str(self.runtime / "bin/tonerelay-mcp"), "env": {"TOKEN": "private"}}
        document = {"mcpServers": {"tonerelay-lightroom": entry, "other": {"command": "other"}}, "theme": "dark"}
        path.write_text(json.dumps(document), encoding="utf-8")
        lifecycle.record_client(self.ledger, "client", path, "tonerelay-lightroom")
        return path

    def test_prepare_records_location_before_download(self):
        self.assertFalse((self.base / "source").exists())
        self.assertEqual(self.ledger.record["source_destination"], str(self.base / "source"))
        self.assertEqual(self.ledger.record["runtime_root"], str(self.runtime))

    def test_plan_and_offline_check_are_read_only(self):
        before = self.ledger.path.read_bytes()
        result = lifecycle.uninstall(self.ledger, self.api)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertTrue(self.software.exists())
        self.assertTrue(self.entry.is_symlink())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = bootstrap.main(["uninstall-check", "--runtime-root", str(self.runtime)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "removal_pending")
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_full_removal_preserves_data_other_servers_and_backup(self):
        config = self.client()
        before = config.read_bytes()
        source = self.base / "source"
        source.mkdir()
        (source / "download.txt").write_text("download", encoding="utf-8")
        self.ledger.remember_tree(source)
        self.ledger.record["managed_source"] = str(source)
        self.ledger.save()
        result = self.apply()
        self.assertEqual(result["status"], "software_removed")
        self.assertFalse(self.runtime.exists())
        self.assertFalse(source.exists())
        self.assertFalse(self.entry.is_symlink())
        self.assertEqual((self.data / "personal.json").read_text(), "keep")
        self.assertEqual(json.loads(config.read_text()), {"mcpServers": {"other": {"command": "other"}}, "theme": "dark"})
        backup = Path(self.ledger.record["clients"]["client"]["backup"])
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        self.assertTrue(Path(result["resume_script"]).is_file())
        self.assertTrue(self.ledger.path.exists())
        self.assertEqual(bootstrap.data_root(self.runtime), self.data)
        self.assertEqual(self.apply()["status"], "software_removed")
        self.assertEqual(len(list(backup.parent.glob("*-client.json"))), 1)

    def test_normal_lightroom_quit_is_a_resumable_checkpoint(self):
        self.scope["lightroom"] = [1234]
        result = self.apply()
        self.assertEqual(result["status"], "needs_lightroom_quit")
        self.assertTrue(self.software.exists())
        self.assertTrue(self.entry.is_symlink())
        self.assertTrue(Path(result["resume_script"]).is_file())
        self.scope["lightroom"] = []
        self.assertEqual(self.apply()["status"], "software_removed")

    def test_changed_and_unrecorded_files_are_reported_and_retained(self):
        self.software.write_text("user change", encoding="utf-8")
        extra = self.software.parent / "personal.jpg"
        extra.write_bytes(b"personal")
        result = self.apply()
        self.assertEqual(result["status"], "residual_review_required")
        self.assertTrue(self.software.exists())
        self.assertTrue(extra.exists())
        self.assertIn(str(extra.relative_to(self.base)), result["unrecorded"])

    def test_link_removal_keeps_external_target(self):
        link = self.runtime / "external"
        link.symlink_to(self.data, target_is_directory=True)
        self.ledger.remember(link)
        self.ledger.save()
        self.assertEqual(self.apply()["status"], "software_removed")
        self.assertFalse(link.is_symlink())
        self.assertTrue((self.data / "personal.json").exists())

    def test_empty_selected_data_directory_is_retained(self):
        (self.data / "personal.json").unlink()
        self.ledger.remember_tree(self.data)
        result = self.apply()
        self.assertEqual(result["status"], "software_removed")
        self.assertTrue(self.data.is_dir())

    def test_original_single_root_layout_keeps_internal_data_and_recovery(self):
        root = self.directory / "old-layout"
        ledger = lifecycle.Ledger(root, create=True)
        data = root / "data"
        data.mkdir()
        personal = data / "episode.json"
        personal.write_text("keep")
        software = root / "bin/launcher"
        software.parent.mkdir()
        software.write_text("software")
        ledger.checkpoint("runtime_installed", data)
        ledger.remember_tree(root / "bin")
        # Even explicitly recorded historical data stays protected and is not
        # misreported as incomplete software removal.
        ledger.remember_tree(data)
        with mock.patch.object(bootstrap, "lightroom_entry", return_value=self.directory / "absent-entry"):
            api = types.SimpleNamespace(**vars(bootstrap))
            result = lifecycle.uninstall(ledger, api, apply=True, clients_reviewed=True)
        self.assertEqual(result["status"], "software_removed")
        self.assertEqual(personal.read_text(), "keep")
        self.assertFalse(software.exists())
        self.assertTrue(ledger.path.exists())
        self.assertTrue(Path(result["resume_script"]).is_file())

    def test_changed_json_client_blocks_before_mutation(self):
        path = self.client()
        document = json.loads(path.read_text())
        document["mcpServers"]["tonerelay-lightroom"]["command"] = "different"
        path.write_text(json.dumps(document))
        result = self.apply()
        self.assertEqual(result["status"], "blocked_client_changed")
        self.assertTrue(self.software.exists())
        self.assertFalse((self.base / "backups").exists())

    def test_official_client_needs_observed_removal_evidence(self):
        path = self.directory / "official.conf"
        path.write_text("registered")
        lifecycle.record_client(self.ledger, "official", path, "plugin", kind="official")
        self.assertEqual(self.apply()["status"], "blocked_client_changed")
        path.write_text("removed")
        lifecycle.record_client(self.ledger, "official", path, "plugin", kind="official", evidence="Official removal API returned uninstalled")
        self.assertEqual(lifecycle.client_state(self.ledger.record["clients"]["official"]), "operator_verified_removed")
        path.write_text("changed again")
        self.assertEqual(lifecycle.client_state(self.ledger.record["clients"]["official"]), "official_removal_required")

    def test_unknown_process_or_respawn_blocks_file_removal(self):
        self.scope["unattributed"] = [{"pid": 99}]
        self.assertEqual(self.apply()["status"], "blocked_process_ownership")
        self.scope["unattributed"] = []
        self.scope["runtime"] = [{"pid": 88}]
        self.assertEqual(self.apply()["status"], "blocked_processes")
        self.assertTrue(self.software.exists())

    def test_non_probe_jobs_block_and_queued_probe_is_retained(self):
        journal = self.data / "lr-ops/jobs.jsonl"
        journal.parent.mkdir()
        probe = {"job_id": "probe", "command": "probe_client", "status": "queued"}
        edit = {"job_id": "edit", "command": "apply_settings", "status": "dispatched"}
        journal.write_text(json.dumps(probe) + "\n" + json.dumps(edit) + "\n")
        self.assertEqual(self.apply()["status"], "blocked_jobs")
        journal.write_text(journal.read_text() + json.dumps({**edit, "status": "done"}) + "\n")
        before = journal.read_bytes()
        result = self.apply()
        self.assertEqual(result["status"], "software_removed")
        self.assertEqual(result["jobs"]["queued_probes"], [probe])
        self.assertEqual(journal.read_bytes(), before)

    def test_incomplete_journal_fails_closed(self):
        journal = self.data / "lr-ops/jobs.jsonl"
        journal.parent.mkdir()
        journal.write_text('{"job_id":"unknown"')
        with self.assertRaises(ValueError):
            self.apply()
        self.assertTrue(self.software.exists())

    def test_parent_replaced_by_internal_link_reports_residue(self):
        shutil.rmtree(self.software.parent)
        self.software.parent.symlink_to(self.data, target_is_directory=True)
        result = self.apply()
        self.assertEqual(result["status"], "residual_review_required")
        self.assertTrue(self.software.parent.is_symlink())
        self.assertTrue((self.data / "personal.json").exists())

    def test_parent_link_escape_is_refused_without_deleting_target(self):
        outside = self.directory / "outside"
        outside.mkdir()
        target = outside / "personal.json"
        target.write_text("keep")
        shutil.rmtree(self.software.parent)
        self.software.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(target.read_text(), "keep")

    def test_alias_ancestors_canonicalize_but_leaf_link_is_preserved(self):
        alias = self.directory / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        self.ledger.remember(alias / "runtime/active")
        self.assertEqual(self.ledger.record["files"]["runtime/active"]["kind"], "link")

    def test_operation_lock_blocks_overlapping_mutations(self):
        with lifecycle.operation_lock(self.runtime):
            with self.assertRaisesRegex(ValueError, "Another installation operation"):
                with lifecycle.operation_lock(self.runtime):
                    self.fail("Second lock acquired")

    def test_process_inventory_ignores_shell_text_but_reports_real_unattributed_services(self):
        output = f'101 /bin/zsh -c "{self.runtime}/bin/tonerelay mcp-run"\n102 python -m workbench_mcp\n'
        api = types.SimpleNamespace(bridge_record=lambda root: None,
                                    process_identity=lambda pid: {"created": str(pid)},
                                    process_executable=lambda pid: "/bin/zsh" if pid == 101 else "/usr/bin/python3")
        # Exercise the original implementation rather than the scoped fixture stub.
        # setUp patches process_snapshot; use a separate module loaded from its file.
        spec = importlib.util.spec_from_file_location("inventory_test", lifecycle.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module.subprocess, "run", return_value=types.SimpleNamespace(stdout=output)):
            report = module.process_snapshot(self.runtime, api)
        self.assertEqual(report["runtime"], [])
        self.assertEqual([row["pid"] for row in report["unattributed"]], [102])

    def test_interrupted_file_removal_resumes_from_saved_runner(self):
        original = Path.unlink
        failed = False
        def interrupt(path, *args, **kwargs):
            nonlocal failed
            if path == self.software and not failed:
                failed = True
                raise OSError("interrupted deletion")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", new=interrupt):
            with self.assertRaises(OSError):
                self.apply()
        self.assertTrue(Path(self.ledger.record["resume_script"]).is_file())
        self.assertEqual(self.apply()["status"], "software_removed")
        saved = Path(self.ledger.record["resume_script"])
        result = subprocess.run([sys.executable, str(saved), "--help"], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = bootstrap.main(["uninstall-check", "--runtime-root", str(self.runtime)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "software_removed")


@unittest.skipUnless(sys.platform == "darwin", "Mac process creation identities")
class OwnedMcpProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "runtime space 中文"
        self.version = self.root / "versions/v1"
        scripts = self.version / "source/plugins/tonerelay-lightroom/scripts"
        scripts.mkdir(parents=True)
        for name in ("tonerelay_bootstrap_macos.py", "tonerelay_lifecycle.py"):
            shutil.copyfile(Path(bootstrap.__file__).with_name(name), scripts / name)
        configuration = self.version / "source/services/lr-ops/lr_ops/configuration.py"
        configuration.parent.mkdir(parents=True)
        shutil.copyfile(bootstrap._config_path, configuration)
        python = bootstrap.venv_python(self.version)
        python.parent.mkdir(parents=True)
        python.symlink_to(Path(sys.executable).resolve())
        # Model an installed venv using the verified test environment's deps;
        # a bare symlink to base Python cannot import the Runtime dependencies.
        if sys.prefix != sys.base_prefix:
            shutil.copyfile(Path(sys.prefix) / "pyvenv.cfg", python.parent.parent / "pyvenv.cfg")
            site = python.parent.parent / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            site.mkdir(parents=True)
            dependency_site = Path(sys.prefix) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            (site / "test-dependencies.pth").write_text(str(dependency_site) + "\n")
        module = self.version / "source/apps/workbench-mcp/workbench_mcp"
        module.mkdir(parents=True)
        (module / "__init__.py").write_text("")
        (module / "__main__.py").write_text(
            'import json,os,sys\nfor line in sys.stdin:\n print(json.dumps({"pid":os.getpid(),"echo":json.loads(line)}),flush=True)\n')
        (self.root / "active").symlink_to(self.version, target_is_directory=True)
        self.ledger = lifecycle.Ledger(self.root, create=True)
        self.ledger.checkpoint("runtime_installed", self.root / "data")
        bootstrap.write_bin_scripts(self.root)
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for process in self.children:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def start(self):
        env = os.environ.copy()
        env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"] = str(self.root / "data")
        process = subprocess.Popen([str(bootstrap.launcher_path(self.root))], env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        process.stdin.write('{"request":"test"}\n')
        process.stdin.flush()
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        line = lines.get(timeout=5)
        self.assertTrue(line, process.stderr.read() if process.poll() is not None else "No stdio reply")
        reply = json.loads(line)
        path = self.root / "run" / f'mcp-{reply["pid"]}.json'
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists())
        return process, path

    def test_multiple_stdio_instances_record_identity_and_stop_without_orphans(self):
        first, first_path = self.start()
        second, second_path = self.start()
        for path in (first_path, second_path):
            record = json.loads(path.read_text())
            self.assertEqual(record["identity"], bootstrap.process_identity(record["pid"]))
        lifecycle.stop_mcp(self.ledger, types.SimpleNamespace(**vars(bootstrap)))
        first.wait(timeout=5)
        second.wait(timeout=5)
        self.assertFalse(first_path.exists())
        self.assertFalse(second_path.exists())

    def test_changed_process_identity_is_never_signalled(self):
        process, path = self.start()
        record = json.loads(path.read_text())
        record["identity"]["created"] = "not-this-process"
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "identity changed"):
            lifecycle.stop_mcp(self.ledger, types.SimpleNamespace(**vars(bootstrap)))
        self.assertIsNone(process.poll())

    def test_root_marker_cannot_authorize_stopping_an_unrelated_child(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(process)
        path = self.root / "run" / f"mcp-{process.pid}.json"
        lifecycle.atomic_json(path, {"schema": "tonerelay-owned-mcp-v1", "runtime_root": str(self.root),
                                    "version_root": str(self.version),
                                    "command": [str(bootstrap.venv_python(self.version)), "-m", "workbench_mcp"],
                                    "pid": process.pid, "identity": bootstrap.process_identity(process.pid)})
        with self.assertRaisesRegex(ValueError, "launch ownership is unknown"):
            lifecycle.stop_mcp(self.ledger, types.SimpleNamespace(**vars(bootstrap)))
        self.assertIsNone(process.poll())

    def test_uninstall_checkpoint_refuses_client_respawn(self):
        self.ledger.checkpoint("clients_removed")
        result = subprocess.run([str(bootstrap.launcher_path(self.root))], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Uninstallation is in progress", result.stderr)
        self.assertFalse(list((self.root / "run").glob("mcp-*.json")))

    def test_real_runtime_initialize_and_tools_list_through_recorded_launcher(self):
        source = self.version / "source"
        shutil.rmtree(source)
        bootstrap.copy_runtime_source(bootstrap.bundled_source_root(), source)
        env = os.environ.copy()
        env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"] = str(self.root / "data")
        process = subprocess.Popen([str(bootstrap.launcher_path(self.root))], env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        lines = queue.Queue()
        def pump():
            for line in process.stdout:
                lines.put(line)
        threading.Thread(target=pump, daemon=True).start()
        bootstrap._mcp_send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                                 "clientInfo": {"name": "lifecycle-test", "version": "1"}}})
        reply = bootstrap._mcp_receive(lines, 10)
        self.assertEqual(reply.get("id"), 1, reply)
        self.assertTrue(reply["result"]["instructions"])
        bootstrap._mcp_send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        bootstrap._mcp_send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        reply = bootstrap._mcp_receive(lines, 10)
        self.assertEqual(reply.get("id"), 2, reply)
        tools = reply["result"]["tools"]
        self.assertTrue(tools)
        self.assertTrue(all(tool["name"].startswith("trlr_") and isinstance(tool["inputSchema"], dict) for tool in tools))
        records = list((self.root / "run").glob("mcp-*.json"))
        self.assertEqual(len(records), 1)
        child = json.loads(records[0].read_text())["pid"]
        process.stdin.close()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(bootstrap.process_alive(child))
        self.assertFalse(records[0].exists())


if __name__ == "__main__":
    unittest.main()
