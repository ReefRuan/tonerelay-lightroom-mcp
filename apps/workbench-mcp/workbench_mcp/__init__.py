"""MCP server exposing ToneRelay for Lightroom to AI clients.

Runs over stdio (newline-delimited JSON-RPC 2.0, MCP protocol revision
2024-11-05). It reuses the Workbench pipeline directly; the only network hop
in the editing path is the separate lr-ops bridge used by the Lightroom plugin.
"""

__version__ = "0.6.0"
