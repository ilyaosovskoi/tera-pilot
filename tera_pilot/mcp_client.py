"""
MCP Client for Tera Pilot v1.1.0 — Model Context Protocol client.

Speaks the Model Context Protocol (MCP) over stdio using JSON-RPC 2.0
with Content-Length framing (same framing as LSP — see tera_pilot/lsp_client.py
for the template). An MCP server is a subprocess that exposes a set of
"tools" (callables with JSON Schema inputs) to the LLM via this client.

Protocol summary:
  1. Client spawns server: `subprocess.Popen(command, args, env, stdin=PIPE, stdout=PIPE)`
  2. Client sends `initialize` request with protocolVersion + clientInfo
  3. Server responds with capabilities + serverInfo
  4. Client sends `initialized` notification
  5. Client calls `tools/list` to discover available tools
  6. For each tool, server returns {name, description, inputSchema (JSON Schema)}
  7. When the LLM wants to call a tool, client sends `tools/call` with
     {name, arguments} and the server returns {content: [{type: "text", text: "..."}]}

Security:
  - The MCP server runs as a separate subprocess. Tera Pilot is responsible for
    spawning only servers the user has explicitly configured in Settings →
    MCP. We do NOT auto-discover or auto-start MCP servers.
  - Tool call results are returned to the LLM as observations. The agent
    autonomy setting (always_ask / new_files_only / never_ask) gates
    whether the agent should be confirmed before EACH MCP tool call —
    see AgentRuntime._is_write_or_execute_tool.
  - We sandbox the working directory to the project root by default. The
    server process inherits Tera Pilot's environment, but users can override
    env per-server in the config.

This implementation is synchronous (each tool call blocks until the
server responds). The agent loop is already on a background thread, so
this is fine — it doesn't block the UI. A timeout prevents hangs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

MCP_PROTOCOL_VERSION = "2024-11-05"  # latest stable MCP protocol version
DEFAULT_TIMEOUT = 30.0  # seconds per tool call
INIT_TIMEOUT = 10.0  # seconds for initialize handshake


# ── Data classes ────────────────────────────────────────────────────────

@dataclass
class MCPTool:
    """A single tool exposed by an MCP server."""
    name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass
class MCPServerInfo:
    """Metadata about a connected MCP server."""
    name: str
    version: str = ""
    protocol_version: str = ""
    capabilities: Dict[str, Any] = field(default_factory=dict)
    instructions: str = ""  # server may provide usage instructions


# ── MCPClient ───────────────────────────────────────────────────────────

class MCPClient:
    """Synchronous MCP client over stdio.

    Lifecycle:
      client = MCPClient(name="filesystem", command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
      client.start()         # spawn subprocess, send initialize
      tools = client.list_tools()  # discover tools
      result = client.call_tool("read_file", {"path": "/tmp/foo.txt"})
      client.stop()          # shutdown + exit
    """

    def __init__(self, name: str, command: List[str],
                 env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None):
        self.name = name
        self.command = command
        self.env = env or {}
        self.cwd = cwd
        self.process: Optional[subprocess.Popen] = None
        self._request_id = 0
        self._pending: Dict[int, Dict[str, Any]] = {}  # id -> {event, result, error}
        self._lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._should_stop = False
        self.server_info: Optional[MCPServerInfo] = None
        self.tools: List[MCPTool] = []
        self._initialized = False
        # v1.1.3-fix (bug 3.2): track whether tool discovery succeeded.
        # When True, is_running() reports a "degraded" state so the UI
        # can show "running but no tools discovered" instead of pretending
        # everything is fine.
        self._tool_discovery_failed: bool = False

    # ── Lifecycle ──────────────────────────────────────────────────

    # v1.1.3-fix (bug 3.3): env vars that are SAFE to forward to MCP
    # subprocesses. Anything NOT in this list (or in the server's
    # explicit "env" config) is dropped, so secrets like
    # OPENAI_API_KEY / ANTHROPIC_API_KEY / GITHUB_TOKEN are NOT leaked
    # to `npx -y some-random-package`.
    _ENV_WHITELIST_PREFIXES = (
        "PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP",
        "LANG", "LC_", "TZ",
        # macOS-specific
        "DYLD_", "SSL_CERT",
        # Windows-specific
        "SYSTEMROOT", "WINDIR", "APPDATA", "LOCALAPPDATA",
        "PROGRAMFILES", "PROGRAMDATA", "COMSPEC", "PATHEXT",
        "HOMEDRIVE", "HOMEPATH", "USERNAME",
    )

    def _build_sandboxed_env(self) -> Dict[str, str]:
        """Return a filtered environment dict for the MCP subprocess.

        v1.1.3-fix (bug 3.3): starts from an empty dict and copies
        ONLY:
          - vars matching ``_ENV_WHITELIST_PREFIXES`` (PATH, HOME, etc.)
          - vars explicitly set in ``self.env`` (from mcp.json)

        This prevents the parent process's secrets (LLM provider API
        keys, GitHub tokens, etc.) from leaking to MCP servers —
        especially important for `npx -y @something/from-npm` which
        could be a typo-squatted malicious package.
        """
        env: Dict[str, str] = {}
        for key, value in os.environ.items():
            if any(key == prefix or key.startswith(prefix) for prefix in self._ENV_WHITELIST_PREFIXES):
                env[key] = value
        # Explicit per-server env (from mcp.json) always wins.
        env.update(self.env)
        # Debug log: show which keys were forwarded (values are NOT
        # logged — they may still contain secrets from mcp.json).
        logger.debug(
            "[mcp:%s] forwarding %d env vars: %s",
            self.name, len(env), sorted(env.keys()),
        )
        return env

    def start(self) -> bool:
        """Spawn the MCP server subprocess and perform the initialize
        handshake. Returns True on success, False on failure."""
        if self.process and self.process.poll() is None:
            logger.warning("[mcp:%s] already running", self.name)
            return True

        try:
            # v1.1.3-fix (bug 3.3): use a WHITELIST of env vars to pass
            # to the MCP server, instead of inheriting all of os.environ.
            # The previous code did `full_env = dict(os.environ); full_env.update(self.env)`
            # which leaked every secret in the parent process (OPENAI_API_KEY,
            # ANTHROPIC_API_KEY, etc.) to any MCP server — including
            # `npx -y some-typo-squatting-package` from npm. We now
            # forward only the bare minimum needed for a subprocess to
            # function (PATH, HOME, TMPDIR, locale) plus anything the
            # user explicitly listed in mcp.json's "env" for this server.
            full_env = self._build_sandboxed_env()
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
                cwd=self.cwd,
                bufsize=0,  # unbuffered — we handle framing ourselves
            )
        except FileNotFoundError as e:
            logger.error("[mcp:%s] command not found: %s — %s", self.name, self.command, e)
            return False
        except Exception as e:
            logger.error("[mcp:%s] failed to start: %s", self.name, e)
            return False

        # Start the reader thread BEFORE sending initialize so we don't
        # miss the response.
        self._should_stop = False
        self._reader_thread = threading.Thread(
            target=self._read_loop, name=f"mcp-reader-{self.name}", daemon=True,
        )
        self._reader_thread.start()

        # initialize handshake
        try:
            resp = self._request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "tera_pilot",
                    "version": "1.1.0",
                },
            }, timeout=INIT_TIMEOUT)
        except Exception as e:
            logger.error("[mcp:%s] initialize failed: %s", self.name, e)
            self.stop()
            return False

        # Parse server info
        try:
            si = resp.get("result", {})
            self.server_info = MCPServerInfo(
                name=si.get("serverInfo", {}).get("name", self.name),
                version=si.get("serverInfo", {}).get("version", ""),
                protocol_version=si.get("protocolVersion", ""),
                capabilities=si.get("capabilities", {}),
                instructions=si.get("instructions", ""),
            )
        except Exception as e:
            logger.warning("[mcp:%s] failed to parse server info: %s", self.name, e)
            self.server_info = MCPServerInfo(name=self.name)

        # Send initialized notification (no response expected)
        try:
            self._notify("notifications/initialized", {})
        except Exception as e:
            logger.warning("[mcp:%s] initialized notification failed: %s", self.name, e)

        self._initialized = True
        logger.info(
            "[mcp:%s] initialized — server=%s v%s protocol=%s",
            self.name, self.server_info.name, self.server_info.version,
            self.server_info.protocol_version,
        )

        # Discover tools
        # v1.1.3-fix (bug 3.2): if tool discovery fails, return False
        # instead of True. The previous code logged a warning but still
        # returned True, which made is_running() report "running" while
        # the catalog was empty — the agent would never see MCP tools
        # in the system prompt and would have no way to know why.
        try:
            self.list_tools()
            if not self.tools:
                # Discovery returned an empty list (or the server returned
                # no tools). This is a soft failure — we don't tear down
                # the connection, but we report it so the UI can show
                # "running but no tools discovered".
                logger.warning(
                    "[mcp:%s] tool discovery returned 0 tools — server may "
                    "be misconfigured or incomplete", self.name,
                )
                self._tool_discovery_failed = True
            else:
                self._tool_discovery_failed = False
        except Exception as e:
            logger.warning("[mcp:%s] tool discovery failed: %s", self.name, e)
            self._tool_discovery_failed = True
            # v1.1.3-fix (bug 3.2): surface the failure to the caller
            # so the UI can show "start failed" instead of "running
            # with 0 tools". We do NOT call self.stop() here — the
            # caller may want to retry list_tools() without re-spawning
            # the subprocess.
            return False

        return True

    def stop(self) -> None:
        """Shutdown the MCP server cleanly. Sends `shutdown` request,
        then `exit` notification, then kills the subprocess if it's
        still running.

        v1.1.3-fix (bug 3.1): the previous implementation set
        ``_should_stop = True`` BEFORE sending the ``shutdown`` request.
        ``_should_stop`` makes the reader thread exit immediately, so
        the response to ``shutdown`` was never read — the request
        timed out after 2 seconds (which the except block swallowed),
        and the server never received a clean shutdown. We now send
        ``shutdown`` FIRST (while the reader is still alive to read
        the response), THEN set ``_should_stop`` to terminate the
        reader, THEN kill the subprocess.
        """
        if not self.process or self.process.poll() is not None:
            # v1.1.3-fix (bug 3.1): still set _should_stop so any
            # lingering reader thread exits cleanly.
            self._should_stop = True
            return

        # v1.1.3-fix (bug 3.1): send shutdown BEFORE setting _should_stop,
        # so the reader thread is still alive to read the response.
        try:
            self._request("shutdown", {}, timeout=2.0)
        except Exception:
            pass
        try:
            self._notify("exit", {})
        except Exception:
            pass

        # Now signal the reader thread to exit.
        self._should_stop = True

        # Give it a moment, then kill
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1.0)
        except Exception as e:
            logger.warning("[mcp:%s] kill failed: %s", self.name, e)
        finally:
            self.process = None
            self._initialized = False
            logger.info("[mcp:%s] stopped", self.name)

    def is_running(self) -> bool:
        return bool(self.process and self.process.poll() is None and self._initialized)

    # ── Tool discovery & invocation ────────────────────────────────

    def list_tools(self) -> List[MCPTool]:
        """Call tools/list and cache the result. Returns the list of
        MCPTool dataclasses."""
        if not self._initialized:
            return []
        try:
            resp = self._request("tools/list", {}, timeout=10.0)
            tools_raw = resp.get("result", {}).get("tools", [])
            self.tools = [
                MCPTool(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}),
                )
                for t in tools_raw
            ]
            logger.info("[mcp:%s] discovered %d tools: %s",
                        self.name, len(self.tools),
                        [t.name for t in self.tools])
            return self.tools
        except Exception as e:
            logger.error("[mcp:%s] tools/list failed: %s", self.name, e)
            self.tools = []
            return []

    def call_tool(self, tool_name: str, arguments: Dict[str, Any],
                  timeout: float = DEFAULT_TIMEOUT) -> str:
        """Call a tool on the MCP server. Returns the concatenated
        text content of the result (MCP tools may return multiple
        content blocks — we join the text ones).

        Raises TimeoutError if the server doesn't respond in time.
        Raises RuntimeError if the server returns an error.

        v1.1.3-fix (bug 3.7): validate that ``arguments`` is a dict.
        The MCP spec requires ``arguments`` to be a JSON object; an
        array or scalar produces a cryptic "invalid params" error
        from most servers. We now raise a clear ValueError before
        the round-trip.
        """
        if not self._initialized:
            raise RuntimeError(f"MCP server {self.name} not initialized")
        # v1.1.3-fix (bug 3.7): enforce MCP-spec type for arguments.
        if not isinstance(arguments, dict):
            raise ValueError(
                f"MCP arguments must be a JSON object (dict), got "
                f"{type(arguments).__name__}. Wrap array/scalar args "
                f"in a {{}} object keyed by the parameter name."
            )
        resp = self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        }, timeout=timeout)
        # v1.1.3-fix (bug 3.10): handle null result explicitly. The MCP
        # spec allows {"result": null} as a valid response, but the old
        # code's `result.get("content", [])` would AttributeError on None.
        if resp.get("error"):
            err = resp["error"]
            raise RuntimeError(f"MCP error: {err.get('message', str(err))}")
        result = resp.get("result")
        if result is None:
            # Server explicitly returned null — valid JSON-RPC, just no
            # content. Return an empty string so the agent sees "no output".
            return ""
        if not isinstance(result, dict):
            # Some servers return a bare string or array — coerce to str.
            return str(result)
        # MCP results have content: [{type: "text"|"image"|"resource", ...}]
        # We extract the text content for the LLM observation.
        content_blocks = result.get("content", [])
        text_parts: List[str] = []
        for block in content_blocks:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    # For images we just note their presence — LLM can't
                    # see inline images in this text-based agent loop.
                    text_parts.append(f"[image: {block.get('mimeType', 'unknown')}, {len(block.get('data', ''))} bytes]")
                elif block.get("type") == "resource":
                    res = block.get("resource", {})
                    text_parts.append(f"[resource: {res.get('uri', '?')}]")
        if not text_parts:
            # Some servers return plain text
            if isinstance(result, dict) and "text" in result:
                return str(result["text"])
            return json.dumps(result, default=str)
        return "\n".join(text_parts)

    # ── JSON-RPC plumbing ──────────────────────────────────────────

    def _next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _send_message(self, message: Dict[str, Any]) -> None:
        if not self.process or self.process.poll() is not None:
            raise RuntimeError(f"MCP server {self.name} not running")
        data = json.dumps(message)
        header = f"Content-Length: {len(data.encode('utf-8'))}\r\n\r\n"
        full = header + data
        with self._lock:
            try:
                self.process.stdin.write(full.encode("utf-8"))
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise RuntimeError(f"MCP server {self.name} pipe closed: {e}")

    def _read_loop(self) -> None:
        """Background thread: read framed JSON-RPC messages from the
        server's stdout and route them to pending request waiters.

        v1.1.3-fix (bug 3.4): the previous implementation called
        ``self.process.stdout.read(1)`` in a tight loop, which blocks
        indefinitely if the server doesn't send data. If the server
        hung after sending part of a header, the reader thread never
        exited — even after ``_should_stop=True``. We now use
        ``select.select()`` with a 0.5s timeout on POSIX so the loop
        wakes up periodically and can check ``_should_stop``. On
        Windows (where select on pipes is unreliable), we fall back to
        the blocking read but at least check ``_should_stop`` between
        bytes (slower but still responsive to cancellation).
        """
        import select as _select
        # v1.1.3-fix (bug 3.4): on Windows, select() doesn't work on
        # pipes — fall back to blocking read with periodic cancel checks.
        use_select = hasattr(_select, "select") and self.process is not None
        try:
            stdout_fd = self.process.stdout.fileno() if use_select else None
        except (AttributeError, ValueError, OSError):
            use_select = False
            stdout_fd = None

        while not self._should_stop and self.process and self.process.poll() is None:
            try:
                # v1.1.3-fix (bug 3.4): wait up to 0.5s for data, then
                # re-check _should_stop. This makes the reader responsive
                # to cancellation even if the server stops sending.
                if use_select:
                    ready, _, _ = _select.select([stdout_fd], [], [], 0.5)
                    if not ready:
                        continue  # timeout — re-check _should_stop
                # Read Content-Length header
                header = b""
                while True:
                    if use_select:
                        ready, _, _ = _select.select([stdout_fd], [], [], 0.5)
                        if not ready:
                            # No data within 0.5s mid-header — check cancel
                            if self._should_stop:
                                return
                            continue
                    byte = self.process.stdout.read(1)
                    if not byte:
                        return  # EOF
                    header += byte
                    if header.endswith(b"\r\n\r\n"):
                        break
                    if len(header) > 1024:
                        # Header too long — something is wrong
                        logger.error("[mcp:%s] header too long: %r", self.name, header[:200])
                        return
                    # v1.1.3-fix (bug 3.4): check cancel between bytes
                    # so the reader exits even on the no-select path.
                    if self._should_stop and not use_select:
                        return

                content_length = 0
                for line in header.decode("utf-8", errors="replace").strip().split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        try:
                            content_length = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            pass

                if content_length <= 0:
                    continue

                body = self.process.stdout.read(content_length)
                if len(body) < content_length:
                    return  # truncated — connection closing

                try:
                    msg = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError as e:
                    logger.warning("[mcp:%s] invalid JSON: %s", self.name, e)
                    continue

                # Route to waiter
                msg_id = msg.get("id")
                if msg_id is None:
                    # Notification — log and ignore (we don't subscribe to any)
                    logger.debug("[mcp:%s] notification: %s", self.name, msg.get("method"))
                    continue

                with self._lock:
                    entry = self._pending.get(msg_id)
                    if entry:
                        entry["result"] = msg
                        entry["event"].set()
            except Exception as e:
                if not self._should_stop:
                    logger.warning("[mcp:%s] reader error: %s", self.name, e)
                return

    def _request(self, method: str, params: Dict[str, Any],
                 timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for the response.

        Returns the full response message dict (including ``result``,
        ``error``, and ``id``). Callers should check ``error`` before
        using ``result``.

        v1.1.3-fix (bug 3.10): the previous implementation returned
        ``entry["result"] or {}``, which masked a legitimate
        ``{"result": null}`` response (valid in JSON-RPC 2.0) as an
        empty dict. Callers then tried to extract fields from the
        empty dict and silently returned empty strings. We now return
        the full response dict so callers can distinguish "server
        returned null" from "server hasn't responded".
        """
        req_id = self._next_id()
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        entry: Dict[str, Any] = {"event": threading.Event(), "result": None}
        with self._lock:
            self._pending[req_id] = entry
        self._send_message(msg)
        if not entry["event"].wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"MCP {self.name} {method} timed out after {timeout}s")
        with self._lock:
            self._pending.pop(req_id, None)
        # v1.1.3-fix (bug 3.10): return the full response dict instead
        # of ``entry["result"] or {}``. The ``or {}`` masked null
        # responses; callers now get the actual server response and
        # can check for "result" in resp / resp.get("error") properly.
        resp = entry["result"]
        if resp is None:
            # Server didn't respond at all (shouldn't happen — the
            # event was set, so SOMETHING was written). Return an
            # explicit error response so callers don't crash on None.
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": f"No response from MCP server {self.name}",
                },
            }
        return resp

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._send_message(msg)


# ── Helper: build a tool-schema string for the system prompt ────────────

def format_mcp_tool_for_prompt(server_name: str, tool: MCPTool) -> str:
    """Format a single MCP tool as a JSON example for the system prompt,
    matching the style of TOOL_SCHEMA in agent_runtime.py.

    Example output:
      {"tool": "call_mcp_tool", "args": {"server": "filesystem", "tool": "read_file", "args": {"path": "<value>"}}}
      filesystem.read_file: Read a file from the filesystem.

    v1.1.3-fix (bug 3.8): the previous implementation built the args
    hint by string concatenation (``"{" + ", ".join(...) + "}"``),
    which produced invalid JSON if a property name contained a quote
    or backslash. We now use ``json.dumps()`` so all escaping is
    handled correctly.
    """
    # v1.1.3-fix (bug 3.8): use json.dumps for the args hint so property
    # names with special characters (quotes, backslashes, control chars)
    # are escaped correctly.
    try:
        props = tool.input_schema.get("properties", {}) if tool.input_schema else {}
        if props:
            # Build {"key": "<value>"} for each property — <value> is a
            # placeholder string the model replaces with the actual value.
            args_hint = json.dumps(
                {k: "<value>" for k in props.keys()},
                ensure_ascii=False,
            )
        else:
            args_hint = "{}"
    except Exception:
        args_hint = "{}"
    desc = (tool.description or "").strip().split("\n")[0][:200]
    # Build the outer JSON object via dict so all parts are properly escaped.
    # We use a sentinel value for the args hint, then string-replace the
    # quoted sentinel with the raw args_hint object (so the final text
    # shows a nested JSON object, not a stringified hint).
    sentinel = "__ARGS_HINT_SENTINEL__"
    outer = {
        "tool": "call_mcp_tool",
        "args": {
            "server": server_name,
            "tool": tool.name,
            "args": sentinel,
        },
    }
    outer_str = json.dumps(outer, ensure_ascii=False)
    # Replace the quoted sentinel with the raw args hint object text.
    outer_str = outer_str.replace(f'"{sentinel}"', args_hint)
    return f"{outer_str}\n  {server_name}.{tool.name}: {desc}"
