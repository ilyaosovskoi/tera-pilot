"""approval_modal.py — confirmation dialog for side-effecting actions.

The legacy runtime gates dangerous actions (e.g. `execute_command`) through a
confirm callback when autonomy is "always_ask". This modal renders that request
and returns the user's decision via `dismiss(bool)`.

Guardian MODIFY verdict adds a third "Use Fix" button for proposed alternative args.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static, Input, TextArea

from .motion import entrance


class ApprovalModal(ModalScreen[Optional[bool]]):
    """Legacy confirm modal (Approve/Deny). Returns True/False."""
    BINDINGS = [
        Binding("y", "approve", "Approve"),
        Binding("n", "deny", "Deny"),
        Binding("escape", "deny", "Deny"),
    ]

    def __init__(self, info: Dict[str, Any]) -> None:
        super().__init__()
        self._action = str(info.get("action", "action"))
        self._summary = str(info.get("summary", ""))

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            yield Label("Approve this action?", id="approval-title")
            yield Static(f"[b]{self._action}[/b]", id="approval-action")
            yield Static(self._summary, id="approval-summary")
            with Vertical(id="approval-buttons"):
                yield Button("Approve (y)", variant="success", id="approve")
                yield Button("Deny (n)", variant="error", id="deny")

    def on_mount(self) -> None:
        """v2.3.1: entrance animation — fade + slight rise."""
        try:
            entrance(self.query_one("#approval-box"))
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class GuardianModal(ModalScreen[str]):
    """Guardian MODIFY verdict modal with 3 buttons: Approve / Reject / Use Fix.

    Returns: "approve", "reject", or "use_fix"
    """
    BINDINGS = [
        Binding("a", "approve", "Approve"),
        Binding("r", "reject", "Reject"),
        Binding("u", "use_fix", "Use Fix"),
        Binding("escape", "reject", "Reject"),
    ]

    def __init__(self, info: Dict[str, Any]) -> None:
        super().__init__()
        self._action = str(info.get("action", "action"))
        self._summary = str(info.get("summary", ""))
        self._rationale = str(info.get("rationale", ""))
        self._suggested_args = info.get("suggested_args")

    def compose(self) -> ComposeResult:
        with Vertical(id="guardian-box"):
            yield Label("⚠ Guardian Safety Review", id="guardian-title")
            yield Static(f"[b]{self._action}[/b]", id="guardian-action")
            yield Static(self._summary, id="guardian-summary")
            if self._rationale:
                yield Static(f"[yellow]Rationale:[/yellow] {self._rationale}", id="guardian-rationale")
            if self._suggested_args:
                import json
                suggested_text = json.dumps(self._suggested_args, indent=2, ensure_ascii=False)
                yield Static(f"[green]Proposed fix:[/green]", id="guardian-fix-label")
                yield TextArea(suggested_text, read_only=True, id="guardian-fix-text", show_line_numbers=False)
            with Vertical(id="guardian-buttons"):
                yield Button("Approve (a)", variant="success", id="approve")
                yield Button("Use Fix (u)", variant="primary", id="use_fix")
                yield Button("Reject (r)", variant="error", id="reject")

    def on_mount(self) -> None:
        """v2.3.1: entrance animation — fade + slight rise."""
        try:
            entrance(self.query_one("#guardian-box"))
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_approve(self) -> None:
        self.dismiss("approve")

    def action_reject(self) -> None:
        self.dismiss("reject")

    def action_use_fix(self) -> None:
        self.dismiss("use_fix")
