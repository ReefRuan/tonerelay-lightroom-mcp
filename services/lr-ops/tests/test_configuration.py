"""The shared file preserves concurrent writes and confines owned paths."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from lr_ops import configuration

from lr_ops.configuration import (
    ConfigConflict, ConfigError, ConfigStore, defaults, process_environment,
)


def concurrent_update(path, section, key, start, count):
    store = ConfigStore(Path(path), required=True)
    if not start.wait(10):
        raise RuntimeError("writer start timed out")
    for _ in range(count):
        def change(value):
            if isinstance(value[key], bool):
                value[key] = not value[key]
            else:
                value[key] -= 1
            time.sleep(0.002)
        store.update(section, change)


def hold_config_lock(path, ready, release):
    with ConfigStore(Path(path), required=True)._lock():
        ready.set()
        if not release.wait(10):
            raise RuntimeError("lock holder release timed out")


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "ToneRelay"
        self.store = ConfigStore(self.root / "config.json")

    def test_partial_save_retains_other_sections_and_prior_good_backup(self):
        original = self.store.initialize()
        result = self.store.update("presentation", lambda v: v.update(codex_preview_max_edge=512))
        self.assertEqual(result["targets"], original["targets"])
        self.assertEqual(result["revision"], 1)
        backup = self.root / "backups/config-last-good.json"
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)
        self.assertEqual(self.store.read(), result)
        if os.name != "nt":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_stale_revision_cannot_overwrite_another_writer(self):
        self.store.initialize()
        other = ConfigStore(self.store.path)
        other.update("execution", lambda v: v.update(allow_structured=False))
        before = self.store.path.read_bytes()
        with self.assertRaises(ConfigConflict):
            self.store.update("presentation", lambda v: v.update(codex_preview_max_edge=256), expected_revision=0)
        self.assertEqual(self.store.path.read_bytes(), before)

    def run_writers(self, fields):
        self.store.initialize()
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        processes = [context.Process(target=concurrent_update,
                     args=(str(self.store.path), section, key, start, 9))
                     for section, key in fields]
        try:
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        return self.store.read()

    def test_separate_processes_preserve_different_sections(self):
        result = self.run_writers([
            ("presentation", "codex_preview_max_edge"),
            ("execution", "allow_structured"),
        ])
        self.assertEqual(result["revision"], 18)
        self.assertEqual(result["presentation"]["codex_preview_max_edge"], 1015)
        self.assertFalse(result["execution"]["allow_structured"])

    def test_separate_processes_preserve_changes_in_the_same_section(self):
        result = self.run_writers([
            ("presentation", "codex_preview_max_edge"),
            ("presentation", "edit_render_max_edge"),
        ])
        self.assertEqual(result["revision"], 18)
        self.assertEqual(result["presentation"], {
            "codex_preview_max_edge": 1015, "edit_render_max_edge": 2039,
        })

    @unittest.skipUnless(os.name == "nt", "Windows byte-range locking regression")
    def test_waits_for_a_byte_locked_by_another_process_before_reading(self):
        self.store.initialize()
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        holder = context.Process(target=hold_config_lock,
                                 args=(str(self.store.path), ready, release))
        timer = threading.Timer(0.15, release.set)
        try:
            holder.start()
            self.assertTrue(ready.wait(10))
            timer.start()
            result = self.store.update("execution", lambda v: v.update(allow_structured=False))
            self.assertFalse(result["execution"]["allow_structured"])
            self.assertEqual(result["revision"], 1)
            holder.join(10)
            self.assertEqual(holder.exitcode, 0)
        finally:
            release.set()
            timer.cancel()
            if holder.is_alive():
                holder.terminate()
                holder.join(5)

    @unittest.skipUnless(os.name == "nt", "Windows file-sharing regression")
    def test_real_open_reader_releases_before_update_commits_without_replaying_change(self):
        self.store.initialize()
        original = os.replace
        blocked = threading.Event()
        errors = []
        results = []
        changes = []

        def replace(temporary, target):
            try:
                return original(temporary, target)
            except PermissionError:
                if Path(target) == self.store.path:
                    blocked.set()
                raise

        def change(value):
            changes.append(True)
            value["allow_structured"] = False

        def update():
            try:
                results.append(self.store.update("execution", change))
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(configuration.os, "replace", side_effect=replace):
            with self.store.path.open("rb") as reader:
                worker = threading.Thread(target=update)
                worker.start()
                try:
                    self.assertTrue(blocked.wait(0.5), "must encounter the real reader lock")
                    self.assertEqual(json.loads(reader.read())["revision"], 0)
                finally:
                    reader.close()
                    worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["revision"], 1)
        self.assertFalse(self.store.read()["execution"]["allow_structured"])
        self.assertEqual(list(self.root.glob(".config-*.tmp")), [])

    def test_atomic_retry_reuses_the_same_flushed_commit(self):
        self.store.initialize()
        original = os.replace
        attempts = []
        error = PermissionError("simulated Windows reader lock")
        error.winerror = 32

        def replace(temporary, target):
            attempts.append((temporary, Path(temporary).read_bytes()))
            if len(attempts) == 1:
                raise error
            return original(temporary, target)

        with mock.patch.object(configuration.os, "replace", side_effect=replace), \
             mock.patch.object(configuration.time, "sleep") as sleep:
            self.store._atomic_write(self.store.path, {"revision": 1})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        sleep.assert_called_once_with(0.01)
        self.assertEqual(json.loads(self.store.path.read_text(encoding="utf-8")), {"revision": 1})
        self.assertEqual(list(self.root.glob(".config-*.tmp")), [])

    def test_atomic_persistent_windows_denial_is_bounded_and_preserves_previous_config(self):
        self.store.initialize()
        before = self.store.path.read_bytes()
        error = PermissionError("persistent Windows access denial")
        error.winerror = 5
        with mock.patch.object(configuration.os, "replace", side_effect=error) as replace, \
             mock.patch.object(configuration.time, "monotonic", side_effect=[0.0, 0.1, 1.1]), \
             mock.patch.object(configuration.time, "sleep"), \
             self.assertRaises(PermissionError):
            self.store._atomic_write(self.store.path, {"revision": 1})
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".config-*.tmp")), [])

    def test_atomic_other_errors_propagate_without_retry(self):
        self.store.initialize()
        before = self.store.path.read_bytes()
        for error in (PermissionError("POSIX permission denied"), OSError("I/O failure")):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(configuration.os, "replace", side_effect=error) as replace, \
                 mock.patch.object(configuration.time, "sleep") as sleep, \
                 self.assertRaises(type(error)):
                self.store._atomic_write(self.store.path, {"revision": 1})
            replace.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(self.store.path.read_bytes(), before)
            self.assertEqual(list(self.root.glob(".config-*.tmp")), [])

    def test_missing_managed_or_damaged_file_never_uses_defaults(self):
        with self.assertRaises(ConfigError):
            ConfigStore(self.store.path, required=True).read()
        self.root.mkdir()
        self.store.path.write_text("broken JSON")
        with self.assertRaises(ConfigError):
            self.store.update("execution", lambda v: v.update(allow_structured=False))
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "broken JSON")
        self.assertFalse((self.root / "backups/config-last-good.json").exists())

    def test_unsafe_or_overlapping_owned_paths_are_rejected(self):
        self.store.initialize()
        for relative in ("../outside", str(self.root.parent / "outside"),
                         "data/subdirectory", "versions/cache", ".hidden", "cache//pip"):
            with self.subTest(relative=relative), self.assertRaises(ConfigError):
                self.store.update("paths", lambda v: v.update(cache=relative))
        self.assertEqual(self.store.read()["revision"], 0)

    def test_symlinked_config_or_owned_directory_is_rejected(self):
        self.store.initialize()
        outside = self.root.parent / "outside"
        outside.mkdir()
        try:
            (self.root / "cache").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink creation unavailable: {exc}")
        with self.assertRaises(ConfigError):
            self.store.read()
        (self.root / "cache").unlink()
        self.store.path.rename(self.root / "actual.json")
        self.store.path.symlink_to(self.root / "actual.json")
        with self.assertRaises(ConfigError):
            ConfigStore(self.store.path)

    def test_inherited_paths_and_user_pip_settings_cannot_escape_the_root(self):
        self.store.initialize()
        inherited = {key: "outside" for key in (
            "LR_XMP_MIMIC_RUNTIME_DATA_ROOT", "TMPDIR", "TEMP", "TMP",
            "PIP_CACHE_DIR", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX",
            "PIP_TARGET", "PIP_PREFIX", "PYTHONHOME", "PYTHONUSERBASE",
        )}
        inherited["PIP_INDEX_URL"] = "https://example.invalid/simple"
        env = process_environment(self.store, inherited)
        for key in ("LR_XMP_MIMIC_RUNTIME_DATA_ROOT", "TMPDIR", "TEMP", "TMP",
                    "PIP_CACHE_DIR", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX"):
            self.assertIn(self.root, Path(env[key]).parents)
        for key in ("PIP_TARGET", "PIP_PREFIX", "PYTHONHOME", "PYTHONUSERBASE"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PIP_USER"], "false")
        self.assertEqual(env["PIP_INDEX_URL"], inherited["PIP_INDEX_URL"])

    def test_invalid_field_and_invalid_endpoint_are_rejected_before_save(self):
        self.store.initialize()
        before = self.store.path.read_bytes()
        for base in ("http://host:bad", "http://host:99999", "http://user:secret@host", "ftp://host"):
            with self.subTest(base=base), self.assertRaises(ConfigError):
                self.store.update("targets", lambda v: v["targets"][0].update(base=base))
        with self.assertRaises(ConfigError):
            self.store.update("folders", lambda v: v.update(recursiv=True))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_direct_legacy_client_recognizes_only_matching_source_and_venv(self):
        version = self.root / "versions" / "test"
        source = version / "source"
        source.mkdir(parents=True)
        (version / ".venv").mkdir()
        (version / ".venv/pyvenv.cfg").write_text("home = test")
        active = self.root / "active"
        try:
            active.symlink_to(version, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(configuration.installed_runtime_root(source))
                with mock.patch.object(configuration.sys, "prefix", str(version / ".venv")):
                    self.assertEqual(configuration.installed_runtime_root(source), self.root)
        finally:
            active.unlink()

    def test_worker_cache_stays_in_configured_root_and_is_generation_specific(self):
        self.store.initialize()
        original_cache = configuration.sys.pycache_prefix
        original_temp = tempfile.tempdir
        try:
            with mock.patch.dict(os.environ, {"TONERELAY_MCP_WORKER": "1", "TONERELAY_MCP_GENERATION": "a" * 32,
                                            "PYTHONPYCACHEPREFIX": "outside"}, clear=True), mock.patch.object(os, "chdir"):
                configuration.apply_process_environment(self.store)
                expected = self.root / "cache/python/mcp" / ("a" * 32)
                self.assertEqual(configuration.sys.pycache_prefix, str(expected))
                self.assertEqual(os.environ["PYTHONPYCACHEPREFIX"], str(expected))
        finally:
            configuration.sys.pycache_prefix = original_cache
            tempfile.tempdir = original_temp


if __name__ == "__main__":
    unittest.main()
