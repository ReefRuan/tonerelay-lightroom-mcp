"""Import and composition boundaries between MCP and the Runtime core."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

import workbench_mcp.server as mcp_server
from workbench.runtime import build_workbench as runtime_build_workbench


REPO_ROOT = Path(__file__).resolve().parents[3]


class AdapterBoundaryTests(unittest.TestCase):
    def test_mcp_uses_the_single_runtime_factory(self):
        self.assertIs(mcp_server.build_workbench, runtime_build_workbench)

    def test_mcp_package_does_not_import_an_http_adapter(self):
        package = Path(mcp_server.__file__).parent
        forbidden = {"workbench.http", "workbench.app"}
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
            with self.subTest(path=path.name):
                self.assertFalse(
                    any(
                        name in forbidden
                        or any(name.startswith(item + ".") for item in forbidden)
                        for name in imported
                    ),
                    imported,
                )

    def test_clean_mcp_import_does_not_load_http(self):
        env = dict(os.environ)
        roots = [
            REPO_ROOT / "apps" / "workbench-mcp",
            REPO_ROOT / "apps" / "workbench",
            REPO_ROOT / "services" / "lr-ops",
        ]
        if env.get("PYTHONPATH"):
            roots.append(Path(env["PYTHONPATH"]))
        env["PYTHONPATH"] = os.pathsep.join(map(str, roots))
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import workbench_mcp.server; "
                "assert 'workbench.http' not in sys.modules; "
                "assert 'workbench.app' not in sys.modules",
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
