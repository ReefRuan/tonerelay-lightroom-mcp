"""CLI for the lr-ops service.

    python -m lr_ops serve                      # start the bridge service
    python -m lr_ops probe                      # Lightroom/plugin versions
    python -m lr_ops selection                  # current target photo
    python -m lr_ops state <photo_uuid>         # develop settings
    python -m lr_ops baseline <photo_uuid>      # capture baseline
    python -m lr_ops mkcopy <photo_uuid> [name]
    python -m lr_ops apply <photo_uuid> '{"Exposure2012": 0.35}'
    python -m lr_ops reset <photo_uuid>
    python -m lr_ops export <photo_uuid> [long_edge] [out.jpg]
    python -m lr_ops import <local file>        # stage a file and import it

Every command except `serve` submits a job and waits for the plugin to
execute it. Start the service first, then the bridge inside Lightroom
(Library > Plug-in Extras > Start local bridge).

`serve` accepts --host/--port so the bridge can be reached from another
machine; everything else talks to it over loopback by default.

`export` and `import` go through the bridge's staging area rather than naming
a path: the bridge decides where files live on the Lightroom side, so this CLI
never needs to share a disk with Lightroom. Pass `--token` (or set
LR_XMP_MIMIC_LOCAL_TOKEN) when the service enforces one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import server as server_mod
from .catalog_binding import CatalogBinding

BASE = f"http://{server_mod.HOST}:{server_mod.PORT}"
TOKEN = (server_mod._BRIDGE["token"] if server_mod._BRIDGE
         else os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN") or None)


def _headers(extra: dict | None = None) -> dict:
    headers = dict(extra or {})
    if TOKEN:
        headers["X-LR-XMP-Token"] = TOKEN
    return headers


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    for key, value in _headers({"Content-Type": "application/json"}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Cannot reach lr-ops at {BASE} ({exc}). Start it with: python -m lr_ops serve"
        )


def upload_file(path: Path) -> str:
    """Stage a local file on the Lightroom side and return its stage id.

    The bridge decides the real path; we only send bytes and a name.
    """
    data = path.read_bytes()
    query = urllib.parse.urlencode({"name": path.name})
    req = urllib.request.Request(
        f"{BASE}/files?{query}", data=data, method="POST"
    )
    for key, value in _headers({"Content-Type": "application/octet-stream"}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))["stage_id"]
    except urllib.error.URLError as exc:
        raise SystemExit(f"Upload failed ({exc})")


def fetch_output(job_id: str, destination: Path) -> Path:
    """Download the artifact a job produced, whatever the plugin named it."""
    req = urllib.request.Request(f"{BASE}/files/{job_id}/output")
    for key, value in _headers().items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            destination.write_bytes(resp.read())
            return destination
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not fetch job output ({exc})")


def submit_and_wait(command: str, params: dict, timeout: float = server_mod.JOB_TIMEOUT_SEC) -> dict:
    job_id = _request("POST", "/jobs", {"command": command, "params": params})["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = _request("GET", f"/jobs/{job_id}")
        if job["status"] in ("done", "failed"):
            result = job.get("result") or {}
            if not result.get("ok"):
                raise SystemExit(f"Job failed in Lightroom: {result.get('error')}")
            return {**(result.get("data") or {}), "_job_id": job_id}
        time.sleep(0.4)
    raise SystemExit(
        f"Timed out after {timeout:.0f}s. Is the bridge running inside Lightroom?"
    )


def bound_submitter(
    timeout: float = server_mod.JOB_TIMEOUT_SEC,
) -> Callable[[str, dict], dict]:
    """Probe once and return a submitter pinned to that mode and Catalog."""
    probe = submit_and_wait("probe_client", {}, timeout=timeout)
    try:
        binding = CatalogBinding.from_probe(probe)
    except ValueError as exc:
        raise SystemExit(f"Cannot bind the active Lightroom Catalog: {exc}") from exc

    def submit(command: str, params: dict) -> dict:
        return submit_and_wait(command, binding.apply(params), timeout=timeout)

    return submit


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(prog="lr_ops")
    parser.add_argument("command", choices=[
        "serve", "probe", "selection", "state", "baseline",
        "mkcopy", "apply", "reset", "export", "import", "xmp", "mask",
    ])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--host", default=None, help="bind address (serve only)")
    parser.add_argument("--port", type=int, default=None, help="port (serve only)")
    parser.add_argument("--token", default=None, help="shared token for this call")
    args = parser.parse_args(argv)

    if args.token:
        global TOKEN
        TOKEN = args.token

    if args.command == "serve":
        server_mod.run(port=args.port or server_mod.PORT, host=args.host or server_mod.HOST)
        return

    if args.command == "probe":
        out = submit_and_wait("probe_client", {})
    else:
        submit = bound_submitter()
        if args.command == "selection":
            out = submit("get_selection", {})
        elif args.command in ("xmp", "mask"):
            params = json.loads(args.args[1]) if len(args.args) > 1 else {}
            params["photo_uuid"] = args.args[0]
            out = submit("update_xmp_sidecar" if args.command == "xmp" else "apply_mask", params)
        elif args.command == "state":
            out = submit("get_develop_state", {"photo_uuid": args.args[0]})
        elif args.command == "baseline":
            out = submit("capture_baseline", {"photo_uuid": args.args[0]})
        elif args.command == "mkcopy":
            params = {"photo_uuid": args.args[0]}
            if len(args.args) > 1:
                params["name"] = args.args[1]
            out = submit("create_virtual_copy", params)
        elif args.command == "apply":
            settings = json.loads(args.args[1])
            out = submit(
                "apply_candidate", {"photo_uuid": args.args[0], "settings": settings}
            )
        elif args.command == "reset":
            out = submit("reset_to_baseline", {"photo_uuid": args.args[0]})
        elif args.command == "export":
            params = {
                "photo_uuid": args.args[0],
                "long_edge": int(args.args[1]) if len(args.args) > 1 else 2048,
            }
            out = submit("export_preview", params)
            destination = Path(args.args[2]) if len(args.args) > 2 else (
                server_mod.DATA_ROOT / "previews" / "preview.jpg"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            fetch_output(out["_job_id"], destination)
            out = {**out, "saved_to": str(destination)}
        elif args.command == "import":
            stage_id = upload_file(Path(args.args[0]))
            out = submit("import_photo", {"stage_id": stage_id})
        else:  # pragma: no cover
            parser.error("unknown command")

    out.pop("_job_id", None)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
