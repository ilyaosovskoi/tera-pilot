"""Tests for tera_pilot.mcp_server — Tera Pilot AS an MCP server.

Covers:
  - read-only mode: read tools listed, write tools absent, write tool
    calls rejected with isError.
  - write mode: write tools added.
  - tools/call dispatches through the real ToolEngine (file content is
    read from the workspace).
  - stdio Content-Length framing round-trip: initialize → tools/list →
    tools/call produces the expected JSON-RPC responses.
  - unknown method → -32601 JSON-RPC error.

No network, no subprocess.
"""

import io
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.mcp_server import MCPServerMode  # noqa: E402


@pytest.fixture()
def workspace():
    ws = tempfile.mkdtemp(prefix="mcp-server-mode-")
    Path(ws, "hello.txt").write_text("world", encoding="utf-8")
    return ws


# ── tool catalog filtering ────────────────────────────────────────────


def test_read_only_mode_excludes_write_tools(workspace):
    server = MCPServerMode(workspace=workspace, allow_writes=False)
    names = {t["name"] for t in server.list_tools()}
    assert "read_file" in names
    assert "list_files" in names
    assert "write_file" not in names
    assert "execute_command" not in names
    assert "delete_file" not in names


def test_write_mode_adds_write_tools(workspace):
    server = MCPServerMode(workspace=workspace, allow_writes=True)
    names = {t["name"] for t in server.list_tools()}
    assert "read_file" in names
    assert "write_file" in names
    assert "execute_command" in names


def test_explicit_allowed_tools_overrides(workspace):
    server = MCPServerMode(workspace=workspace, allow_writes=False,
                           allowed_tools=["read_file"])
    names = {t["name"] for t in server.list_tools()}
    assert names == {"read_file"}


# ── tools/call dispatch ───────────────────────────────────────────────


def test_call_read_file_returns_content(workspace):
    server = MCPServerMode(workspace=workspace, allow_writes=False)
    r = server.call_tool("read_file", {"path": "hello.txt"})
    assert r["isError"] is False
    assert r["content"][0]["text"] == "world"


def test_call_write_file_blocked_in_read_only(workspace):
    server = MCPServerMode(workspace=workspace, allow_writes=False)
    r = server.call_tool("write_file", {"path": "x.txt", "content": "x"})
    assert r["isError"] is True
    assert "not available" in r["content"][0]["text"]
    assert not Path(workspace, "x.txt").exists()


def test_call_unknown_tool_reports_error(workspace):
    server = MCPServerMode(workspace=workspace, allow_writes=False)
    r = server.call_tool("no_such_tool", {})
    assert r["isError"] is True
    assert "not available" in r["content"][0]["text"]


# ── stdio framing round-trip ──────────────────────────────────────────


def _framed(messages):
    out = b""
    for m in messages:
        body = json.dumps(m).encode("utf-8")
        out += f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    return out


def test_stdio_round_trip_initialize_list_call(workspace):
    server = MCPServerMode(workspace=workspace, allow_writes=False)
    incoming = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "read_file", "arguments": {"path": "hello.txt"}}},
    ]
    fake_stdin = io.TextIOWrapper(io.BytesIO(_framed(incoming)), encoding="utf-8")
    fake_stdout = io.StringIO()

    with mock.patch("sys.stdin", fake_stdin), mock.patch("sys.stdout", fake_stdout):
        server.start()

    out = fake_stdout.getvalue()
    assert "Content-Length:" in out
    assert '"serverInfo"' in out
    assert '"tools"' in out
    assert '"world"' in out, "tools/call over stdio must return the file content"


def test_stdio_unknown_method_returns_error(workspace):
    server = MCPServerMode(workspace=workspace, allow_writes=False)
    incoming = [
        {"jsonrpc": "2.0", "id": 9, "method": "bogus/method", "params": {}},
    ]
    fake_stdin = io.TextIOWrapper(io.BytesIO(_framed(incoming)), encoding="utf-8")
    fake_stdout = io.StringIO()

    with mock.patch("sys.stdin", fake_stdin), mock.patch("sys.stdout", fake_stdout):
        server.start()

    out = fake_stdout.getvalue()
    assert '"code": -32601' in out
    assert "Method not found" in out


def test_stdio_initialized_notification_gets_no_response(workspace):
    """A notification (no id) must not receive a response — and the
    server must keep running afterwards (the next request still works)."""
    server = MCPServerMode(workspace=workspace, allow_writes=False)
    incoming = [
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}},
    ]
    fake_stdin = io.TextIOWrapper(io.BytesIO(_framed(incoming)), encoding="utf-8")
    fake_stdout = io.StringIO()

    with mock.patch("sys.stdin", fake_stdin), mock.patch("sys.stdout", fake_stdout):
        server.start()

    out = fake_stdout.getvalue()
    # Only ONE response (to the ping); the notification got none.
    assert out.count("Content-Length:") == 1
    assert '"result": {}' in out
