"""Client-only updates preserve working MCP configuration and local edits."""
import importlib.util
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "tonerelay_codex_skill.py"
SPEC = importlib.util.spec_from_file_location("codex_skill", SCRIPT)
skill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(skill)


class CodexSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.dest = self.root / "source", self.root / "skills" / "installed"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("startup 中文", encoding="utf-8")
        (self.source / "references").mkdir()
        (self.source / "references" / "installation.md").write_text("install", encoding="utf-8")
        self.backups = self.root / "backups"

    def setup_skill(self, install=True):
        return skill.setup(self.source, self.dest, self.backups, install=install)

    def test_status_does_not_write_and_install_is_idempotent(self):
        config = self.root / "config.toml"
        config.write_text('command = "working launcher"', encoding="utf-8")
        before = config.read_bytes()
        self.assertEqual(self.setup_skill(False)["status"], "update_available")
        self.assertFalse(self.dest.parent.exists())
        self.assertEqual(self.setup_skill()["status"], "installed")
        self.assertEqual(skill.hashes(self.dest), skill.hashes(self.source))
        receipt = (self.dest / skill.RECEIPT).stat().st_mtime_ns
        self.assertEqual(self.setup_skill()["status"], "current")
        self.assertEqual((self.dest / skill.RECEIPT).stat().st_mtime_ns, receipt)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(self.backups.exists())

    def test_update_backs_up_complete_previous_version_and_removes_old_files(self):
        self.setup_skill()
        previous = skill.hashes(self.dest)
        (self.source / "references" / "installation.md").unlink()
        (self.source / "SKILL.md").write_text("new", encoding="utf-8")
        result = self.setup_skill()
        self.assertEqual(skill.hashes(Path(result["backup"])), previous)
        self.assertEqual(skill.hashes(self.dest), skill.hashes(self.source))

    def test_local_or_unmanaged_edits_are_preserved(self):
        self.setup_skill()
        (self.dest / "SKILL.md").write_text("local", encoding="utf-8")
        for receipt in (True, False):
            if not receipt:
                (self.dest / skill.RECEIPT).unlink()
            with self.assertRaisesRegex(ValueError, "local changes"):
                self.setup_skill()
            self.assertEqual((self.dest / "SKILL.md").read_text(encoding="utf-8"), "local")

    def test_failed_replacement_restores_previous_install(self):
        self.setup_skill()
        previous = skill.hashes(self.dest)
        (self.source / "SKILL.md").write_text("new", encoding="utf-8")
        rename = Path.rename

        def fail_new(path, target):
            if path.name == "new":
                raise OSError("simulated replacement failure")
            return rename(path, target)

        with mock.patch.object(Path, "rename", fail_new), self.assertRaises(OSError):
            self.setup_skill()
        self.assertEqual(skill.hashes(self.dest), previous)

    def test_overlapping_directories_are_rejected(self):
        with self.assertRaises(ValueError):
            skill.setup(self.source, self.source / "nested", self.backups, install=True)

    def test_enabled_plugin_blocks_a_duplicate_standalone_install(self):
        config = self.root / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('[plugins."tonerelay-lightroom@personal"]\nenabled = true\n', encoding="utf-8")
        before = config.read_bytes()
        with mock.patch.object(Path, "home", return_value=self.root), contextlib.redirect_stdout(io.StringIO()):
            code = skill.main(["install", "--source", str(self.source),
                               "--destination", str(self.dest)])
        self.assertEqual(code, 1)
        self.assertFalse(self.dest.exists())
        self.assertEqual(config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
