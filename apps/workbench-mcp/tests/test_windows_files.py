"""Exercise Windows preview confinement with real filesystem handles."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from workbench.store import StoreError
from workbench_mcp import windows_files


@unittest.skipUnless(os.name == "nt", "Windows handle semantics")
class WindowsPreviewFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "目录 with spaces"
        self.session_id = str(uuid.uuid4())
        self.previews = self.root / "sessions" / self.session_id / "previews"
        self.previews.mkdir(parents=True)
        self.preview = self.previews / "预览 image.jpg"
        self.content = b"exact persisted preview snapshot"
        self.preview.write_bytes(self.content)

    def read(self, limit=1024):
        return windows_files.read_preview(self.root.resolve(), self.session_id,
                                          self.preview.name, limit)

    def junction(self, link, target):
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isjunction(link))

    def test_unicode_spaces_and_bounded_single_snapshot(self):
        self.assertEqual(self.read(), self.content)
        self.assertEqual(self.read(8), self.content[:9])

    def test_alternate_data_stream_address_is_rejected(self):
        stream_name = self.preview.name + ":other"
        (self.previews / stream_name).write_bytes(b"stream payload")
        with self.assertRaisesRegex(StoreError, "escapes its preview directory"):
            windows_files.read_preview(self.root.resolve(), self.session_id,
                                       stream_name, 1024)

    def test_store_root_junction_is_allowed(self):
        linked = self.base / "Store root junction"
        self.junction(linked, self.root)
        self.assertEqual(windows_files.read_preview(
            linked.resolve(), self.session_id, self.preview.name, 1024), self.content)

    def test_child_directory_junction_is_rejected(self):
        outside = self.base / "outside previews"
        self.previews.rename(outside)
        self.junction(self.previews, outside)
        with self.assertRaisesRegex(StoreError, "symlinks are not accepted"):
            self.read()

    def test_file_replaced_by_symlink_before_open_is_rejected(self):
        outside = self.base / "outside.jpg"
        outside.write_bytes(b"outside content")
        original_open = windows_files._open_handle

        def replace_before_open(kernel, path, *, directory, allow_reparse=False):
            if not directory:
                self.preview.unlink()
                self.preview.symlink_to(outside)
            return original_open(kernel, path, directory=directory,
                                 allow_reparse=allow_reparse)

        with patch.object(windows_files, "_open_handle", side_effect=replace_before_open):
            with self.assertRaisesRegex(StoreError, "symlinks are not accepted"):
                self.read()

    def test_child_directory_replaced_by_junction_before_open_is_rejected(self):
        outside = self.base / "outside previews"
        original_open = windows_files._open_handle

        def replace_before_open(kernel, path, *, directory, allow_reparse=False):
            if directory and Path(path).name == "previews":
                self.previews.rename(outside)
                self.junction(self.previews, outside)
            return original_open(kernel, path, directory=directory,
                                 allow_reparse=allow_reparse)

        with patch.object(windows_files, "_open_handle", side_effect=replace_before_open):
            with self.assertRaisesRegex(StoreError, "symlinks are not accepted"):
                self.read()

    def test_anchored_directories_cannot_be_renamed_during_read(self):
        original_open = windows_files._open_handle
        attempts = []

        def attempt_swap(kernel, path, *, directory, allow_reparse=False):
            if not directory:
                for ancestor in (self.base, self.root, self.root / "sessions",
                                 self.previews.parent, self.previews):
                    with self.assertRaises(PermissionError):
                        ancestor.rename(ancestor.with_name(ancestor.name + "-moved"))
                    attempts.append(ancestor)
            return original_open(kernel, path, directory=directory,
                                 allow_reparse=allow_reparse)

        with patch.object(windows_files, "_open_handle", side_effect=attempt_swap):
            self.assertEqual(self.read(), self.content)
        self.assertEqual(len(attempts), 5)

    def test_final_file_cannot_be_written_or_replaced_until_snapshot_closes(self):
        original_fdopen = os.fdopen

        def attempt_change(descriptor, mode):
            with self.assertRaises(PermissionError):
                self.preview.write_bytes(b"tampered")
            with self.assertRaises(PermissionError):
                self.preview.rename(self.previews / "moved.jpg")
            return original_fdopen(descriptor, mode)

        with patch.object(windows_files.os, "fdopen", side_effect=attempt_change):
            self.assertEqual(self.read(), self.content)
        # The file handle is released after the single snapshot read.
        self.preview.write_bytes(b"later edit")

    def test_handles_are_closed_after_a_rejected_child(self):
        outside = self.base / "outside previews"
        self.previews.rename(outside)
        self.junction(self.previews, outside)
        with self.assertRaises(StoreError):
            self.read()
        self.previews.rmdir()
        self.root.rename(self.base / "root after failure")


if __name__ == "__main__":
    unittest.main()
