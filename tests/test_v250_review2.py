"""Regression tests for the v2.5.0 review round 2.

Covers bugs found by manual audit (after the first fix round):
1. fuzzy.subsequence_positions must be case-insensitive on BOTH sides
   (old docstring said "lowered by the caller" but only text was lowered).
2. smart_route.prompt_text must accept dict-style messages
   (old code used getattr only -> dicts fell through to last message).
3. Runtime smart branch must record health exactly once per attempt
   (first round removed the double record_success; this test locks it).
4. /route display must list failed attempts, never the final ok.
5. Palette keyboard nav must skip group headers; Enter on a header must
   land on the next real command instead of doing nothing.
6. Slow-provider demotion must be by provider_id and survive truncation.
7. Local probe default timeout must stay <= 1s (ordering runs it
   synchronously; 2s x 3 locals stalled the first turn).
"""

import sys

sys.path.insert(0, "tests")

from tera_pilot.providers.base import ProviderConfig, ProviderError  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402


# ── 1. fuzzy case-insensitivity ───────────────────────────────────────

def test_subsequence_positions_case_insensitive():
    from tera_pilot_tui.fuzzy import subsequence_positions
    assert subsequence_positions("CM", "/commit") == [1, 3]
    assert subsequence_positions("cm", "/COMMIT") == [1, 3]
    assert subsequence_positions("Rev", "Review") == [0, 1, 2]
    assert subsequence_positions("xyz", "/commit") is None


def test_match_score_uppercase_query():
    from tera_pilot_tui.fuzzy import match_score
    score, pos = match_score("REV", "review", "code review")
    assert score > 0 and pos == [0, 1, 2]


def test_match_score_weak_desc_only_filtered():
    from tera_pilot_tui.fuzzy import match_score
    # single-char graze inside a long description must not surface
    score, _ = match_score("z", "commit", "a very long description about zzz nothing")
    assert score == 0.0


# ── 2. prompt_text dict messages ──────────────────────────────────────

def test_prompt_text_dict_messages():
    from tera_pilot.smart_route import prompt_text
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second question"},
    ]
    assert prompt_text(msgs) == "second question"


def test_prompt_text_mixed_objects_and_dicts():
    from tera_pilot.providers.base import ProviderMessage
    from tera_pilot.smart_route import prompt_text
    msgs = [
        ProviderMessage(role="user", content="obj question"),
        {"role": "user", "content": "dict question"},
    ]
    assert prompt_text(msgs) == "dict question"
    assert prompt_text([]) == ""


# ── 3. single health record per smart attempt ─────────────────────────

def test_runtime_smart_records_health_once(monkeypatch, tmp_path):
    import json
    from tera_pilot import smart_route as sr
    from tera_pilot.smart_route import Candidate, get_health_store
    from tera_pilot.agent_runtime import AgentRuntime

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".tera_pilot").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".tera_pilot" / "config.json").write_text(json.dumps({
        "router_smart_failover": True,
        "router_call_timeout_s": 5,
    }))
    get_health_store().reset_for_test()
    sr.reset_probe_cache_for_test()

    class Groq(FakeProvider):
        provider_id = "groq"

    class Cerebras(FakeProvider):
        provider_id = "cerebras"

    groq = Groq(ProviderConfig(provider_id="groq", model="m"),
                errors=[ProviderError("HTTP 503 down")],
                script=['{"final_answer": "unused"}'])
    cerebras = Cerebras(ProviderConfig(provider_id="cerebras", model="m"),
                        script=['{"final_answer": "backup ok"}'])

    class Reg:
        def __init__(self):
            self._active = groq

        @property
        def active(self):
            return self._active

        def get(self, pid):
            return {"groq": groq, "cerebras": cerebras}[pid]

        def list_providers(self):
            return [{"id": "groq", "configured": True},
                    {"id": "cerebras", "configured": True}]

    reg = Reg()
    agent = AgentRuntime(registry=reg, workspace=str(tmp_path),
                         max_iterations=2, enable_planning=False)
    monkeypatch.setattr(
        sr, "order_candidates",
        lambda prompt, registry, **kw: (
            [Candidate("groq", "m1", "t"), Candidate("cerebras", "m2", "t")],
            {"complexity": "simple", "local_first_applied": False,
             "dropped_down": [], "reasoning": "t"}))
    result = agent.run("fix typo")
    assert result.success is True
    snap = {h["provider_id"]: h for h in get_health_store().snapshot()}
    # exactly one error for groq, exactly one success-call for cerebras —
    # the old double-record bug gave cerebras n=2 here.
    assert snap["groq"]["consec_errors"] == 1
    assert snap["cerebras"]["calls"] == 1
    get_health_store().reset_for_test()


# ── 4. /route text shows failures, not the final ok ───────────────────

def test_exec_route_lists_failures_only():
    import asyncio

    async def _go():
        from tera_pilot_tui.app import TeraPilotTUIApp
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            said = []
            app._wf_say = lambda text, error=False: said.append(text)  # noqa: ARG005
            app.bridge.get_smart_status = lambda: {
                "ok": True, "enabled": True, "local_first": True,
                "call_timeout_s": 5, "slow_secs": 30, "health": [],
                "last_route": {
                    "complexity": "simple",
                    "used": {"provider_id": "cerebras", "model": "m2",
                             "latency_s": 1.2},
                    "attempts": [
                        {"provider_id": "groq", "outcome": "error",
                         "error": "HTTP 503 down"},
                        {"provider_id": "cerebras", "outcome": "ok",
                         "error": ""},
                    ],
                },
            }
            app._exec_route("")
            assert said, "expected _wf_say output"
            text = said[0]
            assert "failed over from groq" in text
            assert "failed over from cerebras" not in text
            assert "cerebras/m2" in text

    asyncio.run(_go())


def test_exec_route_no_route_yet():
    import asyncio

    async def _go():
        from tera_pilot_tui.app import TeraPilotTUIApp
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            said = []
            app._wf_say = lambda text, error=False: said.append(text)  # noqa: ARG005
            app.bridge.get_smart_status = lambda: {
                "ok": True, "enabled": False, "local_first": True,
                "call_timeout_s": 5, "slow_secs": 30, "health": [],
                "last_route": None,
            }
            app._exec_route("")
            assert "No routed turn yet" in said[0]

    asyncio.run(_go())


# ── 5. palette nav skips headers ──────────────────────────────────────

def test_palette_nav_skips_headers():
    import asyncio

    async def _go():
        from tera_pilot_tui.app import TeraPilotTUIApp
        from tera_pilot_tui.widgets.command_palette import CommandPalette
        from textual.widgets import OptionList
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Bare palette (no app on_result callback) so selecting
            # during the test has no side effects on the screen stack.
            pal = CommandPalette()
            app.push_screen(pal)
            await pilot.pause(0.3)
            ol = pal.query_one(OptionList)
            assert ol.option_count > 2
            # stepping down must never land on a group header
            first_real = next(i for i, oid in enumerate(pal._option_ids) if oid)
            ol.highlighted = first_real
            for _ in range(ol.option_count):
                before = ol.highlighted
                pal.action_navigate_down()
                after = ol.highlighted
                if after != before:
                    assert pal._option_ids[after] != "", \
                        f"nav landed on header at {after}"
                else:
                    break
            # stepping up must never land on a header either
            for _ in range(ol.option_count):
                before = ol.highlighted
                pal.action_navigate_up()
                after = ol.highlighted
                if after != before:
                    assert pal._option_ids[after] != "", \
                        f"nav landed on header at {after}"
                else:
                    break
            # Enter on a header must jump to a real command.
            # Use a fresh palette so the dismiss() below is the only one.
            pal.dismiss(result=None)
            await pilot.pause(0.2)
            pal2 = CommandPalette()
            app.push_screen(pal2)
            await pilot.pause(0.3)
            ol2 = pal2.query_one(OptionList)
            headers = [i for i, oid in enumerate(pal2._option_ids) if not oid]
            assert headers
            ol2.highlighted = headers[0]
            pal2.action_select_item()
            await pilot.pause(0.2)
            # dismissed with the next real command (not None, not header)
            assert app.screen is not pal2
            assert app._exception is None

    asyncio.run(_go())


# ── 6. slow demotion by provider id ───────────────────────────────────

def test_slow_demoted_behind_healthy():
    from tera_pilot import smart_route as sr
    from tera_pilot.smart_route import get_health_store

    store = get_health_store()
    store.reset_for_test()
    sr.reset_probe_cache_for_test()
    cfg = {"router_smart_failover": False, "router_local_first": False,
           "router_slow_secs": 10.0, "router_call_timeout_s": 5.0,
           "router_max_failovers": 5, "router_down_after_errors": 99,
           "router_down_cooldown_s": 300.0, "router_probe_ttl_s": 60.0}
    store.record_success("fast", 0.5)
    store.record_success("fast", 0.5)
    store.record_success("slowp", 30.0)
    store.record_success("slowp", 30.0)

    class FakeRouter:
        def route(self, prompt, configured_providers=None):
            return {"complexity": "moderate", "provider_id": "slowp",
                    "model": "m", "fallbacks": [{"provider_id": "fast",
                                                 "model": "m"}],
                    "reasoning": "t"}

    class Reg:
        def list_providers(self):
            return [{"id": "slowp", "configured": True},
                    {"id": "fast", "configured": True}]

        def get(self, pid):
            raise AssertionError("no local probes expected here")

    cands, _ = sr.order_candidates("some moderate task here", Reg(),
                                   router=FakeRouter(), cfg=cfg)
    assert [c.provider_id for c in cands][0] == "fast"
    store.reset_for_test()


# ── 7. probe timeout sane ─────────────────────────────────────────────

def test_local_probe_default_timeout_sane():
    import inspect
    from tera_pilot.smart_route import local_reachable
    timeout = inspect.signature(local_reachable).parameters["timeout"].default
    assert timeout <= 1.0, f"probe default {timeout}s stalls ordering"
