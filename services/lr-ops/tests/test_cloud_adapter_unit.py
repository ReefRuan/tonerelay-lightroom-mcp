"""Run the Lightroom desktop adapter tests when Node is installed."""

import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "bridges" / "lightroom-cloud"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CloudAdapterUnitTests(unittest.TestCase):
    def test_adapter_unit_tests_pass(self):
        if not ADAPTER.is_dir():
            self.skipTest(f"adapter not present at {ADAPTER}")

        completed = subprocess.run(
            ["node", "--test", "test/adapter.test.mjs", "test/shutdown.test.mjs"],
            cwd=ADAPTER,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if completed.returncode != 0:
            self.fail(
                "node --test failed\n"
                f"stdout:\n{completed.stdout[-4000:]}\n"
                f"stderr:\n{completed.stderr[-4000:]}"
            )
