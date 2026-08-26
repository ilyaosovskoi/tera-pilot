"""Unit tests for tera_pilot.mcp_client — no subprocess spawning.

Covers pure logic and error paths that don't need a live MCP server:
- ``_build_sandboxed_env``: secret env vars are NOT forwarded; the
  whitelist + explicit per-server env is.
- ``format_mcp_tool_for_prompt``: JSON escaping of property names with
  quotes/backslashes, empty-schema fallback.
- ``call_tool``: uninitialized → RuntimeError, non-dict args →
  ValueError, null result, bare-string result, content-block result.
- ``_request``: timeout, no-response edge cases (via a stub stdin).
- ``stop`` on a dead process is a no-op.

No network, no subprocess, no LLM.
"""

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.mcp_client import (  # noqa: E402
    MCPClient,
    MCPTool,
    format_mcp_tool_for_prompt,
)


def _client(**kwargs) -> MCPClient:
    kwargs.setdefault("name", "test")
    kwargs.setdefault("command", ["npx", "-y", "some-server"])
    return MCPClient(**kwargs)


# ═══════════════════════════════════════════════════════════════════
# Env sandboxing (_build_sandboxed_env)
# ═══════════════════════════════════════════════════════════════════

def test_env_sandbox_drops_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/u")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    env = _client()._build_sandboxed_env()
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env.get("PATH") == "/usr/bin:/bin"
    assert env.get("HOME") == "/home/u"
    assert env.get("LANG") == "en_US.UTF-8"


def test_env_sandbox_keeps_explicit_server_env():
    client = _client(env={"GITHUB_TOKEN": "explicit", "CUSTOM": "1"})
    env = client._build_sandboxed_env()
    # Explicit per-server env always wins, even for a "secret" name.
    assert env["GITHUB_TOKEN"] == "explicit"
    assert env["CUSTOM"] == "1"


def test_env_sandbox_whitelist_prefix_lc():
    """LC_* locale vars are forwarded by prefix."""
    client = _client()
    os.environ["LC_ALL"] = "C"
    env = client._build_sandboxed_env()
    assert env.get("LC_ALL") == "C"


# ═══════════════════════════════════════════════════════════════════
# format_mcp_tool_for_prompt
# ═══════════════════════════════════════════════════════════════════

def test_format_tool_escapes_quoted_property_names():
    tool = MCPTool(
        name="run",
        description="Run a thing",
        input_schema={
            "type": "object",
            "properties": {
                'weird"name': {"type": "string"},
                "back\\slash": {"type": "string"},
            },
        },
    )
    out = format_mcp_tool_for_prompt("filesystem", tool)
    # The args hint must be valid JSON — parse the embedded object.
    import json
    # The hint is `{"weird\"name": "<value>", ...}` inside the outer JSON.
    assert '"weird\\"name": "<value>"' in out
    assert '"back\\\\slash": "<value>"' in out
    assert out.startswith('{"tool": "call_mcp_tool"')


def test_format_tool_no_schema_fallback():
    tool = MCPTool(name="ping", description="Ping")
    out = format_mcp_tool_for_prompt("filesystem", tool)
    assert '"args": {}' in out
    assert "filesystem.ping: Ping" in out


def test_format_tool_truncates_long_description():
    tool = MCPTool(name="x", description="word " * 100)
    out = format_mcp_tool_for_prompt("srv", tool)
    assert len(out) < 300


# ═══════════════════════════════════════════════════════════════════
# call_tool argument validation
# ═══════════════════════════════════════════════════════════════════

def test_call_tool_uninitialized_raises():
    client = _client()
    with pytest.raises(RuntimeError, match="not initialized"):
        client.call_tool("read_file", {})


def test_call_tool_non_dict_args_raises():
    client = _client()
    client._initialized = True
    with pytest.raises(ValueError, match="must be a JSON object"):
        client.call_tool("read_file", ["a", "b"])


def test_call_tool_null_result():
    """A valid {\"result\": null} response must return \"\" — not crash."""
    client = _client()
    client._initialized = True
    client._request = lambda method, params, timeout=30.0: {
        "jsonrpc": "2.0", "id": 1, "result": None,
    }
    assert client.call_tool("x", {}) == ""


def test_call_tool_bare_string_result():
    client = _client()
    client._initialized = True
    client._request = lambda method, params, timeout=30.0: {
        "jsonrpc": "2.0", "id": 1, "result": "plain text",
    }
    assert client.call_tool("x", {}) == "plain text"


def test_call_tool_content_blocks_joined():
    client = _client()
    client._initialized = True
    client._request = lambda method, params, timeout=30.0: {
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
                {"type": "image", "mimeType": "image/png", "data": "aGk="},
            ],
        },
    }
    assert client.call_tool("x", {}) == "first\nsecond\n[image: image/png, 4 bytes]"


def test_call_tool_error_response_raises():
    client = _client()
    client._initialized = True
    client._request = lambda method, params, timeout=30.0: {
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32602, "message": "invalid params"},
    }
    with pytest.raises(RuntimeError, match="invalid params"):
        client.call_tool("x", {})


def test_call_tool_unknown_content_type_jsonified():
    client = _client()
    client._initialized = True
    client._request = lambda method, params, timeout=30.0: {
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "audio", "data": "..."}]},
    }
    # No text blocks → fall back to JSON dump of the result.
    out = client.call_tool("x", {})
    assert "audio" in out


# ═══════════════════════════════════════════════════════════════════
# JSON-RPC plumbing
# ═══════════════════════════════════════════════════════════════════

def test_request_timeout_cleans_pending():
    client = _client()
    client.process = object()  # stub — only _send_message is reached
    # Stub _send_message to do nothing; the event will never be set.
    client._send_message = lambda msg: None
    with pytest.raises(TimeoutError):
        client._request("tools/list", {}, timeout=0.01)
    assert client._pending == {}


def test_request_no_response_returns_error_dict():
    """If the event is set but no result was written, return an error
    dict instead of crashing on None."""
    client = _client()
    client.process = object()

    def _fake_send(msg):
        # Simulate the reader thread setting the event for this id
        # WITHOUT ever writing a result.
        rid = msg["id"]
        client._pending[rid]["event"].set()

    client._send_message = _fake_send
    resp = client._request("x", {}, timeout=1.0)
    assert "error" in resp
    assert resp["error"]["code"] == -32603


def test_stop_on_dead_process_noop():
    client = _client()
    client.process = None
    client._should_stop = False
    client.stop()  # must not raise
    assert client._should_stop


def test_is_running_false_when_not_started():
    client = _client()
    assert not client.is_running()
