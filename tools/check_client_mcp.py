#!/usr/bin/env python3
"""Fail loudly when a client MCP manifest points at a broken ToneRelay install.

A plugin card can look healthy while its MCP server silently never mounts
because the manifest bakes in a stale launcher path (e.g. a temporary
test-run directory). This checker turns that silent failure into an explicit
report: it validates every MCP server entry of a client manifest and exits
non-zero on the first category of problems found.

Supported manifest shapes:

- Kimi plugin manifest (``kimi.plugin.json``): JSON with an ``mcpServers``
  object whose entries have ``command`` / ``args`` / ``env``.
- Codex CLI config (``config.toml``): TOML with ``[mcp_servers.<name>]``
  tables and an ``[mcp_servers.<name>.env]`` sub-table.

Checks per server entry:

1. ``command`` exists on disk (``.exe`` implied on Windows).
2. Every ``PYTHONPATH`` entry exists.
3. ``LR_XMP_MIMIC_RUNTIME_DATA_ROOT`` exists when set.
4. Heuristic warning when a path smells like a temporary test-run layout,
   which is how the silent-failure regression this checker guards against
   was introduced.

Only the Python standard library is used so the check runs before any
Workbench dependencies are installed. Usage::

    python tools/check_client_mcp.py <manifest> [<manifest>...]

Exit codes: 0 = all checks passed, 1 = at least one hard failure,
2 = manifest unreadable or no MCP servers declared.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python < 3.11
    tomllib = None


TEST_RUN_MARKERS = ("test-run", "test-runs")


class Failure(Exception):
    """Raised for manifest-level errors that prevent any checking."""


def load_servers(path: Path) -> dict[str, dict]:
    """Return {server_name: {command, args, env}} from a client manifest."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Failure(f"{path}: unreadable: {exc}") from exc

    if path.suffix.lower() == ".toml":
        if tomllib is None:
            raise Failure(f"{path}: tomllib unavailable on this interpreter")
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Failure(f"{path}: invalid TOML: {exc}") from exc
        servers = data.get("mcp_servers", {})
    else:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Failure(f"{path}: invalid JSON: {exc}") from exc
        servers = data.get("mcpServers", {})

    if not isinstance(servers, dict) or not servers:
        raise Failure(f"{path}: no MCP servers declared")

    normalized: dict[str, dict] = {}
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            raise Failure(f"{path}: server {name!r}: entry must be an object")
        env = entry.get("env") or {}
        if not isinstance(env, dict):
            raise Failure(f"{path}: server {name!r}: env must be an object")
        normalized[str(name)] = {
            "command": entry.get("command"),
            "args": entry.get("args") or [],
            "env": {str(k): str(v) for k, v in env.items()},
        }
    return normalized


def resolve_command(command: str) -> Path:
    """Expand a command the same way a shell would, implying .exe on Windows."""
    expanded = Path(os.path.expandvars(command)).expanduser()
    if os.name == "nt" and expanded.suffix == "":
        exe = expanded.with_suffix(".exe")
        if exe.exists():
            return exe
    return expanded


def split_paths(value: str) -> list[str]:
    return [p for p in value.split(os.pathsep) if p]


def check_manifest(path: Path) -> tuple[list[str], list[str]]:
    """Check one manifest; return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    servers = load_servers(path)

    for name, entry in servers.items():
        label = f"{path.name}:{name}"
        command = entry["command"]
        if not command:
            errors.append(f"{label}: missing 'command'")
        else:
            resolved = resolve_command(command)
            if not resolved.exists():
                errors.append(
                    f"{label}: command not found: {resolved} "
                    f"(manifest entry: {command})"
                )
            for marker in TEST_RUN_MARKERS:
                if marker in str(resolved).replace("/", "\\").lower():
                    warnings.append(
                        f"{label}: command lives under a '{marker}' path; "
                        f"client entries must use the stable runtime root"
                    )
                    break

        env = entry["env"]
        for var in ("LR_XMP_MIMIC_RUNTIME_DATA_ROOT",):
            value = env.get(var)
            if value and not Path(os.path.expandvars(value)).exists():
                errors.append(f"{label}: env {var} does not exist: {value}")
        for var in ("PYTHONPATH",):
            value = env.get(var)
            if value:
                for item in split_paths(os.path.expandvars(value)):
                    if not Path(item).exists():
                        errors.append(
                            f"{label}: env {var} entry does not exist: {item}"
                        )
    return errors, warnings


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    hard_failures = 0
    had_output = False
    for raw_path in argv:
        path = Path(raw_path)
        try:
            errors, warnings = check_manifest(path)
        except Failure as exc:
            print(f"FAIL {exc}")
            had_output = True
            hard_failures += 1
            continue
        had_output = True
        for warning in warnings:
            print(f"WARN {warning}")
        for error in errors:
            print(f"FAIL {error}")
        if not errors and not warnings:
            print(f"OK   {path}: all MCP server entries resolve")
        hard_failures += 1 if errors else 0
    if not had_output:
        return 2
    if hard_failures:
        print(
            f"\n{hard_failures} manifest(s) failed: ToneRelay MCP will not "
            f"mount until the entries above are fixed. Do not report this "
            f"as 'tools not installed' — the manifest is broken.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
