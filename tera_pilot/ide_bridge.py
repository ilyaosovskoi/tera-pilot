"""
IDE bridge — agent-side server for editor extensions (VS Code, JetBrains).

The extension (see ``editors/vscode/``) is a TCP client; this server runs
inside Tera Pilot (started with ``tera-pilot-bridge`` or ``/bridge`` in
the TUI). Framing is newline-delimited JSON, like the ACP server:

  IDE → agent  (request):  {"id": 1, "token": "...", "method": "...", "params": {...}}
  agent → IDE (response): {"id": 1, "ok": true, "result": {...}}

Agent → IDE traffic (diff preview, approval prompts, open-file) flows
through a long-poll queue: the IDE calls ``poll_events`` (blocks up to
25s) and answers approvals with ``resolve_approval``. This keeps a
single client connection model — no back-connect, no firewall rules.

Security: binds 127.0.0.1 only; every request carries a bearer token
(``bridge_token`` in config.json, generated on first start, file 0600).
Nothing is executed from IDE input — ``provide_context`` only *stores*
text the agent may read; approvals resolve prompts the agent created.

Protocol methods (IDE → agent):
  ping, provide_context, get_context, poll_events, resolve_approval,
  list_approvals
Agent → IDE events (via poll_events):
  notify, open_file, show_diff, request_approval
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import secrets
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

VERSION = "1.0"
POLL_TIMEOUT = 25.0


def _bridge_home() -> Path:
    home = Path(os.path.expanduser("~/.tera_pilot"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def load_or_create_token() -> str:
    """Read ``bridge_token`` from config.json, generating one if absent."""
    try:
        from tera_pilot.utils import load_config, save_config
        cfg = load_config() or {}
        token = str(cfg.get("bridge_token") or "")
        if len(token) < 32:
            token = secrets.token_urlsafe(32)
            cfg["bridge_token"] = token
            save_config(cfg)
        try:
            os.chmod(str(_bridge_home() / "config.json"), 0o600)
        except OSError:
            pass
        return token
    except Exception:
        return secrets.token_urlsafe(32)


class IDEBridgeServer:
    """Threaded TCP server implementing the IDE bridge protocol."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 token: Optional[str] = None) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("IDE bridge binds localhost only")
        self.host = "127.0.0.1" if host == "localhost" else host
        self.token = token or load_or_create_token()
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._approvals: Dict[str, Dict[str, Any]] = {}
        self._approval_counter = 0
        self._lock = threading.Lock()
        self._context: Dict[str, Any] = {}
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port = port
        self._on_approval: Optional[Callable[[Dict[str, Any]], None]] = None

    # — lifecycle —

    @property
    def port(self) -> int:
        if self._server is not None:
            return self._server.server_address[1]
        return self._port

    def start(self) -> Dict[str, Any]:
        if self._server is not None:
            return {"ok": True, "host": self.host, "port": self.port}
        server = self

        class _Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:  # type: ignore[override]
                try:
                    for raw in self.rfile:
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line.decode("utf-8"))
                        except ValueError:
                            self._send({"id": None, "ok": False,
                                        "error": "parse error"})
                            continue
                        self._send(server.handle_message(msg))
                except (ConnectionResetError, BrokenPipeError):
                    pass
                except Exception as exc:
                    logger.debug("[ide-bridge] handler failed: %s", exc)

            def _send(self, payload: Dict[str, Any]) -> None:
                try:
                    self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
                    self.wfile.flush()
                except (ConnectionResetError, BrokenPipeError):
                    pass

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(
            (self.host, self._port), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True, name="tera-ide-bridge")
        self._thread.start()
        logger.info("[ide-bridge] listening on %s:%d", self.host, self.port)
        return {"ok": True, "host": self.host, "port": self.port}

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass

    # — protocol —

    def handle_message(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        msg_id = msg.get("id")
        if not isinstance(msg, dict) or not secrets.compare_digest(
                str(msg.get("token", "")), self.token):
            return {"id": msg_id, "ok": False, "error": "unauthorized"}
        method = msg.get("method", "")
        params = msg.get("params") or {}
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            return {"id": msg_id, "ok": False,
                    "error": f"unknown method: {method}"}
        try:
            return {"id": msg_id, "ok": True, "result": handler(params)}
        except Exception as exc:
            return {"id": msg_id, "ok": False, "error": str(exc)[:300]}

    def _m_ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"version": VERSION, "server": "tera-pilot-ide-bridge"}

    def _m_provide_context(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._context = {
                "selection": params.get("selection"),
                "open_files": list(params.get("open_files") or [])[:20],
                "diagnostics": list(params.get("diagnostics") or [])[:50],
                "updated_at": time.time(),
            }
        return {"ok": True}

    def _m_get_context(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            return dict(self._context)

    def _m_poll_events(self, params: Dict[str, Any]) -> Dict[str, Any]:
        timeout = min(25.0, max(1.0, float(params.get("timeout", POLL_TIMEOUT))))
        events = []
        try:
            first = self._events.get(timeout=timeout)
            events.append(first)
            while True:
                try:
                    events.append(self._events.get_nowait())
                except queue.Empty:
                    break
        except queue.Empty:
            pass
        return {"events": events}

    def _m_resolve_approval(self, params: Dict[str, Any]) -> Dict[str, Any]:
        approval_id = str(params.get("id", ""))
        decision = bool(params.get("decision", False))
        with self._lock:
            entry = self._approvals.get(approval_id)
            if entry is None:
                return {"ok": False, "error": f"no pending approval {approval_id!r}"}
            if entry.get("decision") is not None:
                return {"ok": False, "error": "already decided"}
            entry["decision"] = decision
            entry["event"].set()
        if self._on_approval is not None:
            try:
                self._on_approval({"id": approval_id, "decision": decision})
            except Exception:
                pass
        return {"ok": True}

    def _m_list_approvals(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            pending = [{k: v for k, v in e.items() if k != "event"}
                       for e in self._approvals.values()
                       if e.get("decision") is None]
        return {"pending": pending}

    # — agent side —

    def emit(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Queue an agent → IDE event (notify, open_file, show_diff)."""
        self._events.put({"event": event, "payload": payload or {},
                          "ts": time.time()})

    def request_approval(self, action: str, summary: str,
                         timeout: float = 300.0) -> Optional[bool]:
        """Ask the IDE; block until resolve/timeout. None = unanswered."""
        with self._lock:
            self._approval_counter += 1
            approval_id = str(self._approval_counter)
            event = threading.Event()
            self._approvals[approval_id] = {
                "id": approval_id, "action": action,
                "summary": summary[:300], "created": time.time(),
                "event": event, "decision": None,
            }
        self._events.put({"event": "request_approval",
                          "payload": {"id": approval_id, "action": action,
                                      "summary": summary[:300]},
                          "ts": time.time()})
        resolved = event.wait(timeout=max(10.0, timeout))
        with self._lock:
            entry = self._approvals.pop(approval_id, None)
        if not resolved or entry is None:
            return None
        return entry["decision"]

    def set_approval_callback(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        self._on_approval = fn


class IDEBridgeClient:
    """Minimal blocking client (used by tests and the example extension logic)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 token: str = "", timeout: float = 30.0) -> None:
        import socket as _socket
        self._sock = _socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._file = self._sock.makefile("r", encoding="utf-8")
        self._token = token
        self._next_id = 0
        self._lock = threading.Lock()

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            payload = json.dumps({"id": req_id, "token": self._token,
                                  "method": method, "params": params or {}}) + "\n"
            self._sock.sendall(payload.encode("utf-8"))
            line = self._file.readline()
        if not line:
            raise ConnectionError("IDE bridge closed the connection")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "unknown error"))
        if resp.get("id") != req_id:
            raise RuntimeError("request id mismatch")
        return resp.get("result")

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point: ``tera-pilot-bridge [--port N]``."""
    parser = argparse.ArgumentParser(
        prog="tera-pilot-bridge",
        description="IDE bridge server for editor extensions (localhost only).")
    parser.add_argument("--port", type=int, default=0,
                        help="TCP port (default: ephemeral, printed on stdout).")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind host, localhost only (default: 127.0.0.1).")
    args = parser.parse_args(argv)
    server = IDEBridgeServer(host=args.host, port=args.port)
    info = server.start()
    print(f"IDE bridge on {info['host']}:{info['port']} (token in config.json: bridge_token)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0
