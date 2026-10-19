"""MCP server exposing ToneRelay for Lightroom to AI clients.

Runs over stdio (newline-delimited JSON-RPC 2.0, MCP protocol revision
2024-11-05). It reuses the Workbench pipeline directly; the only network hop
in the editing path is the bundled lr-ops bridge used by the Lightroom plugin.
"""

from pathlib import Path
import sys

# Direct module entrypoints also include the service bundled in the source
# tree, before server.py has established the rest of the Runtime imports.
_service = Path(__file__).resolve().parents[3] / "services" / "lr-ops"
if str(_service) not in sys.path:
    sys.path.insert(0, str(_service))
from lr_ops import __version__
