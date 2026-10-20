"""Runtime data-root holder and shared validation error.

Domain stores own their own locking and atomic persistence. This module never
loads or creates legacy editing contexts.
"""
from pathlib import Path
from lr_ops.configuration import runtime_data_root

DATA_ROOT = runtime_data_root() / "workbench"


class StoreError(RuntimeError):
    pass


class RuntimeState:
    def __init__(self, root: Path = DATA_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
