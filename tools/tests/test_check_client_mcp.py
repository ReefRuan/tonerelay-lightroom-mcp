from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "check_client_mcp.py"
SPEC = importlib.util.spec_from_file_location("check_client_mcp", SCRIPT)
check_client_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_client_mcp)


class CheckClientMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "ToneRelay" / "runtime"
        python = self.runtime / "active" / ".venv" / "Scripts"
        python.mkdir(parents=True)
        (python / "python.exe").write_text("", encoding="utf-8")
        source = self.runtime / "active" / "source" / "apps"
        source.mkdir(parents=True)
        self.env = {
            "LR_XMP_MIMIC_RUNTIME_DATA_ROOT": str(self.runtime / "data"),
            "PYTHONPATH": str(source),
        }
        (self.runtime / "data").mkdir()

    def kimi_manifest(self, command: str, env: dict[str, str]) -> Path:
        path = self.root / "kimi.plugin.json"
        path.write_text(json.dumps({
            "mcpServers": {"tonerelay-lightroom": {
                "command": command,
                "args": ["-m", "workbench_mcp"],
                "env": env,
            }},
        }), encoding="utf-8")
        return path

    def test_passing_kimi_manifest_reports_ok(self):
        manifest = self.kimi_manifest(
            str(self.runtime / "active" / ".venv" / "Scripts" / "python.exe"),
            self.env,
        )
        errors, warnings = check_client_mcp.check_manifest(manifest)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_missing_command_is_a_hard_failure(self):
        manifest = self.kimi_manifest(
            str(self.root / "gone" / "python.exe"), self.env,
        )
        errors, _ = check_client_mcp.check_manifest(manifest)
        self.assertTrue(any("command not found" in e for e in errors))

    def test_stale_test_run_path_warns(self):
        stale = self.root / "ToneRelay-test-runs" / "run" / "python.exe"
        stale.parent.mkdir(parents=True)
        stale.write_text("", encoding="utf-8")
        manifest = self.kimi_manifest(str(stale), self.env)
        _, warnings = check_client_mcp.check_manifest(manifest)
        self.assertTrue(any("stable runtime root" in w for w in warnings))

    def test_missing_env_path_is_a_hard_failure(self):
        env = dict(self.env)
        env["PYTHONPATH"] = str(self.root / "no-such-source")
        manifest = self.kimi_manifest(
            str(self.runtime / "active" / ".venv" / "Scripts" / "python.exe"),
            env,
        )
        errors, _ = check_client_mcp.check_manifest(manifest)
        self.assertTrue(any("PYTHONPATH" in e for e in errors))

    def test_codex_toml_manifest_is_supported(self):
        toml = self.root / "config.toml"
        toml.write_text(
            '[mcp_servers.tonerelay-lightroom]\n'
            f'command = "{self.runtime.as_posix()}/active/.venv/Scripts/python.exe"\n'
            'args = ["-m", "workbench_mcp"]\n'
            '\n'
            '[mcp_servers.tonerelay-lightroom.env]\n'
            f'LR_XMP_MIMIC_RUNTIME_DATA_ROOT = "{self.runtime.as_posix()}/data"\n'
            f'PYTHONPATH = "{self.runtime.as_posix()}/active/source/apps"\n',
            encoding="utf-8",
        )
        errors, warnings = check_client_mcp.check_manifest(toml)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_manifest_without_servers_is_a_failure(self):
        manifest = self.root / "empty.json"
        manifest.write_text('{"mcpServers": {}}', encoding="utf-8")
        with self.assertRaises(check_client_mcp.Failure):
            check_client_mcp.check_manifest(manifest)

    def test_main_exit_codes(self):
        good = self.kimi_manifest(
            str(self.runtime / "active" / ".venv" / "Scripts" / "python.exe"),
            self.env,
        )
        self.assertEqual(check_client_mcp.main([str(good)]), 0)
        bad = self.kimi_manifest(str(self.root / "gone.exe"), self.env)
        self.assertEqual(check_client_mcp.main([str(bad)]), 1)
        self.assertEqual(check_client_mcp.main([]), 2)


if __name__ == "__main__":
    unittest.main()
