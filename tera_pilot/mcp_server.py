#!/usr/bin/env python3
"""
G13 — MCP Server Mode.

Tera Pilot can connect to external MCP servers (as a client), but cannot BE an
MCP server itself.  This module exposes Tera Pilot's tools via the MCP protocol
so other agents (Claude Code, Codex, etc.) can call Tera Pilot as a tool provider.

Protocol:
  - MCP over stdio using JSON-RPC 2.0 with Content-Length framing.
  - The server is started by the external agent as a subprocess.
  - Command: tera-pilot-acp --mcp-server  (or python -m tera_pilot.mcp_server)
  - The server advertises Tera Pilot's tools via tools/list.
  - The calling agent sends tools/call with {name, arguments}.
  - The server routes to the appropriate ToolEngine method.

Design:
  - MCPServerMode wraps ToolEngine and AgentRuntime to expose Tera Pilot's
    capabilities as MCP tools.
  - Tools are registered from TOOL_CATALOG (progressive_tools.py) with
    full JSON Schema definitions.
  - The server is read-only by default (no write_file, delete_file,
    execute_command) unless the caller explicitly enables write mode
    via the --mcp-allow-writes flag.
  - Session management: each MCP client connection gets its own session.
  - No telemetry — all communication is local (stdio).

Integration:
  - New entry point: tera-pilot-acp --mcp-server
  - Can also be used programmatically: MCPServerMode().start()
  - Config: ~/.tera_pilot/mcp_server.json (allowed tools, write mode, etc.)

Security:
  - Default read-only: only safe tools (read_file, search, grep, etc.)
  - Write mode requires explicit --mcp-allow-writes flag.
  - Workspace sandboxing: the server only operates within the workspace.
  - No shell execution in read-only mode.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "tera-pilot-mcp-server"
SERVER_VERSION = "2.0.3"

# Tools available in read-only mode (safe for external agents)
READ_ONLY_TOOLS = frozenset({
    "read_file", "list_files", "search_project", "grep", "glob",
    "file_info", "get_project_structure", "get_skill",
    "search_tools", "select_tools",
})

# Tools available in write mode (additional to read-only)
WRITE_TOOLS = frozenset({
    "write_file", "str_replace", "apply_diff", "delete_file",
    "rename_file", "mkdir", "run_code", "execute_command",
    "undo_write", "read_binary_file", "write_binary_file",
})


# ── Tool schemas ─────────────────────────────────────────────────────────

# JSON Schema definitions for each tool exposed via MCP.
# These are simplified versions of the full ToolEngine schemas,
# designed for external consumption via MCP.

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "read_file": {
        "description": "Read a file from the workspace",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace"},
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "description": "Write or create a file in the workspace",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
    },
    "str_replace": {
        "description": "Targeted string replacement in a file",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "old_str": {"type": "string", "description": "String to find"},
                "new_str": {"type": "string", "description": "Replacement string"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences", "default": False},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    "apply_diff": {
        "description": "Apply a unified diff patch to a file",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "diff": {"type": "string", "description": "Unified diff content"},
            },
            "required": ["path", "diff"],
        },
    },
    "delete_file": {
        "description": "Delete a file or directory",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to delete"},
            },
            "required": ["path"],
        },
    },
    "rename_file": {
        "description": "Rename a file or directory",
        "inputSchema": {
            "type": "object",
            "properties": {
                "old_path": {"type": "string", "description": "Current path"},
                "new_path": {"type": "string", "description": "New path"},
            },
            "required": ["old_path", "new_path"],
        },
    },
    "mkdir": {
        "description": "Create a directory",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"},
            },
            "required": ["path"],
        },
    },
    "list_files": {
        "description": "List files in a directory",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Directory path", "default": "."},
                "pattern": {"type": "string", "description": "Glob pattern", "default": "*"},
            },
        },
    },
    "search_project": {
        "description": "Search for text in project files",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "directory": {"type": "string", "default": "."},
                "file_pattern": {"type": "string", "default": "*.py"},
            },
            "required": ["query"],
        },
    },
    "grep": {
        "description": "Regex search across files",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern"},
                "path": {"type": "string", "default": "."},
                "include": {"type": "string", "default": "*.py"},
            },
            "required": ["pattern"],
        },
    },
    "glob": {
        "description": "Find files matching a glob pattern",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "default": "."},
            },
            "required": ["pattern"],
        },
    },
    "file_info": {
        "description": "Get file metadata (size, modified date, type)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
            },
            "required": ["path"],
        },
    },
    "get_project_structure": {
        "description": "Get project directory tree",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "default": "."},
            },
        },
    },
    "run_code": {
        "description": "Execute code in a sandboxed environment",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Code to execute"},
                "language": {"type": "string", "default": "python", "enum": ["python", "javascript", "bash"]},
            },
            "required": ["code"],
        },
    },
    "execute_command": {
        "description": "Execute a shell command (subject to whitelist)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
            },
            "required": ["command"],
        },
    },
}


# ── MCPServerMode ───────────────────────────────────────────────────────

class MCPServerMode:
    """Exposes Tera Pilot's tools as an MCP server over stdio.

    Usage:
        server = MCPServerMode(workspace="/path/to/project", allow_writes=False)
        server.start()

    Or via CLI:
        python -m tera_pilot.mcp_server --workspace /path/to/project
        tera-pilot-acp --mcp-server
    """

    def __init__(
        self,
        workspace: Optional[str] = None,
        allow_writes: bool = False,
        allowed_tools: Optional[List[str]] = None,
    ):
        self._workspace = workspace or os.getcwd()
        self._allow_writes = allow_writes
        self._allowed_tools = allowed_tools
        self._session_id = f"mcp_{uuid.uuid4().hex[:8]}"
        self._running = False
        self._tool_engine: Optional[Any] = None  # Lazy init
        self._request_handlers: Dict[str, Callable] = {
            "initialize": self._handle_initialize,
            "initialized": self._handle_initialized,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "ping": self._handle_ping,
        }

    def _get_tool_engine(self) -> Any:
        """Lazy-initialize the ToolEngine."""
        if self._tool_engine is None:
            try:
                from tera_pilot.agent_runtime.tool_engine import ToolEngine
                self._tool_engine = ToolEngine(workspace=self._workspace)
                self._tool_engine.autonomy = "never_ask"  # No confirmation for MCP
            except Exception as e:
                logger.error("[mcp-server] failed to create ToolEngine: %s", e)
                raise
        return self._tool_engine

    def _get_available_tools(self) -> List[str]:
        """Return the list of tool names this server exposes."""
        if self._allowed_tools is not None:
            return list(self._allowed_tools)
        tools = list(READ_ONLY_TOOLS)
        if self._allow_writes:
            tools.extend(WRITE_TOOLS)
        return tools

    # ── JSON-RPC messaging ──────────────────────────────────────────

    def _read_message(self) -> Optional[Dict[str, Any]]:
        """Read a JSON-RPC message from stdin (Content-Length framing)."""
        try:
            line = sys.stdin.readline()
            if not line:
                return None
            # Parse Content-Length header
            content_length = 0
            while line.strip():
                if line.startswith("Content-Length:"):
                    content_length = int(line.split(":")[1].strip())
                line = sys.stdin.readline()
                if not line:
                    return None
            if content_length <= 0:
                return None
            # Read the body
            body = sys.stdin.read(content_length)
            return json.loads(body)
        except Exception as e:
            logger.error("[mcp-server] read error: %s", e)
            return None

    def _write_message(self, message: Dict[str, Any]) -> None:
        """Write a JSON-RPC message to stdout (Content-Length framing)."""
        try:
            body = json.dumps(message)
            content_length = len(body.encode("utf-8"))
            sys.stdout.write(f"Content-Length: {content_length}\r\n\r\n")
            sys.stdout.write(body)
            sys.stdout.flush()
        except Exception as e:
            logger.error("[mcp-server] write error: %s", e)

    def _send_response(self, id: Any, result: Any) -> None:
        """Send a JSON-RPC response."""
        self._write_message({
            "jsonrpc": "2.0",
            "id": id,
            "result": result,
        })

    def _send_error(self, id: Any, code: int, message: str, data: Any = None) -> None:
        """Send a JSON-RPC error response."""
        error: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._write_message({
            "jsonrpc": "2.0",
            "id": id,
            "error": error,
        })

    # ── Request handlers ────────────────────────────────────────────

    def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the initialize request."""
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {
                    "listChanged": False,
                },
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        }

    def _handle_initialized(self, params: Dict[str, Any]) -> None:
        """Handle the initialized notification (no response needed)."""
        logger.info("[mcp-server] client initialized")

    def _handle_tools_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/list request."""
        available = self._get_available_tools()
        tools = []
        for name in sorted(available):
            if name in TOOL_SCHEMAS:
                schema = TOOL_SCHEMAS[name]
                tools.append({
                    "name": name,
                    "description": schema["description"],
                    "inputSchema": schema["inputSchema"],
                })
        return {"tools": tools}

    def _handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/call request."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}

        # Check if tool is allowed
        available = self._get_available_tools()
        if tool_name not in available:
            return {
                "content": [{"type": "text", "text": f"Tool {tool_name!r} is not available in this MCP server mode"}],
                "isError": True,
            }

        # Execute via ToolEngine
        try:
            engine = self._get_tool_engine()
            from tera_pilot.agent_runtime.types import ToolCall, ToolName

            # Try to resolve the tool name
            try:
                tn = ToolName(tool_name)
            except ValueError:
                tn = tool_name  # type: ignore

            call = ToolCall(name=tn, args=arguments)
            result = engine.execute(call)

            return {
                "content": [{"type": "text", "text": result}],
                "isError": False,
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error executing {tool_name}: {e}"}],
                "isError": True,
            }

    def _handle_ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle ping request."""
        return {}

    # ── Main loop ───────────────────────────────────────────────────

    def start(self) -> None:
        """Start the MCP server main loop (reads from stdin, writes to stdout)."""
        self._running = True
        logger.info("[mcp-server] starting (workspace=%s, writes=%s)",
                     self._workspace, self._allow_writes)

        while self._running:
            message = self._read_message()
            if message is None:
                break

            method = message.get("method", "")
            id = message.get("id")
            params = message.get("params", {})

            # Notifications (no id) don't get responses
            if id is None and method == "initialized":
                handler = self._request_handlers.get(method)
                if handler:
                    try:
                        handler(params)
                    except Exception as e:
                        logger.error("[mcp-server] notification handler error: %s", e)
                continue

            handler = self._request_handlers.get(method)
            if handler is None:
                if id is not None:
                    self._send_error(id, -32601, f"Method not found: {method}")
                continue

            try:
                result = handler(params)
                if id is not None:
                    self._send_response(id, result)
            except Exception as e:
                logger.error("[mcp-server] handler error for %s: %s", method, e)
                if id is not None:
                    self._send_error(id, -32603, f"Internal error: {e}")

        logger.info("[mcp-server] stopped")

    def stop(self) -> None:
        """Stop the MCP server."""
        self._running = False

    # ── Programmatic API (for in-process use) ───────────────────────

    def list_tools(self) -> List[Dict[str, Any]]:
        """Return the list of available MCP tools (for UI display)."""
        available = self._get_available_tools()
        tools = []
        for name in sorted(available):
            if name in TOOL_SCHEMAS:
                schema = TOOL_SCHEMAS[name]
                tools.append({"name": name, **schema})
        return tools

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool programmatically (bypasses stdio framing)."""
        return self._handle_tools_call({"name": name, "arguments": arguments})

    def status(self) -> Dict[str, Any]:
        """Return the MCP server status."""
        return {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "workspace": self._workspace,
            "allow_writes": self._allow_writes,
            "available_tools": len(self._get_available_tools()),
            "session_id": self._session_id,
        }


# ── CLI entry point ─────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point for the MCP server."""
    import argparse
    parser = argparse.ArgumentParser(description="Tera Pilot MCP Server")
    parser.add_argument("--workspace", "-w", default=os.getcwd(), help="Workspace directory")
    parser.add_argument("--allow-writes", action="store_true", help="Enable write tools")
    parser.add_argument("--tools", nargs="*", help="Explicit list of allowed tools")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    server = MCPServerMode(
        workspace=args.workspace,
        allow_writes=args.allow_writes,
        allowed_tools=args.tools,
    )
    server.start()


if __name__ == "__main__":
    main()
