"""Append-only issue log for tool errors and boundary events.

Tool errors and explicit manual observations are appended as JSON lines.
Existing historical records remain readable; their context is never migrated
or used to execute work.

Storage: <runtime-data root>/issues.jsonl (alongside Episode state, never in
Git). One JSON object per line, newest entries appended at the end.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

# Known kinds (free-form strings are allowed; these are the ones the
# toolchain records automatically):
#   tool_error     an MCP tool call raised (tool, args summary, error type)
#   manual         reported by a human or agent via trlr_report_issue


class IssueStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, kind: str, summary: str, *, tool: str | None = None,
               episode_id: str | None = None, detail: dict | None = None) -> dict:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "ts": time.time(),
            "kind": str(kind),
            "summary": str(summary)[:500],
        }
        if tool:
            entry["tool"] = str(tool)
        if episode_id:
            entry["episode_id"] = str(episode_id)
        if detail is not None:
            entry["detail"] = detail
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return entry

    def list(self, kind: str | None = None, episode_id: str | None = None,
             limit: int = 50) -> list:
        if not self.path.exists():
            return []
        entries = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # 跳过损坏行，不让一条坏记录毁掉整个查询
                if kind and entry.get("kind") != kind:
                    continue
                if episode_id and entry.get("episode_id") != episode_id:
                    continue
                entries.append(entry)
        limit = max(1, min(int(limit), 500))
        return entries[-limit:][::-1]  # newest first
