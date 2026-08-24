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


# ── degraded flag (v2.3.4-fix) ─────────────────────────────────────────


def test_degraded_flag_false_when_tools_ran_then_prose(tmp_path, fake):
    """Regression: tools executed in earlier iterations, then the model
    wrapped up in prose on iteration 3+. The run DID real work, so
    ``degraded_prose`` must be False — the old code set it whenever
    iteration 3+ emitted prose, warning the UI "verify the result" over
    a completed run (reproduced deterministically)."""
    fake._script = [
        FakeProvider.tool_call("write_file", {"path": "a.txt", "content": "hello"}),
        FakeProvider.tool_call("read_file", {"path": "a.txt"}),
        "I wrote the file, it contains hello.",
    ]
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=False)
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "write a file a.txt with 'hello' and read it back")
    result = _wait_result(t, holder)

    assert result.success is True
    assert [tc.name.value for tc in (result.tool_calls or [])] == ["write_file", "read_file"]
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert (result.metadata or {}).get("degraded_prose") is False


def test_degraded_flag_true_when_no_tool_ever_ran(tmp_path, fake):
    """Pure-prose run (no tool call in any iteration) must still be
    flagged degraded — the flag exists precisely to catch this."""
    fake._script = [
        "I'll write the code: print('x')",
        "Here is the solution: print('x')",
        "The file is created.",
    ]
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=False)
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "write a file x.py")
    result = _wait_result(t, holder)

    assert (result.metadata or {}).get("degraded_prose") is True


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


def test_no_ui_confirm_handler_fails_closed(tmp_path, fake):
    """v2.3.4-security (P0.4): headless mode (no UI confirm handler)
    must not deadlock the agent — AND must fail CLOSED: the side-
    effecting command is rejected, never silently run."""
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
    # The command was BLOCKED (fail-closed), not silently approved.
    assert result.tool_calls and result.tool_calls[0].name == ToolName.EXECUTE_COMMAND
    assert "[REJECTED BY USER]" in (result.tool_calls[0].result or "")


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
    # v2.3.4: quota/429 errors get _RETRY_QUOTA_MAX_ATTEMPTS attempts (the
    # upstream explicitly asks us to retry shortly) — exhaust every one so
    # the run fails instead of succeeding on the scripted final answer.
    from tera_pilot.agent_runtime.runtime import AgentRuntime
    budget = AgentRuntime._RETRY_QUOTA_MAX_ATTEMPTS
    fake._script = [FakeProvider.final_answer("never")]
    fake._errors = [err] * budget
    bridge, _, _, _ = make_bridge(tmp_path, fake, streaming=False)
    t, holder = run_in_thread(bridge, "task")
    result = _wait_result(t, holder, timeout=180)

    assert result.success is False
    assert fake.call_count >= budget


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


def test_get_active_provider_id_follows_switches(tmp_path, reg, fake):
    """get_active_provider_id() must report the currently active provider.
    The TUI's `/model <model>` command relies on it to set a custom
    model on the active provider without switching."""
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),
    )
    assert bridge.get_active_provider_id() == "fake"
    r = bridge.set_provider("ollama")
    assert r.get("ok") is True
    assert bridge.get_active_provider_id() == "ollama"


def test_bridge_applies_saved_config_from_disk(tmp_path, reg, fake):
    """The TUI bridge must apply ~/.tera_pilot/config.json (api_key,
    model, active_provider) when building its registry — exactly like
    the daemon does. Before the fix it ignored the file and silently
    fell back to the built-in defaults (no key, hardcoded model)."""
    from tera_pilot.utils import save_config

    save_config({
        "active_provider": "fake",
        "providers": {
            "fake": {
                "api_key": "sk-saved-key",
                "model": "saved-model",
                "api_base": "http://localhost:9999/v1",
            },
        },
    })

    # No explicit ProviderChoice overrides — everything must come from disk.
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    bridge.list_providers()  # trigger the lazy registry build

    assert bridge.get_active_provider_id() == "fake"
    assert bridge._get_active_model() == "saved-model"
    cfg = bridge._registry.get("fake").config
    assert cfg.api_key == "sk-saved-key"
    assert cfg.api_base == "http://localhost:9999/v1"


def test_bridge_honors_agent_max_iterations_from_config(tmp_path, reg, fake):
    """The TUI must honor `agent_max_iterations` from config.json — not
    hardcode 8 (the user saw "Max iterations (8) reached" despite
    having configured 12)."""
    from tera_pilot.utils import save_config

    save_config({"agent_max_iterations": 15, "providers": {}})
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    assert bridge.max_iterations == 15


def test_bridge_explicit_max_iterations_wins(tmp_path, reg, fake):
    """An explicit max_iterations argument must win over config.json."""
    from tera_pilot.utils import save_config

    save_config({"agent_max_iterations": 15, "providers": {}})
    bridge = TeraPilotBridge(workspace=str(tmp_path), max_iterations=3)
    assert bridge.max_iterations == 3


def test_bridge_default_max_iterations_when_no_config(tmp_path, reg, fake):
    """No config → the historical default of 8."""
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    assert bridge.max_iterations == 8


def test_budget_iterations_knob_is_applied(tmp_path, reg, fake):
    """v2.3.6: `/budget iterations N` (token_budget.max_iterations) must
    actually reach the agent loop once changed from its module default.
    Previously it was persisted and displayed but never applied."""
    from tera_pilot.token_budget import set_token_budget, reset_token_budget

    set_token_budget(max_iterations=15)
    try:
        bridge = TeraPilotBridge(workspace=str(tmp_path))
        bridge.ensure_agent()
        assert bridge._agent.max_iterations == 15
    finally:
        reset_token_budget()


def test_default_budget_does_not_override_config_iterations(tmp_path, reg, fake):
    """v2.3.6: the token budget's DEFAULT max_iterations (8) must not
    clobber agent_max_iterations from config.json."""
    from tera_pilot.utils import save_config

    save_config({"agent_max_iterations": 12, "providers": {}})
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    bridge.ensure_agent()
    assert bridge._agent.max_iterations == 12


def test_explicit_max_iterations_wins_over_budget(tmp_path, reg, fake):
    """v2.3.6: an explicit CLI --max-iterations must beat the token
    budget knob."""
    from tera_pilot.token_budget import set_token_budget, reset_token_budget

    set_token_budget(max_iterations=20)
    try:
        bridge = TeraPilotBridge(workspace=str(tmp_path), max_iterations=5)
        bridge.ensure_agent()
        assert bridge._agent.max_iterations == 5
    finally:
        reset_token_budget()


def test_heavy_code_section_gets_iteration_floor(tmp_path, reg, fake):
    """v2.3.6: the heavy_code section gets at least 20 iterations,
    mirroring the API server."""
    bridge = TeraPilotBridge(
        workspace=str(tmp_path), section="heavy_code", max_iterations=3,
    )
    bridge.ensure_agent()
    assert bridge._agent.max_iterations == 20


def test_status_reports_active_provider_without_prior_turn(tmp_path, reg, fake):
    """v2.3.6: status() must report the real provider/model on the FIRST
    call — the InfoBox used to show "unknown" until the first turn built
    the registry (and /settings silently defaulted to OpenAI)."""
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    s = bridge.status()
    assert s.get("provider") is not None
    assert s.get("model") is not None
    assert s["provider"] == "fake"


def test_runtime_tracks_touched_files_on_agent(tmp_path, reg, fake):
    """v2.3.6: the runtime's ToolEngine is reachable as ``agent.tools``
    and tracks written files there — /checkpoint save reads this (it
    used to poke ``agent._tool_engine`` which never existed, so the
    backup manifest was always empty)."""
    fake._script = [
        FakeProvider.tool_call("write_file", {"path": "a.txt", "content": "x"}),
        FakeProvider.final_answer("done"),
    ]
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),
    )
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "write a file")
    result = _wait_result(t, holder)

    assert result.success is True
    assert "a.txt" in (bridge._agent.tools._touched_files or [])


def test_productive_run_auto_extends_past_soft_cap(tmp_path, reg, fake):
    """v2.3.6: a run that keeps doing real work (executing tools) must
    NOT be cut off at the soft iteration cap. The budget auto-extends
    while a tool ran successfully recently, so big multi-step tasks
    finish instead of dying with "Max iterations reached" mid-work."""
    fake._script = [
        FakeProvider.tool_call("write_file", {"path": "f1.txt", "content": "1"}),
        FakeProvider.tool_call("write_file", {"path": "f2.txt", "content": "2"}),
        FakeProvider.tool_call("write_file", {"path": "f3.txt", "content": "3"}),
        FakeProvider.tool_call("write_file", {"path": "f4.txt", "content": "4"}),
        FakeProvider.tool_call("write_file", {"path": "f5.txt", "content": "5"}),
        FakeProvider.final_answer("finished the big task"),
    ]
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),
        max_iterations=3,  # fewer than the 5 tool steps the task needs
    )
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "do a big multi-step task")
    result = _wait_result(t, holder)

    assert result.success is True
    assert result.output == "finished the big task"
    for i in range(1, 6):
        assert (tmp_path / f"f{i}.txt").read_text() == str(i)


def test_stuck_run_does_not_auto_extend(tmp_path, reg, fake):
    """v2.3.6: a run that keeps FAILING its tool calls (e.g. reading a
    file that does not exist) must NOT extend the budget — the soft cap
    still stops it. Auto-extension requires recent SUCCESSFUL tool work."""
    fake._script = [
        FakeProvider.tool_call("read_file", {"path": "/no/such/file.txt"}),
        FakeProvider.tool_call("read_file", {"path": "/no/such/file.txt"}),
        FakeProvider.tool_call("read_file", {"path": "/no/such/file.txt"}),
        FakeProvider.tool_call("read_file", {"path": "/no/such/file.txt"}),
    ]
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),
        max_iterations=3,
    )
    _auto_approve(bridge)
    t, holder = run_in_thread(bridge, "read a file")
    result = _wait_result(t, holder)

    assert result.success is False
    assert "Max iterations (3) reached" in (result.error or "")


def test_auto_extension_stops_at_hard_cap(tmp_path, reg, fake):
    """v2.3.6: auto-extension is bounded — even a productive run stops
    at the hard ceiling instead of looping forever."""
    fake._script = [
        FakeProvider.tool_call("write_file", {"path": f"f{i}.txt", "content": str(i)})
        for i in range(1, 7)
    ]
    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake"),
        max_iterations=3,
    )
    _auto_approve(bridge)
    bridge.ensure_agent()
    bridge._agent.hard_max_iterations = 5  # shrink the ceiling for the test
    t, holder = run_in_thread(bridge, "do a long task")
    result = _wait_result(t, holder)

    assert result.success is False
    assert "Max iterations (5) reached" in (result.error or "")
    # The 5 allowed iterations ran; iteration 6 never started.
    for i in range(1, 6):
        assert (tmp_path / f"f{i}.txt").read_text() == str(i)
    assert not (tmp_path / "f6.txt").exists()


def test_bridge_explicit_provider_choice_overrides_saved_config(tmp_path, reg, fake):
    """A ProviderChoice passed explicitly (e.g. from the CLI) must win
    over ~/.tera_pilot/config.json."""
    from tera_pilot.utils import save_config

    save_config({
        "active_provider": "ollama",
        "providers": {
            "fake": {"api_key": "sk-saved-key", "model": "saved-model"},
        },
    })

    bridge = TeraPilotBridge(
        workspace=str(tmp_path),
        provider=ProviderChoice(provider_id="fake", model="cli-model"),
    )
    bridge.list_providers()

    assert bridge.get_active_provider_id() == "fake"
    assert bridge._get_active_model() == "cli-model"
    cfg = bridge._registry.get("fake").config
    # Saved key is preserved; only the explicitly overridden field changes.
    assert cfg.api_key == "sk-saved-key"
    assert cfg.model == "cli-model"


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


# ── provider model/API-key configuration (v2.3.5) ──────────────────────

def test_configure_provider_sets_model_and_key(tmp_path, reg, fake):
    """bridge.configure_provider() must actually apply model + API key.

    Regression: the Quick Settings modal and `/provider <pid> <model>`
    called ``registry.configure(pid, model=...)`` / ``(pid, api_key=...)``
    directly — but ProviderRegistry.configure() takes a ProviderConfig
    object, so both calls raised TypeError and were silently swallowed
    (the UI claimed success while nothing changed).
    """
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    r = bridge.configure_provider("fake", model="my-model", api_key="sk-test")
    assert r.get("ok") is True, r
    assert r.get("model") == "my-model"

    prov = bridge._registry.get("fake")
    assert prov.config.model == "my-model"
    assert prov.config.api_key == "sk-test"

    # Partial update preserves the rest of the config.
    r2 = bridge.configure_provider("fake", model="other-model")
    assert r2.get("ok") is True
    prov2 = bridge._registry.get("fake")
    assert prov2.config.model == "other-model"
    assert prov2.config.api_key == "sk-test"  # unchanged


def test_configure_provider_unknown_id_fails(tmp_path, reg):
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    r = bridge.configure_provider("no_such_provider", model="x")
    assert r.get("ok") is False
    assert "no_such_provider" in (r.get("error") or "")


def test_configure_provider_persists_to_config(tmp_path, reg, fake):
    """v2.3.6: model + API key set through configure_provider (Quick
    Settings / `/model <pid> <model>`) must survive a restart — the
    old code applied them live but never wrote config.json, so the next
    launch reverted to the saved (old) values."""
    from tera_pilot.utils import load_config, save_config

    save_config({"active_provider": "fake", "providers": {}})
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    bridge.list_providers()  # build the registry

    r = bridge.configure_provider("fake", model="persisted-model", api_key="sk-persist")
    assert r.get("ok") is True, r

    cfg = load_config()
    entry = (cfg.get("providers") or {}).get("fake") or {}
    assert entry.get("model") == "persisted-model"
    assert entry.get("api_key") == "sk-persist"
    assert cfg.get("active_provider") == "fake"

    # A fresh bridge (simulating restart) loads the persisted values.
    bridge2 = TeraPilotBridge(workspace=str(tmp_path))
    bridge2.list_providers()
    assert bridge2._get_active_model() == "persisted-model"
    assert bridge2._registry.get("fake").config.api_key == "sk-persist"


def test_set_provider_with_model_preserves_api_key(tmp_path, reg, fake):
    """v2.3.6: set_provider(pid, model) must NOT wipe the saved
    api_key / api_base — the cost-router path calls it with a model,
    and the old code built a bare ProviderConfig that dropped the
    credentials (a cost-router switch silently broke the provider)."""
    from tera_pilot.utils import save_config

    save_config({
        "active_provider": "fake",
        "providers": {"fake": {"api_key": "sk-keep", "model": "m1"}},
    })
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    bridge.list_providers()

    r = bridge.set_provider("fake", model="m2")
    assert r.get("ok") is True, r

    cfg = bridge._registry.get("fake").config
    assert cfg.model == "m2"
    assert cfg.api_key == "sk-keep"  # not wiped

    # And the switch is persisted with both fields.
    from tera_pilot.utils import load_config
    entry = (load_config().get("providers") or {}).get("fake") or {}
    assert entry.get("model") == "m2"
    assert entry.get("api_key") == "sk-keep"


def test_bridge_tolerates_stale_active_provider_in_config(tmp_path, reg, fake):
    """v2.3.6: an active_provider in config.json that is no longer
    registered (stale id / failed custom provider) must not crash the
    bridge — fall back to a registered provider, like the daemon does.
    Previously _build_registry raised ProviderError and the TUI became
    unusable on startup."""
    from tera_pilot.utils import save_config

    save_config({"active_provider": "no-such-provider", "providers": {}})
    bridge = TeraPilotBridge(workspace=str(tmp_path))

    # These all used to raise; now they must return sane values.
    providers = bridge.list_providers()
    assert providers, "should still list registered providers"
    assert bridge.get_active_provider_id() in {p["id"] for p in providers}
    s = bridge.status()
    assert s.get("provider") is not None

    # ensure_agent must also survive.
    bridge.ensure_agent()
    assert bridge._agent is not None


def test_registry_configure_rejects_kwargs(reg):
    """Guard against reintroducing the broken kwargs call pattern."""
    from tera_pilot.providers import ProviderConfig
    with pytest.raises(TypeError):
        reg.configure("fake", model="x")
    with pytest.raises(TypeError):
        reg.configure("fake", api_key="x")
    # The correct call keeps working.
    reg.configure("fake", ProviderConfig(provider_id="fake", model="ok"))
    assert reg.get("fake").config.model == "ok"


# ── Notifier: status() must not self-deadlock (v2.3.5) ─────────────────

def test_notifier_status_no_self_deadlock(tmp_path):
    """Notifier.status() must not deadlock on its own lock.

    Regression: Notifier._lock was a plain threading.Lock, and
    status() called list_backends() while holding that lock —
    list_backends() takes the same lock, so status() hung forever
    (a fresh bridge's /notify hung the TUI thread).
    """
    import threading
    from tera_pilot.notifier import Notifier

    n = Notifier()

    # Run in a thread with a timeout; a deadlock would hang here.
    result = {}
    t = threading.Thread(
        target=lambda: result.setdefault("out", n.status()), daemon=True
    )
    t.start()
    t.join(5)
    assert not t.is_alive(), "Notifier.status() deadlocked on its own lock"
    assert "total_backends" in result["out"]


def test_notifier_rlock_is_reentrant():
    """The lock must be an RLock so nested locked calls don't hang."""
    from tera_pilot.notifier import Notifier

    n = Notifier()
    assert isinstance(n._lock, type(__import__("threading").RLock())), \
        "Notifier._lock must be reentrant (RLock)"


# ── MCP-server status after agent spawn (v2.3.5) ───────────────────────

def test_mcp_server_status_no_agent_no_crash(tmp_path, reg, fake):
    """/mcp-server must work before AND after the agent is spawned.

    Regression: mcp_server_status() read self._agent.workspace, but the
    AgentRuntime has no .workspace attribute — so after the first turn
    (when _agent is set) /mcp-server crashed with AttributeError.
    """
    bridge = TeraPilotBridge(workspace=str(tmp_path))

    # Before any agent exists.
    r1 = bridge.mcp_server_status()
    assert r1.get("ok") is True

    # Spawn the agent like a real turn does.
    bridge.ensure_agent()

    # After the agent exists — this used to raise AttributeError.
    r2 = bridge.mcp_server_status()
    assert r2.get("ok") is True


# ── QuickSettingsModal Advanced button (v2.3.5) ────────────────────────

def test_quick_settings_advanced_callback_fires():
    """The /settings 'Advanced…' button must hand off to full settings.

    Regression: QuickSettingsModal dismissed to nothing on 'Advanced…'
    (the code claimed "the caller will open the full model palette" but
    no callback was ever wired), so the button was dead.
    """
    from tera_pilot_tui.widgets.settings_modal import QuickSettingsModal

    fired = []
    modal = QuickSettingsModal(None, on_advanced=lambda: fired.append(True))
    assert modal._on_advanced is not None
    modal._on_advanced()
    assert fired == [True]


# ── GitHub repo set: single "owner/repo" arg (v2.3.5) ───────────────────

def test_github_set_repo_accepts_single_slash_string(tmp_path):
    """/github repo owner/repo must work with the one-arg form.

    Regression: the TUI's /github repo command passed the whole
    ``owner/repo`` string as a single argument, but bridge's
    github_set_repo(owner, repo) required two — TypeError, and the
    repo was never set.
    """
    from tera_pilot_tui.bridge import TeraPilotBridge
    from tera_pilot.github_automation import get_github_automation

    bridge = TeraPilotBridge(workspace=str(tmp_path))

    # One-arg form (what the TUI sends).
    r = bridge.github_set_repo("octocat/Hello-World")
    assert r.get("ok") is True, r
    assert r.get("repo") == "octocat/Hello-World"
    assert get_github_automation()._repo == "octocat/Hello-World"

    # Two-arg form still works.
    r2 = bridge.github_set_repo("octocat", "repo2")
    assert r2.get("ok") is True, r2
    assert r2.get("repo") == "octocat/repo2"

    # Invalid forms are rejected, not crashed.
    r3 = bridge.github_set_repo("not-a-slash-string")
    assert r3.get("ok") is False


# ── Guardian level on a fresh agent (v2.3.5) ───────────────────────────

def test_guardian_level_fresh_agent(tmp_path, reg, fake):
    """/guardian <level> must work on a freshly-spawned agent.

    Regression: ToolEngine.__init__ sets _guardian_config = None, so
    ``hasattr`` was always True and set_guardian_level crashed with
    AttributeError: 'NoneType' object has no attribute 'provider_id'.
    """
    from tera_pilot_tui.bridge import TeraPilotBridge

    bridge = TeraPilotBridge(workspace=str(tmp_path))

    r = bridge.set_guardian_level("all")
    assert r.get("ok") is True, r
    assert bridge.get_guardian_level().get("level") == "all"

    # Switching levels preserves provider/model from the previous config.
    r2 = bridge.set_guardian_level("dangerous_only")
    assert r2.get("ok") is True, r2
    assert bridge.get_guardian_level().get("level") == "dangerous_only"

    # Invalid level is rejected cleanly.
    r3 = bridge.set_guardian_level("bogus")
    assert r3.get("ok") is False
