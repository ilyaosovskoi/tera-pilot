"""Local-prototype tests for smart routing/failover + fuzzy menus.

NOT COMMITTED (per task scope): behavioural checks for
tera_pilot/smart_route.py, the runtime smart branch, /route status
and tera_pilot_tui/fuzzy.py + menu rendering.
"""

import json
import sys

import pytest

sys.path.insert(0, "tests")

from tera_pilot.providers.base import ProviderConfig, ProviderError  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def smart_home(isolated_home):
    (isolated_home / ".tera_pilot").mkdir(parents=True, exist_ok=True)
    (isolated_home / ".tera_pilot" / "config.json").write_text(json.dumps({
        "router_smart_failover": True,
        "router_local_first": True,
        "router_call_timeout_s": 5,
        "router_slow_secs": 30,
        "router_max_failovers": 3,
    }))
    return isolated_home


class FakeGroq(FakeProvider):
    provider_id = "groq"


class FakeOllama(FakeProvider):
    provider_id = "ollama"


class FakeCerebras(FakeProvider):
    provider_id = "cerebras"


class StubRegistry:
    """Duck-typed registry: configured set + instances by pid."""

    def __init__(self, instances, configured=None):
        self._instances = dict(instances)
        first = next(iter(instances.values()))
        self._active = first
        self._configured = (configured if configured is not None
                            else set(instances))

    @property
    def active(self):
        return self._active

    def get(self, pid):
        return self._instances.get(pid)

    def list_providers(self):
        return [{"id": pid, "configured": pid in self._configured}
                for pid in self._instances]


def _cfg(**kw):
    base = {"router_smart_failover": False, "router_local_first": True,
            "router_slow_secs": 120.0, "router_call_timeout_s": 5.0,
            "router_max_failovers": 3, "router_down_after_errors": 3,
            "router_down_cooldown_s": 300.0, "router_probe_ttl_s": 60.0}
    base.update(kw)
    return base


# ── health store ────────────────────────────────────────────────────

def test_health_down_and_recovery():
    from tera_pilot.smart_route import HealthStore
    store = HealthStore()
    cfg = _cfg(router_down_after_errors=2, router_down_cooldown_s=1000)
    assert store.status("x", cfg) == "ok"
    store.record_error("x")
    assert store.status("x", cfg) == "ok"
    store.record_error("x")
    assert store.status("x", cfg) == "down"
    store.record_success("x", 1.0)
    assert store.status("x", cfg) == "ok"
    cfg2 = _cfg(router_down_after_errors=2, router_down_cooldown_s=0)
    store.record_error("x")
    store.record_error("x")
    assert store.status("x", cfg2) == "ok"  # cooldown elapsed


def test_health_slow_ema():
    from tera_pilot.smart_route import HealthStore
    store = HealthStore()
    cfg = _cfg(router_slow_secs=10)
    store.record_success("s", 30.0)
    assert store.status("s", cfg) == "ok"  # needs >= 2 calls
    store.record_success("s", 30.0)
    assert store.status("s", cfg) == "slow"
    assert store.snapshot(cfg)[0]["ema_latency_s"] == 30.0


# ── local probes ────────────────────────────────────────────────────

def test_local_probe_cached(monkeypatch):
    from tera_pilot import smart_route as sr
    sr.reset_probe_cache_for_test()
    calls = []

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(sr.urllib.request, "urlopen", fake_urlopen)
    assert sr.local_reachable("ollama") is True
    assert sr.local_reachable("ollama") is True
    assert len(calls) == 1  # second hit served from cache
    assert sr.local_reachable("openai") is False  # not a local pid
    sr.reset_probe_cache_for_test()


def test_local_probe_down(monkeypatch):
    from tera_pilot import smart_route as sr
    sr.reset_probe_cache_for_test()

    def boom(req, timeout=None):
        raise ConnectionError("refused")

    monkeypatch.setattr(sr.urllib.request, "urlopen", boom)
    assert sr.local_reachable("lmstudio", "http://localhost:1234/v1") is False
    sr.reset_probe_cache_for_test()


# ── ordering ────────────────────────────────────────────────────────

def test_order_local_first_for_trivial(monkeypatch):
    from tera_pilot import smart_route as sr
    sr.reset_probe_cache_for_test()
    monkeypatch.setattr(sr, "local_reachable", lambda pid, base="", **kw: pid == "ollama")
    groq = FakeGroq(ProviderConfig(provider_id="groq", model="m"))
    ollama = FakeOllama(ProviderConfig(provider_id="ollama", model="m"))
    reg = StubRegistry({"groq": groq, "ollama": ollama})
    cands, decision = sr.order_candidates("fix typo", reg, cfg=_cfg())
    assert decision["complexity"] == "trivial"
    assert cands[0].provider_id == "ollama"
    assert decision["local_first_applied"] is True
    sr.reset_probe_cache_for_test()


def test_order_drops_down_providers():
    from tera_pilot import smart_route as sr
    from tera_pilot.smart_route import get_health_store
    store = get_health_store()
    store.reset_for_test()
    groq = FakeGroq(ProviderConfig(provider_id="groq", model="m"))
    ollama = FakeOllama(ProviderConfig(provider_id="ollama", model="m"))
    reg = StubRegistry({"groq": groq, "ollama": ollama})
    for _ in range(3):
        store.record_error("groq")
    cands, decision = sr.order_candidates(
        "fix typo", reg, cfg=_cfg(router_local_first=False))
    pids = [c.provider_id for c in cands]
    assert "groq" not in pids
    assert decision["dropped_down"] == ["groq"]
    store.reset_for_test()


# ── failover executor ───────────────────────────────────────────────

def _resp(text, pid):
    from tera_pilot.providers.base import ProviderResponse
    return ProviderResponse(text=text, model="m", provider=pid)


def test_failover_error_then_ok():
    from tera_pilot.smart_route import Candidate, generate_with_failover, get_health_store
    get_health_store().reset_for_test()

    class Boom:
        provider_id = "a"

        def generate(self, messages, **kw):
            raise ProviderError("HTTP 503 Service Unavailable")

    class Good:
        provider_id = "b"

        def generate(self, messages, **kw):
            return _resp("fine", "b")

    class Reg:
        def get(self, pid):
            return {"a": Boom(), "b": Good()}[pid]

    resp, rep = generate_with_failover(
        Reg(), [{"role": "user", "content": "hi"}],
        [Candidate("a", "m1"), Candidate("b", "m2")], timeout_s=5.0)
    assert resp.text == "fine"
    assert rep["used"]["provider_id"] == "b"
    assert [a["outcome"] for a in rep["attempts"]] == ["error", "ok"]
    get_health_store().reset_for_test()


def test_failover_timeout_abandons_and_continues():
    import time as _time
    from tera_pilot.smart_route import Candidate, generate_with_failover, get_health_store
    get_health_store().reset_for_test()

    class Slow:
        provider_id = "s"

        def generate(self, messages, **kw):
            _time.sleep(30)
            return _resp("slow", "s")

    class Good:
        provider_id = "g"

        def generate(self, messages, **kw):
            return _resp("fast", "g")

    class Reg:
        def get(self, pid):
            return {"s": Slow(), "g": Good()}[pid]

    t0 = _time.time()
    resp, rep = generate_with_failover(
        Reg(), [{"role": "user", "content": "hi"}],
        [Candidate("s", "m1"), Candidate("g", "m2")], timeout_s=2.0)
    elapsed = _time.time() - t0
    assert resp.text == "fast"
    assert elapsed < 10
    assert [a["outcome"] for a in rep["attempts"]] == ["timeout", "ok"]
    get_health_store().reset_for_test()


def test_failover_non_retryable_raises_at_once():
    from tera_pilot.smart_route import Candidate, generate_with_failover
    from tera_pilot.providers.base import ProviderError

    class Auth:
        provider_id = "a"

        def generate(self, messages, **kw):
            raise ProviderError("HTTP 401 invalid API key")

    class Reg:
        def get(self, pid):
            return Auth()

    with pytest.raises(ProviderError):
        generate_with_failover(
            Reg(), [{"role": "user", "content": "hi"}],
            [Candidate("a", "m1"), Candidate("b", "m2")], timeout_s=5.0)


def test_failover_cancel_stops_chain():
    from tera_pilot.smart_route import Candidate, generate_with_failover

    class Never:
        provider_id = "n"

        def generate(self, messages, **kw):
            raise ProviderError("HTTP 500 boom")

    class Reg:
        def get(self, pid):
            return Never()

    with pytest.raises(RuntimeError, match="cancelled"):
        generate_with_failover(
            Reg(), [{"role": "user", "content": "hi"}],
            [Candidate("n", "m1")], timeout_s=5.0,
            cancel_check=lambda: True)


# ── runtime smart branch ────────────────────────────────────────────

def test_runtime_smart_failover_end_to_end(smart_home, monkeypatch):
    from tera_pilot import smart_route as sr
    from tera_pilot.smart_route import get_health_store
    from tera_pilot.agent_runtime import AgentRuntime

    get_health_store().reset_for_test()
    sr.reset_probe_cache_for_test()
    # local "down" so the cloud candidate leads, then fails over to local
    monkeypatch.setattr(sr, "local_reachable", lambda pid, base="", **kw: False)
    groq = FakeGroq(
        ProviderConfig(provider_id="groq", model="m"),
        errors=[ProviderError("HTTP 503 Service Unavailable")],
        script=['{"final_answer": "unused"}'],
    )
    cerebras = FakeCerebras(
        ProviderConfig(provider_id="cerebras", model="m"),
        script=['{"final_answer": "hello from backup cloud"}'],
    )
    ollama = FakeOllama(
        ProviderConfig(provider_id="ollama", model="m"),
        script=['{"final_answer": "hello from local"}'],
    )
    reg = StubRegistry({"groq": groq, "cerebras": cerebras, "ollama": ollama})
    agent = AgentRuntime(registry=reg, workspace=str(smart_home),
                         max_iterations=2, enable_planning=False)
    # Pin the candidate chain (ordering itself is unit-tested above) so
    # this test proves RUNTIME integration: smart branch → failover →
    # success → last_route + health.
    from tera_pilot.smart_route import Candidate
    monkeypatch.setattr(
        sr, "order_candidates",
        lambda prompt, registry, **kw: (
            [Candidate("groq", "m1", "test"), Candidate("cerebras", "m2", "test")],
            {"complexity": "simple", "local_first_applied": False,
             "dropped_down": [], "reasoning": "test"}))
    result = agent.run("fix typo in readme")
    assert result.success is True
    assert "hello from backup cloud" in result.output
    last = agent._last_route
    assert last is not None
    assert last["used"]["provider_id"] == "cerebras"
    assert [a["outcome"] for a in last["attempts"]] == ["error", "ok"]
    health = {h["provider_id"]: h for h in get_health_store().snapshot()}
    assert health["groq"]["consec_errors"] == 1
    get_health_store().reset_for_test()
    sr.reset_probe_cache_for_test()


def test_order_appends_reachable_local(smart_home, monkeypatch):
    """SIMPLE-tier prompts gain a reachable local fallback even though
    the shipped tiers list locals only under TRIVIAL."""
    from tera_pilot import smart_route as sr
    sr.reset_probe_cache_for_test()
    monkeypatch.setattr(sr, "local_reachable",
                        lambda pid, base="", **kw: pid == "ollama")
    groq = FakeGroq(ProviderConfig(provider_id="groq", model="m"))
    ollama = FakeOllama(ProviderConfig(provider_id="ollama", model="m"))
    reg = StubRegistry({"groq": groq, "ollama": ollama})
    cands, decision = sr.order_candidates(
        "refactor this function for clarity", reg, cfg=_cfg())
    pids = [c.provider_id for c in cands]
    assert "ollama" in pids
    sr.reset_probe_cache_for_test()


def test_runtime_smart_off_by_default(smart_home, monkeypatch):
    import json as _json
    (smart_home / ".tera_pilot" / "config.json").write_text(_json.dumps({}))
    from tera_pilot.smart_route import load_smart_config
    assert load_smart_config()["router_smart_failover"] is False


# ── fuzzy matcher ───────────────────────────────────────────────────

def test_fuzzy_basics():
    from tera_pilot_tui.fuzzy import match_score, subsequence_positions
    assert subsequence_positions("cm", "/commit") == [1, 3]
    assert subsequence_positions("xyz", "/commit") is None
    score, pos = match_score("rev", "review", "AI-powered code review")
    assert score > 0 and pos == [0, 1, 2]
    assert match_score("xyz", "review", "code") == (0.0, [])


def test_fuzzy_ranking_prefers_names():
    from tera_pilot_tui.fuzzy import match_commands
    from tera_pilot_tui.widgets.command_palette import BUILTIN_COMMANDS
    top = [c.id for c, _s, _p in match_commands("commit", BUILTIN_COMMANDS)[:2]]
    assert top[0] == "commit"
    ids = [c.id for c, _s, _p in match_commands("cm", BUILTIN_COMMANDS)]
    assert "commit" in ids[:3]
    assert len(match_commands("", BUILTIN_COMMANDS)) == len(BUILTIN_COMMANDS)


def test_fuzzy_positions_highlight_label():
    from tera_pilot_tui.fuzzy import match_commands, highlight_text
    from tera_pilot_tui.widgets.command_palette import BUILTIN_COMMANDS
    matched = match_commands("rev", BUILTIN_COMMANDS)
    assert matched
    cmd, _score, pos = matched[0]
    assert cmd.id == "review"
    text = highlight_text(cmd.label, pos)
    assert text.plain == cmd.label


@pytest.mark.asyncio
async def test_palette_fuzzy_filter_counts():
    from tera_pilot_tui.app import TeraPilotTUIApp

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_command_palette()
        await pilot.pause(0.3)
        from textual.widgets import OptionList
        ol = app.screen.query_one(OptionList)
        assert ol.option_count > 0
        await pilot.press("c", "m")
        await pilot.pause(0.4)
        assert ol.option_count > 0
        # highlighted row must be a real command, never a group header
        # (the palette screen itself hosts _option_ids)
        highlighted = ol.highlighted
        assert highlighted is not None
        assert app.screen._option_ids[highlighted] != ""
        assert app._exception is None


@pytest.mark.asyncio
async def test_inline_suggestions_fuzzy():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox
    from tera_pilot_tui.widgets.command_suggestions import CommandSuggestions

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box = app.query_one(InputBox)
        box.value = "/cm"
        await pilot.pause(0.4)
        sug = app.query_one(CommandSuggestions)
        ids = [item.id for item in sug._items]
        assert "commit" in ids
        assert app._exception is None
