"""TUI copy/paste + approval placement + no-pulse tests (v2.5.0).

Covers the visual-behavior round:
- clipboard.copy_text: empty input, tool success/failure, OSC52 fallback.
- ChatLog tracks last_answer / last_prompt for /copy + Ctrl+O.
- /copy + Ctrl+O copy through the clipboard helper (mocked).
- Terminal bracketed-paste (events.Paste) lands in the composer.
- ApprovalCard + CommandSuggestions render ABOVE the input
  (regression: dock:bottom + display-toggle mislayed them below the
  input, over the statusline — verified by widget regions).
- No "breathing" pulse class while a turn runs.
"""

import asyncio

import pytest

from textual import events


# ── clipboard helper (pure, no app) ───────────────────────────────────

def test_copy_empty_is_noop():
    from tera_pilot_tui.clipboard import copy_text
    assert copy_text("") == (False, "empty")


def test_copy_prefers_platform_tool(monkeypatch):
    from tera_pilot_tui import clipboard as cb
    calls = []

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(cb.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cb.subprocess, "run",
                        lambda *a, **k: (calls.append(a[0]), FakeProc())[1])
    ok, method = cb.copy_text("hello")
    assert ok is True and method in (
        "pbcopy", "clip", "wl-copy", "xclip", "xsel")
    assert calls, "expected a subprocess call"


def test_copy_falls_back_to_osc52(monkeypatch):
    from tera_pilot_tui import clipboard as cb

    class FakeApp:
        def __init__(self):
            self.copied = None

        def copy_to_clipboard(self, text):
            self.copied = text

    monkeypatch.setattr(cb.shutil, "which", lambda name: None)
    app = FakeApp()
    ok, method = cb.copy_text("hello", app=app)
    assert (ok, method) == (True, "osc52")
    assert app.copied == "hello"


def test_copy_unavailable_without_tool_or_app(monkeypatch):
    from tera_pilot_tui import clipboard as cb
    monkeypatch.setattr(cb.shutil, "which", lambda name: None)
    assert cb.copy_text("hello") == (False, "unavailable")


def test_copy_never_raises(monkeypatch):
    from tera_pilot_tui import clipboard as cb
    monkeypatch.setattr(cb.shutil, "which", lambda name: (_ for _ in ()).throw(
        RuntimeError("boom")))
    assert cb.copy_text("hello") == (False, "unavailable")


# ── ChatLog tracks copy sources ───────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_chatlog_tracks_last_turn():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = app.query_one(ChatLog)
            assert chat.last_answer == "" and chat.last_prompt == ""
            chat.add_user("my question")
            assert chat.last_prompt == "my question"
            chat.add_final("the answer")
            assert chat.last_answer == "the answer"
            assert app._exception is None

    _run(_go())


# ── /copy + Ctrl+O ────────────────────────────────────────────────────

def test_exec_copy_answer(monkeypatch):
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    async def _go():
        from tera_pilot_tui import clipboard as cb
        captured = {}
        monkeypatch.setattr(cb, "copy_text",
                            lambda text, app=None: (captured.setdefault(
                                "text", text), "pbcopy")[1] and (True, "pbcopy"))
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = app.query_one(ChatLog)
            chat.add_user("q")
            chat.add_final("answer text here")
            app._exec_copy("")
            await pilot.pause(0.2)
            assert captured.get("text") == "answer text here"
            assert app._exception is None

    _run(_go())


def test_exec_copy_prompt_variant(monkeypatch):
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    async def _go():
        from tera_pilot_tui import clipboard as cb
        captured = {}
        monkeypatch.setattr(cb, "copy_text",
                            lambda text, app=None: (captured.setdefault(
                                "text", text), (True, "pbcopy"))[1])
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = app.query_one(ChatLog)
            chat.add_user("prompt words")
            chat.add_final("ans")
            app._exec_copy("prompt")
            await pilot.pause(0.2)
            assert captured.get("text") == "prompt words"
            assert app._exception is None

    _run(_go())


def test_exec_copy_empty_reports_error():
    from tera_pilot_tui.app import TeraPilotTUIApp

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            said = []
            app._wf_say = lambda text, error=False: said.append((text, error))
            app._exec_copy("")
            assert said and said[0][1] is True
            assert "Nothing to copy" in said[0][0]
            assert app._exception is None

    _run(_go())


def test_ctrl_o_copies_last_answer(monkeypatch):
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    async def _go():
        from tera_pilot_tui import clipboard as cb
        captured = {}
        monkeypatch.setattr(cb, "copy_text",
                            lambda text, app=None: (captured.setdefault(
                                "text", text), (True, "pbcopy"))[1])
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one(ChatLog).add_final("ctrl-o answer")
            await pilot.press("ctrl+o")
            await pilot.pause(0.3)
            assert captured.get("text") == "ctrl-o answer"
            assert app._exception is None

    _run(_go())


def test_copy_in_palette_and_help():
    from tera_pilot_tui.widgets.command_palette import BUILTIN_COMMANDS
    assert any(c.id == "copy" for c in BUILTIN_COMMANDS)

    from tera_pilot_tui.app import TeraPilotTUIApp

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from tera_pilot_tui.widgets.chat_log import ChatLog
            chat = app.query_one(ChatLog)
            before = len(chat.lines)
            app._exec_help("actions")
            await pilot.pause(0.2)
            assert len(chat.lines) > before
            assert app._exception is None

    _run(_go())


# ── bracketed paste into the composer ─────────────────────────────────

def test_bracketed_paste_lands_in_composer():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            box = app.query_one(InputBox)
            box.value = ""
            box.post_message(events.Paste("pasted from terminal"))
            await pilot.pause(0.3)
            assert "pasted from terminal" in box.value
            assert app._exception is None

    _run(_go())


# ── approval + suggestions sit ABOVE the input ────────────────────────

def test_approval_card_above_input():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.approval_card import PendingApproval

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            card = app.query_one("#approval-card")
            card.show_request({"action": "run", "summary": "s"},
                              False, PendingApproval(False, lambda d: None))
            await pilot.pause(0.4)
            btns = app.query_one("#approval-card-buttons")
            box = app.query_one("#input")
            bar = app.query_one("#statusbar")
            assert btns.region.bottom <= box.region.y
            assert card.region.bottom <= box.region.y
            assert card.region.bottom <= bar.region.y  # no status overlap
            assert app._exception is None

    _run(_go())


def test_approval_guardian_variant_above_input():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.approval_card import PendingApproval

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            card = app.query_one("#approval-card")
            card.show_request({"action": "run", "summary": "s",
                               "suggested_args": {"a": 1}},
                              True, PendingApproval(True, lambda d: None))
            await pilot.pause(0.4)
            btns = app.query_one("#approval-card-buttons")
            box = app.query_one("#input")
            assert btns.region.bottom <= box.region.y
            assert app._exception is None

    _run(_go())


def test_suggestions_bar_above_input():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.command_suggestions import CommandSuggestions
    from tera_pilot_tui.widgets.input_box import InputBox

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one(InputBox).value = "/mo"
            await pilot.pause(0.4)
            sug = app.query_one(CommandSuggestions)
            box = app.query_one("#input")
            assert sug.has_class("visible")
            assert sug.region.bottom <= box.region.y
            assert app._exception is None

    _run(_go())


# ── no breathing pulse while working ──────────────────────────────────

def test_no_pulse_class_while_working():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            box = app.query_one(InputBox)
            app._turn_running = True
            app._refresh_status("thinking")
            await pilot.pause(0.1)
            assert box.has_class("working")
            for _ in range(4):
                app._tick_status_animation()
                await pilot.pause(0.05)
            assert not box.has_class("pulse")
            app._turn_running = False
            app._refresh_status("idle")
            await pilot.pause(0.1)
            assert not box.has_class("working")
            assert not box.has_class("pulse")
            assert app._exception is None

    _run(_go())


# ── horizontal scrollbars are gone ────────────────────────────────────

@pytest.mark.parametrize("theme", ["dark", "light"])
def test_no_horizontal_scrollbars(theme):
    import re
    from pathlib import Path
    for name in ("styles_dark.tcss", "styles_light.tcss"):
        text = (Path("tera_pilot_tui") / name).read_text()
        assert "scrollbar-size: 1 1" not in text, f"{name} still enables h-bar"
        assert re.search(r"scrollbar-size-horizontal:\s*0", text), name
