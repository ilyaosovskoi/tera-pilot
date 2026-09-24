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


# ── Inline approval card (v2.5.0) ───────────────────────────────────


def test_key_decision_map():
    from tera_pilot_tui.widgets.approval_card import key_decision
    assert key_decision("y", False) == "allow"
    assert key_decision("enter", False) == "allow"
    assert key_decision("n", False) == "deny"
    assert key_decision("escape", False) == "deny"
    assert key_decision("a", True) == "approve"
    assert key_decision("u", True) == "use_fix"
    assert key_decision("enter", True) == "use_fix"
    assert key_decision("r", True) == "reject"
    assert key_decision("escape", True) == "reject"
    assert key_decision("x", False) is None
    assert key_decision("y", True) is None
    assert key_decision("", False) is None


def _recording_bridge():
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

    return RecordingBridge(workspace=".")


@pytest.mark.asyncio
async def test_inline_approve_via_keyboard_y():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    bridge = _recording_bridge()
    app = TeraPilotTUIApp(bridge=bridge)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_confirm({"action": "execute_command", "summary": "Run: echo hi"})
        await pilot.pause(0.2)
        assert app._inline_approval is not None
        await pilot.press("y")
        await pilot.pause(0.2)
        text = "\n".join(str(line) for line in app.query_one(ChatLog).lines)
        assert "Allowed" in text
    assert bridge.confirmations == [True]
    assert app._exception is None


@pytest.mark.asyncio
async def test_inline_deny_via_keyboard_n_and_escape():
    from tera_pilot_tui.app import TeraPilotTUIApp

    for key in ("n", "escape"):
        bridge = _recording_bridge()
        app = TeraPilotTUIApp(bridge=bridge)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._show_confirm({"action": "execute_command", "summary": "Run: echo hi"})
            await pilot.pause(0.2)
            await pilot.press(key)
            await pilot.pause(0.2)
        assert bridge.confirmations == [False], key
        assert app._exception is None


@pytest.mark.asyncio
async def test_inline_guardian_use_fix_via_keyboard_u():
    from tera_pilot_tui.app import TeraPilotTUIApp

    bridge = _recording_bridge()
    app = TeraPilotTUIApp(bridge=bridge)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_confirm({
            "action": "execute_command",
            "summary": "Run: rm -rf /tmp/important",
            "guardian_verdict": "MODIFY",
            "suggested_args": {"command": "rm /tmp/important"},
        })
        await pilot.pause(0.2)
        assert app._inline_approval is not None and app._inline_approval.guardian
        await pilot.press("u")
        await pilot.pause(0.2)
    assert bridge.guardian_verdicts == ["use_fix"]
    assert bridge.confirmations == []
    assert app._exception is None


@pytest.mark.asyncio
async def test_composer_frozen_while_approval_pending():
    """Typing keys must not reach the composer while the card is pending;
    ctrl combos still pass through."""
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.input_box import InputBox

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box = app.query_one(InputBox)
        assert box.value == ""
        app._show_confirm({"action": "execute_command", "summary": "Run: echo hi"})
        await pilot.pause(0.2)
        await pilot.press("x")
        await pilot.pause(0.1)
        assert box.value == ""
        assert app._inline_approval is not None and app._inline_approval.active
        await pilot.press("n")
        await pilot.pause(0.2)
        assert app._inline_approval is None or not app._inline_approval.active
        await pilot.press("x")
        await pilot.pause(0.1)
        assert box.value == "x"
        assert app._exception is None


@pytest.mark.asyncio
async def test_stale_approval_expires_denied():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.approval_card import ApprovalCard

    bridge = _recording_bridge()
    app = TeraPilotTUIApp(bridge=bridge)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_confirm({"action": "execute_command", "summary": "Run: echo hi"})
        await pilot.pause(0.2)
        assert app._inline_approval is not None
        app._close_stale_approval()
        await pilot.pause(0.1)
        card = app.query_one(ApprovalCard)
        assert not card.has_class("visible")
    assert bridge.confirmations == [False]
    assert app._exception is None


def test_card_button_mapping_without_layout():
    """Button ids map to decisions without a running app (pure dispatch)."""
    from textual.widgets import Button
    from tera_pilot_tui.widgets.approval_card import ApprovalCard, PendingApproval

    card = ApprovalCard()
    got = []
    card._pending = PendingApproval(False, got.append)
    for bid, want in (("ap-allow", "allow"), ("ap-deny", "deny"),
                      ("ap-approve", "approve"), ("ap-use-fix", "use_fix"),
                      ("ap-reject", "reject")):
        card._pending = PendingApproval("ap-" in bid and bid != "ap-allow" and bid != "ap-deny", got.append)
        event = Button.Pressed(Button("x", id=bid))
        card.on_button_pressed(event)
    assert got == ["allow", "deny", "approve", "use_fix", "reject"]
    # Unknown button id is ignored.
    card._pending = PendingApproval(False, got.append)
    card.on_button_pressed(Button.Pressed(Button("x", id="nope")))
    assert len(got) == 5
    # Double resolve fires once.
    calls = []
    pending = PendingApproval(False, calls.append)
    pending.resolve("allow")
    pending.resolve("deny")
    assert calls == ["allow"]


# ── Mascot (v2.5.0) ─────────────────────────────────────────────────


def test_mascot_frames_valid():
    from tera_pilot_tui.widgets.mascot import FRAMES, WIDTH
    assert len(FRAMES) == 2
    for frame in FRAMES:
        assert all(len(row) == WIDTH for row in frame)
        assert all(set(row) <= set(".DKAW") for row in frame)
    assert FRAMES[0] != FRAMES[1]


def test_mascot_render_dims_and_themes():
    from tera_pilot_tui.widgets.mascot import render_mascot, WIDTH
    for dark in (True, False):
        for frame in (0, 1):
            text = render_mascot(dark=dark, frame=frame)
            lines = text.plain.split("\n")
            assert all(len(line) == WIDTH for line in lines)
            assert 8 <= len(lines) <= 9
    assert render_mascot(True, 0).plain != render_mascot(True, 1).plain
    assert render_mascot(False, 0).plain == render_mascot(True, 0).plain


def test_mascot_quips():
    from tera_pilot_tui.widgets.mascot import QUIPS, random_quip
    assert len(QUIPS) >= 5
    assert all(q and len(q) < 80 for q in QUIPS)
    assert random_quip() in QUIPS


@pytest.mark.asyncio
async def test_mascot_command_renders():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    app = TeraPilotTUIApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._exec_mascot("")
        await pilot.pause(0.2)
        text = "\n".join(str(line) for line in app.query_one(ChatLog).lines)
        assert "Pilot:" in text
        assert app._exception is None
