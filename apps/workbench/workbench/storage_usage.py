"""Bounded read-only accounting, separate caches from durable user artifacts."""
import os
from pathlib import Path
import stat
import time


def storage_usage(root, config, budget_seconds=5):
    if type(budget_seconds) is not int or not 1 <= budget_seconds <= 30:
        raise ValueError("budget_seconds must be 1..30")
    paths = config.paths()
    root = Path(root)
    categories = [("runtime_cache", paths["cache"], "rebuildable_cache"),
                  ("photo_previews", root / "photo-previews", "rebuildable_cache"),
                  ("image_delivery", root / "image-delivery", "conversation_media"),
                  ("episodes", root / "episodes", "durable_history"),
                  ("settings_operations", root / "settings-operations", "durable_history"),
                  ("exports", root / "exports", "saved_exports"),
                  ("folder_queries", root / "folder-queries.sqlite3", "expiring_query_cache"),
                  ("bridge_state", paths["data"] / "lr-ops", "jobs_logs_and_staging")]
    deadline = time.monotonic() + budget_seconds
    result = []
    for name, path, kind in categories:
        size, count, errors, complete = 0, 0, 0, True
        pending = [path]
        while pending:
            if time.monotonic() >= deadline or count >= 1000000:
                complete = False
                break
            item = pending.pop()
            try:
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    with os.scandir(item) as entries:
                        for entry in entries:
                            pending.append(Path(entry.path))
                            if time.monotonic() >= deadline:
                                complete = False
                                break
                    if not complete:
                        break
                elif stat.S_ISREG(info.st_mode):
                    size += info.st_size
                    count += 1
            except FileNotFoundError:
                continue
            except OSError:
                errors += 1
                complete = False
        result.append(dict(category=name, kind=kind, bytes=size if complete else None,
                           observed_bytes=size, files=count, complete=complete, errors=errors))
    return dict(categories=result, complete=all(v["complete"] for v in result),
                budget_seconds=budget_seconds, read_only=True,
                scope="configured Runtime roots only; legacy caches outside these roots are not counted")
