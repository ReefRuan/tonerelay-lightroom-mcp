"""Run the Lightroom cloud adapter's unit tests through pytest.

The adapter is JavaScript (it talks to Adobe's External Controller WebSocket with
no dependencies), but this repository has exactly one test entry point: pytest.
This wrapper keeps that true — `pytest services/lr-ops/tests` covers the cloud
executor too, and skips cleanly on a machine without Node.

Not wired into this checkout yet: drop it in at
`services/lr-ops/tests/test_cloud_adapter_unit.py` together with
`bridges/lightroom-cloud/`.
"""

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
            ["node", "--test", "test/"],
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
