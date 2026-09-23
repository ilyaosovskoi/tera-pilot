"""
Synchronous LSP client for agent tools (go-to-definition, references, symbols).

``LSPClient`` in ``lsp_client.py`` is signal/event based (built for the UI);
an agent tool needs a blocking request/response call. This module speaks
bare JSON-RPC over stdio to ``python-lsp-server`` (``pylsp``) or
``jedi-language-server`` — whichever is installed — with Content-Length
framing, a reader thread, and per-request queues. Original implementation;
only the server *binary* is reused.

Positions are 0-based lines/characters per the LSP spec; the engine
translates the agent-facing 1-based lines.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class LspError(Exception):
    pass


def find_server_command() -> Optional[List[str]]:
    """``python3 -m pylsp`` preferred, jedi-language-server fallback."""
    py = shutil.which("python3") or "python3"
    for module in ("pylsp", "jedi_language_server"):
        try:
            proc = subprocess.run(
                [py, "-m", module, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                return [py, "-m", module]
        except Exception:
            continue
    return None


class LspSyncClient:
    """Blocking JSON-RPC client over one server subprocess."""

    def __init__(self, workspace: str, timeout: float = 30.0) -> None:
        cmd = find_server_command()
        if cmd is None:
            raise LspError(
                "no language server installed — run: pip install python-lsp-server")
        self._timeout = timeout
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=str(workspace),
            )
        except Exception as exc:
            raise LspError(f"cannot start language server: {exc}") from exc
        self._next_id = 0
        self._lock = threading.Lock()
        self._queues: Dict[int, "queue.Queue[Any]"] = {}
        self._init_event = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="tera-lsp-reader")
        self._reader.start()
        self._initialize(workspace)

    # — transport —

    def _send(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(header + body)
            self._proc.stdin.flush()
        except Exception as exc:
            raise LspError(f"LSP send failed: {exc}") from exc

    def _read_loop(self) -> None:
        stream = self._proc.stdout
        assert stream is not None
        while True:
            try:
                length = self._read_headers(stream)
                if length is None:
                    return
                body = self._read_exact(stream, length)
                if body is None:
                    return
                try:
                    msg = json.loads(body.decode("utf-8"))
                except ValueError:
                    continue
                req_id = msg.get("id")
                if req_id is not None:
                    with self._lock:
                        q = self._queues.get(int(req_id))
                    if q is not None:
                        q.put(msg)
            except Exception:
                return

    @staticmethod
    def _read_headers(stream: Any) -> Optional[int]:
        length: Optional[int] = None
        while True:
            line = stream.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                return length
            if line.lower().startswith(b"content-length:"):
                try:
                    length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    pass

    @staticmethod
    def _read_exact(stream: Any, n: int) -> Optional[bytes]:
        buf = b""
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _request(self, method: str, params: Dict[str, Any],
                 timeout: float = 20.0) -> Any:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            q: "queue.Queue[Any]" = queue.Queue()
            self._queues[req_id] = q
        try:
            self._send({"jsonrpc": "2.0", "id": req_id,
                        "method": method, "params": params})
        except LspError:
            with self._lock:
                self._queues.pop(req_id, None)
            raise
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise LspError(f"LSP {method} timed out after {timeout}s")
        finally:
            with self._lock:
                self._queues.pop(req_id, None)
        if isinstance(msg, dict) and "error" in msg:
            err = msg["error"]
            raise LspError(f"LSP {method} error: {err}")
        return msg.get("result") if isinstance(msg, dict) else None

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _initialize(self, workspace: str) -> None:
        root_uri = Path(workspace).resolve().as_uri()
        try:
            self._request("initialize", {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {},
            }, timeout=self._timeout)
        except LspError as exc:
            self.shutdown()
            raise LspError(f"LSP initialize failed: {exc}") from exc
        self._notify("initialized", {})
        self._init_event.set()

    def shutdown(self) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": 10 ** 9, "method": "shutdown", "params": {}})
        except Exception:
            pass
        try:
            self._notify("exit", {})
        except Exception:
            pass
        try:
            self._proc.terminate()
        except Exception:
            pass

    # — document sync + queries —

    @staticmethod
    def _uri(path: Path) -> str:
        return path.resolve().as_uri()

    def open_document(self, path: Path, language_id: str = "python") -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LspError(f"cannot read {path}: {exc}") from exc
        self._notify("textDocument/didOpen", {
            "textDocument": {"uri": self._uri(path), "languageId": language_id,
                             "version": 1, "text": text},
        })

    def definition(self, path: Path, line: int, character: int) -> List[Dict[str, Any]]:
        self.open_document(path)
        result = self._request("textDocument/definition", {
            "textDocument": {"uri": self._uri(path)},
            "position": {"line": line, "character": character},
        })
        return self._locations(result)

    def references(self, path: Path, line: int, character: int) -> List[Dict[str, Any]]:
        self.open_document(path)
        result = self._request("textDocument/references", {
            "textDocument": {"uri": self._uri(path)},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True},
        })
        return self._locations(result)

    def document_symbols(self, path: Path) -> List[Dict[str, Any]]:
        self.open_document(path)
        result = self._request("textDocument/documentSymbol", {
            "textDocument": {"uri": self._uri(path)},
        })
        flat: List[Dict[str, Any]] = []

        def _walk(items: Any, depth: int = 0) -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("name", "?")
                kind = item.get("kind", "?")
                rng = ((item.get("range") or item.get("location") or {}).get("start") or {})
                flat.append({"name": name, "kind": kind,
                             "line": int(rng.get("line", 0)) + 1})
                _walk(item.get("children"), depth + 1)

        _walk(result)
        return flat

    @staticmethod
    def _locations(result: Any) -> List[Dict[str, Any]]:
        if result is None:
            return []
        items = result if isinstance(result, list) else [result]
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") or item.get("targetUri", "")
            rng = item.get("range") or item.get("targetRange") or {}
            start = rng.get("start", {}) or {}
            out.append({"uri": uri,
                        "line": int(start.get("line", 0)) + 1,
                        "character": int(start.get("character", 0))})
        return out
