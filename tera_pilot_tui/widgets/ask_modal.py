"""ask_modal.py — interactive question dialog for the ask_user tool.

When the agent calls ask_user and a UI is wired, this modal pops up with
the question, up to 8 suggested-option buttons (keys 1-8) and a free-text
input. Returns the chosen option text, the typed answer, or None when
skipped (Escape) — in which case the engine falls back to its in-band
"proceed with a stated assumption" behaviour.
"""

from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from .motion import entrance


class AskUserModal(ModalScreen[Optional[str]]):
    """Question modal. Dismisses with str (answer) or None (skip)."""

    BINDINGS = [
        Binding("escape", "skip", "Skip"),
    ]

    def __init__(self, question: str, options: List[str]) -> None:
        super().__init__()
        self._question = question[:500]
        self._options = [o[:120] for o in (options or [])][:8]

    def compose(self) -> ComposeResult:
        with Vertical(id="ask-box"):
            yield Label("❓ Agent question", id="ask-title")
            yield Static(self._question, id="ask-question")
            for i, opt in enumerate(self._options, start=1):
                yield Button(f"{i}. {opt}", id=f"ask-opt-{i}",
                             classes="ask-option", variant="default")
            yield Input(placeholder="Or type your own answer, Enter to send…",
                        id="ask-input")
            with Vertical(id="ask-buttons"):
                yield Button("Send typed answer (Enter)", variant="success", id="ask-send")
                yield Button("Skip — let the agent decide (Esc)", id="ask-skip")

    def on_mount(self) -> None:
        try:
            entrance(self.query_one("#ask-box"))
        except Exception:
            pass
        try:
            self.query_one("#ask-input", Input).focus()
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id or ""
        if btn.startswith("ask-opt-"):
            try:
                idx = int(btn.rsplit("-", 1)[1]) - 1
                self.dismiss(self._options[idx])
                return
            except (ValueError, IndexError):
                pass
        if btn == "ask-send":
            self._send_input()
            return
        if btn == "ask-skip":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ask-input":
            self._send_input()

    def _send_input(self) -> None:
        try:
            text = self.query_one("#ask-input", Input).value.strip()
        except Exception:
            text = ""
        self.dismiss(text[:1000] if text else None)

    def action_skip(self) -> None:
        self.dismiss(None)
