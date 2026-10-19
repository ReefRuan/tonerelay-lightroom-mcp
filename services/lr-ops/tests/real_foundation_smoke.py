"""Raw HTTP F1 diagnostics against a running Lightroom bridge.

Use one mode at a time so a failure identifies a layer:
  --mode probe   read plugin/catalog identity and list photos
  --mode copy    reproduce virtual-copy creation once (or --count N)
  --mode export  export 200/600/2048 and inspect actual JPEG dimensions

After one unbound probe, every other job carries the returned access mode and
opaque Catalog identity. ``--output`` keeps complete JSON evidence, including
job IDs and any failure, and export JPEGs beside it.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

from PIL import Image

from lr_ops.catalog_binding import CatalogBinding

BASE = "http://127.0.0.1:17777"


def request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    token = os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN")
    if token:
        req.add_header("X-LR-XMP-Token", token)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def submit_job(command: str, payload: dict, evidence: list[dict], timeout: float = 45) -> dict:
    submitted = request("POST", "/jobs", {"command": command, "params": payload})
    job_id = submitted["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = request("GET", f"/jobs/{job_id}")
        if state.get("status") in ("done", "failed"):
            result = state.get("result") or {}
            evidence.append({"job_id": job_id, "command": command, "params": payload,
                             "status": state.get("status"), "result": result})
            if not result.get("ok"):
                raise RuntimeError(f"{command} failed (job {job_id}): {result.get('error')}")
            data = dict(result.get("data") or {})
            data["_job_id"] = job_id
            return data
        time.sleep(0.25)
    evidence.append({"job_id": job_id, "command": command, "params": payload, "status": "timeout"})
    raise TimeoutError(f"timed out waiting for {command} (job {job_id})")


class Jobs:
    """One probe followed by commands pinned to that exact Runtime contract."""

    def __init__(self, evidence: list[dict]):
        self.evidence = evidence
        self.binding: CatalogBinding | None = None

    def probe(self) -> dict:
        probe = submit_job("probe_client", {}, self.evidence)
        self.binding = CatalogBinding.from_probe(probe)
        return probe

    def run(self, command: str, params: dict) -> dict:
        if self.binding is None:
            raise RuntimeError("probe_client must complete before Catalog commands")
        return submit_job(command, self.binding.apply(params), self.evidence)


def fetch_output(output_ref: str, destination: Path) -> Path:
    req = urllib.request.Request(BASE + f"/files/{output_ref}/output")
    token = os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN")
    if token:
        req.add_header("X-LR-XMP-Token", token)
    with urllib.request.urlopen(req, timeout=30) as response:
        destination.write_bytes(response.read())
    return destination


def choose_photo(photos: list[dict], uuid: str | None) -> dict:
    if uuid:
        matches = [p for p in photos if p.get("uuid") == uuid]
        if not matches:
            raise RuntimeError(f"photo UUID not found: {uuid}")
        return matches[0]
    masters = [p for p in photos if not p.get("is_virtual_copy") and p.get("file_name", "").lower().endswith((".jpg", ".jpeg"))]
    if not masters:
        raise RuntimeError("active Catalog has no master JPEG candidate; pass --photo-uuid")
    return masters[0]


def resolve_photo(uuid: str | None, photos: list[dict], jobs: Jobs) -> dict:
    if uuid:
        # Resolve a requested target directly so evidence does not need to embed
        # the entire catalog listing.
        return jobs.run("get_develop_state", {"photo_uuid": uuid})["photo"]
    return choose_photo(photos, None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("probe", "copy", "export"), required=True)
    parser.add_argument("--photo-uuid")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("f1-real-evidence.json"))
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    evidence: list[dict] = []
    jobs = Jobs(evidence)
    report: dict = {"mode": args.mode, "evidence": evidence}
    try:
        report["probe"] = jobs.probe()
        report["catalog"] = {
            "access_mode": report["probe"]["access_mode"],
            "catalog_identity": report["probe"]["catalog_identity"],
            "catalog_id": report["probe"].get("catalog_id"),
        }
        if args.mode == "probe":
            report["photos"] = jobs.run("list_photos", {"limit": 500}).get("photos", [])
        else:
            photos = []
            if not args.photo_uuid:
                photos = jobs.run("list_photos", {"limit": 500}).get("photos", [])
            source = resolve_photo(args.photo_uuid, photos, jobs)
            report["source"] = source
            if args.mode == "copy":
                copy_failure: Exception | None = None
                try:
                    report["source_snapshot_before"] = jobs.run(
                        "get_develop_snapshot", {"photo_uuid": source["uuid"]}
                    )
                    report["copies"] = []
                    for index in range(args.count):
                        created = jobs.run("create_virtual_copy", {
                            "photo_uuid": source["uuid"],
                            "name": f"F1 HTTP {index + 1}",
                        })
                        copy = created.get("copy") or {}
                        if not copy.get("uuid") or not copy.get("is_virtual_copy"):
                            raise AssertionError(f"create_virtual_copy returned invalid copy: {copy}")
                        report["copies"].append(created)
                    uuids = [item["copy"]["uuid"] for item in report["copies"]]
                    if len(set(uuids)) != len(uuids):
                        raise AssertionError(f"virtual-copy UUIDs are not unique: {uuids}")
                except Exception as exc:
                    copy_failure = exc
                # The after snapshot is cleanup evidence: take it even when the
                # copy run failed, and surface both failures if it fails too.
                try:
                    report["source_snapshot_after"] = jobs.run(
                        "get_develop_snapshot", {"photo_uuid": source["uuid"]}
                    )
                except Exception as exc:
                    report["snapshot_after_error"] = repr(exc)
                    if copy_failure is None:
                        copy_failure = exc
                    else:
                        copy_failure = RuntimeError(
                            f"original failure: {copy_failure!r}; cleanup snapshot also failed: {exc!r}"
                        )
                if copy_failure is not None:
                    raise copy_failure
                before_settings = report["source_snapshot_before"].get("settings")
                after_settings = report["source_snapshot_after"].get("settings")
                if before_settings != after_settings:
                    raise AssertionError("source develop snapshot changed during virtual-copy creation")
            else:
                dimensions = (source.get("width", 0), source.get("height", 0))
                if max(dimensions) < 2048:
                    raise RuntimeError(f"source is too small for exact 200/600/2048 assertions: {dimensions}")
                output_dir = args.output.parent / (args.output.stem + "-artifacts")
                output_dir.mkdir(parents=True, exist_ok=True)
                report["exports"] = []
                for requested in (200, 600, 2048):
                    exported = jobs.run("export_preview", {
                        "photo_uuid": source["uuid"], "long_edge": requested,
                    })
                    path = fetch_output(exported["output_ref"], output_dir / f"{requested}.jpg")
                    with Image.open(path) as image:
                        actual = image.size
                    export_record = {"requested": requested, "actual": actual, "path": str(path), "job_id": exported["_job_id"]}
                    report["exports"].append(export_record)
                    if max(actual) != requested:
                        raise AssertionError(f"requested {requested}, actual {actual}")
        report["ok"] = True
    except Exception as exc:
        report["ok"] = False
        report["error"] = repr(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
