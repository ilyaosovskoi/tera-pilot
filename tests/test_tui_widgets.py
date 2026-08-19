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
    assert modal._selected_provider in {"openai", "ollama"}  # sane default
    # The model prefill path uses bridge.status(), not bridge.get_status()
    assert hasattr(bridge, "status")


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
