"""Local Plugin preparation must preserve the host and working MCP connection."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "tonerelay_codex_plugin.py"
SPEC = importlib.util.spec_from_file_location("codex_plugin", SCRIPT)
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class CodexPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.destination = self.root / "marketplace"
        self.runtime = self.root / "runtime"
        self.python = self.runtime / "active" / ".venv" / "Scripts" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"interpreter")
        manifest = self.source / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": plugin.NAME, "skills": "./skills/",
                                        "mcpServers": "./.mcp.json"}), encoding="utf-8")
        self.skill = self.source / "skills" / plugin.NAME / "SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("startup 中文", encoding="utf-8")
        scripts = self.source / "scripts"
        scripts.mkdir()
        (scripts / "control.py").write_text("print('control')", encoding="utf-8")
        (scripts / "__pycache__").mkdir()
        (scripts / "__pycache__" / "control.pyc").write_bytes(b"cache")
        (self.source / ".mcp.json").write_text('{"command":"/bin/sh"}', encoding="utf-8")
        (self.source / "tests").mkdir()
        (self.source / "tests" / "private.txt").write_text("excluded", encoding="utf-8")

    def prepare(self, **kwargs):
        with mock.patch.object(plugin.bootstrap, "IS_WINDOWS", True):
            return plugin.prepare(self.source, self.destination, self.runtime, **kwargs)

    def test_windows_plugin_contains_skill_and_launches_stable_active_runtime(self):
        result = self.prepare()
        package = self.destination / "plugins" / plugin.NAME
        server = json.loads((package / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][plugin.NAME]
        self.assertEqual(Path(server["command"]), self.python)
        self.assertEqual(server["args"], ["-m", "workbench_mcp"])
        self.assertEqual((package / "skills" / plugin.NAME / "SKILL.md").read_bytes(), self.skill.read_bytes())
        self.assertFalse((package / "tests").exists())
        self.assertFalse((package / "scripts" / "__pycache__").exists())
        catalog = json.loads((self.destination / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual((self.destination / catalog["plugins"][0]["source"]["path"]).resolve(), package.resolve())
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(self.prepare()["status"], "current")

    def test_reuses_only_tonerelay_and_does_not_modify_client_configuration(self):
        config = self.root / "config.toml"
        config.write_text('[mcp_servers.other]\ncommand="other"\n'
                          '[mcp_servers.tonerelay-lightroom]\ncommand=' + json.dumps(str(self.python)) +
                          '\nargs=["-m", "workbench_mcp"]\n'
                          '[mcp_servers.tonerelay-lightroom.env]\nTEMP="custom tmp"\n', encoding="utf-8")
        original = config.read_bytes()
        self.prepare(client_config=config)
        entry = json.loads((self.destination / "plugins" / plugin.NAME / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(list(entry["mcpServers"]), [plugin.NAME])
        self.assertEqual(entry["mcpServers"][plugin.NAME]["env"], {"TEMP": "custom tmp"})
        self.assertEqual(config.read_bytes(), original)

    def test_update_preserves_previous_package_and_refuses_local_edits(self):
        self.prepare()
        previous = plugin.inventory(self.destination)
        self.skill.write_text("updated", encoding="utf-8")
        report = self.prepare()
        self.assertEqual(plugin.inventory(Path(report["backup"])), previous)
        target = self.destination / "plugins" / plugin.NAME / "skills" / plugin.NAME / "SKILL.md"
        target.write_text("local edits", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "locally modified"):
            self.prepare()
        self.assertEqual(target.read_text(encoding="utf-8"), "local edits")

    def test_unknown_directory_missing_runtime_and_links_are_not_overwritten(self):
        self.python.unlink()
        with self.assertRaisesRegex(ValueError, "Runtime"):
            self.prepare()
        self.python.write_bytes(b"interpreter")
        self.destination.mkdir()
        (self.destination / "user.txt").write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unowned"):
            self.prepare()
        self.assertTrue((self.destination / "user.txt").is_file())
        with mock.patch.object(plugin, "linked", side_effect=lambda p: p == self.destination.parent):
            with self.assertRaisesRegex(ValueError, "links"):
                self.prepare()

    def test_failed_swap_restores_previous_package(self):
        self.prepare()
        before = plugin.inventory(self.destination)
        self.skill.write_text("new version", encoding="utf-8")
        rename = Path.rename

        def fail_new(path, target):
            if path.name == "new":
                raise OSError("simulated replacement failure")
            return rename(path, target)

        with mock.patch.object(Path, "rename", fail_new), self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(plugin.inventory(self.destination), before)


if __name__ == "__main__":
    unittest.main()
