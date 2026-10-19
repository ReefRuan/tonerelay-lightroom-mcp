"""Keep lr-ops tests off the real runtime data root.

server.py resolves DATA_ROOT/LOG_DIR/STAGING_DIR at import time, so the
environment must be set before the first import of lr_ops.server.
"""

import os
import tempfile

os.environ.setdefault(
    "LR_XMP_MIMIC_RUNTIME_DATA_ROOT",
    tempfile.mkdtemp(prefix="lr-ops-test-"),
)
