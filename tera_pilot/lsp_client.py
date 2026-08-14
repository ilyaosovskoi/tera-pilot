"""
LSP Client for Tera Pilot (Qt-free, v2.2.0).

Language Server Protocol client for Python (via python-lsp-server /
jedi-language-server). Provides: autocomplete, hover, go-to-definition,
diagnostics, signature help. Async operations run on plain
``threading.Thread`` — no Qt / PySide6 dependency.

v2.2.0: the legacy ``QObject`` / ``Signal`` / ``QThread`` based LSPClient
has been rewritten on top of :mod:`threading` and a tiny callback-list
``_Signal`` shim that preserves the public ``.connect()`` / ``.emit()``
API. All existing call sites (the legacy ``web_bridge`` bridge, the
``api_server`` HTTP handler, the TUI) keep working unchanged.
"""

from __future__ import annotations

import os
import json
import logging
import subprocess
import threading
from contextlib import suppress
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


# ── Tiny signal shim ──────────────────────────────────────────────────

class _Signal:
    """Drop-in replacement for ``PySide6.QtCore.Signal``.

    Only the ``connect`` + ``emit`` API is implemented. Thread-safe
    via a lock so emit() from the reader thread doesn't race with
    connect() from the main thread.
    """

    def __init__(self, *arg_types: Any) -> None:
        self._listeners: List[Callable[..., None]] = []
        self._lock = threading.Lock()

    def connect(self, fn: Callable[..., None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def disconnect(self, fn: Optional[Callable[..., None]] = None) -> None:
        with self._lock:
            if fn is None:
                self._listeners.clear()
            else:
                try:
                    self._listeners.remove(fn)
                except ValueError:
                    pass

    def emit(self, *args: Any) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(*args)
            except Exception:
                logger.exception("[lsp] signal listener raised")


# ── Data classes ──────────────────────────────────────────────────────

class LSPMethod(Enum):
    INITIALIZE = "initialize"
    INITIALIZED = "initialized"
    SHUTDOWN = "shutdown"
    EXIT = "exit"
    TEXT_DOCUMENT_DID_OPEN = "textDocument/didOpen"
    TEXT_DOCUMENT_DID_CHANGE = "textDocument/didChange"
    TEXT_DOCUMENT_DID_SAVE = "textDocument/didSave"
    TEXT_DOCUMENT_DID_CLOSE = "textDocument/didClose"
    TEXT_DOCUMENT_COMPLETION = "textDocument/completion"
    TEXT_DOCUMENT_HOVER = "textDocument/hover"
    TEXT_DOCUMENT_DEFINITION = "textDocument/definition"
    TEXT_DOCUMENT_DIAGNOSTIC = "textDocument/diagnostic"
    TEXT_DOCUMENT_SIGNATURE_HELP = "textDocument/signatureHelp"
    TEXT_DOCUMENT_FORMATTING = "textDocument/formatting"


@dataclass
class CompletionItem:
    label: str
    kind: int
    detail: str = ""
    documentation: str = ""
    insert_text: str = ""
    sort_text: str = ""


@dataclass
class HoverInfo:
    contents: str
    range: Optional[Dict] = None


@dataclass
class Location:
    uri: str
    range: Dict[str, Any]


@dataclass
class Diagnostic:
    range: Dict[str, Any]
    severity: int
    message: str
    source: str = ""
    code: str = ""


# ── LSP Client ────────────────────────────────────────────────────────

class LSPClient:
    """
    LSP client for Tera Pilot IDE.
    Communicates with python-lsp-server via JSON-RPC over stdio.
    """

    # Public signals (callback-list based, drop-in for Qt signals).
    completions_ready = _Signal(str, list)        # uri, List[CompletionItem]
    hover_ready = _Signal(str, object)            # uri, HoverInfo | None
    definitions_ready = _Signal(str, list)        # uri, List[Location]
    diagnostics_ready = _Signal(str, list)        # uri, List[Diagnostic]
    signature_help_ready = _Signal(str, object)   # uri, dict
    server_started = _Signal(bool, str)           # success, message
    server_stopped = _Signal()

    def __init__(self, parent: Any = None) -> None:
        # parent is intentionally ignored — kept for backward compat
        # with the legacy QObject constructor signature.
        self.process: Optional[subprocess.Popen] = None
        self._request_id = 0
        self._pending_requests: Dict[int, str] = {}  # id -> method
        self._lock = threading.Lock()
        self._initialized = False
        self._read_thread: Optional[threading.Thread] = None
        self._should_stop = False
        self._server_capabilities: Dict[str, Any] = {}
        self._open_documents: Dict[str, Dict] = {}  # uri -> version info
        self._workspace_path: str = str(Path.home())

    def _next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _send_message(self, message: Dict[str, Any]):
        """Send JSON-RPC message to server.

        v1.0.5-security: hold `self._lock` around stdin write/flush so
        concurrent writes from the UI thread (did_open/did_change) and
        the shutdown thread don't interleave bytes on the same pipe
        (BUGS_REPORT H-LSP-2).
        """
        if not self.process or self.process.poll() is not None:
            logger.warning("LSP server not running")
            return

        data = json.dumps(message)
        header = f"Content-Length: {len(data.encode('utf-8'))}\r\n\r\n"
        full_message = header + data

        with self._lock:
            try:
                self.process.stdin.write(full_message.encode("utf-8"))
                self.process.stdin.flush()
            except Exception as e:
                logger.error(f"Failed to send LSP message: {e}")

    def _read_responses(self):
        """Background thread: read responses from LSP server."""
        while not self._should_stop and self.process and self.process.poll() is None:
            try:
                # Read header
                header = b""
                while True:
                    byte = self.process.stdout.read(1)
                    if not byte:
                        return
                    header += byte
                    if header.endswith(b"\r\n\r\n"):
                        break

                # Parse Content-Length
                content_length = 0
                for line in header.decode("utf-8").strip().split("\r\n"):
                    if line.startswith("Content-Length:"):
                        try:
                            content_length = int(line.split(":")[1].strip())
                        except ValueError:
                            content_length = 0

                if content_length == 0:
                    continue

                # Read body
                body = self.process.stdout.read(content_length)
                if not body:
                    continue

                response = json.loads(body.decode("utf-8"))
                self._handle_response(response)

            except Exception as e:
                logger.debug(f"LSP read error: {e}")

    def _handle_response(self, response: Dict[str, Any]):
        """Handle JSON-RPC response from server."""
        if "id" in response:
            req_id = response["id"]
            with self._lock:
                method = self._pending_requests.pop(req_id, None)

            if method == LSPMethod.TEXT_DOCUMENT_COMPLETION.value:
                items = self._parse_completions(response.get("result", {}))
                self.completions_ready.emit("", items)

            elif method == LSPMethod.TEXT_DOCUMENT_HOVER.value:
                hover = self._parse_hover(response.get("result"))
                self.hover_ready.emit("", hover)

            elif method == LSPMethod.TEXT_DOCUMENT_DEFINITION.value:
                locations = self._parse_locations(response.get("result", []))
                self.definitions_ready.emit("", locations)

            elif method == LSPMethod.TEXT_DOCUMENT_SIGNATURE_HELP.value:
                self.signature_help_ready.emit("", response.get("result", {}))

            elif method == LSPMethod.INITIALIZE.value:
                if "result" in response:
                    self._server_capabilities = response["result"].get("capabilities", {})
                    self._initialized = True
                    self._send_initialized()
                    self.server_started.emit(True, "LSP server initialized")

        # Handle server-initiated notifications
        if "method" in response:
            method = response["method"]
            params = response.get("params", {})

            if method == "textDocument/publishDiagnostics":
                uri = params.get("uri", "")
                diagnostics = self._parse_diagnostics(params.get("diagnostics", []))
                self.diagnostics_ready.emit(uri, diagnostics)

    def _parse_completions(self, result: Any) -> List[CompletionItem]:
        """Parse completion response."""
        items = []
        if isinstance(result, dict):
            result = result.get("items", [])
        elif not isinstance(result, list):
            return items

        for item in result:
            items.append(CompletionItem(
                label=item.get("label", ""),
                kind=item.get("kind", 0),
                detail=item.get("detail", ""),
                documentation=str(item.get("documentation", "")),
                insert_text=item.get("insertText", item.get("label", "")),
                sort_text=item.get("sortText", ""),
            ))
        return items

    def _parse_hover(self, result: Any) -> Optional[HoverInfo]:
        """Parse hover response."""
        if not result:
            return None
        contents = result.get("contents", "")
        if isinstance(contents, dict):
            contents = contents.get("value", "")
        return HoverInfo(contents=str(contents), range=result.get("range"))

    def _parse_locations(self, result: Any) -> List[Location]:
        """Parse definition/declaration locations."""
        locations = []
        if not result:
            return locations
        if isinstance(result, dict):
            result = [result]
        for loc in result:
            locations.append(Location(
                uri=loc.get("uri", ""),
                range=loc.get("range", {}),
            ))
        return locations

    def _parse_diagnostics(self, diagnostics: List[Dict]) -> List[Diagnostic]:
        """Parse diagnostics."""
        result = []
        for d in diagnostics:
            result.append(Diagnostic(
                range=d.get("range", {}),
                severity=d.get("severity", 1),
                message=d.get("message", ""),
                source=d.get("source", ""),
                code=str(d.get("code", "")),
            ))
        return result

    def start_server(self, workspace_path: Optional[str] = None):
        """Start python-lsp-server (or jedi-language-server fallback)."""
        if workspace_path:
            self._workspace_path = workspace_path

        try:
            # Check if pylsp is available
            result = subprocess.run(
                ["python3", "-m", "pylsp", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                # Try jedi-language-server as fallback
                result = subprocess.run(
                    ["python3", "-m", "jedi_language_server", "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode != 0:
                    self.server_started.emit(
                        False,
                        "python-lsp-server not installed. Run: pip install python-lsp-server",
                    )
                    return
                cmd = ["python3", "-m", "jedi_language_server"]
            else:
                cmd = ["python3", "-m", "pylsp"]

            self._should_stop = False

            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._workspace_path,
                text=False,  # Work with bytes
            )

            logger.info(f"LSP server started (PID: {self.process.pid})")

            # v1.0.5-security / v2.2.0: the reader is now a plain
            # threading.Thread that calls ``_read_responses`` directly
            # (no QThread / worker / signal indirection). Same framing
            # logic, just no Qt event loop dependency.
            self._read_thread = threading.Thread(
                target=self._read_responses,
                name=f"LSPReader-{cmd[-1]}",
                daemon=True,
            )
            self._read_thread.start()

            # Send initialize
            self._send_initialize()

            logger.info("LSP initialized successfully")

        except FileNotFoundError as e:
            logger.error(f"LSP server not found: {e}")
            self._cleanup()
            self.server_started.emit(False, f"LSP server not found: {e}")
        except Exception as e:
            logger.error(f"Failed to start LSP server: {e}")
            self._cleanup()
            self.server_started.emit(False, f"Failed to start LSP: {e}")

    def _send_initialize(self):
        """Send initialize request."""
        req_id = self._next_id()
        with self._lock:
            self._pending_requests[req_id] = LSPMethod.INITIALIZE.value

        self._send_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": {
                "processId": os.getpid(),
                "rootUri": Path(self._workspace_path).as_uri(),
                "capabilities": {
                    "textDocument": {
                        "completion": {
                            "dynamicRegistration": False,
                            "completionItem": {
                                "snippetSupport": True,
                                "commitCharactersSupport": True,
                                "documentationFormat": ["markdown", "plaintext"],
                                "deprecatedSupport": True,
                                "preselectSupport": True,
                            }
                        },
                        "hover": {
                            "dynamicRegistration": False,
                            "contentFormat": ["markdown", "plaintext"]
                        },
                        "definition": {
                            "dynamicRegistration": False,
                            "linkSupport": True
                        },
                        "signatureHelp": {
                            "dynamicRegistration": False,
                            "signatureInformation": {
                                "documentationFormat": ["markdown", "plaintext"]
                            }
                        },
                        "synchronization": {
                            "dynamicRegistration": False,
                            "willSave": False,
                            "willSaveWaitUntil": False,
                            "didSave": True
                        },
                        "publishDiagnostics": {
                            "relatedInformation": True,
                            "versionSupport": True,
                            "tagSupport": {"valueSet": [1, 2]}
                        }
                    }
                },
                "workspaceFolders": [{
                    "uri": Path(self._workspace_path).as_uri(),
                    "name": Path(self._workspace_path).name
                }]
            }
        })

    def _send_initialized(self):
        """Send initialized notification."""
        self._send_message({
            "jsonrpc": "2.0",
            "method": "initialized",
            "params": {}
        })

    def stop_server(self):
        """Shutdown LSP server gracefully."""
        self._cleanup()

    def _cleanup(self):
        """Clean up LSP resources: stop reader thread, terminate process."""
        self._should_stop = True
        if self._read_thread is not None and self._read_thread.is_alive():
            # Reader is daemon — give it a moment, then move on.
            try:
                self._read_thread.join(timeout=2.0)
            except Exception:
                pass
            self._read_thread = None

        # Terminate process
        if self.process is not None:
            if self.process.poll() is None:  # Still running
                try:
                    # Graceful shutdown first
                    self._send_message({
                        "jsonrpc": "2.0",
                        "id": self._next_id(),
                        "method": "shutdown",
                        "params": {}
                    })
                    self._send_message({
                        "jsonrpc": "2.0",
                        "method": "exit",
                        "params": {}
                    })
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("LSP server didn't shutdown gracefully, terminating")
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        logger.error("LSP server won't terminate, killing")
                        self.process.kill()
                except Exception as e:
                    logger.error(f"Error during LSP shutdown: {e}")
                    with suppress(Exception):
                        self.process.kill()

            self.process = None
            self._initialized = False
            self.server_stopped.emit()

    def close(self):
        """Explicitly close LSP connection."""
        self._cleanup()

    def __del__(self):
        """Ensure cleanup on object destruction."""
        try:
            self._cleanup()
        except Exception:
            pass

    # ── Document sync ───────────────────────────────────────────

    def did_open(self, uri: str, language_id: str, text: str, version: int = 1):
        """Notify server that document was opened."""
        self._open_documents[uri] = {"version": version, "text": text}
        self._send_message({
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
                    "text": text
                }
            }
        })

    def did_change(self, uri: str, text: str, version: int):
        """Notify server that document changed."""
        self._open_documents[uri] = {"version": version, "text": text}
        self._send_message({
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "version": version
                },
                "contentChanges": [{"text": text}]
            }
        })

    def did_save(self, uri: str):
        """Notify server that document was saved."""
        self._send_message({
            "jsonrpc": "2.0",
            "method": "textDocument/didSave",
            "params": {
                "textDocument": {"uri": uri}
            }
        })

    def did_close(self, uri: str):
        """Notify server that document was closed."""
        if uri in self._open_documents:
            del self._open_documents[uri]
        self._send_message({
            "jsonrpc": "2.0",
            "method": "textDocument/didClose",
            "params": {
                "textDocument": {"uri": uri}
            }
        })

    # ── Requests ────────────────────────────────────────────────

    def request_completion(self, uri: str, line: int, character: int):
        """Request completions at position."""
        req_id = self._next_id()
        with self._lock:
            self._pending_requests[req_id] = LSPMethod.TEXT_DOCUMENT_COMPLETION.value

        self._send_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "textDocument/completion",
            "params": {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character}
            }
        })

    def request_hover(self, uri: str, line: int, character: int):
        """Request hover info at position."""
        req_id = self._next_id()
        with self._lock:
            self._pending_requests[req_id] = LSPMethod.TEXT_DOCUMENT_HOVER.value

        self._send_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "textDocument/hover",
            "params": {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character}
            }
        })

    def request_definition(self, uri: str, line: int, character: int):
        """Request go-to-definition at position."""
        req_id = self._next_id()
        with self._lock:
            self._pending_requests[req_id] = LSPMethod.TEXT_DOCUMENT_DEFINITION.value

        self._send_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "textDocument/definition",
            "params": {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character}
            }
        })

    def request_signature_help(self, uri: str, line: int, character: int):
        """Request signature help at position."""
        req_id = self._next_id()
        with self._lock:
            self._pending_requests[req_id] = LSPMethod.TEXT_DOCUMENT_SIGNATURE_HELP.value

        self._send_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "textDocument/signatureHelp",
            "params": {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character}
            }
        })

    def is_ready(self) -> bool:
        """Check if LSP server is initialized and ready."""
        return self._initialized and self.process is not None

    def get_capabilities(self) -> Dict[str, Any]:
        """Get server capabilities."""
        return self._server_capabilities.copy()


__all__ = [
    "LSPClient",
    "LSPMethod",
    "CompletionItem",
    "HoverInfo",
    "Location",
    "Diagnostic",
]
