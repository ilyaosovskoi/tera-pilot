"""Menu visuals round (v2.5.0): palette + suggestions anchored above the
composer, unmissable selection, non-clickable group headers.

- The Ctrl+P palette used to float center-bottom over the chat AND the
  typed line; it must dock directly above the input (container bottom
  <= input top), including sub-palettes.
- Group headers are disabled Options: grayed, skipped by keyboard nav,
  ignored by mouse clicks.
- The inline /-suggestions highlight the choice with a ❯ marker + bold
  label (not just the list tint).
- Selection component styles exist in BOTH themes (parity).
"""

import asyncio

import pytest
from textual.widgets import OptionList


def _run(coro):
    return asyncio.run(coro)


# ── palette docks above the composer ──────────────────────────────────

def test_palette_above_input():
    from tera_pilot_tui.app import TeraPilotTUIApp

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            in_top = app.query_one("#input").region.y
            app.open_command_palette()
            await pilot.pause(0.6)
            cont = app.screen.query_one("#palette-container")
            assert cont.region.bottom <= in_top
            assert app._exception is None

    _run(_go())


def test_sub_palette_above_input():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.command_palette import SECTIONS

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            in_top = app.query_one("#input").region.y
            app._open_sub_palette("section", SECTIONS)
            await pilot.pause(0.6)
            cont = app.screen.query_one("#palette-container")
            assert cont.region.bottom <= in_top
            assert app._exception is None

    _run(_go())


def test_palette_above_y_helper():
    from tera_pilot_tui.app import TeraPilotTUIApp

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._palette_above_y() == app.query_one("#input").region.y
            assert app._exception is None

    _run(_go())


# ── group headers are disabled options ────────────────────────────────

def test_group_headers_disabled():
    from tera_pilot_tui.app import TeraPilotTUIApp

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_command_palette()
            await pilot.pause(0.5)
            pal = app.screen
            ol = pal.query_one(OptionList)
            headers = [i for i, oid in enumerate(pal._option_ids) if not oid]
            assert headers, "expected group headers"
            for i in headers:
                assert ol.get_option_at_index(i).disabled is True
            # every real row stays enabled
            for i, oid in enumerate(pal._option_ids):
                if oid:
                    assert ol.get_option_at_index(i).disabled is False
            assert app._exception is None

    _run(_go())


def test_header_click_ignored():
    from tera_pilot_tui.app import TeraPilotTUIApp

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_command_palette()
            await pilot.pause(0.5)
            await pilot.click("#palette-list", offset=(10, 1))  # header row
            await pilot.pause(0.4)
            # Still the same palette (nothing selected, nothing opened).
            assert type(app.screen).__name__ == "CommandPalette"
            assert app._exception is None

    _run(_go())


def test_real_row_click_selects():
    from tera_pilot_tui.app import TeraPilotTUIApp

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_command_palette()
            await pilot.pause(0.5)
            pal = app.screen
            idx = pal._option_ids.index("mode")
            await pilot.click("#palette-list", offset=(10, idx + 1))
            await pilot.pause(0.5)
            assert type(app.screen).__name__ != "CommandPalette"
            assert app._exception is None

    _run(_go())


def test_keyboard_never_lands_on_header():
    from tera_pilot_tui.app import TeraPilotTUIApp

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_command_palette()
            await pilot.pause(0.5)
            pal = app.screen
            ol = pal.query_one(OptionList)
            for _ in range(ol.option_count + 2):
                await pilot.press("down")
                await pilot.pause(0.02)
                assert pal._option_ids[ol.highlighted] != ""
            for _ in range(ol.option_count + 2):
                await pilot.press("up")
                await pilot.pause(0.02)
                assert pal._option_ids[ol.highlighted] != ""
            assert app._exception is None

    _run(_go())


# ── inline suggestions: visible choice ────────────────────────────────

def test_suggestion_choice_marker():
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
            ol = sug.query_one(OptionList)
            first = str(ol.get_option_at_index(0).prompt)
            assert first.startswith("❯ "), first[:20]
            if ol.option_count > 1:
                second = str(ol.get_option_at_index(1).prompt)
                assert second.startswith("  ")
            assert app._exception is None

    _run(_go())


# ── selection styles in both themes ───────────────────────────────────

@pytest.mark.parametrize("theme", ["dark", "light"])
def test_menu_selection_styles_parity(theme):
    from pathlib import Path
    base = Path("tera_pilot_tui")
    for name in ("styles_dark.tcss", "styles_light.tcss"):
        text = (base / name).read_text()
        assert ".option-list--option-highlighted" in text, name
        assert ".option-list--option-hover" in text, name
        assert ".option-list--option-disabled" in text, name
