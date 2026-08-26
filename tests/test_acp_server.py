"""Regression tests for the ACP (Agent Client Protocol) server.

Covers:
  - ``_run_and_stream`` must iterate ``AgentRuntime.run_stream`` (a SYNC
    generator of text chunks) without crashing. The old ``async for`` over
    the sync generator raised ``TypeError: 'async for' requires an object
    with __aiter__`` on every ``prompt/send`` with the real runtime.
  - ``cli_main``'s ``--no-confirm`` handling must not raise NameError:
    the env var is set AFTER ``import os`` (previously ``os`` was used
    before the import).

No LLM or network calls — everything runs against fake runtimes.
"""

import asyncio
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.agent.acp_server import (  # noqa: E402
    ACPServer,
    _run_and_stream,
)


# ── _run_and_stream: sync generator support ─────────────────────────


class _SyncStreamRuntime:
    """Mimics AgentRuntime.run_stream: a SYNC generator of text chunks."""

    def run_stream(self, prompt: str):
        yield f"thinking about: {prompt} "
        yield "and more"


class _LegacyRuntime:
    """Mimics a runtime without run_stream (falls back to run())."""

    def run(self, prompt: str):
        return f"RESULT:{prompt}"


@pytest.mark.asyncio
async def test_run_and_stream_iterates_sync_generator():
    events = []
    async for ev in _run_and_stream(_SyncStreamRuntime(), "hi"):
        events.append(ev)
    # Text chunks must be wrapped in session/update-friendly events.
    assert events == [
        {"type": "chunk", "text": "thinking about: hi "},
        {"type": "chunk", "text": "and more"},
    ]


@pytest.mark.asyncio
async def test_run_and_stream_falls_back_to_run():
    events = []
    async for ev in _run_and_stream(_LegacyRuntime(), "hi"):
        events.append(ev)
    assert events == [{"type": "result", "result": "RESULT:hi"}]


# ── cli_main --no-confirm: no NameError ──────────────────────────────


def test_cli_main_no_confirm_does_not_crash():
    """--no-confirm must be stripped and the env var set — the code
    path previously used ``os.environ`` before ``import os``."""
    import tera_pilot.agent.acp_server as mod

    env_before = os.environ.get("TERA_PILOT_ACP_NO_CONFIRM")

    # cli_main with --no-confirm dispatches to asyncio.run(server.run_stdio()),
    # which blocks on stdin — so we can't call it directly. Instead assert
    # the ordering invariant that used to be violated: the module-level
    # source must import os before the --no-confirm block.
    src = Path(mod.__file__).read_text(encoding="utf-8")
    import_pos = src.index("import os")
    no_confirm_pos = src.index('"--no-confirm" in args')
    assert import_pos < no_confirm_pos, (
        "cli_main uses os.environ before importing os → NameError on "
        "--no-confirm (the original bug)"
    )

    if env_before is None:
        os.environ.pop("TERA_PILOT_ACP_NO_CONFIRM", None)


# ── ACPServer prompt/send end-to-end (fake runtime) ──────────────────


@pytest.mark.asyncio
async def test_prompt_send_streams_chunks_and_turn_end():
    """A prompt/send against a run_stream runtime must yield chunk events
    and a final turn_end without raising."""
    async def _factory(sid, cwd):
        return _SyncStreamRuntime()

    server = ACPServer(runtime_factory=_factory)

    # Writer backed by an in-memory sink so no real pipes are needed.
    sink = _Sink()
    writer = _Writer(sink)

    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    })
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/tmp"},
    })
    sid = next(iter(server._sessions))
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 3, "method": "prompt/send",
        "params": {"sessionId": sid, "prompt": "do a thing"},
    })

    out = sink.buffer.getvalue()
    lines = [l for l in out.splitlines() if l.strip()]
    # id 3 must get the ack.
    ack = next(l for l in lines if '"id": 3' in l)
    assert '"ok": true' in ack
    # Streaming must emit chunk notifications + a final turn_end.
    assert '"method": "session/update"' in out
    assert '"type": "chunk"' in out
    assert '"type": "turn_end"' in out
    # No internal-error response for the prompt.
    assert "-32603" not in out


# ── More protocol surface: session/info, session/load, turn/cancel ──

class _Sink:
    """In-memory response sink so no real pipes are needed."""

    def __init__(self):
        self.buffer = io.StringIO()

    def write(self, data):
        self.buffer.write(data.decode("utf-8") if isinstance(data, bytes) else data)


class _Writer:
    def __init__(self, sink):
        self.sink = sink

    def write(self, data):
        self.sink.write(data)

    async def drain(self):
        return None


async def _new_session(server, writer) -> str:
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    })
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/tmp"},
    })
    return next(iter(server._sessions))


def _messages(server, writer) -> list:
    return [l for l in writer.sink.buffer.getvalue().splitlines() if l.strip()]


@pytest.mark.asyncio
async def test_session_info_reports_state():
    async def _factory(sid, cwd):
        return _SyncStreamRuntime()

    server = ACPServer(runtime_factory=_factory)
    sink = _Sink()
    writer = _Writer(sink)
    sid = await _new_session(server, writer)
    sink.buffer.seek(0); sink.buffer.truncate()

    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 3, "method": "session/info", "params": {"sessionId": sid},
    })
    out = sink.buffer.getvalue()
    assert f'"sessionId": "{sid}"' in out
    assert '"cwd": "/tmp"' in out
    assert '"turnInFlight": false' in out


@pytest.mark.asyncio
async def test_session_info_unknown_session_errors():
    async def _factory(sid, cwd):
        return _SyncStreamRuntime()

    server = ACPServer(runtime_factory=_factory)
    sink = _Sink()
    writer = _Writer(sink)
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 1, "method": "session/info", "params": {"sessionId": "nope"},
    })
    assert '"error"' in sink.buffer.getvalue()
    assert '-32602' in sink.buffer.getvalue()


@pytest.mark.asyncio
async def test_session_load_ok_and_missing():
    async def _factory(sid, cwd):
        return _SyncStreamRuntime()

    server = ACPServer(runtime_factory=_factory)
    sink = _Sink()
    writer = _Writer(sink)
    sid = await _new_session(server, writer)
    sink.buffer.seek(0); sink.buffer.truncate()

    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 3, "method": "session/load", "params": {"sessionId": sid},
    })
    assert f'"sessionId": "{sid}"' in sink.buffer.getvalue()

    sink.buffer.seek(0); sink.buffer.truncate()
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 4, "method": "session/load", "params": {"sessionId": "missing"},
    })
    assert '-32602' in sink.buffer.getvalue()


@pytest.mark.asyncio
async def test_turn_cancel_returns_ok_even_without_runtime():
    async def _factory(sid, cwd):
        return _SyncStreamRuntime()

    server = ACPServer(runtime_factory=_factory)
    sink = _Sink()
    writer = _Writer(sink)
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 1, "method": "turn/cancel", "params": {"sessionId": "unknown"},
    })
    assert '"ok": true' in sink.buffer.getvalue()


@pytest.mark.asyncio
async def test_prompt_send_unknown_session_errors():
    async def _factory(sid, cwd):
        return _SyncStreamRuntime()

    server = ACPServer(runtime_factory=_factory)
    sink = _Sink()
    writer = _Writer(sink)
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 1, "method": "prompt/send",
        "params": {"sessionId": "nope", "prompt": "hi"},
    })
    assert '-32602' in sink.buffer.getvalue()


@pytest.mark.asyncio
async def test_unknown_method_returns_error():
    async def _factory(sid, cwd):
        return _SyncStreamRuntime()

    server = ACPServer(runtime_factory=_factory)
    sink = _Sink()
    writer = _Writer(sink)
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "id": 1, "method": "bogus/method", "params": {},
    })
    assert '-32601' in sink.buffer.getvalue()


@pytest.mark.asyncio
async def test_notification_without_id_gets_no_response():
    async def _factory(sid, cwd):
        return _SyncStreamRuntime()

    server = ACPServer(runtime_factory=_factory)
    sink = _Sink()
    writer = _Writer(sink)
    # A notification (no id) — e.g. an unknown method — must NOT get a
    # response, per JSON-RPC 2.0.
    await server._handle_message(writer, {
        "jsonrpc": "2.0", "method": "bogus/method", "params": {},
    })
    assert sink.buffer.getvalue() == ""
