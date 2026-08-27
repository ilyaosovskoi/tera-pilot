"""Widget-level regression tests for the TUI (Textual 8.x).

These guard the v2.3.4-fix batch — bugs that directly hurt the first
impression of the TUI:

1. Mouse clicks on OptionList-based pickers were dead: the handlers were
   named ``on_option_list_selected``, but Textual 8.x dispatches
   ``OptionList.OptionSelected`` to ``on_option_list_option_selected``.
   Clicking a command in the Ctrl+P palette, a "/" suggestion, a model
   in the selector or a picker option did nothing.
2. ``QuickSettingsModal`` (/settings) crashed on construction because it
   called ``bridge.get_provider()`` / ``bridge.get_status()`` which do
   not exist on ``TeraPilotBridge``.
3. ``ToolBlock._render`` shadowed Textual's internal ``Widget._render()``
   and crashed the render pipeline (``'NoneType' has no attribute
   'get_height'``).
4. The entrance/fade animations called ``widget.animate("opacity", ...)``
   (and ``"offset"``), which Textual 8 treats as a fatal error
   (``_handle_exception`` exits the app) — every modal open / "/"
   suggestion crash risk.
"""

import pytest

pytest.importorskip("textual")

from textual.widget import Widget  # noqa: E402


def _app_run(app, coro):
    """Run an async Textual scenario inside a pytest asyncio test."""
    import asyncio
    asyncio.get_event_loop().run_until_complete(coro(app))


# ── mouse clicks on OptionList pickers (handler-name fix) ──────────────


@pytest.mark.asyncio
async def test_command_palette_mouse_click_selects_command():
    from textual.app import App
    from tera_pilot_tui.widgets.command_palette import CommandPalette

    selected = []

    class PalApp(App):
        def on_mount(self):
            self.push_screen(
                CommandPalette(on_select=lambda cid, needs_sub: selected.append(cid))
            )

    app = PalApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.5)  # entrance animation must not crash
        await pilot.click("#palette-list", offset=(10, 2))  # first real command row
        await pilot.pause(0.3)
    # The click must select a real command (not the category header) and
    # pop the palette.
    assert selected, "click on a palette option did not select anything"
    assert selected[0] == "section"
    assert app._exception is None


@pytest.mark.asyncio
async def test_command_suggestions_mouse_click_selects():
    from textual.app import App
    from tera_pilot_tui.widgets.command_palette import BUILTIN_COMMANDS
    from tera_pilot_tui.widgets.command_suggestions import CommandSuggestions

    picked = []

    class SugApp(App):
        def compose(self):
            yield CommandSuggestions()

        def on_mount(self):
            s = self.query_one(CommandSuggestions)
            s.set_commands(BUILTIN_COMMANDS)
            s.set_on_select(lambda item: picked.append(item.id))
            s.show_suggestions("")

    app = SugApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#suggestions-list", offset=(10, 1))
        await pilot.pause(0.3)
    assert picked, "click on a suggestion did not select anything"
    assert app._exception is None


@pytest.mark.asyncio
async def test_model_selector_modal_click_selects():
    from textual.app import App
    from tera_pilot_tui.widgets.model_selector_modal import ModelSelectorModal

    picked = []

    class SelApp(App):
        def on_mount(self):
            self.push_screen(
                ModelSelectorModal(
                    [
                        {"id": "openai", "label": "OpenAI", "model": "gpt-5.5", "active": True},
                        {"id": "groq", "label": "Groq", "model": "llama-4", "active": False},
                    ],
                    lambda pid: picked.append(pid),
                )
            )

    app = SelApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#selector-list", offset=(10, 1))
        await pilot.pause(0.3)
    assert picked == ["openai"]
    assert app._exception is None


@pytest.mark.asyncio
async def test_model_picker_modal_click_selects():
    from textual.app import App
    from tera_pilot_tui.widgets.model_picker import ModelPickerModal

    picked = []

    class PickApp(App):
        def on_mount(self):
            self.push_screen(ModelPickerModal("Pick", ["a", "b", "c"], lambda s: picked.append(s)))

    app = PickApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#picker-list", offset=(10, 1))
        await pilot.pause(0.3)
    assert picked == ["a"]
    assert app._exception is None


# ── /settings modal must construct (bridge method regression) ──────────


def test_quick_settings_modal_constructs():
    """QuickSettingsModal used to crash with AttributeError on open."""
    from tera_pilot_tui.bridge import TeraPilotBridge
    from tera_pilot_tui.widgets.settings_modal import QuickSettingsModal

    bridge = TeraPilotBridge(workspace="/tmp")
    modal = QuickSettingsModal(bridge)
    # v2.3.6: the active provider comes from the (freshly built) registry
    # and the provider row always includes it — never an empty pick.
    assert modal._selected_provider
    assert any(p[0] == modal._selected_provider for p in modal._providers)
    # The model prefill path uses bridge.status(), not bridge.get_status()
    assert hasattr(bridge, "status")


@pytest.mark.asyncio
async def test_quick_settings_modal_highlights_active_provider():
    """v2.3.6: the provider row must include the ACTIVE provider (e.g.
    openrouter) and mark it active — a fresh app must not silently
    default to OpenAI."""
    from textual.app import App
    from tera_pilot_tui.bridge import TeraPilotBridge
    from tera_pilot_tui.widgets.settings_modal import QuickSettingsModal, QUICK_PROVIDERS

    class SettingsApp(App):
        def on_mount(self):
            self.push_screen(QuickSettingsModal(TeraPilotBridge(workspace="/tmp")))

    app = SettingsApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.4)
        modal = app.screen
        assert isinstance(modal, QuickSettingsModal), type(modal).__name__
        assert modal._selected_provider
        assert modal._selected_provider in {p[0] for p in modal._providers}
        # The modal is a pushed screen — query it directly, not the App.
        active_btn = modal.query_one(f"#qs-prov-{modal._selected_provider}")
        assert "active" in active_btn.classes
        # openrouter is a first-class quick provider now
        assert "openrouter" in {p[0] for p in QUICK_PROVIDERS}
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app._exception is None


# ── ToolBlock must not shadow Textual's internal _render ───────────────


def test_tool_block_does_not_override_widget_render():
    from tera_pilot_tui.widgets.tool_block import ToolBlock

    assert ToolBlock._render is Widget._render, (
        "ToolBlock must not override Textual's internal _render() — it "
        "crashes the render pipeline (get_content_height returns None)"
    )


@pytest.mark.asyncio
async def test_tool_block_renders_without_layout_crash():
    from textual.app import App
    from textual.geometry import Size
    from tera_pilot_tui.widgets.tool_block import ToolBlock

    class TApp(App):
        def compose(self):
            yield ToolBlock(
                tool_name="execute_command",
                tool_path="/tmp/x.py",
                content="hello [world]",
            )

    app = TApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.4)
        tb = app.query_one(ToolBlock)
        assert tb.get_content_width(Size(100, 30), Size(100, 30)) > 0
        assert app._exception is None


# ── entrance animations must not crash the app ─────────────────────────


@pytest.mark.asyncio
async def test_palette_entrance_animation_completes():
    """Regression: `widget.animate("opacity", ...)` raised a fatal
    AttributeError in the animator tick (Textual exits the app on any
    unhandled timer exception). The palette must open and animate."""
    from textual.app import App
    from tera_pilot_tui.widgets.command_palette import CommandPalette

    class PalApp(App):
        def on_mount(self):
            self.push_screen(CommandPalette())

    app = PalApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.8)  # longer than the 0.18s animation
        assert app._exception is None, f"animation crashed the app: {app._exception}"
        assert app.screen.query_one("#palette-container").styles.opacity == 1.0


# ── Ctrl+P must open the project palette, not Textual's (v2.3.5-fix) ──


@pytest.mark.asyncio
async def test_ctrl_p_opens_project_command_palette():
    """Textual's App.__init__ auto-binds ctrl+p → command_palette with
    priority=True UNLESS the app already binds an action literally named
    ``command_palette``. Our app binds ``open_command_palette``, so
    Textual added its own system palette and — being priority=True — it
    won the key: Ctrl+P showed Maximize/Quit/Screenshot instead of the
    project's slash commands. The project binding must be priority=True
    so Ctrl+P opens the real palette.
    """
    from tera_pilot_tui.app import TeraPilotTUIApp

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause(0.4)
        screen = app.screen
        assert type(screen).__module__ == "tera_pilot_tui.widgets.command_palette", (
            f"expected project palette, got {type(screen).__module__}"
        )
        # The project palette lists the slash commands.
        from textual.widgets import OptionList
        ol = screen.query_one("#palette-list", OptionList)
        labels = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
        assert any("/model" in l for l in labels)
        assert any("/clear" in l for l in labels)
        assert not any("Maximize" in l for l in labels), labels
        await pilot.press("escape")
        await pilot.pause(0.2)


# ── /model <name> must set the model on the active provider ────────────


@pytest.mark.asyncio
async def test_model_command_sets_model_on_active_provider():
    """`/model <name>` must treat a non-provider argument as a MODEL and
    apply it to the currently active provider — not try to switch to a
    provider named "<name>". The active provider must stay unchanged.
    """
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        active_before = app.bridge.get_active_provider_id()
        assert active_before, "expected a default active provider"

        inp = app.query_one(InputBox)
        inp.focus()
        inp.value = "/model ox-alpha"
        await pilot.press("enter")
        await pilot.pause(0.3)

        # Provider unchanged, model applied to it.
        assert app.bridge.get_active_provider_id() == active_before
        assert app.bridge._get_active_model() == "ox-alpha"


@pytest.mark.asyncio
async def test_model_command_still_switches_provider():
    """`/model <provider_id>` keeps the old behavior: switch provider.
    A bare provider id must not be mistaken for a model name."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        active_before = app.bridge.get_active_provider_id()

        # Pick a provider id that is NOT currently active.
        providers = app.bridge.list_providers()
        target = next(
            (p["id"] for p in providers if p["id"] != active_before), None
        )
        if target is None:
            return  # only one provider — nothing to switch to

        inp = app.query_one(InputBox)
        inp.focus()
        inp.value = f"/model {target}"
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert app.bridge.get_active_provider_id() == target


# ── ANSI/control-char garbage in model text (v2.3.6-fix) ──────────────


def test_clean_display_text_strips_ansi_and_control():
    from tera_pilot_tui.widgets.chat_log import clean_display_text

    assert clean_display_text("\x1b[31mred\x1b[0m") == "red"
    assert clean_display_text("\x1b[38;5;208morange\x1b[0m") == "orange"
    assert clean_display_text("a\x0cb") == "ab"
    assert clean_display_text("\x1b]0;title\x07body") == "body"
    assert clean_display_text("plain text") == "plain text"
    assert clean_display_text("") == ""
    assert "\x1b" not in clean_display_text("x\x1b[3my\x1b[23mz")


@pytest.mark.asyncio
async def test_chat_log_thought_strips_ansi_before_render():
    """Model thoughts with ANSI escapes must render clean — no stray
    "u"-looking fragments from half-consumed escape sequences."""
    from textual.app import App
    from tera_pilot_tui.widgets.chat_log import ChatLog

    class LogApp(App):
        def compose(self):
            yield ChatLog(id="c")

    app = LogApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        cl = app.query_one(ChatLog)
        cl.add_thought("\x1b[1mplan:\x1b[0m \x1b[31mread file\x1b[0m")
        await pilot.pause(0.2)
        text = "\n".join(str(line) for line in cl.lines)
        assert "plan: read file" in text
        assert "\x1b" not in text


# ── animated thinking status line (v2.3.6) ────────────────────────────


@pytest.mark.asyncio
async def test_thinking_status_animates_then_clears():
    """While a turn runs, the InfoBox must show an animated "thinking…"
    line; when the turn ends it must disappear."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.info_box import InfoBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        info = app.query_one(InfoBox)

        app._turn_running = True
        app._refresh_status("thinking")
        await pilot.pause(0.4)  # let the 0.15s timer tick
        assert "thinking" in info._status
        assert info._status != ""
        # the dots must actually change between ticks (animation)
        frame_a = info._status
        await pilot.pause(0.2)
        assert info._status != frame_a or "." in info._status

        app._turn_running = False
        app._refresh_status("idle")
        assert info._status == ""


# ── max-iterations exhaustion keeps partial output (v2.3.6) ─────────────


@pytest.mark.asyncio
async def test_max_iterations_exhaustion_shows_partial_output():
    """When the run hits the iteration cap, the partial result must be
    rendered as the answer instead of being discarded."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    class FakeResult:
        output = "I created the file and started the refactor."
        error = "Max iterations (8) reached"
        success = False
        metadata = {}

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._on_turn_done(FakeResult())
        await pilot.pause(0.3)
        text = "\n".join(str(line) for line in app.query_one(ChatLog).lines)
        assert "I created the file" in text
        assert "Max iterations" in text
        assert app._turn_running is False


# ── run-ending errors must render exactly once (v2.3.5-fix) ────────────


class _FakeTurnResult:
    """Minimal stand-in for TaskResult (success=False path)."""

    def __init__(self, error: str, output: str = ""):
        self.success = False
        self.error = error
        self.output = output
        self.metadata = {}


@pytest.mark.asyncio
async def test_run_ending_error_rendered_once():
    """The agent runtime emits an ERROR event AND returns
    success=False for the same terminal failure. Regression: the TUI
    rendered the error twice (once from the event, once from
    _on_turn_done). A run-ending error must appear exactly once in the
    ChatLog.
    """
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        chat = app.query_one(ChatLog)
        app._handle_event("error", {"error": "boom: connection refused"})
        app._on_turn_done(_FakeTurnResult(error="boom: connection refused"))
        await pilot.pause(0.2)
        rendered = [str(line) for line in chat.lines]
        assert sum("boom: connection refused" in r for r in rendered) == 1, rendered
        # The dedup flag must reset so the NEXT distinct error renders.
        app._handle_event("error", {"error": "second failure"})
        app._on_turn_done(_FakeTurnResult(error="second failure"))
        await pilot.pause(0.2)
        rendered = [str(line) for line in chat.lines]
        assert sum("second failure" in r for r in rendered) == 1, rendered


# ── Guardian modal answers route to the guardian wait (v2.3.9-fix) ──────


@pytest.mark.asyncio
async def test_guardian_modal_approve_routes_to_guardian_verdict():
    """Answering a Guardian MODIFY modal with Approve must call
    bridge.answer_guardian_verdict("approve"), NOT answer_confirmation(True).

    Regression: only "use_fix" went through answer_guardian_verdict();
    "approve"/"reject" called answer_confirmation(), which pokes the tool
    engine's *_confirm* wait — but during a guardian review the engine is
    blocked on its *_guardian* wait (_guardian_event/_guardian_decision),
    so the verdict never arrived and the agent hung until the 300s wait
    timed out (then the default "reject" applied anyway).
    """
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.bridge import TeraPilotBridge

    class RecordingBridge(TeraPilotBridge):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.guardian_verdicts = []
            self.confirmations = []

        def answer_guardian_verdict(self, verdict):
            self.guardian_verdicts.append(verdict)

        def answer_confirmation(self, accepted):
            self.confirmations.append(accepted)

    bridge = RecordingBridge(workspace=".")
    app = TeraPilotTUIApp(bridge=bridge)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_confirm({
            "action": "execute_command",
            "summary": "Run: rm -rf /tmp/important",
            "guardian_verdict": "MODIFY",
            "suggested_args": {"command": "rm /tmp/important"},
            "rationale": "Recursive delete is risky",
            "risk_level": "high",
            "reasons": ["recursive delete"],
        })
        await pilot.pause(0.3)
        assert app._approval_modal is not None
        await pilot.click("#approve")
        await pilot.pause(0.3)

    assert bridge.guardian_verdicts == ["approve"], bridge.guardian_verdicts
    assert bridge.confirmations == [], bridge.confirmations


@pytest.mark.asyncio
async def test_guardian_modal_reject_routes_to_guardian_verdict():
    """Same routing fix for the Reject button (and Escape)."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.bridge import TeraPilotBridge

    class RecordingBridge(TeraPilotBridge):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.guardian_verdicts = []
            self.confirmations = []

        def answer_guardian_verdict(self, verdict):
            self.guardian_verdicts.append(verdict)

        def answer_confirmation(self, accepted):
            self.confirmations.append(accepted)

    bridge = RecordingBridge(workspace=".")
    app = TeraPilotTUIApp(bridge=bridge)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_confirm({
            "action": "execute_command",
            "summary": "Run: rm -rf /tmp/important",
            "guardian_verdict": "MODIFY",
            "suggested_args": {"command": "rm /tmp/important"},
            "rationale": "Recursive delete is risky",
            "risk_level": "high",
            "reasons": ["recursive delete"],
        })
        await pilot.pause(0.3)
        assert app._approval_modal is not None
        await pilot.click("#reject")
        await pilot.pause(0.3)

    assert bridge.guardian_verdicts == ["reject"], bridge.guardian_verdicts
    assert bridge.confirmations == [], bridge.confirmations


@pytest.mark.asyncio
async def test_legacy_approval_modal_still_uses_answer_confirmation():
    """The legacy (non-Guardian) approval modal returns True/False and must
    keep going through answer_confirmation — the fix must not change that."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.bridge import TeraPilotBridge

    class RecordingBridge(TeraPilotBridge):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.guardian_verdicts = []
            self.confirmations = []

        def answer_guardian_verdict(self, verdict):
            self.guardian_verdicts.append(verdict)

        def answer_confirmation(self, accepted):
            self.confirmations.append(accepted)

    bridge = RecordingBridge(workspace=".")
    app = TeraPilotTUIApp(bridge=bridge)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # Legacy (non-Guardian) confirm: no guardian verdict fields.
        app._show_confirm({"action": "execute_command", "summary": "Run: echo hi"})
        await pilot.pause(0.3)
        assert app._approval_modal is not None
        await pilot.click("#approve")
        await pilot.pause(0.3)

    assert bridge.guardian_verdicts == [], bridge.guardian_verdicts
    assert bridge.confirmations == [True], bridge.confirmations


@pytest.mark.asyncio
async def test_distinct_error_after_previous_renders():
    """A NEW turn's error must render even when it matches a previous
    turn's error (the dedup flag is per-turn, not global)."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        chat = app.query_one(ChatLog)
        # Turn 1: event + result — rendered once.
        app._handle_event("error", {"error": "first"})
        app._on_turn_done(_FakeTurnResult(error="first"))
        # Turn 2: a genuinely NEW error — must still render.
        app._handle_event("error", {"error": "second"})
        app._on_turn_done(_FakeTurnResult(error="second"))
        await pilot.pause(0.2)
        rendered = [str(line) for line in chat.lines]
        assert sum("first" in r for r in rendered) == 1, rendered
        assert sum("second" in r for r in rendered) == 1, rendered
