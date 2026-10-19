"""Workbench state files must be read as UTF-8 on every host.

A Windows host spawns the server without ``PYTHONUTF8``: reads that do not
pin an encoding fall back to the ANSI code page (GBK on a Chinese system).
An ``episode.json`` written by this same Runtime as UTF-8 then fails to
round-trip: CJK text raises UnicodeDecodeError, and the ``©`` byte pair is
silently decoded as a different character, which turned the stored-snapshot
equality check into a permanent false conflict. These tests strip
``PYTHONUTF8``/``PYTHONIOENCODING`` like the desktop spawn environment and
pin both reads through the real stdio server. On a non-UTF-8 host (Windows
with an ANSI code page) both tests fail against the old code; on a host that
is already UTF-8 (for example macOS) they verify the same correct reads
without reproducing the pre-fix failure.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
APPS = REPOSITORY_ROOT / "apps"

EPISODE_ID = "6b9d2f10-1111-4222-8333-444455556666"
PHOTO_UUID = "6b9d2f10-2222-4333-8444-555566667777"

INTENT = "本地读取编码回归测试"
NOTE = "基准原图"
COPYRIGHT = "© 2018 Adobe Systems, Inc."


def _jpeg_bytes(color):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def build_episode_tree(data_root: Path):
    """Lay down one minimal published episode whose state carries CJK and ©."""
    directory = data_root / "workbench" / "episodes" / EPISODE_ID
    directory.mkdir(parents=True)
    render = _jpeg_bytes((180, 40, 40))
    reference = _jpeg_bytes((40, 40, 180))
    (directory / "v0.jpg").write_bytes(render)
    (directory / "reference.jpg").write_bytes(reference)
    state = {
        "id": EPISODE_ID,
        "schema_version": 2,
        "mode": "source_edit",
        "revision": 3,
        "status": "ready",
        "source_uuid": PHOTO_UUID,
        "source_name": "TEST.ARW",
        "intent": INTENT,
        "versions": [{
            "version": 0,
            "photo_uuid": PHOTO_UUID,
            "patch": {},
            "snapshot": {
                "WhiteBalance": "As Shot",
                "Look": {"Name": "Adobe Color", "Amount": 1,
                         "Copyright": COPYRIGHT},
            },
            "readback_mismatches": [],
            "note": NOTE,
            "preview": "v0.jpg",
            "sha256": hashlib.sha256(render).hexdigest(),
        }],
        "current_version": 0,
        "reference_sha256": hashlib.sha256(reference).hexdigest(),
        "messages": [],
        "operations": {},
        "error": None,
    }
    (directory / "episode.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha256(render).hexdigest()


def run_server(requests, data_root: Path):
    """Run the real stdio server once under the app-spawned Windows condition.

    ``PYTHONUTF8`` and ``PYTHONIOENCODING`` are removed, exactly like the
    desktop client's MCP spawn environment; text reads therefore go through
    the ANSI code page unless the code pins UTF-8 explicitly.
    """
    payload = "".join(
        json.dumps(request, ensure_ascii=False) + "\n" for request in requests
    ).encode("utf-8")
    env = os.environ.copy()
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    env["PYTHONPATH"] = os.pathsep.join([
        str(APPS / "workbench-mcp"),
        str(APPS / "workbench"),
        str(REPOSITORY_ROOT / "services" / "lr-ops"),
    ])
    env["LR_XMP_MIMIC_RUNTIME_DATA_ROOT"] = str(data_root)
    proc = subprocess.run(
        [sys.executable, "-m", "workbench_mcp"],
        input=payload, capture_output=True, timeout=120,
        env=env, cwd=str(REPOSITORY_ROOT),
    )
    responses = [
        json.loads(line)
        for line in proc.stdout.decode("utf-8").splitlines()
        if line.strip()
    ]
    return proc.returncode, responses, proc.stderr


class StateReadEncodingTests(unittest.TestCase):
    def call(self, data_root: Path, name, arguments):
        code, responses, stderr = run_server([{
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }], data_root)
        self.assertEqual(code, 0, stderr[-400:])
        self.assertEqual(len(responses), 1)
        result = responses[0]["result"]
        text = result["content"][0]["text"]
        self.assertFalse(result.get("isError"), text)
        return json.loads(text)

    def test_episode_readback_survives_a_non_utf8_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_episode_tree(Path(tmp))
            state = self.call(Path(tmp), "trlr_get_episode",
                              {"episode_id": EPISODE_ID,
                               "include_snapshots": True})
        self.assertEqual(state["intent"], INTENT)
        self.assertEqual(state["versions"][0]["note"], NOTE)
        self.assertEqual(
            state["versions"][0]["snapshot"]["Look"]["Copyright"], COPYRIGHT)

    def test_episode_render_measure_survives_a_non_utf8_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest = build_episode_tree(Path(tmp))
            measurement = self.call(
                Path(tmp), "trlr_image_measure",
                {"source": {"episode_id": EPISODE_ID, "name": "v0.jpg"}})
        self.assertEqual(measurement["source"]["kind"], "episode_artifact")
        self.assertEqual(measurement["source"]["sha256"], digest)


if __name__ == "__main__":
    unittest.main()
