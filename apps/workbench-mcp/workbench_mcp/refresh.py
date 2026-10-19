"""Read-only MCP generation identity; no Bridge or Catalog operations."""
import hashlib
import os
from pathlib import Path


def source_root():
    return Path(__file__).resolve().parents[3]


def installation_root(loaded_root):
    from lr_ops.configuration import installed_runtime_root
    return installed_runtime_root(loaded_root)


def active_source(loaded_root, install_root):
    # No caller-supplied paths, commands, interpreters or source URLs.
    return loaded_root if install_root is None else (install_root / "active").resolve() / "source"


def fingerprint(root):
    digest = hashlib.sha256()
    for relative in ("apps/workbench-mcp", "apps/workbench", "services/lr-ops", "prompts"):
        folder = root / relative
        if not folder.is_dir():
            raise OSError("mcp.refresh_source_missing")
        for path in sorted(p for p in folder.rglob("*") if p.suffix in (".py", ".md", ".json")
                           and p.is_file() and "__pycache__" not in p.parts):
            digest.update(path.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


LOADED_ROOT = source_root()
INSTALL_ROOT = installation_root(LOADED_ROOT)
LOADED_SHA256 = fingerprint(LOADED_ROOT)


def inspect_generation(server_info, instructions, bridge=None):
    try:
        active = fingerprint(active_source(LOADED_ROOT, INSTALL_ROOT))
        source_status = "available"
    except OSError:
        active, source_status = None, "unavailable"
    return {
        "status": "inspected", "supervised": os.environ.get("TONERELAY_MCP_WORKER") == "1",
        "server_info": server_info, "loaded_source_sha256": LOADED_SHA256,
        "active_source_sha256": active, "active_source_status": source_status,
        "refresh_available": active is not None and active != LOADED_SHA256,
        "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        "bridge": dict(bridge or {}),
    }
