"""Compile the Runtime operator prompt from the repository manifest.

The prompt is an executable project artifact: its ordered source files live in
``prompts/manifest.json`` and MCP initialization consumes that exact build.
Missing, duplicated, escaping, or incomplete sections fail at startup instead
of silently falling back to a stale embedded prompt.
"""

from __future__ import annotations

import json
from pathlib import Path


class PromptContractError(RuntimeError):
    pass


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "prompts" / "manifest.json"


def load_runtime_operator_prompt(manifest_path: Path = DEFAULT_MANIFEST) -> str:
    manifest_path = Path(manifest_path).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PromptContractError(f"cannot read prompt manifest: {exc}") from exc

    if manifest.get("schema_version") != "runtime-prompt-manifest-v1":
        raise PromptContractError("unsupported Runtime prompt manifest")
    if manifest.get("prompt_id") != "runtime-operator":
        raise PromptContractError("unexpected Runtime prompt id")
    if manifest.get("audience") != "editing-agent":
        raise PromptContractError("unexpected Runtime prompt audience")
    if manifest.get("entrypoint") != "mcp.initialize.instructions":
        raise PromptContractError("unexpected Runtime prompt entrypoint")

    root = manifest_path.parents[1].resolve()
    source_root = (root / "prompts" / "runtime-operator").resolve()
    sections = manifest.get("sections")
    if not isinstance(sections, list) or not sections:
        raise PromptContractError("prompt manifest needs ordered sections")

    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    chunks: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            raise PromptContractError("prompt section must be an object")
        section_id = section.get("id")
        relative = section.get("path")
        if not isinstance(section_id, str) or not section_id or section_id in seen_ids:
            raise PromptContractError(f"invalid or duplicate prompt section id: {section_id!r}")
        if not isinstance(relative, str) or not relative:
            raise PromptContractError(f"prompt section {section_id} has no path")
        seen_ids.add(section_id)
        path = (root / relative).resolve()
        if source_root not in path.parents or path.suffix.lower() != ".md":
            raise PromptContractError(
                f"prompt section must be Markdown under prompts/runtime-operator: {relative}")
        if path in seen_paths:
            raise PromptContractError(f"duplicate prompt section path: {relative}")
        seen_paths.add(path)
        try:
            body = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PromptContractError(f"cannot read prompt section {section_id}: {exc}") from exc
        if not body:
            raise PromptContractError(f"prompt section {section_id} is empty")
        chunks.append(body)

    discovered = {path.resolve() for path in source_root.rglob("*.md")}
    orphans = sorted(path.relative_to(root).as_posix() for path in discovered - seen_paths)
    if orphans:
        raise PromptContractError(f"unlisted Runtime prompt sections: {orphans}")

    prompt = "\n\n".join(chunks)
    required = manifest.get("required_fragments")
    if (not isinstance(required, list) or not required
            or any(not isinstance(item, str) or not item for item in required)
            or len(required) != len(set(required))):
        raise PromptContractError("required_fragments must be unique non-empty strings")
    missing = [item for item in required if item not in prompt]
    if missing:
        raise PromptContractError(f"prompt is missing required fragments: {missing}")
    return prompt
