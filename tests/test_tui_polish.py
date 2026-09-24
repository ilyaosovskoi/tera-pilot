"""Polish tests for the v2.5.0 TUI/GUI visual pass.

Covers what the refresh changed:
  1. Design tokens ($variables) exist with identical names in both themes.
  2. Every modal has app-level selectors in both themes (ask/picker/
     selector/quick-settings) — no more default-Textual-blue dialogs.
  3. Chat helpers: result truncation, tool-family colors, user marker.
  4. StatusBar model shortening.
  5. AskUserModal option click returns the option text.
  6. InfoBox renders a single header row (+ status line only when set).
"""

import re
from pathlib import Path

import pytest

pytest.importorskip("textual")

BASE = Path(__file__).resolve().parent.parent / "tera_pilot_tui"

EXPECTED_TOKENS = {
    "$border", "$border-strong", "$surface", "$surface-deep",
    "$surface-raised", "$text", "$text-muted", "$accent",
    "$accent-bright", "$success", "$success-dim", "$warning",
    "$error", "$error-dim", "$primary",
}


def _tokens(path: Path):
    src = path.read_text(encoding="utf-8")
    return set(re.findall(r"(?m)^\$[a-zA-Z][a-zA-Z0-9_-]*", src))


def test_design_tokens_match_across_themes():
    """Both themes define the same $variable names (values differ)."""
    dark = _tokens(BASE / "styles_dark.tcss")
    light = _tokens(BASE / "styles_light.tcss")
    assert dark == light, f"token names diverged: {dark ^ light}"
    assert EXPECTED_TOKENS <= dark


def test_all_modals_have_app_level_selectors_in_both_themes():
    """Ask/picker/selector/quick-settings surfaces exist in both themes."""
    for name in ("styles_dark.tcss", "styles_light.tcss"):
        src = (BASE / name).read_text(encoding="utf-8")
        for selector in (
            "#ask-box", "#ask-title", "#ask-question", "#ask-input",
            "#ask-buttons", "#ask-send", "#ask-skip",
            "#picker-box", "#picker-title",
            "#selector-box", "#selector-title",
            "#qs-container", "#qs-title",
            "#palette-filter:focus",
        ):
            assert selector in src, f"{name} missing {selector}"


def test_truncate_result_preview():
    from tera_pilot_tui.widgets.chat_log import truncate_result_preview
    short = "ok"
    assert truncate_result_preview(short) == short
    long = "x" * 5000
    out = truncate_result_preview(long)
    assert len(out) < len(long)
    assert "Activity" in out
    assert "1,000 more chars" in out or "more chars" in out


def test_tool_family_mapping():
    from tera_pilot_tui.widgets.chat_log import _tool_family
    assert _tool_family("execute_command") == "exec"
    assert _tool_family("read_file") == "read"
    assert _tool_family("write_file") == "edit"
    assert _tool_family("self_verify") == "verify"
    assert _tool_family("something_new") == "label"


def test_shorten_model():
    from tera_pilot_tui.widgets.status_bar import shorten_model
    assert shorten_model("qwen3") == "qwen3"
    long = "nvidia/nemotron-3-super-120b-a12b:free"
    short = shorten_model(long)
    assert len(short) <= 28 and short.endswith("…")
    assert shorten_model("") == "?"


@pytest.mark.asyncio
async def test_info_box_is_single_row():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.info_box import InfoBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        info = app.query_one(InfoBox)
        plain = info._render_text().plain
        assert plain.count("\n") == 0
        for field in ("model", "provider", "dir"):
            assert field in plain
        info.update_status("⠋ thinking")
        assert info._render_text().plain.count("\n") == 1
        assert app._exception is None


@pytest.mark.asyncio
async def test_ask_modal_option_click_returns_option_text():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.ask_modal import AskUserModal

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        answers = []
        app.push_screen(AskUserModal("Pick?", ["alpha", "beta"]), answers.append)
        await pilot.pause(0.2)
        await pilot.click("#ask-opt-2")
        await pilot.pause(0.2)
        assert answers == ["beta"]
        assert app._exception is None


@pytest.mark.asyncio
async def test_ask_modal_skip_returns_none():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.ask_modal import AskUserModal

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        answers = ["unset"]
        app.push_screen(AskUserModal("Pick?", ["alpha"]), answers.append)
        await pilot.pause(0.2)
        await pilot.click("#ask-skip")
        await pilot.pause(0.2)
        assert answers == ["unset", None]
        assert app._exception is None
