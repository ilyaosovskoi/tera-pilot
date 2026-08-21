"""E2E tests for the HTTP-path confirmation flows (v2.3.4-fix).

Before this fix the browser GUI's Agent Mode never asked the user before
running commands:

  * ToolEngine._request_confirmation() failed open on the HTTP path because
    set_confirm_callback() was never wired in _handle_agent_stream — so
    "Always ask" autonomy in Settings was silently bypassed and every
    execute_command / delete_file / git_commit ran without asking.
  * Guardian MODIFY verdicts were computed and then discarded: the engine
    called self._emit() (which did not exist → AttributeError swallowed as
    "review failed") and the HTTP path never wired _guardian_callback, so
    the GUI's Guardian review modal could never open (its respond route 404'd).

These tests drive the REAL API server (as the browser does) through the
full loop: agent requests confirmation → SSE event on the stream → POST
/api/action/respond (or /api/guardian/respond) → the waiting agent thread
unblocks and the run completes.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tera_pilot.api_server import TeraPilotAPIServer  # noqa: E402
from tera_pilot.agent_runtime.tool_engine import ToolEngine  # noqa: E402
from tera_pilot.agent_runtime.types import TaskResult  # noqa: E402


@pytest.fixture()
def api(tmp_path_factory):
    home = tmp_path_factory.mktemp("tera_pilot_confirm_home")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        server = TeraPilotAPIServer(port=0)
        server.start()
        ws = tempfile.mkdtemp(prefix="tera_pilot_confirm_test_")
        server.ctx.config["project_root"] = ws
        yield {"server": server, "port": server.port, "token": server.auth_token, "ws": ws}
        server.stop()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def _post(api, path, payload):
    url = f"http://127.0.0.1:{api['port']}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api["token"],
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read().decode())


class _ConfirmingFakeAgent:
    """Minimal stand-in for AgentRuntime: real ToolEngine (so the real
    `_request_confirmation` wait logic runs), fake run() that requests a
    confirmation and records what the user decided."""

    def __init__(self, engine: ToolEngine):
        self.tools = engine
        self.max_iterations = 8
        self.on_event = lambda *a, **k: None
        self.last_output = None

    def set_section(self, section: str):
        self.tools.section = section

    def set_confirm_callback(self, fn):
        self.tools._confirm_callback = fn

    def set_guardian_callback(self, fn):
        self.tools._guardian_callback = fn

    def set_cancel_check(self, fn):
        self.tools._cancel_check = fn

    def run(self, text, task_type=None):
        accepted = self.tools._request_confirmation("execute_command", "Run: echo hi")
        self.last_output = f"accepted={accepted}"
        return TaskResult(success=True, output=self.last_output, metadata={})


def _stream_events(api, ws, sink, prompt="Run a test command"):
    """Open /api/agent/stream and append SSE events to `sink` as they arrive.

    `sink` must be a caller-owned list — events are appended IMMEDIATELY per
    event (not after the stream closes), so the caller can react to an
    `action_confirm`/`guardian_review` event while the stream is still
    blocked waiting for the answer.
    """
    url = f"http://127.0.0.1:{api['port']}/api/agent/stream"
    payload = json.dumps({"text": prompt, "project_root": ws}).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api["token"],
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                sink.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                continue


def _wait_for(events, ev_type, timeout=15.0):
    """Poll a shared list until an event of `ev_type` appears; return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in events:
            if e.get("type") == ev_type:
                return e
        time.sleep(0.05)
    return None


def test_action_confirm_e2e(api):
    """Agent asks permission → SSE `action_confirm` → POST respond → run completes."""
    server = api["server"]
    engine = ToolEngine(workspace=api["ws"])
    engine.autonomy = "always_ask"
    agent = _ConfirmingFakeAgent(engine)
    server.ctx.get_agent_runtime = lambda workspace: agent

    events = []
    stream = threading.Thread(
        target=lambda: _stream_events(api, api["ws"], events),
        daemon=True,
    )
    stream.start()

    confirm = _wait_for(events, "action_confirm")
    assert confirm is not None, f"no action_confirm SSE event, got: {events[:6]}"
    assert confirm.get("confirm_id")
    assert confirm.get("action") == "execute_command"
    assert "echo" in confirm.get("summary", "")

    st, data = _post(api, "/api/action/respond", {
        "accepted": True, "confirm_id": confirm["confirm_id"],
    })
    assert st == 200
    assert data.get("ok") is True

    stream.join(timeout=20)
    assert not stream.is_alive(), "agent stream did not finish after respond"
    assert any(e.get("type") == "done" for e in events), "missing `done` event"
    assert agent.last_output == "accepted=True", agent.last_output


def test_action_confirm_denied(api):
    """Denying the confirmation makes _request_confirmation return False."""
    server = api["server"]
    engine = ToolEngine(workspace=api["ws"])
    engine.autonomy = "always_ask"
    agent = _ConfirmingFakeAgent(engine)
    server.ctx.get_agent_runtime = lambda workspace: agent

    events = []
    stream = threading.Thread(
        target=lambda: _stream_events(api, api["ws"], events),
        daemon=True,
    )
    stream.start()

    confirm = _wait_for(events, "action_confirm")
    assert confirm is not None
    st, data = _post(api, "/api/action/respond", {
        "accepted": False, "confirm_id": confirm["confirm_id"],
    })
    assert st == 200 and data.get("ok") is True

    stream.join(timeout=20)
    assert any(e.get("type") == "done" for e in events)
    assert agent.last_output == "accepted=False", agent.last_output


class _CancellableLoopAgent:
    """run() keeps working until the cancel check fires — mirrors the real
    runtime loop, which polls is_cancelled() at each iteration boundary."""

    def __init__(self, engine: ToolEngine):
        self.tools = engine
        self.max_iterations = 8
        self.on_event = lambda *a, **k: None
        self.stopped = False

    def set_section(self, section: str):
        self.tools.section = section

    def set_confirm_callback(self, fn):
        self.tools._confirm_callback = fn

    def set_guardian_callback(self, fn):
        self.tools._guardian_callback = fn

    def set_cancel_check(self, fn):
        self.tools._cancel_check = fn

    def run(self, text, task_type=None):
        t0 = time.monotonic()
        while time.monotonic() - t0 < 30:
            if self.tools.is_cancelled():
                self.stopped = True
                return TaskResult(success=False, output="", error="Cancelled by user", metadata={})
            time.sleep(0.02)
        return TaskResult(success=True, output="finished normally", metadata={})


def test_agent_stop_cancels_running_agent(api):
    """POST /api/agent/stop must halt a running agent stream: the cancel
    flag reaches the runtime's cancel check and the SSE connection closes.
    (v2.3.4-fix regression guard for the GUI Stop button — the click used
    to be swallowed client-side, but the backend must also be verified.)"""
    server = api["server"]
    engine = ToolEngine(workspace=api["ws"])
    agent = _CancellableLoopAgent(engine)
    server.ctx.get_agent_runtime = lambda workspace: agent

    events = []
    stream = threading.Thread(
        target=lambda: _stream_events(api, api["ws"], events),
        daemon=True,
    )
    stream.start()

    # Wait until the agent run actually started (chat_info is emitted
    # before the run begins; the run itself then blocks in the loop).
    assert _wait_for(events, "chat_info") is not None

    st, data = _post(api, "/api/agent/stop", {})
    assert st == 200
    assert data.get("ok") is True

    stream.join(timeout=15)
    assert not stream.is_alive(), "agent stream did not terminate after /api/agent/stop"
    assert agent.stopped is True, "agent did not observe the cancel flag"


def test_action_respond_no_pending(api):
    """POSTing an unknown/stale confirm_id must fail loudly, not silently pass."""
    st, data = _post(api, "/api/action/respond", {
        "accepted": True, "confirm_id": "does-not-exist",
    })
    assert st == 200
    assert data.get("ok") is False
    assert "confirm_id" in data.get("error", "")


class _GuardianFakeAgent:
    """Fake run() that simulates the engine's guardian flow: clears the
    guardian event, invokes the wired _guardian_callback (which blocks on
    SSE until the user answers), then reports the decision."""

    def __init__(self, engine: ToolEngine):
        self.tools = engine
        self.max_iterations = 8
        self.on_event = lambda *a, **k: None
        self.last_output = None

    def set_section(self, section: str):
        self.tools.section = section

    def set_confirm_callback(self, fn):
        self.tools._confirm_callback = fn

    def set_guardian_callback(self, fn):
        self.tools._guardian_callback = fn

    def set_cancel_check(self, fn):
        self.tools._cancel_check = fn

    def run(self, text, task_type=None):
        self.tools._guardian_event.clear()
        self.tools._guardian_decision = None
        cb = self.tools._guardian_callback
        cb({
            "action": "execute_command",
            "args": {"command": "rm -rf /tmp/important"},
            "guardian_verdict": "MODIFY",
            "suggested_args": {"command": "rm /tmp/important"},
            "rationale": "Recursive delete is risky",
            "risk_level": "high",
            "reasons": ["recursive delete"],
        })
        # The HTTP callback blocks until the user answers, then calls
        # respond_guardian() which records the decision on the engine.
        decision = self.tools._guardian_decision
        self.last_output = f"decision={decision}"
        return TaskResult(success=True, output=self.last_output, metadata={})


def test_guardian_review_e2e(api):
    """Guardian MODIFY verdict → SSE `guardian_review` → POST respond `use_fix`
    → the decision reaches the waiting agent thread."""
    server = api["server"]
    engine = ToolEngine(workspace=api["ws"])
    agent = _GuardianFakeAgent(engine)
    server.ctx.get_agent_runtime = lambda workspace: agent

    events = []
    stream = threading.Thread(
        target=lambda: _stream_events(api, api["ws"], events),
        daemon=True,
    )
    stream.start()

    review = _wait_for(events, "guardian_review")
    assert review is not None, f"no guardian_review SSE event, got: {events[:6]}"
    assert review.get("review_id")
    assert review.get("guardian_verdict") == "MODIFY"
    assert review.get("suggested_args") == {"command": "rm /tmp/important"}

    st, data = _post(api, "/api/guardian/respond", {
        "verdict": "use_fix", "review_id": review["review_id"],
    })
    assert st == 200
    assert data.get("ok") is True

    stream.join(timeout=20)
    assert not stream.is_alive(), "agent stream did not finish after guardian respond"
    assert any(e.get("type") == "done" for e in events), "missing `done` event"
    assert agent.last_output == "decision=use_fix", agent.last_output


def test_guardian_respond_no_pending(api):
    st, data = _post(api, "/api/guardian/respond", {
        "verdict": "approve", "review_id": "nope",
    })
    assert st == 200
    assert data.get("ok") is False
    assert "review_id" in data.get("error", "")


def test_guardian_respond_rejects_invalid_verdict(api):
    st, data = _post(api, "/api/guardian/respond", {
        "verdict": "maybe", "review_id": "anything",
    })
    assert st == 200
    assert data.get("ok") is False
    assert "invalid verdict" in data.get("error", "")


def test_saved_autonomy_applied_to_runtime(api):
    """get_agent_runtime() must honor the saved agent_autonomy config, so a
    server restart no longer silently resets 'never_ask' to the default."""
    server = api["server"]
    server.ctx.config["agent_autonomy"] = "never_ask"
    runtime = server.ctx.get_agent_runtime(api["ws"])
    assert runtime.tools.autonomy == "never_ask"

    # And a different saved value is applied on a fresh runtime.
    server.ctx.config["agent_autonomy"] = "always_ask"
    # Force a fresh runtime (simulates restart)
    server.ctx._agent_runtime = None
    runtime2 = server.ctx.get_agent_runtime(api["ws"])
    assert runtime2.tools.autonomy == "always_ask"


def test_saved_guardian_level_applied_to_runtime(api):
    """The saved Guardian level must reach the engine, otherwise Guardian
    stays silently OFF in the browser GUI after a restart."""
    server = api["server"]
    server.ctx.config["guardian_level"] = "dangerous_only"
    runtime = server.ctx.get_agent_runtime(api["ws"])
    assert runtime.tools._guardian_config.level == "dangerous_only"

    server.ctx.config["guardian_level"] = "all"
    server.ctx._agent_runtime = None
    runtime2 = server.ctx.get_agent_runtime(api["ws"])
    assert runtime2.tools._guardian_config.level == "all"
