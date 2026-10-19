"""Atomic Episode commits tolerate Windows readers without replaying edits."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from workbench.studio import engine


def sharing_error(code=32):
    error = PermissionError("simulated Windows sharing denial")
    error.winerror = code
    return error


class AtomicEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "episode.json"
        self.path.write_text('{"revision": 1}', encoding="utf-8")

    def test_transient_windows_denial_retries_the_same_finished_commit(self):
        original = Path.replace
        attempts = []

        def replace(temp, target):
            attempts.append((temp, temp.read_bytes()))
            if len(attempts) == 1:
                self.assertEqual(json.loads(target.read_text()), {"revision": 1})
                raise sharing_error()
            return original(temp, target)

        with mock.patch.object(Path, "replace", replace), \
             mock.patch.object(engine.time, "sleep") as sleep:
            engine.atomic(self.path, {"revision": 2})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        sleep.assert_called_once_with(0.01)
        self.assertEqual(json.loads(self.path.read_text()), {"revision": 2})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_persistent_windows_denial_is_bounded_and_preserves_previous_state(self):
        before = self.path.read_bytes()
        with mock.patch.object(Path, "replace", side_effect=sharing_error(5)) as replace, \
             mock.patch.object(engine.time, "monotonic", side_effect=[0.0, 0.1, 1.1]), \
             mock.patch.object(engine.time, "sleep"), \
             self.assertRaises(PermissionError):
            engine.atomic(self.path, {"revision": 2})
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_other_permission_errors_are_not_retried(self):
        before = self.path.read_bytes()
        with mock.patch.object(Path, "replace", side_effect=PermissionError("denied")) as replace, \
             mock.patch.object(engine.time, "sleep") as sleep, \
             self.assertRaises(PermissionError):
            engine.atomic(self.path, {"revision": 2})
        replace.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "requires Windows file-sharing semantics")
    def test_real_windows_open_reader_can_finish_before_atomic_commit(self):
        original = Path.replace
        blocked = threading.Event()
        errors = []

        def replace(temp, target):
            try:
                return original(temp, target)
            except PermissionError:
                blocked.set()
                raise

        def commit():
            try:
                engine.atomic(self.path, {"revision": 2})
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(Path, "replace", replace):
            with self.path.open("rb") as reader:
                worker = threading.Thread(target=commit)
                worker.start()
                try:
                    self.assertTrue(blocked.wait(0.5), "replacement must hit the real reader lock")
                    self.assertEqual(json.loads(reader.read()), {"revision": 1})
                finally:
                    reader.close()
                    worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(self.path.read_text()), {"revision": 2})
