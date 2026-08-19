"""Regression test for the SSE "success reported as timeout" bug (v2.3.4).

Root cause (api_server.py): ``_handle_agent_stream`` started a background
thread and returned immediately. ``BaseHTTPRequestHandler.handle()`` had
already evaluated ``close_connection`` (False — keep-alive) and moved on to
the next ``handle_one_request()``, which blocked in ``rfile.readline()``.
The ``close_connection = True`` set by the stream thread's ``finally`` was
therefore never read: after a successful `done` event the socket stayed
open, and a client reading until EOF (like eval/runner.py) hung until ITS
read timeout — reporting a real success as a timeout/error.

The fix blocks the handler method on ``stream_done.wait()`` so the request
loop actually reads ``close_connection`` and closes the socket right after
the terminal event.

This test spins up the real API server, swaps in a fake agent runtime that
finishes instantly (no provider / no network), opens ``/api/agent/stream``,
drains the SSE body and asserts the connection closes within a few seconds
of the `done` event — not merely that `done` was sent.
"""

import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tera_pilot.api_server import TeraPilotAPIServer  # noqa: E402
from tera_pilot.agent_runtime.types import TaskResult  # noqa: E402


class _FakeTools:
    """Shape the agent stream handler touches: diff-review + run knobs."""

    diff_review_enabled = False
    RUN_TIMEOUT = 15
    MAX_OUTPUT = 2000
    _diff_review_callback = None


class _FakeAgent:
    """Minimal stand-in for AgentRuntime: completes one turn instantly."""

    def __init__(self) -> None:
        self.on_event = None
        self.tools = _FakeTools()
        self.max_iterations = 8

    def set_section(self, section: str) -> None:
        pass

    def set_cancel_check(self, fn) -> None:
        pass

    def run(self, text: str, task_type=None) -> TaskResult:
        return TaskResult(success=True, output="fixed the missing return", metadata={})


@pytest.fixture()
def api(tmp_path_factory):
    # Isolate ~/.tera_pilot so the test never touches the developer's real
    # config / license / chat files.
    home = tmp_path_factory.mktemp("tera_pilot_home")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        server = TeraPilotAPIServer(port=0)
        server.start()
        ws = tempfile.mkdtemp(prefix="tera_pilot_sse_test_")
        server.ctx.config["project_root"] = ws
        yield {"server": server, "port": server.port, "token": server.auth_token, "ws": ws}
        server.stop()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_agent_stream_connection_closes_after_done(api):
    server = api["server"]
    # The real agent runtime would need a configured provider + network;
    # swap in a fake that returns a successful result instantly so the test
    # measures the SSE close behavior, not the LLM.
    server.ctx.get_agent_runtime = lambda workspace: _FakeAgent()

    url = f"http://127.0.0.1:{api['port']}/api/agent/stream"
    payload = json.dumps({
        "text": "Fix apply_discount to return the discounted value.",
        "project_root": api["ws"],
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api["token"],
        },
    )
    saw_done = False
    done_at: float | None = None
    eof_at: float | None = None
    with urllib.request.urlopen(req, timeout=15) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "done":
                saw_done = True
                done_at = time.monotonic()
        # Iteration ends only at EOF — i.e. when the server actually closed
        # the connection (pre-fix it stayed open and this blocked until the
        # 15 s client timeout).
        eof_at = time.monotonic()

    assert saw_done, "expected a `done` SSE event from /api/agent/stream"
    assert done_at is not None and eof_at is not None
    close_delay = eof_at - done_at
    # Before the fix the socket stayed open after `done` and the read below
    # blocked for the full client timeout; the connection must close within
    # a couple of seconds of the terminal event.
    assert close_delay < 3.0, (
        f"connection stayed open {close_delay:.2f}s after `done` — "
        "close_connection is not being read by the request loop"
    )


def test_chat_stream_connection_closes_after_done(api):
    """Same guarantee for the plain chat SSE path (same bug class)."""
    server = api["server"]
    # Fake the provider registry so stream() is never really called: swap
    # in a stub that yields a couple of chunks. ``registry.active`` is a
    # property, so shadow ``_get_or_create`` instead. NOTE: the registry is
    # a PROCESS-WIDE singleton (get_registry()), so the patch must be
    # restored afterwards or it leaks into every later test that shares it.
    server.ctx.config["active_provider"] = "groq"
    registry = server.ctx.registry
    original_get_or_create = registry._get_or_create

    class _FakeProvider:
        provider_id = "groq"
        label = "Fake Groq"
        is_loaded = True

        def stream(self, messages, skill=None):
            yield "hello"
            yield " world"

    registry._get_or_create = lambda pid: _FakeProvider()
    try:
        _assert_chat_stream_closes(api)
    finally:
        registry._get_or_create = original_get_or_create


def _assert_chat_stream_closes(api) -> None:
    """POST /api/chat/stream and assert EOF arrives right after `done`."""
    url = f"http://127.0.0.1:{api['port']}/api/chat/stream"
    payload = json.dumps({"text": "say hello"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api["token"],
        },
    )
    saw_done = False
    done_at: float | None = None
    eof_at: float | None = None
    with urllib.request.urlopen(req, timeout=15) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "done":
                saw_done = True
                done_at = time.monotonic()
        eof_at = time.monotonic()

    assert saw_done, "expected a `done` SSE event from /api/chat/stream"
    assert done_at is not None and eof_at is not None
    close_delay = eof_at - done_at
    assert close_delay < 3.0, (
        f"chat stream connection stayed open {close_delay:.2f}s after `done`"
    )
