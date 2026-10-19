"""Session and round persistence.

A session binds one source photo, one reference photo and a mode. Each
round records the submitted settings, the LR-rendered preview file and
timestamps. Everything lives under the runtime-data root, never in Git.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

DATA_ROOT = Path(
    os.environ.get(
        "LR_XMP_MIMIC_RUNTIME_DATA_ROOT",
        Path.home() / "lr-xmp-mimic-runtime-data",
    )
) / "workbench"

MODES = ("color_match", "style_match")


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, root: Path = DATA_ROOT):
        self.root = root
        (self.root / "sessions").mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        try:
            canonical = str(uuid.UUID(session_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise StoreError("invalid session id") from exc
        if canonical != session_id:
            raise StoreError("invalid session id")
        return self.root / "sessions" / session_id

    def _meta_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "session.json"

    def create_session(self, source_uuid: str, reference_uuid: str, mode: str) -> dict:
        if mode not in MODES:
            raise StoreError(f"mode must be one of {MODES}")
        session_id = str(uuid.uuid4())
        session = {
            "session_id": session_id,
            "source_uuid": source_uuid,
            "reference_uuid": reference_uuid,
            "mode": mode,
            "working_copy_uuid": None,
            "created_at": time.time(),
            "rounds": [],
        }
        session_dir = self._session_dir(session_id)
        (session_dir / "previews").mkdir(parents=True, exist_ok=True)
        self._write(session)
        return session

    def get_session(self, session_id: str) -> dict:
        path = self._meta_path(session_id)
        if not path.exists():
            raise StoreError(f"unknown session {session_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_sessions(self) -> list[dict]:
        out = []
        sessions_dir = self.root / "sessions"
        for path in sorted(sessions_dir.glob("*/session.json")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                out.append({
                    "session_id": meta["session_id"],
                    "mode": meta["mode"],
                    "created_at": meta["created_at"],
                    "rounds": len(meta["rounds"]),
                })
            except (ValueError, KeyError):
                continue
        return out

    def set_working_copy(self, session_id: str, copy_uuid: str) -> None:
        session = self.get_session(session_id)
        session["working_copy_uuid"] = copy_uuid
        self._write(session)

    def set_slot_preview(self, session_id: str, slot: str, preview_path: str,
                         sha256: str) -> dict:
        """Persist the exact rendered bytes behind a session preview slot."""
        if slot not in ("source", "reference"):
            raise StoreError("slot must be source or reference")
        session = self.get_session(session_id)
        previews = session.setdefault("slot_previews", {})
        previews[slot] = {"preview_path": preview_path, "sha256": sha256}
        self._write(session)
        return previews[slot]

    def add_round(self, session_id: str, settings: dict, preview_path: str,
                  before: dict | None = None, after: dict | None = None,
                  preview_sha256: str | None = None,
                  after_pending: bool = False) -> dict:
        session = self.get_session(session_id)
        round_record = {
            "round": len(session["rounds"]) + 1,
            "settings": settings,
            "before": before,
            "after": after,
            "preview_path": preview_path,
            "created_at": time.time(),
        }
        if preview_sha256 is not None:
            round_record["preview_sha256"] = preview_sha256
        if after_pending:
            round_record["after_pending"] = True
        session["rounds"].append(round_record)
        self._write(session)
        return round_record

    def preview_file(self, session_id: str, round_no: int) -> Path:
        session = self.get_session(session_id)
        for record in session["rounds"]:
            if record["round"] == round_no:
                path = Path(record["preview_path"])
                if not path.exists():
                    raise StoreError(f"preview file missing: {path}")
                return path
        raise StoreError(f"unknown round {round_no}")

    def preview_dir(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "previews"

    def _write(self, session: dict) -> None:
        path = self._meta_path(session["session_id"])
        path.write_text(
            json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8"
        )
