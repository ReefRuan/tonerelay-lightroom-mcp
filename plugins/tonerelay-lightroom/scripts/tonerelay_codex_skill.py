#!/usr/bin/env python3
"""Install/update only the Codex startup skill; leave MCP and Runtime alone."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile

NAME = "tonerelay-lightroom"
RECEIPT = ".tonerelay-install.json"


def linked(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def hashes(root: Path) -> dict[str, str]:
    if linked(root):
        raise ValueError("skill directory must not be a link")
    result = {}
    for path in sorted(root.rglob("*")):
        if linked(path):
            raise ValueError("skill contents must not contain links")
        if path.is_file() and path.relative_to(root).as_posix() != RECEIPT:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def setup(source: Path, destination: Path, backup_root: Path, *, install=False) -> dict:
    source = source.absolute()
    destination = destination.absolute()
    if not (source / "SKILL.md").is_file():
        raise ValueError("source has no SKILL.md")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("source and destination must be separate directories")
    expected = hashes(source)
    present = hashes(destination)
    receipt = destination / RECEIPT
    old = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else None
    if destination.exists() and present != expected and (
            old is None or old.get("files") != present):
        raise ValueError("existing skill has untracked/local changes; preserve it before updating")
    report = {"status": "current" if destination.is_dir() and present == expected else "update_available",
              "destination": str(destination), "files": len(expected), "backup": None,
              "mcp_configuration_changed": False}
    if not install or report["status"] == "current":
        return report
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_root.absolute() / stamp
        if destination == backup or destination in backup.parents:
            raise ValueError("backup must be outside the installed skill")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(destination, backup)
        report["backup"] = str(backup)
    # Both renames stay beside destination; restore the previous tree if the
    # replacement fails. The backup above remains available independently.
    with tempfile.TemporaryDirectory(prefix=".tonerelay-install-", dir=destination.parent) as temporary:
        work = Path(temporary)
        staged, previous = work / "new", work / "previous"
        shutil.copytree(source, staged)
        if hashes(staged) != expected:
            raise ValueError("source changed during copy")
        (staged / RECEIPT).write_text(json.dumps({"files": expected}, indent=2) + "\n", encoding="utf-8")
        if destination.exists():
            destination.rename(previous)
        try:
            staged.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    report["status"] = "installed"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "install"))
    parser.add_argument("--source", type=Path,
                        default=Path(__file__).resolve().parents[1] / "skills" / NAME)
    parser.add_argument("--destination", type=Path,
                        default=Path.home() / ".agents" / "skills" / NAME)
    args = parser.parse_args(argv)
    try:
        # A Plugin-managed copy must use the official update flow, rather than
        # installing a duplicate standalone skill with the same name.
        import tomllib
        config = Path.home() / ".codex" / "config.toml"
        settings = tomllib.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
        if any(key.split("@", 1)[0] == NAME and value.get("enabled", True)
               for key, value in settings.get("plugins", {}).items()):
            raise ValueError("ToneRelay Plugin is enabled; update it through the official Plugins interface")
        result = setup(args.source, args.destination,
                       Path.home() / ".codex" / "backups" / NAME,
                       install=args.action == "install")
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
