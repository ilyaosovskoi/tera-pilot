"""Integration tests for the TUI backend path.

Drive the REAL ``AgentRuntime`` + ``ToolEngine`` through the REAL
``TeraPilotBridge`` (the exact object the Textual TUI uses) with the
deterministic ``FakeProvider`` from ``tests/fake_provider.py`` — no
network, no API keys.

Covers the failure modes a user can actually hit:

- streaming and non-streaming turns
- cancel (Stop) mid-stream
- approval flow (Allow/Deny)
- provider errors: invalid key, timeout, rate limit
- workspace / provider / model switching
- long output and partial tool failures
- checkpoint + undo
- recovery after a provider error
"""

import os
import sys
import threading
from pathlib import Path

import pytest

# tests/ is not a package — add it to sys.path so `fake_provider` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_provider import FakeProvider, auth_error  # noqa: E402

from tera_pilot.providers.base import ProviderCapability, ProviderError  # noqa: E402
from tera_pilot.agent_runtime.types import ToolName  # noqa: E402
from tera_pilot_tui.bridge import TeraPilotBridge, ProviderChoice  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Isolate ~/.tera_pilot so no real user config leaks into tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def reg(isolated_home):
    from tera_pilot.providers import get_registry
    r = get_registry()
    r.register(FakeProvider)  # idempotent
    yield r
    r.set_active("ollama")
    # Drop injected fake instances so the next test starts clean.
    r._instances.pop("fake", None)


@pytest.fixture
def fake(reg):
    fp = FakeProvider()
    reg._instances["fake"] = fp
    reg.set_active("fake")
    return fp


def make_bridge(tmp_path, fp, *, streaming=True, enable_planning=False):
    """Build a bridge wired to `fp` with event + confirm capture."""
    if not streaming:
        fp.capabilities = frozenset(
            {ProviderCapability.CHAT, ProviderCapability.TOOL_CALLING}
        )
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),  # no model → no reconfigure
        enable_planning=enable_planning,
    )
    events = []
    confirms = []
    confirm_event = threading.Event()

    def sink(kind, data):
        events.append((kind, dict(data)))

    def on_confirm(info):
        confirms.append(dict(info))
        confirm_event.set()

    bridge.set_event_sink(sink)
    bridge.set_confirm_handler(on_confirm)
    return bridge, events, confirms, confirm_event


def run_in_thread(bridge, prompt, **kwargs):
    """Run bridge.run_prompt on a worker thread; returns (thread, holder)."""
    holder = {}

    def runner():
        try:
            holder["result"] = bridge.run_prompt(prompt, **kwargs)
        except Exception as exc:  # pragma: no cover - defensive
            holder["error"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return t, holder


def _wait_result(thread, holder, timeout=60):
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "agent turn did not finish in time (hang?)"
    assert "error" not in holder, f"run_prompt raised: {holder['error']!r}"
    return holder["result"]


def _auto_approve(bridge):
    bridge.set_confirm_handler(lambda info: bridge.answer_confirmation(True))


# ── streaming / non-streaming ──────────────────────────────────────────


def test_streaming_turn_writes_file_and_streams_tokens(tmp_path, fake):
    fake._script = [
        FakeProvider.tool_call("write_file", {"path": "out.txt", "content": "hello"}),
        FakeProvider.final_answer("done-streaming"),
    ]
    bridge, events, _, _ = make_bridge(tmp_path, fake, streaming=True)
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "write a file")
    result = _wait_result(t, holder)

    assert result.success is True
    assert (tmp_path / "out.txt").read_text() == "hello"
    kinds = [k for k, _ in events]
    assert "token_delta" in kinds, "streaming should emit token_delta events"
    assert result.output == "done-streaming"


def test_non_streaming_turn_no_token_deltas(tmp_path, fake):
    fake._script = [
        FakeProvider.tool_call("write_file", {"path": "out2.txt", "content": "hi"}),
        FakeProvider.final_answer("done-non-streaming"),
    ]
    bridge, events, _, _ = make_bridge(tmp_path, fake, streaming=False)
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "write a file")
    result = _wait_result(t, holder)

    assert result.success is True
    assert (tmp_path / "out2.txt").read_text() == "hi"
    kinds = [k for k, _ in events]
    assert "token_delta" not in kinds, "non-streaming must not emit token_deltas"
    assert result.output == "done-non-streaming"


# ── cancel ─────────────────────────────────────────────────────────────


def test_cancel_mid_stream_returns_promptly(tmp_path, fake):
    fake._script = [FakeProvider.final_answer("x" * 400)]
    fake.block_at_call = 1
    fake.block_after_chunks = 2
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=True)
    t, holder = run_in_thread(bridge, "long task")

    assert fake.blocked_event.wait(timeout=15), "fake provider never blocked"
    bridge.request_stop()
    fake.release_event.set()

    result = _wait_result(t, holder, timeout=30)
    assert result.success is False
    assert result.error and "cancel" in result.error.lower()


# ── approval flow ──────────────────────────────────────────────────────


def test_approval_allow_runs_command(tmp_path, fake):
    fake._script = [
        FakeProvider.tool_call("execute_command", {"command": "ls"}),
        FakeProvider.final_answer("approved"),
    ]
    bridge, _, confirms, confirm_event = make_bridge(tmp_path, fake, streaming=False)
    t, holder = run_in_thread(bridge, "list files")

    assert confirm_event.wait(timeout=15), "no confirmation was requested"
    assert confirms and confirms[0].get("action") == "execute_command"
    bridge.answer_confirmation(True)

    result = _wait_result(t, holder)
    assert result.success is True
    assert result.output == "approved"
    assert result.tool_calls and result.tool_calls[0].name == ToolName.EXECUTE_COMMAND
    assert result.tool_calls[0].error is None  # tool actually ran


def test_approval_deny_skips_command(tmp_path, fake):
    fake._script = [
        FakeProvider.tool_call("execute_command", {"command": "ls"}),
        FakeProvider.final_answer("after-deny"),
    ]
    bridge, _, _, confirm_event = make_bridge(tmp_path, fake, streaming=False)
    t, holder = run_in_thread(bridge, "list files")

    assert confirm_event.wait(timeout=15)
    bridge.answer_confirmation(False)

    result = _wait_result(t, holder)
    assert result.success is True
    assert result.tool_calls and result.tool_calls[0].name == ToolName.EXECUTE_COMMAND
    assert "[REJECTED BY USER]" in (result.tool_calls[0].result or "")


def test_no_ui_confirm_handler_fails_open(tmp_path, fake):
    """Headless mode (no confirm handler) must not deadlock the agent."""
    fake._script = [
        FakeProvider.tool_call("execute_command", {"command": "ls"}),
        FakeProvider.final_answer("headless-ok"),
    ]
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),
        enable_planning=False,
    )
    t, holder = run_in_thread(bridge, "list files")
    result = _wait_result(t, holder)
    assert result.success is True
    assert result.output == "headless-ok"


# ── provider errors ────────────────────────────────────────────────────


def test_invalid_api_key_fails_with_clear_message(tmp_path, fake):
    fake._script = [FakeProvider.final_answer("never reached")]
    fake._errors = [auth_error()]
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=False)
    t, holder = run_in_thread(bridge, "task")
    result = _wait_result(t, holder)

    assert result.success is False
    assert result.error and "API key" in result.error
    assert fake.call_count == 1, "auth errors must NOT be retried"


def test_timeout_is_retried_then_fails(tmp_path, fake):
    err = ProviderError(
        "HTTPSConnectionPool(host='api.openai.com', port=443): Read timed out. (read timeout=120)"
    )
    # _RETRY_MAX_ATTEMPTS is 5 — exhaust every attempt so the run fails.
    fake._script = [FakeProvider.final_answer("never")]
    fake._errors = [err] * 5
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=False)
    t, holder = run_in_thread(bridge, "task")
    result = _wait_result(t, holder, timeout=120)

    assert result.success is False
    assert fake.call_count >= 5, "transient errors should be retried"
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


def test_rate_limit_is_retried(tmp_path, fake):
    err = ProviderError("HTTP 429 Too Many Requests (rate limit exceeded)")
    # _RETRY_MAX_ATTEMPTS is 5 — exhaust every attempt so the run fails.
    fake._script = [FakeProvider.final_answer("never")]
    fake._errors = [err] * 5
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=False)
    t, holder = run_in_thread(bridge, "task")
    result = _wait_result(t, holder, timeout=120)

    assert result.success is False
    assert fake.call_count >= 5


# ── workspace / provider / model switching ─────────────────────────────


def test_workspace_switch_writes_into_new_workspace(tmp_path, fake):
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    bridge = TeraPilotBridge(
        workspace=str(ws1),
        provider=ProviderChoice(provider_id="fake"),
        enable_planning=False,
    )
    assert bridge.change_workspace(str(ws2)).get("ok") is True
    assert Path(bridge.workspace) == ws2

    fake._script = [
        FakeProvider.tool_call("write_file", {"path": "out.txt", "content": "w2"}),
        FakeProvider.final_answer("done"),
    ]
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "write in ws2")
    result = _wait_result(t, holder)

    assert result.success is True
    assert (ws2 / "out.txt").read_text() == "w2"
    assert not (ws1 / "out.txt").exists()


def test_change_workspace_rejects_missing_dir(tmp_path, bridge=None):
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),
    )
    r = bridge.change_workspace(str(tmp_path / "nope"))
    assert r.get("ok") is False
    assert "Not a directory" in r.get("error", "")


def test_set_provider_unknown_id_reports_error(tmp_path, reg, fake):
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),
    )
    r = bridge.set_provider("definitely-not-a-provider")
    assert r.get("ok") is False
    assert "definitely-not-a-provider" in r.get("error", "")
    # The active provider must be untouched.
    assert reg.active_id == "fake"


def test_set_provider_switches_model_and_rebuilds(tmp_path, reg, fake):
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),
    )
    r = bridge.set_provider("fake", model="fake-2")
    assert r.get("ok") is True
    assert r.get("model") == "fake-2"
    assert bridge._agent is None  # rebuilt on the next turn
    # Next turn runs on the fresh config (no scripted script left) —
    # must complete, not crash.
    t, holder = run_in_thread(bridge, "hello")
    result = _wait_result(t, holder)
    assert result is not None


# ── long output / partial failure ──────────────────────────────────────


def test_long_streamed_output_not_truncated(tmp_path, fake):
    long_text = "L" * 60_000
    fake._script = [FakeProvider.final_answer(long_text)]
    bridge, events, _, _ = make_bridge(tmp_path, fake, streaming=True)
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "long")
    result = _wait_result(t, holder)

    assert result.success is True
    assert result.output == long_text
    # Every chunk must be delivered exactly once (no double-append bug)
    # and cover the full raw streamed text (JSON wrapper included).
    raw_text = FakeProvider.final_answer(long_text)
    deltas = sum(len(d.get("delta", "")) for k, d in events if k == "token_delta")
    assert deltas == len(raw_text)


def test_tool_error_is_partial_failure_not_crash(tmp_path, fake):
    fake._script = [
        FakeProvider.tool_call("write_file", {"path": "../escape.txt", "content": "x"}),
        FakeProvider.final_answer("continued-after-error"),
    ]
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=False)
    t, holder = run_in_thread(bridge, "try to escape")
    result = _wait_result(t, holder)

    assert result.success is True  # agent recovered and finished
    assert result.output == "continued-after-error"
    assert result.tool_calls and result.tool_calls[0].name == ToolName.WRITE_FILE
    assert result.tool_calls[0].error  # the sandbox blocked the write
    assert not (tmp_path.parent / "escape.txt").exists()


# ── checkpoint + undo ──────────────────────────────────────────────────


@pytest.fixture
def checkpoint_cleanup(isolated_home):
    from tera_pilot.checkpoint import reset_checkpoint_manager
    reset_checkpoint_manager()
    yield
    reset_checkpoint_manager()


def test_checkpoint_rewind_restores_workspace_files(
    tmp_path, fake, checkpoint_cleanup, monkeypatch
):
    # cwd differs from the bridge workspace on purpose: this used to make
    # checkpoint backups/restores hit the wrong directory (silent no-ops).
    monkeypatch.chdir(tmp_path.parent)
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "a.txt").write_text("v1", encoding="utf-8")

    bridge = TeraPilotBridge(
        workspace=str(ws),
        provider=ProviderChoice(provider_id="fake"),
    )
    r1 = bridge.create_checkpoint(touched_files=["a.txt"])
    assert r1.get("ok") is True

    (ws / "a.txt").write_text("v2", encoding="utf-8")
    r2 = bridge.create_checkpoint(touched_files=["a.txt"])
    assert r2.get("ok") is True

    rw = bridge.rewind_checkpoint(1)
    assert rw.get("ok") is True, f"rewind failed: {rw}"
    assert (ws / "a.txt").read_text() == "v1"


def test_undo_write_restores_previous_content(tmp_path, fake, checkpoint_cleanup):
    (tmp_path / "u.txt").write_text("v1", encoding="utf-8")
    fake._script = [
        FakeProvider.tool_call("write_file", {"path": "u.txt", "content": "v2"}),
        FakeProvider.tool_call("undo_write", {"path": "u.txt"}),
        FakeProvider.final_answer("undone"),
    ]
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=False)
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "write then undo")
    result = _wait_result(t, holder)

    assert result.success is True
    assert (tmp_path / "u.txt").read_text() == "v1"


# ── recovery after error ───────────────────────────────────────────────


def test_recovery_after_provider_error(tmp_path, fake):
    fake._script = [FakeProvider.final_answer("first-ok")]
    fake._errors = [auth_error()]
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=False)

    t1, h1 = run_in_thread(bridge, "first task")
    r1 = _wait_result(t1, h1)
    assert r1.success is False and "API key" in r1.error

    # Same bridge, same agent — clear the failure and run again.
    fake._errors = []
    fake._script = [FakeProvider.final_answer("recovered")]
    t2, h2 = run_in_thread(bridge, "second task")
    r2 = _wait_result(t2, h2)
    assert r2.success is True
    assert r2.output == "recovered"


# ── backend_runner (CI adapter) ────────────────────────────────────────


def test_backend_runner_reports_provider_error_cleanly(tmp_path, reg, fake):
    """run_task() must return a report (not raise) when the provider fails."""
    from tera_pilot_tui import backend_runner

    fake._script = [FakeProvider.final_answer("n/a")]
    # A bad key fails EVERY call (planning + run loop), not just the first.
    fake._errors = [auth_error()] * 5

    report = backend_runner.run_task(
        "task", workspace=str(tmp_path), provider_id="fake", max_iterations=2
    )
    assert report["schema_version"] == 1
    assert report["ok"] is False
    assert "API key" in (report.get("error") or "")
    import json
    json.dumps(report)  # must be JSON-serializable
