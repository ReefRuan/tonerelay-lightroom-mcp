"""Stable client-owned stdio endpoint, with serial replaceable MCP workers.

Explicit refresh only, between requests. Never replay a failed operation and
never stop the shared Bridge. A failed candidate leaves the old worker intact.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid
from .refresh import source_root, installation_root, active_source


class WorkerError(OSError):
    pass


def tool_result(data, error=False):
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": error}


def data_from(result):
    if result.get("isError"):
        raise WorkerError("mcp.refresh_candidate_unready")
    try:
        value = json.loads(result["content"][0]["text"])
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise WorkerError("mcp.refresh_candidate_invalid") from exc


class Worker:
    def __init__(self, root, install_root=None):
        python = sys.executable
        if install_root is not None:
            python = str(root.parent / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            if not Path(python).is_file():
                raise WorkerError("mcp.refresh_interpreter_missing")
        for relative in ("apps/workbench-mcp/workbench_mcp/worker.py", "services/lr-ops/lr_ops/configuration.py"):
            if not (root / relative).is_file():
                raise WorkerError("mcp.refresh_source_missing")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(str(root / p) for p in (
            "apps/workbench-mcp", "apps/workbench", "services/lr-ops"))
        env.update(TONERELAY_MCP_WORKER="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        generation = uuid.uuid4().hex
        env["TONERELAY_MCP_GENERATION"] = generation
        cache_base = Path(env.get("PYTHONPYCACHEPREFIX") or
                          (Path(env.get("LR_XMP_MIMIC_RUNTIME_DATA_ROOT", str(Path.home() / ".cache/tonerelay"))) / "cache/python"))
        env["PYTHONPYCACHEPREFIX"] = str(cache_base / "mcp" / generation)
        command = [python, "-m", "workbench_mcp.worker"]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=None, text=True, encoding="utf-8", env=env,
                                        cwd=Path.cwd(), **options)
        self.messages = queue.Queue()
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            self.messages.put(None)

    def exchange(self, message, timeout=None):
        if self.closed or self.process.poll() is not None:
            raise WorkerError("mcp.worker_unavailable: request was not submitted")
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise WorkerError("mcp.worker_connection_lost: request result is unknown; do not replay writes") from exc
        if "id" not in message:
            return None
        try:
            reply = self.messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise WorkerError("mcp.worker_response_timeout: result unknown; inspect the original job, do not replay writes") from exc
        if not isinstance(reply, dict) or reply.get("id") != message["id"]:
            raise WorkerError("mcp.worker_connection_lost: request result is unknown; do not replay writes")
        return reply

    def internal(self, method, params=None, timeout=20):
        response = self.exchange({"jsonrpc": "2.0", "id": "refresh-" + uuid.uuid4().hex,
                                  "method": method, "params": params or {}}, timeout=timeout)
        if "error" in response:
            raise WorkerError("mcp.refresh_candidate_unready")
        return response["result"]

    def inspect(self):
        return data_from(self.internal("tools/call", {"name": "trlr_refresh_mcp", "arguments": {"action": "inspect"}}))

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Never kill a worker whose result could be unknown.
            threading.Thread(target=self.process.wait, daemon=True).start()
        if self.process.poll() is not None:
            self.process.stdout.close()


class Supervisor:
    def __init__(self):
        self.loaded_root = source_root()
        self.install_root = installation_root(self.loaded_root)
        self.worker = Worker(self.loaded_root, self.install_root)
        self.initialize_params = {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "tonerelay-supervisor", "version": "1"}}
        self.initialized = False
        self.generation = 1

    def refresh(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) - {"action"}:
            raise ValueError("Only action=inspect|refresh is accepted")
        action = arguments.get("action", "refresh")
        if action not in ("inspect", "refresh"):
            raise ValueError("action must be inspect or refresh")
        if action == "inspect":
            return {**self.worker.inspect(), "generation": self.generation}, False
        candidate = None
        try:
            root = active_source(self.loaded_root, self.install_root)
            candidate = Worker(root, self.install_root)
            initialized = candidate.internal("initialize", self.initialize_params)
            if initialized.get("protocolVersion") != "2024-11-05":
                raise WorkerError("mcp.refresh_protocol_mismatch")
            candidate.exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = candidate.internal("tools/list").get("tools")
            if not isinstance(tools, list) or not any(t.get("name") == "trlr_refresh_mcp" for t in tools):
                raise WorkerError("mcp.refresh_candidate_missing_tool")
            state = candidate.inspect()
            if state["loaded_source_sha256"] != state["active_source_sha256"]:
                raise WorkerError("mcp.refresh_source_changed_during_startup")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if candidate is not None:
                candidate.close()
            reason = str(exc) if isinstance(exc, WorkerError) else "mcp.refresh_candidate_failed"
            return {"status": "refresh_failed", "reason": reason, "previous_worker_preserved": True,
                    "bridge_stopped": False, "generation": self.generation}, False
        previous = self.worker
        self.worker = candidate
        self.generation += 1
        previous.close()
        return {
            **state, "status": "refreshed", "generation": self.generation,
            "stdio_connection_preserved": True, "bridge_stopped": False,
            "pending_jobs_replayed": False,
            "runtime_operator_instructions": initialized.get("instructions", ""),
            "tools_sha256": hashlib.sha256(json.dumps(tools, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "client_cache_status": "unknown; tools list notification requests rediscovery; clients that ignore it need official reconnect",
        }, True

    def serve(self, stdin, stdout):
        def emit(message):
            stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            stdout.flush()
        try:
            for line in stdin:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                    continue
                if not isinstance(message, dict):
                    emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
                    continue
                params = message.get("params")
                refresh_call = (message.get("method") == "tools/call" and isinstance(params, dict)
                                and params.get("name") == "trlr_refresh_mcp" and "id" in message)
                try:
                    notify = False
                    if refresh_call:
                        arguments = params.get("arguments", {})
                        data, notify = self.refresh({} if arguments is None else arguments)
                        response = {"jsonrpc": "2.0", "id": message["id"],
                                    "result": tool_result(data, data["status"] == "refresh_failed")}
                    else:
                        response = self.worker.exchange(message)
                        if message.get("method") == "initialize" and response and "result" in response:
                            self.initialize_params = message.get("params") or self.initialize_params
                            response["result"]["capabilities"]["tools"]["listChanged"] = True
                        if message.get("method") == "notifications/initialized":
                            self.initialized = True
                    if response is not None:
                        emit(response)
                    if notify and self.initialized:
                        emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                except (OSError, ValueError) as exc:
                    if "id" in message:
                        reason = str(exc) if isinstance(exc, WorkerError) else "mcp.invalid_refresh_arguments"
                        emit({"jsonrpc": "2.0", "id": message["id"],
                              "result": tool_result({"status": "failed", "reason": reason}, True)})
        finally:
            self.worker.close()


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    Supervisor().serve(sys.stdin, sys.stdout)
