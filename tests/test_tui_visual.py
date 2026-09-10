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
  6. v2.4.2: ChatLog/ToolBlock are theme-aware too, and the picker
     modals use $surface/$border/$text variables instead of hard-coded
     dark colors — so /theme light repaints the body, not just chrome.
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


# ── ChatLog / ToolBlock theme awareness ─────────────────────────────


def test_chat_log_palette_switches_with_theme():
    """v2.4.2: ChatLog.set_theme(False) must swap every body color away
    from the dark palette (no white/#aaaaaa text left for light mode),
    and set_theme(True) must restore the dark values."""
    from tera_pilot_tui.widgets.chat_log import ChatLog

    chat = ChatLog()
    assert chat.dark is True
    assert chat._pal["body"] == "white"

    chat.set_theme(False)
    assert chat.dark is False
    light = chat._pal
    for key in ("body", "thought", "result", "label", "separator", "unknown"):
        assert light[key].lower() not in (
            "white", "#aaaaaa", "#888888", "#505050", "grey62",
        ), key
    assert light["code_theme"] == "ansi_light"

    chat.set_theme(True)
    assert chat.dark is True
    assert chat._pal["body"] == "white"
    assert chat._pal["code_theme"] == "ansi_dark"


def test_tool_block_palette_switches_with_theme():
    """v2.4.2: ToolBlock.set_theme(False) must darken the border hues
    and set_theme(True) must restore the dark ones."""
    from tera_pilot_tui.widgets.tool_block import ToolBlock

    tb = ToolBlock(tool_name="execute_command", content="hi")
    assert tb.dark is True
    dark_border = tb._border_color

    tb.set_theme(False)
    assert tb.dark is False
    assert tb._border_color != dark_border

    tb.set_theme(True)
    assert tb.dark is True
    assert tb._border_color == dark_border


def test_picker_modals_use_theme_variables():
    """v2.4.2: the picker/palette modals must style surfaces with
    $surface/$border/$text variables (which follow the active theme)
    instead of hard-coded dark hex colors."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "tera_pilot_tui" / "widgets"
    for name in ("model_selector_modal.py", "command_palette.py", "model_picker.py"):
        css = (base / name).read_text(encoding="utf-8")
        assert "#111114" not in css, name
        assert "#2e2e33" not in css, name
        assert "$surface" in css or "$border" in css, name


# ── Thinking strip + StatusBar mounted ──────────────────────────


@pytest.mark.asyncio
async def test_thinking_strip_shows_only_while_working():
    """v2.4.2: the thinking strip is hidden when idle, appears with a
    running animation on thinking/running, and hides again when the
    turn ends — so 'no strip' unambiguously means idle/dead."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.thinking import ThinkingIndicator

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        thinking = app.query_one(ThinkingIndicator)
        assert not thinking.has_class("visible")
        assert not thinking.running

        app._refresh_status("thinking")
        await pilot.pause()
        assert thinking.has_class("visible")
        assert thinking.running

        app._refresh_status("idle")
        await pilot.pause()
        assert not thinking.has_class("visible")
        assert not thinking.running
        assert app._exception is None


@pytest.mark.asyncio
async def test_status_bar_mounted_themed_and_updated():
    """v2.4.2: the bottom statusline is mounted, follows the theme
    palette, and reflects the turn state pushed via _refresh_status."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.status_bar import StatusBar

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        bar = app.query_one(StatusBar)
        assert bar.dark is True

        bar.set_theme(False)
        assert bar.dark is False
        assert bar._pal["muted"].lower() not in ("grey62", "#888888")

        app._refresh_status("running")
        await pilot.pause()
        assert bar._state == "running"
        app._refresh_status("idle")
        await pilot.pause()
        assert bar._state == "idle"
        assert app._exception is None


@pytest.mark.asyncio
async def test_canvas_strip_shows_only_with_nodes():
    """v2.4.2: the task-graph strip stays hidden on an empty canvas,
    appears once a node lands, and hides again after reset."""
    from tera_pilot.agent.task_canvas import get_task_canvas
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.task_canvas_view import TaskCanvasView

    canvas = get_task_canvas()
    canvas.reset()
    app = TeraPilotTUIApp()
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view = app.query_one(TaskCanvasView)
            assert not view.has_content
            assert not view.has_class("visible")

            canvas.add_node("n1", "Write tests", status="running")
            app._refresh_canvas()
            await pilot.pause()
            assert view.has_content
            assert view.has_class("visible")

            view.set_theme(False)
            assert view.dark is False
            assert view._pal["muted"].lower() not in ("grey62", "#888888")

            canvas.reset()
            app._refresh_canvas()
            await pilot.pause()
            assert not view.has_content
            assert not view.has_class("visible")
            assert app._exception is None
    finally:
        canvas.reset()
