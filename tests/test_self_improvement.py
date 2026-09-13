"""Tests for the self-improvement loop (v2.4.0).

Covers the three layers the module promises:

1. ``analyze_run`` turns a finished run into evidence-backed proposals
   (budget exhaustion, no tool use, unverified writes, tool-error rate,
   repeated identical calls, file thrashing, empty plan).
2. ``ImprovementBacklog`` persists them per project, dedupes recurring
   signals by fingerprint, and injects the top ones as rules to avoid.
3. ``build_self_task`` / ``handle_improve_command`` prepare a real
   dogfooding task — but only for the Tera Pilot repo.

Hermetic: ``HOME`` is redirected to ``tmp_path``, so nothing touches the
developer's real ~/.tera_pilot.
"""

import sys
from pathlib import Path

import pytest

# tests/ is not a package — add it to sys.path so `fake_provider` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TERA_PILOT_ALLOW_SELF_TASK", raising=False)
    return tmp_path


# ── Run-result stubs (duck-typed, like analyze_run expects) ────────────


class _Call:
    def __init__(self, name, args=None, error=None):
        self.name = name
        self.args = args or {}
        self.error = error


class _Step:
    def __init__(self, name=None, args=None, obs="", thought=""):
        self.thought = thought
        self.observation = obs
        self.action = _Call(name, args) if name else None


class _Result:
    def __init__(self, *, success=True, output="", error=None, iterations=0,
                 steps=None, tool_calls=None, plan=None, metadata=None):
        self.success = success
        self.output = output
        self.error = error
        self.iterations = iterations
        self.steps = steps or []
        self.tool_calls = tool_calls or []
        self.plan = plan
        self.metadata = metadata or {}


def _proposal_categories(proposals):
    return {p.category for p in proposals}


# ── analyze_run ────────────────────────────────────────────────────────


def test_analyze_skips_cancelled_and_quota_runs():
    from tera_pilot.self_improvement import analyze_run
    assert analyze_run(_Result(error="Cancelled by user")) == []
    assert analyze_run(_Result(error="quota_exhausted")) == []
    assert analyze_run(_Result(error="awaiting_plan_approval")) == []


def test_analyze_flags_exhausted_budget_with_measured_evidence():
    from tera_pilot.self_improvement import analyze_run
    proposals = analyze_run(
        _Result(error="Max iterations (40) reached", iterations=40,
                metadata={"total_tokens_in": 1234, "total_tokens_out": 567}),
        workspace="/tmp/ws", planned_iterations=8,
    )
    budget = [p for p in proposals if p.category == "budget"]
    assert len(budget) == 1
    p = budget[0]
    assert p.severity == "high"
    # Evidence must be the real numbers from the run, not prose.
    assert "Max iterations (40) reached" in p.evidence
    assert "iterations=40" in p.evidence
    assert "1234" in p.evidence and "567" in p.evidence


def test_analyze_flags_write_without_verification():
    from tera_pilot.self_improvement import analyze_run
    proposals = analyze_run(
        _Result(iterations=2, steps=[
            _Step("write_file", {"path": "a.py", "content": "x"}, obs="wrote a.py"),
            _Step(None, obs="done"),
        ]),
    )
    ver = [p for p in proposals if p.category == "verification"]
    assert len(ver) == 1
    assert "a.py" in ver[0].evidence
    assert ver[0].severity == "high"


def test_analyze_does_not_flag_verified_write():
    from tera_pilot.self_improvement import analyze_run
    proposals = analyze_run(
        _Result(iterations=3, steps=[
            _Step("write_file", {"path": "a.py"}, obs="wrote a.py"),
            _Step("execute_command", {"command": "pytest"}, obs="2 passed"),
        ]),
    )
    assert "verification" not in _proposal_categories(proposals)


def test_analyze_flags_high_tool_error_rate():
    from tera_pilot.self_improvement import analyze_run
    steps = [
        _Step("read_file", {"path": "/nope"}, obs="[FILE NOT FOUND] /nope"),
        _Step("read_file", {"path": "/nope2"}, obs="[FILE NOT FOUND] /nope2"),
        _Step("execute_command", {"command": "rm -rf /"}, obs="[BLOCKED] denied"),
        _Step("list_files", {}, obs="a.py b.py"),
    ]
    proposals = analyze_run(_Result(iterations=4, steps=steps))
    tooling = [p for p in proposals if p.category == "tooling"]
    assert len(tooling) == 1
    assert "3/4" in tooling[0].evidence
    assert tooling[0].severity == "high"  # 75% >= 60%


def test_analyze_flags_repeated_identical_calls():
    from tera_pilot.self_improvement import analyze_run
    same = {"path": "nope.py"}
    steps = [_Step("read_file", dict(same), obs="[FILE NOT FOUND] nope.py")
             for _ in range(3)]
    proposals = analyze_run(_Result(iterations=3, steps=steps))
    loops = [p for p in proposals if p.category == "loop"]
    assert len(loops) == 1
    assert loops[0].id  # stable fingerprint
    assert "3 identical" in loops[0].evidence


def test_analyze_flags_degraded_prose_run():
    from tera_pilot.self_improvement import analyze_run
    proposals = analyze_run(
        _Result(success=True, iterations=3, metadata={
            "degraded_prose": True, "task_type": "agentic",
        }),
    )
    prompting = [p for p in proposals if p.category == "prompting"]
    assert len(prompting) == 1
    assert "no_tool_use" in prompting[0].id or prompting[0].severity == "high"


def test_analyze_flags_file_thrashing():
    from tera_pilot.self_improvement import analyze_run
    steps = [_Step("str_replace", {"path": "a.py"}, obs="ok") for _ in range(4)]
    proposals = analyze_run(_Result(iterations=4, steps=steps))
    quality = [p for p in proposals if p.category == "quality"]
    assert len(quality) == 1
    assert "a.py" in quality[0].evidence


def test_analyze_is_sorted_by_severity():
    from tera_pilot.self_improvement import analyze_run
    proposals = analyze_run(
        _Result(error="Max iterations (40) reached", iterations=40,
                steps=[_Step("read_file", {"path": "x.py"}, obs="ok")] * 4,
                metadata={"degraded_prose": True, "task_type": "agentic"}),
    )
    ranks = [{"high": 0, "medium": 1, "low": 2}[p.severity] for p in proposals]
    assert ranks == sorted(ranks)


# ── Backlog ────────────────────────────────────────────────────────────


def test_backlog_dedupes_recurring_signals(tmp_path):
    from tera_pilot.self_improvement import ImprovementBacklog, analyze_run
    bl = ImprovementBacklog(tmp_path / "bl.jsonl")
    result = _Result(error="Max iterations (40) reached", iterations=40,
                     steps=[_Step("read_file", {"path": "a.py"}, obs="x=1")])

    first = bl.record(analyze_run(result, workspace=str(tmp_path)))
    assert len(first["created"]) == 1
    assert first["bumped"] == []

    second = bl.record(analyze_run(result, workspace=str(tmp_path)))
    assert second["created"] == []
    assert second["bumped"] == first["created"]

    items = bl.list(status="open")
    assert len(items) == len(first["created"])
    assert items[0].occurrences == 2


def test_backlog_persists_across_instances(tmp_path):
    from tera_pilot.self_improvement import ImprovementBacklog, analyze_run
    path = tmp_path / "bl.jsonl"
    ImprovementBacklog(path).record(analyze_run(
        _Result(error="Max iterations (40) reached", iterations=40,
                steps=[_Step("read_file", {"path": "a.py"}, obs="x=1")]),
    ))
    assert path.exists()
    reloaded = ImprovementBacklog(path)
    assert reloaded.counts()["open"] == 1


def test_backlog_status_transitions_and_top(tmp_path):
    from tera_pilot.self_improvement import ImprovementBacklog, analyze_run
    bl = ImprovementBacklog(tmp_path / "bl.jsonl")
    bl.record(analyze_run(_Result(error="Max iterations (40) reached", iterations=40)))
    bl.record(analyze_run(_Result(iterations=5, steps=[
        _Step("str_replace", {"path": "a.py"}, obs="ok") for _ in range(4)
    ])))
    top = bl.top()
    assert top is not None and top.severity == "high"

    assert bl.set_status(top.id, "applied") is True
    assert bl.counts()["applied"] == 1
    assert bl.get(top.id).status == "applied"
    assert bl.set_status("does-not-exist", "applied") is False

    with pytest.raises(ValueError):
        bl.set_status(top.id, "banana")


def test_backlog_dismiss_keeps_dismissal_on_recurrence(tmp_path):
    from tera_pilot.self_improvement import ImprovementBacklog, analyze_run
    bl = ImprovementBacklog(tmp_path / "bl.jsonl")
    res = _Result(error="Max iterations (40) reached", iterations=40)
    bl.record(analyze_run(res))
    pid = bl.list(status="open")[0].id
    bl.set_status(pid, "dismissed")
    bl.record(analyze_run(res))
    assert bl.get(pid).status == "dismissed"
    assert bl.get(pid).occurrences == 2


def test_fragment_lists_rules_to_avoid_and_skips_empty(tmp_path):
    from tera_pilot.self_improvement import ImprovementBacklog, analyze_run
    bl = ImprovementBacklog(tmp_path / "bl.jsonl")
    assert bl.to_fragment(str(tmp_path)) == ""

    bl.record(analyze_run(
        _Result(error="Max iterations (40) reached", iterations=40),
        workspace=str(tmp_path),
    ))
    frag = bl.to_fragment(str(tmp_path))
    assert "self_improvement" in frag
    assert "RULES TO AVOID" in frag
    assert "context_fragment" in frag


def test_fragment_respects_item_cap(tmp_path):
    from tera_pilot.self_improvement import ImprovementBacklog, analyze_run
    bl = ImprovementBacklog(tmp_path / "bl.jsonl")
    bl.record(analyze_run(_Result(error="Max iterations (40) reached", iterations=40)))
    bl.record(analyze_run(_Result(iterations=3, metadata={
        "degraded_prose": True, "task_type": "agentic"}, steps=[])))
    bl.record(analyze_run(_Result(iterations=5, steps=[
        _Step("str_replace", {"path": "a.py"}, obs="ok") for _ in range(4)
    ])))
    assert bl.counts()["open"] >= 3
    frag = bl.to_fragment(max_items=1)
    assert frag.count("- [") == 1


# ── observe_run ────────────────────────────────────────────────────────


def test_observe_run_records_and_reports(tmp_path):
    from tera_pilot.self_improvement import observe_run
    summary = observe_run(
        _Result(error="Max iterations (40) reached", iterations=40,
                steps=[_Step("read_file", {"path": "a.py"}, obs="x=1")]),
        workspace=str(tmp_path), section="general",
    )
    assert summary["enabled"] is True
    assert summary["recorded"] == 1
    assert len(summary["created"]) == 1
    assert Path(summary["path"]).exists()


def test_observe_run_respects_disabled_config(tmp_path):
    import json
    from tera_pilot.self_improvement import observe_run
    cfg_dir = Path(tmp_path) / ".tera_pilot"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"self_improvement": {"enabled": False}}), encoding="utf-8",
    )
    summary = observe_run(
        _Result(error="Max iterations (40) reached", iterations=40),
        workspace=str(tmp_path),
    )
    assert summary["enabled"] is False
    assert summary["recorded"] == 0


def test_observe_run_never_raises_on_garbage():
    from tera_pilot.self_improvement import observe_run
    assert observe_run(None, workspace="/tmp/x")["recorded"] == 0
    assert observe_run(object(), workspace="/tmp/x")["recorded"] == 0
    assert observe_run(_Result(), workspace="")["recorded"] == 0


# ── Dogfooding task + slash command ────────────────────────────────────


def test_is_tera_pilot_repo_detects_this_repo_and_rejects_others(tmp_path):
    from tera_pilot.self_improvement import is_tera_pilot_repo
    assert is_tera_pilot_repo(str(REPO_ROOT)) is True
    assert is_tera_pilot_repo(str(tmp_path)) is False


def test_is_tera_pilot_repo_env_override(tmp_path, monkeypatch):
    from tera_pilot.self_improvement import is_tera_pilot_repo
    monkeypatch.setenv("TERA_PILOT_ALLOW_SELF_TASK", "1")
    assert is_tera_pilot_repo(str(tmp_path)) is True


def test_build_self_task_contains_evidence_and_guardrails(tmp_path):
    from tera_pilot.self_improvement import (
        ImprovementBacklog, analyze_run, build_self_task,
        build_self_task_compact,
    )
    bl = ImprovementBacklog(tmp_path / "bl.jsonl")
    bl.record(analyze_run(_Result(error="Max iterations (40) reached", iterations=40)))
    p = bl.top()
    task = build_self_task(p, str(tmp_path))
    assert p.evidence in task
    assert "Max iterations (40) reached" in task
    assert "/improve done" in task and p.id in task
    assert "Do NOT weaken" in task

    # The composer variant must be single-line (the TUI Input widget is
    # single-line) while still carrying the guardrails and the id.
    compact = build_self_task_compact(p)
    assert "\n" not in compact
    assert p.id in compact
    assert "do NOT weaken" in compact
    assert p.evidence in compact


def test_handle_improve_command_list_show_task_done():
    from tera_pilot.self_improvement import (
        analyze_run, handle_improve_command, get_backlog,
        reset_backlog_cache_for_test,
    )
    reset_backlog_cache_for_test()
    bl = get_backlog(str(REPO_ROOT))
    bl.clear()
    bl.record(analyze_run(
        _Result(error="Max iterations (40) reached", iterations=40),
        workspace=str(REPO_ROOT),
    ))
    pid = bl.top().id

    listed = handle_improve_command(str(REPO_ROOT), "")
    assert listed["ok"] and pid in listed["text"]

    shown = handle_improve_command(str(REPO_ROOT), f"show {pid}")
    assert shown["ok"] and "Evidence" in shown["text"]

    task = handle_improve_command(str(REPO_ROOT), f"task {pid}")
    assert task["ok"] is True
    assert "Self-improvement task" in task["task_prompt"]
    assert "\n" not in task["composer_prompt"]

    done = handle_improve_command(str(REPO_ROOT), f"done {pid}")
    assert done["ok"] and bl.get(pid).status == "applied"

    bad = handle_improve_command(str(REPO_ROOT), "frobnicate")
    assert bad["ok"] is False and "Unknown" in bad["error"]


def test_handle_improve_command_refuses_foreign_repo(tmp_path):
    from tera_pilot.self_improvement import (
        analyze_run, get_backlog, handle_improve_command,
        reset_backlog_cache_for_test,
    )
    reset_backlog_cache_for_test()
    bl = get_backlog(str(tmp_path))
    bl.record(analyze_run(
        _Result(error="Max iterations (40) reached", iterations=40),
        workspace=str(tmp_path),
    ))
    pid = bl.top().id
    r = handle_improve_command(str(tmp_path), f"task {pid}")
    assert r["ok"] is False
    assert "not the Tera Pilot source tree" in r["text"]
    assert "task_prompt" not in r


def test_handle_improve_command_requires_workspace():
    from tera_pilot.self_improvement import handle_improve_command
    r = handle_improve_command("", "")
    assert r["ok"] is False and "workspace" in r["error"]


# ── Runtime integration ────────────────────────────────────────────────


@pytest.fixture
def fake(monkeypatch):
    from fake_provider import FakeProvider
    from tera_pilot.providers import get_registry
    reg = get_registry()
    reg.register(FakeProvider)
    fp = FakeProvider()
    reg._instances["fake"] = fp
    reg.set_active("fake")
    yield reg, fp
    reg._instances.pop("fake", None)


def test_bridge_improve_command_round_trip(fake):
    """The TUI bridge is the only glue between /improve and the loop."""
    from tera_pilot.self_improvement import (
        ImprovementBacklog, ImprovementProposal, get_backlog,
        reset_backlog_cache_for_test,
    )
    from tera_pilot_tui.bridge import ProviderChoice, TeraPilotBridge

    reg, fp = fake
    ws = str(REPO_ROOT)
    reset_backlog_cache_for_test()
    ImprovementBacklog(get_backlog(ws).path).record([
        ImprovementProposal(
            id="deadbeef01", category="budget", severity="high",
            title="Run ended at the ceiling", evidence="40 iterations",
            root_cause="task too big", suggested_action="split the task",
        )
    ])
    reset_backlog_cache_for_test()

    bridge = TeraPilotBridge(
        workspace=ws, provider=ProviderChoice(provider_id="fake"),
    )
    listed = bridge.handle_improve_command(ws, "")
    assert listed["ok"] is True and "deadbeef01" in listed["text"]

    task = bridge.handle_improve_command(ws, "task deadbeef01")
    assert task["ok"] is True
    assert "Self-improvement task" in task["task_prompt"]




@pytest.mark.asyncio
async def test_tui_improve_command_prefills_single_line_composer():
    """End-to-end: `/improve task` must pre-fill the composer with a
    single-line prompt (the InputBox is a Textual single-line Input)."""
    pytest.importorskip("textual")
    from tera_pilot.self_improvement import (
        ImprovementBacklog, ImprovementProposal, get_backlog,
        reset_backlog_cache_for_test,
    )
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.bridge import ProviderChoice, TeraPilotBridge
    from tera_pilot_tui.widgets.input_box import InputBox

    ws = str(REPO_ROOT)
    reset_backlog_cache_for_test()
    ImprovementBacklog(get_backlog(ws).path).record([
        ImprovementProposal(
            id="feedface01", category="verification", severity="high",
            title="Write without verification", evidence="1 write, 0 checks",
            root_cause="edit considered done on write",
            suggested_action="run the tests after editing",
        )
    ])
    reset_backlog_cache_for_test()

    bridge = TeraPilotBridge(
        workspace=ws, provider=ProviderChoice(provider_id="ollama"),
    )
    app = TeraPilotTUIApp(bridge=bridge)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._exec_improve("task feedface01")
        await pilot.pause()
        box = app.query_one(InputBox)
        assert "Self-improvement task" in box.value
        assert "feedface01" in box.value
        assert "\n" not in box.value
        assert app._exception is None


def test_runtime_records_proposals_after_a_run(tmp_path, fake):
    from tera_pilot.agent_runtime import AgentRuntime
    from tera_pilot.self_improvement import get_backlog, reset_backlog_cache_for_test

    reg, fp = fake
    reset_backlog_cache_for_test()
    fp._script = [
        # Write, then answer in prose without verifying: the run should
        # be recorded as an «unverified write» proposal.
        '{"tool": "write_file", "args": {"path": "a.py", "content": "x=1"}}',
        '{"final_answer": "done"}',
    ]
    rt = AgentRuntime(reg, workspace=str(tmp_path), max_iterations=8,
                      enable_planning=False)
    rt.tools.autonomy = "never_ask"
    result = rt.run("create a.py")
    assert result.success is True

    bl = get_backlog(str(tmp_path))
    open_items = bl.list(status="open")
    assert any(p.category == "verification" for p in open_items), open_items


def test_runtime_injects_self_improvement_fragment(tmp_path, fake):
    from tera_pilot.agent_runtime import AgentRuntime
    from tera_pilot.self_improvement import (
        ImprovementBacklog, ImprovementProposal, get_backlog,
        reset_backlog_cache_for_test,
    )

    reg, fp = fake
    reset_backlog_cache_for_test()
    # Seed a high-severity proposal in the workspace's backlog.
    ImprovementBacklog(get_backlog(str(tmp_path)).path).record([
        ImprovementProposal(
            id="abc1234567", category="loop", severity="high",
            title="Repeated identical read_file calls",
            evidence="3 identical calls",
            root_cause="no memory of previous attempts",
            suggested_action="force a different action after a repeated failure",
        )
    ])
    reset_backlog_cache_for_test()

    fp._script = ['{"final_answer": "nothing to do"}']
    rt = AgentRuntime(reg, workspace=str(tmp_path), max_iterations=2,
                      enable_planning=False)
    rt.tools.autonomy = "never_ask"
    rt.run("say hello")

    printed = "\n".join(
        str(m) for msgs in fp.recorded_messages for m in msgs
    )
    assert "self_improvement" in printed
    assert "force a different action" in printed
