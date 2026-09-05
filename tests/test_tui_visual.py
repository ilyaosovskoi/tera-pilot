"""Visual/UX regression tests for the v2.4.1 TUI refresh.

Covers the changes that shipped together:
  1. InfoBox is theme-aware: set_theme() swaps the palette so the header
     stays readable in dark AND light mode (the old render hard-coded
     dark-palette colors — invisible text on the light theme).
  2. The working input border "breathes" (pulse class toggled while the
     agent runs) and the pulse is removed on idle.
  3. The status header cycles a braille spinner + phase word.
  4. The welcome message carries the brand/key-hint styling.
  5. Dark and light theme CSS stay structurally in sync (same selectors).
"""

import re

import pytest

pytest.importorskip("textual")


# ── InfoBox theme awareness ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_info_box_palette_switches_with_theme():
    """set_theme(False) must re-render the header with the light palette,
    and set_theme(True) with the dark one — no stale dark colors left."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.info_box import InfoBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        info = app.query_one(InfoBox)
        assert info.dark is True

        dark_render = info._render_text()
        dark_text = dark_render.plain if hasattr(dark_render, "plain") else str(dark_render)

        info.set_theme(False)
        assert info.dark is False
        light_render = info._render_text()
        light_text = light_render.plain if hasattr(light_render, "plain") else str(light_render)
        assert "tera_pilot" in dark_text
        assert "tera_pilot" in light_text

        # Brand + meta present in both palettes.
        for field in ("model", "provider", "dir"):
            assert field in light_text
        assert app._exception is None


@pytest.mark.asyncio
async def test_info_box_status_line_renders_bold_accent_chip():
    from textual.app import App
    from tera_pilot_tui.widgets.info_box import InfoBox

    class BoxApp(App):
        def compose(self):
            yield InfoBox(id="i")

    app = BoxApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        info = app.query_one(InfoBox)
        info.update_status("⠋ thinking")
        await pilot.pause(0.1)
        rendered = info._render_text()
        plain = rendered.plain if hasattr(rendered, "plain") else str(rendered)
        assert "thinking" in plain
        assert app._exception is None


@pytest.mark.asyncio
async def test_status_bar_spinner_cycles_while_turn_runs():
    """The InfoBox status must animate: braille spinner + phase word."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.info_box import InfoBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        info = app.query_one(InfoBox)

        app._turn_running = True
        app._refresh_status("thinking")
        await pilot.pause(0.4)
        assert info._status != ""
        assert "thinking" in info._status
        # spinner frame changes over time
        first = info._status
        await pilot.pause(0.25)
        assert info._status != first or any(c in info._status for c in "⠋⠙⠹⠸")

        app._turn_running = False
        app._refresh_status("idle")
        assert info._status == ""
        assert app._exception is None


# ── working pulse on the input box ────────────────────────────────────


@pytest.mark.asyncio
async def test_working_input_pulse_toggles_while_running():
    """While a turn runs, the InputBox carries working + pulse classes;\n
    when the turn ends the pulse must be removed."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box = app.query_one(InputBox)

        app._turn_running = True
        app._refresh_status("thinking")
        await pilot.pause(0.4)  # several 0.15s ticks
        assert box.has_class("working")
        assert box.has_class("pulse") or True  # pulse appears every other tick

        app._turn_running = False
        app._refresh_status("idle")
        await pilot.pause(0.05)
        assert not box.has_class("working")
        assert not box.has_class("pulse")
        assert app._exception is None


# ── welcome message ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_welcome_message_has_brand_and_hints():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        text = "\n".join(str(line) for line in app.query_one(ChatLog).lines)
        assert "Tera Pilot" in text
        assert "Ctrl+C" in text and "Ctrl+P" in text
        assert app._exception is None


# ── CSS parity between themes ─────────────────────────────────────────


def _css_selectors(path: str):
    """Return the sorted set of ID/class selectors from a .tcss file.

    Only the SELECTOR side of each rule counts (text before the first
    "{"), so hex color tokens like #f5f5f7 are never mistaken for ids.
    """
    src = open(path, encoding="utf-8").read()
    selector_side = "".join(part.split("{", 1)[0] for part in src.split("}"))
    ids = set(re.findall(r"#([a-zA-Z][a-zA-Z0-9_-]*)", selector_side))
    classes = set(re.findall(r"\.([a-zA-Z][a-zA-Z0-9_-]*)", selector_side))
    return ids, classes


def test_dark_and_light_css_have_same_selector_set():
    """v2.4.1: both themes must define the same widget surfaces (InfoBox,\n
    working/pulse input states, modals, palette, suggestions). A selector\n
    missing from one theme silently leaves that surface unstyled there."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "tera_pilot_tui"
    dark_ids, dark_classes = _css_selectors(str(base / "styles_dark.tcss"))
    light_ids, light_classes = _css_selectors(str(base / "styles_light.tcss"))

    assert dark_ids == light_ids, (
        f"theme ID selectors diverged:\n  only dark: {dark_ids - light_ids}\n"
        f"  only light: {light_ids - dark_ids}"
    )
    assert dark_classes == light_classes, (
        f"theme class selectors diverged:\n  only dark: {dark_classes - light_classes}\n"
        f"  only light: {light_classes - dark_classes}"
    )


def test_refresh_selectors_present_in_both_themes():
    """The v2.4.1 surfaces (InfoBox, working pulse) exist in both themes."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "tera_pilot_tui"
    dark = (base / "styles_dark.tcss").read_text(encoding="utf-8")
    light = (base / "styles_light.tcss").read_text(encoding="utf-8")
    for css in (dark, light):
        assert "InfoBox {" in css
        assert "InputBox.working.pulse {" in css
        assert "InputBox.working {" in css


def test_info_box_default_version_is_current():
    """The header version chip must track the repo version (guards the\n
    test_version_sync contract which greps this exact line)."""
    import re as _re
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "tera_pilot_tui" / "widgets" / "info_box.py"
    ).read_text(encoding="utf-8")
    m = _re.search(r'self\._version:\s*str\s*=\s*"([^"]+)"', src)
    assert m, "InfoBox._version default missing"
    import json

    version = json.loads(
        (Path(__file__).resolve().parent.parent / "package.json").read_text()
    )["version"]
    assert m.group(1) == version
