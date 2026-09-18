"""Tests for configurable endurance limits (v2.4.0) — «work longer».

Two halves:

- the ``tera_pilot.endurance`` policy object (derivation, clamping,
  config round-trip, environment overrides);
- the ``AgentRuntime`` honoring it (hard ceiling, extension margin,
  wall-clock budget) — driven by the deterministic FakeProvider, so no
  network or API key is involved.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect HOME so no real ~/.tera_pilot config leaks into tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in (
        "TERA_PILOT_HARD_MAX_ITERATIONS",
        "TERA_PILOT_RUN_MAX_SECONDS",
        "TERA_PILOT_ITERATION_EXTEND_FACTOR",
        "TERA_PILOT_ITERATION_EXTEND_MARGIN",
        "TERA_PILOT_HARD_ITERATION_CEILING",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def fake():
    from fake_provider import FakeProvider
    from tera_pilot.providers import get_registry
    reg = get_registry()
    reg.register(FakeProvider)
    fp = FakeProvider()
    reg._instances["fake"] = fp
    reg.set_active("fake")
    yield reg, fp
    reg._instances.pop("fake", None)


# ── Policy object ──────────────────────────────────────────────────────


def test_hard_ceiling_defaults_match_historical_behavior():
    """Defaults must reproduce pre-v2.4.0 exactly: 3× soft, floor 40,
    ceiling 200 — otherwise existing runs silently change length."""
    from tera_pilot.endurance import EnduranceLimits
    lim = EnduranceLimits()
    assert lim.hard_for(8) == 40      # 24 → floor 40
    assert lim.hard_for(3) == 40      # 9  → floor 40
    assert lim.hard_for(20) == 60     # 3×
    assert lim.hard_for(100) == 200   # 300 → ceiling 200
    assert lim.hard_for(0) == 40


def test_explicit_hard_iterations_wins_but_never_below_soft():
    from tera_pilot.endurance import EnduranceLimits
    lim = EnduranceLimits(hard_iterations=5)
    assert lim.hard_for(3) == 5
    assert lim.hard_for(50) == 50


def test_wall_clock_default_is_unlimited():
    from tera_pilot.endurance import EnduranceLimits
    assert EnduranceLimits().wall_clock_enabled() is False
    assert EnduranceLimits(max_wall_seconds=600).wall_clock_enabled() is True


def test_from_dict_clamps_nonsense_values():
    from tera_pilot.endurance import EnduranceLimits
    lim = EnduranceLimits.from_dict({
        "hard_iterations": -5,
        "extend_factor": 0,
        "hard_floor": -1,
        "extend_margin": -3,
        "max_wall_seconds": -10,
    })
    assert lim.hard_iterations == 0
    assert lim.extend_factor == 1
    assert lim.hard_floor == 1
    assert lim.extend_margin == 0
    assert lim.max_wall_seconds == 0.0


def test_normalized_keeps_ceiling_above_floor():
    from tera_pilot.endurance import EnduranceLimits
    lim = EnduranceLimits(hard_floor=100, hard_ceiling=10).normalized()
    assert lim.hard_ceiling == 100


def test_config_round_trip_and_reset(tmp_path):
    from tera_pilot.endurance import (
        get_endurance_limits, reset_endurance_limits, set_endurance_limits,
    )
    assert get_endurance_limits().hard_iterations == 0

    set_endurance_limits(hard_iterations=500, max_wall_seconds=1800,
                         extend_margin=5)
    assert get_endurance_limits().hard_iterations == 500
    assert get_endurance_limits().max_wall_seconds == 1800
    assert get_endurance_limits().extend_margin == 5

    # Persisted under the nested `endurance` key, alongside other settings.
    cfg = json.loads((Path(tmp_path) / ".tera_pilot" / "config.json").read_text())
    assert cfg["endurance"]["hard_iterations"] == 500

    reset_endurance_limits()
    assert get_endurance_limits().hard_iterations == 0
    assert get_endurance_limits().extend_margin == 2


def test_config_write_preserves_other_keys(tmp_path):
    from tera_pilot.endurance import set_endurance_limits
    cfg_path = Path(tmp_path) / ".tera_pilot" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"active_provider": "ollama"}), encoding="utf-8")

    set_endurance_limits(hard_iterations=99)
    cfg = json.loads(cfg_path.read_text())
    assert cfg["active_provider"] == "ollama"
    assert cfg["endurance"]["hard_iterations"] == 99


def test_env_overrides_beat_config(tmp_path, monkeypatch):
    from tera_pilot.endurance import get_endurance_limits, set_endurance_limits
    set_endurance_limits(hard_iterations=100)
    monkeypatch.setenv("TERA_PILOT_HARD_MAX_ITERATIONS", "777")
    monkeypatch.setenv("TERA_PILOT_RUN_MAX_SECONDS", "60")
    lim = get_endurance_limits()
    assert lim.hard_iterations == 777
    assert lim.max_wall_seconds == 60.0


def test_env_garbage_is_ignored():
    import os
    from tera_pilot.endurance import get_endurance_limits
    os.environ["TERA_PILOT_HARD_MAX_ITERATIONS"] = "not-a-number"
    try:
        assert get_endurance_limits().hard_iterations == 0
    finally:
        os.environ.pop("TERA_PILOT_HARD_MAX_ITERATIONS", None)


def test_describe_mentions_the_effective_numbers():
    from tera_pilot.endurance import EnduranceLimits
    lines = "\n".join(EnduranceLimits().describe(soft=8))
    assert "soft cap:         8" in lines
    assert "hard ceiling:     40" in lines
    assert "unlimited" in lines


# ── Runtime integration ────────────────────────────────────────────────


def test_runtime_default_ceiling_is_unchanged(tmp_path, fake):
    from tera_pilot.agent_runtime import AgentRuntime
    reg, _ = fake
    rt = AgentRuntime(reg, workspace=str(tmp_path), max_iterations=3,
                      enable_planning=False)
    assert rt.max_iterations == 3
    assert rt.hard_max_iterations == 40  # historical derivation


def test_runtime_honors_explicit_hard_ceiling(tmp_path, fake):
    from tera_pilot.agent_runtime import AgentRuntime
    from tera_pilot.endurance import EnduranceLimits
    reg, fp = fake
    fp._script = [
        '{"tool": "write_file", "args": {"path": "f%d.txt", "content": "%d"}}' % (i, i)
        for i in range(1, 7)
    ]
    rt = AgentRuntime(reg, workspace=str(tmp_path), max_iterations=3,
                      enable_planning=False,
                      endurance=EnduranceLimits(hard_iterations=5))
    rt.tools.autonomy = "never_ask"
    assert rt.hard_max_iterations == 5

    result = rt.run("do a long task")
    assert result.success is False
    assert "Max iterations (5) reached" in (result.error or "")
    for i in range(1, 6):
        assert (tmp_path / f"f{i}.txt").exists()
    assert not (tmp_path / "f6.txt").exists()


def test_productive_run_extends_with_generous_margin(tmp_path, fake):
    """The default policy still auto-extends a productive run past the
    soft cap (this is the «work longer» behavior users rely on)."""
    from tera_pilot.agent_runtime import AgentRuntime
    reg, fp = fake
    fp._script = [
        '{"tool": "write_file", "args": {"path": "g%d.txt", "content": "%d"}}' % (i, i)
        for i in range(1, 6)
    ] + ['{"final_answer": "finished"}']
    rt = AgentRuntime(reg, workspace=str(tmp_path), max_iterations=3,
                      enable_planning=False)
    rt.tools.autonomy = "never_ask"
    result = rt.run("do a big multi-step task")
    assert result.success is True
    assert result.output == "finished"
    for i in range(1, 6):
        assert (tmp_path / f"g{i}.txt").exists()


def test_margin_zero_disables_extension(tmp_path, fake):
    """``extend_margin=0`` makes the soft cap strict again — a user can
    opt out of auto-extension without lowering the hard ceiling."""
    from tera_pilot.agent_runtime import AgentRuntime
    from tera_pilot.endurance import EnduranceLimits
    reg, fp = fake
    fp._script = [
        '{"tool": "write_file", "args": {"path": "h%d.txt", "content": "%d"}}' % (i, i)
        for i in range(1, 6)
    ]
    rt = AgentRuntime(reg, workspace=str(tmp_path), max_iterations=3,
                      enable_planning=False,
                      endurance=EnduranceLimits(extend_margin=0))
    rt.tools.autonomy = "never_ask"
    result = rt.run("do a big multi-step task")
    assert result.success is False
    assert "Max iterations (3) reached" in (result.error or "")
    assert not (tmp_path / "h4.txt").exists()


def test_wall_clock_budget_stops_the_run(tmp_path, fake, monkeypatch):
    """A wall-clock budget stops the run between iterations instead of
    letting a slow task keep one turn alive forever.

    Time is faked (advancing 1000 s per call) so the test is fully
    deterministic — the real clock would make the boundary racy.
    """
    import time as real_time

    import tera_pilot.agent_runtime.runtime as rt_mod
    from tera_pilot.agent_runtime import AgentRuntime
    from tera_pilot.endurance import EnduranceLimits

    class _FakeTime:
        def __init__(self):
            self.t = 1_000_000.0

        def time(self):
            self.t += 1000.0
            return self.t

        def sleep(self, *args, **kwargs):
            return None

        def __getattr__(self, name):  # delegate everything else
            return getattr(real_time, name)

    monkeypatch.setattr(rt_mod, "time", _FakeTime())

    reg, fp = fake
    rt = AgentRuntime(reg, workspace=str(tmp_path), max_iterations=8,
                      enable_planning=False,
                      endurance=EnduranceLimits(max_wall_seconds=30))
    rt.tools.autonomy = "never_ask"
    result = rt.run("anything")
    assert result.success is False
    assert "wall-clock budget" in (result.error or "")
    # The budget is checked before the first LLM call, so no provider
    # call was made at all.
    assert fp.call_count == 0
    assert result.iterations == 0


def test_bridge_exposes_and_applies_endurance_limits(tmp_path, fake):
    """The TUI bridge must both report the policy and apply a change to
    the LIVE agent — otherwise a user who just raised the ceiling would
    have to restart the TUI for it to matter."""
    from tera_pilot_tui.bridge import ProviderChoice, TeraPilotBridge
    reg, fp = fake
    bridge = TeraPilotBridge(
        workspace=str(tmp_path), provider=ProviderChoice(provider_id="fake"),
    )
    got = bridge.get_endurance_limits()
    assert got["ok"] is True
    assert got["hard_iterations"] == 40  # default derivation for soft=8

    r = bridge.set_endurance_limits(hard_iterations=150, max_wall_seconds=120)
    assert r["ok"] is True
    assert r["hard_iterations"] == 150
    assert r["wall_clock_enabled"] is True

    bridge.ensure_agent()
    assert bridge._agent.endurance.hard_iterations == 150
    assert bridge._agent.hard_max_iterations == 150
    assert bridge._agent.endurance.max_wall_seconds == 120.0


@pytest.mark.asyncio
async def test_tui_endurance_slash_command_applies_changes(tmp_path, fake):
    """End-to-end through the real Textual app: /endurance show + set must
    not crash and must persist the policy."""
    pytest.importorskip("textual")
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.bridge import ProviderChoice, TeraPilotBridge

    reg, fp = fake
    bridge = TeraPilotBridge(
        workspace=str(tmp_path), provider=ProviderChoice(provider_id="fake"),
    )
    app = TeraPilotTUIApp(bridge=bridge)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._exec_endurance("")
        await pilot.pause()
        app._exec_endurance("iterations 120")
        await pilot.pause()
        app._exec_endurance("seconds 60")
        await pilot.pause()
        assert app._exception is None

    from tera_pilot.endurance import get_endurance_limits
    limits = get_endurance_limits()
    assert limits.hard_iterations == 120
    assert limits.max_wall_seconds == 60.0


@pytest.fixture(autouse=True)
def _reset_endurance_between_tests():
    """Keep the on-disk policy from leaking between tests in this module."""
    yield
    try:
        from tera_pilot.endurance import reset_endurance_limits
        reset_endurance_limits()
    except Exception:
        pass


def test_no_wall_clock_cap_by_default(tmp_path, fake):
    from tera_pilot.agent_runtime import AgentRuntime
    reg, fp = fake
    fp._script = ['{"final_answer": "done"}']
    rt = AgentRuntime(reg, workspace=str(tmp_path), max_iterations=2,
                      enable_planning=False)
    rt.tools.autonomy = "never_ask"
    assert rt.endurance.max_wall_seconds == 0.0
    result = rt.run("hi")
    assert result.success is True
