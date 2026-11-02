#!/usr/bin/env python3
"""Prepare a local Windows Codex Plugin; register it with the official Codex CLI.

This only prepares a dedicated, owned marketplace directory. It never edits the
client's installed Plugin cache, config, standalone skill, Runtime or photos.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tonerelay_bootstrap as bootstrap
from tonerelay_codex_skill import hashes, linked

NAME = "tonerelay-lightroom"
MARKETPLACE = "tonerelay-local"
RECEIPT = ".tonerelay-plugin-source.json"


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def inventory(root):
    # Check parents too: a dedicated directory beneath a junction is not owned.
    if any(linked(path) for path in (root, *root.parents)):
        raise ValueError("marketplace path must not contain links")
    return {key: value for key, value in hashes(root).items() if key != RECEIPT}


def prepare(source: Path, destination: Path, runtime_root: Path,
            *, client_config: Path | None = None):
    source, destination, runtime_root = (p.absolute() for p in
                                          (source, destination, runtime_root))
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("source and marketplace must be separate directories")
    if (runtime_root == destination or destination in runtime_root.parents
            or runtime_root in destination.parents):
        raise ValueError("marketplace and Runtime must be separate directories")
    manifest_path = source / ".codex-plugin" / "plugin.json"
    if linked(source) or not manifest_path.is_file():
        raise ValueError("source must be a verified Plugin directory")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("name") != NAME:
        raise ValueError("unexpected Plugin name")
    server = bootstrap.mcp_server_config(runtime_root)[NAME]
    if client_config is not None:
        settings = tomllib.loads(client_config.read_text(encoding="utf-8-sig"))
        server = settings.get("mcp_servers", {}).get(NAME)
        if not isinstance(server, dict) or not server.get("command"):
            raise ValueError("no existing ToneRelay stdio entry to reuse")
        if server.get("url") or server.get("enabled") is False:
            raise ValueError("existing ToneRelay entry must be enabled stdio")
    if not Path(server["command"]).is_file():
        raise ValueError("install the Runtime before preparing its client Plugin")
    files = {}
    for relative in (".codex-plugin", "skills", "scripts"):
        directory = source / relative
        for name in hashes(directory):
            # Only functional client files; no tests, bytecode or source history.
            if "__pycache__" in Path(name).parts or Path(name).suffix == ".pyc":
                continue
            files[f"plugins/{NAME}/{relative}/{name}"] = (directory / name).read_bytes()
    skill_entry = f"plugins/{NAME}/skills/{NAME}/SKILL.md"
    if skill_entry not in files:
        raise ValueError("Plugin source must contain the complete startup skill")
    files[f"plugins/{NAME}/.mcp.json"] = json_bytes({"mcpServers": {NAME: server}})
    files[".agents/plugins/marketplace.json"] = json_bytes({
        "name": MARKETPLACE, "interface": {"displayName": "ToneRelay 本地"},
        "plugins": [{"name": NAME,
                     "source": {"source": "local", "path": f"./plugins/{NAME}"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                     "category": "Productivity"}],
    })
    expected = {key: hashlib.sha256(value).hexdigest() for key, value in files.items()}
    current = inventory(destination)
    receipt_path = destination / RECEIPT
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
    if destination.exists() and receipt.get("files") != current:
        raise ValueError("existing marketplace is unowned or locally modified; preserve it")
    result = {"status": "current", "marketplace_root": str(destination),
              "plugin_id": f"{NAME}@{MARKETPLACE}", "backup": None,
              "client_configuration_changed": False, "client_restart_required": True,
              "official_commands": [["codex", "plugin", "marketplace", "add", str(destination), "--json"],
                                    ["codex", "plugin", "add", f"{NAME}@{MARKETPLACE}", "--json"]]}
    if current == expected:
        return result
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = destination.with_name(destination.name + ".backup-" + stamp)
        shutil.copytree(destination, backup)
        result["backup"] = str(backup)
    with tempfile.TemporaryDirectory(prefix=".tonerelay-plugin-", dir=destination.parent) as temporary:
        staged, previous = Path(temporary) / "new", Path(temporary) / "previous"
        staged.mkdir()
        for name, content in files.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (staged / RECEIPT).write_bytes(json_bytes({"files": expected}))
        if inventory(staged) != expected:
            raise ValueError("prepared Plugin integrity check failed")
        if destination.exists():
            destination.rename(previous)
        try:
            staged.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    result["status"] = "prepared"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--reuse-client-config", type=Path,
                        help="Reuse only the existing ToneRelay stdio entry, including its environment")
    args = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise ValueError("this preparer is for Windows; use the macOS Plugin entry on macOS")
        result = prepare(args.source, args.destination, args.runtime_root,
                         client_config=args.reuse_client_config)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
