"""Regression tests for the v2.4.3 composer (the input line).

Two UX bugs shipped together:

1. The docked StatusBar's top border landed ON the composer's bottom
   border row. Textual's bottom-dock layout overlays one row of a docked
   widget's bottom margin, and the composer only reserved one margin row
   (``margin: 0 2 1 2``), so the statusline covered it.
2. The composer was a Textual single-line ``Input``: a long request
   scrolled horizontally off a one-row field, making a long prompt
   awkward to type and impossible to review. It is now a soft-wrapping
   ``TextArea`` that grows upward (capped by CSS ``max-height``).
"""

import pytest

pytest.importorskip("textual")


def _composer_and_statusline(pilot):
    from tera_pilot_tui.widgets.input_box import InputBox
    from tera_pilot_tui.widgets.status_bar import StatusBar

    return pilot.app.query_one(InputBox), pilot.app.query_one(StatusBar)


# ── layout: the statusline must not cover the composer ────────────────


@pytest.mark.asyncio
async def test_composer_is_not_covered_by_the_statusline():
    from tera_pilot_tui.app import TeraPilotTUIApp

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box, bar = _composer_and_statusline(pilot)

        # The composer's last row (its bottom border) sits above the
        # statusline's first row (its top border) with a blank row in
        # between — no shared row, no cramped edge.
        assert box.region.y + box.region.height < bar.region.y, (
            f"composer {box.region} is covered by statusline {bar.region}"
        )
        # And both are the same width, so the box doesn't run off-screen.
        assert box.region.width == bar.region.width, (box.region, bar.region)
        assert box.region.x == bar.region.x
        assert app._exception is None


@pytest.mark.asyncio
async def test_composer_stays_clear_of_the_statusline_while_growing():
    from tera_pilot_tui.app import TeraPilotTUIApp

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box, bar = _composer_and_statusline(pilot)

        box.focus()
        box.value = "write a long status report " * 40  # wraps well past the cap
        await pilot.pause()
        assert box.region.y + box.region.height < bar.region.y, box.region
        assert box.region.height >= 3

        box.value = ""
        await pilot.pause()
        assert box.region.y + box.region.height < bar.region.y, box.region
        assert app._exception is None


# ── long prompts: type them, review them, submit them whole ───────────


@pytest.mark.asyncio
async def test_composer_accepts_a_long_prompt_and_grows():
    """Typing a long prompt must keep every character and grow the box."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    prompt = "please refactor the whole module and add tests "
    prompt = prompt * 4  # 188 chars — far past a single terminal line

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box = app.query_one(InputBox)
        _, bar = _composer_and_statusline(pilot)
        idle_height = box.region.height

        box.focus()
        await pilot.press(*list(prompt))
        await pilot.pause()

        assert box.value == prompt, "keystrokes were dropped from a long prompt"
        assert box.region.height > idle_height, "composer did not grow"
        assert box.region.height <= 10, "composer outgrew its max-height"
        assert box.region.y + box.region.height < bar.region.y
        assert app._exception is None


@pytest.mark.asyncio
async def test_enter_submits_a_long_prompt_intact():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    # ~250 chars, no trailing space (the composer strips on submit)
    prompt = ("explain the deployment pipeline end to end " * 6).strip()

    app = TeraPilotTUIApp()
    submitted = []
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._run_turn = lambda p: submitted.append(p)  # type: ignore[method-assign]
        box = app.query_one(InputBox)
        box.focus()
        box.value = prompt
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert submitted == [prompt], "the submitted prompt was truncated"
        assert box.value == "", "the composer was not cleared after submit"
        assert app._exception is None


@pytest.mark.asyncio
async def test_shift_enter_composes_a_multiline_prompt():
    """Shift+Enter adds a newline; Enter still submits the whole thing."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    app = TeraPilotTUIApp()
    submitted = []
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._run_turn = lambda p: submitted.append(p)  # type: ignore[method-assign]
        box = app.query_one(InputBox)
        box.focus()

        await pilot.press(*list("first line"))
        await pilot.press("shift+enter")
        await pilot.press(*list("second line"))
        await pilot.pause()
        assert box.value == "first line\nsecond line"

        await pilot.press("enter")
        await pilot.pause()
        assert submitted == ["first line\nsecond line"]
        assert app._exception is None


# ── history recall still works on a single-line prompt ────────────────


@pytest.mark.asyncio
async def test_up_down_recall_history_for_a_single_line_prompt():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box = app.query_one(InputBox)
        box.focus()
        box.remember("first prompt")
        box.remember("second prompt")

        await pilot.press("up")
        await pilot.pause()
        assert box.value == "second prompt"
        await pilot.press("up")
        await pilot.pause()
        assert box.value == "first prompt"
        await pilot.press("down")
        await pilot.pause()
        assert box.value == "second prompt"
        await pilot.press("down")
        await pilot.pause()
        assert box.value == ""
        assert app._exception is None


@pytest.mark.asyncio
async def test_up_moves_the_cursor_inside_a_multiline_prompt():
    """In a multi-line prompt Up/Down edit the text instead of recalling
    history — otherwise the earlier lines would be unreachable."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box = app.query_one(InputBox)
        box.focus()
        box.remember("an old prompt")
        box.value = "line one\nline two"
        await pilot.pause()

        assert box.cursor_location == (1, 8)
        await pilot.press("up")
        await pilot.pause()
        assert box.value == "line one\nline two", "history replaced the draft"
        assert box.cursor_location[0] == 0, box.cursor_location
        assert app._exception is None


# ── slash suggestions are still wired to composer changes ─────────────


@pytest.mark.asyncio
async def test_typing_slash_still_opens_suggestions():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.command_suggestions import CommandSuggestions
    from tera_pilot_tui.widgets.input_box import InputBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box = app.query_one(InputBox)
        sug = app.query_one(CommandSuggestions)
        box.focus()

        await pilot.press("/")
        await pilot.pause()
        assert app._suggestions_active, "composer change did not open suggestions"
        assert sug.is_visible

        await pilot.press("backspace")
        await pilot.pause()
        assert not app._suggestions_active, "suggestions stayed open"
        assert app._exception is None
