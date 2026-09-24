"""approval_card.py — inline confirmation card docked under the chat.

Replaces the centered ApprovalModal/GuardianModal popup for action
confirmations: the request renders as a bordered card directly under
the agent's message (above the composer), with clickable buttons and
single-key answers (y/n, a/u/r). The flow stays blocking — while a
request is pending the composer only accepts decision keys — but the
context (chat history) remains visible, unlike a modal overlay.

Two modes:
  normal    — Allow (y/Enter) / Deny (n/Esc). Routes to answer_confirmation.
  guardian  — Approve original (a) / Use fix (u/Enter) / Reject (r/Esc).
              Routes to answer_guardian_verdict.

``key_decision()`` is a pure helper so the key map is unit-testable
without a running app.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Button, Static


def key_decision(key: str, guardian: bool) -> Optional[str]:
    """Map a pressed key to a decision, or None when the key is not a
    decision key (the caller should then keep the composer frozen)."""
    key = (key or "").lower()
    if guardian:
        if key in ("a",):
            return "approve"
        if key in ("u", "enter"):
            return "use_fix"
        if key in ("r", "escape"):
            return "reject"
        return None
    if key in ("y", "enter"):
        return "allow"
    if key in ("n", "escape"):
        return "deny"
    return None


def hint_text(guardian: bool) -> str:
    if guardian:
        return "a approve original · u use fix · r reject · Enter use fix · Esc reject"
    return "y allow · n deny · Enter allow · Esc deny"


class PendingApproval:
    """One outstanding confirmation. ``resolve()`` fires the app's
    answer callback exactly once; later calls are no-ops."""

    def __init__(self, guardian: bool,
                 on_resolve: Callable[[Optional[str]], None]) -> None:
        self.guardian = guardian
        self._on_resolve = on_resolve
        self.active = True

    def resolve(self, decision: Optional[str]) -> None:
        if not self.active:
            return
        self.active = False
        try:
            self._on_resolve(decision)
        except Exception:
            pass


class ApprovalCard(Widget):
    """Docked confirmation card. Collapsed (height 0) until show_request().

    A plain Widget (like CommandSuggestions) so height:auto measures the
    real content in the dock layout — container Static subclasses mislay
    docked auto height and bury the buttons under the composer.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._guardian = False
        self._action = ""
        self._pending: Optional[PendingApproval] = None

    def compose(self) -> ComposeResult:
        yield Static("", id="approval-card-title")
        yield Static("", id="approval-card-summary")
        yield Static("", id="approval-card-fix")
        with Horizontal(id="approval-card-buttons"):
            pass

    @property
    def pending(self) -> bool:
        return self._pending is not None and self._pending.active

    def show_request(self, info: Dict[str, Any], guardian: bool,
                     pending: PendingApproval) -> None:
        """Fill the strip and make it visible. Any previous pending state
        is superseded (the app expires it as denied first)."""
        self._guardian = guardian
        self._pending = pending
        self._action = str(info.get("action", "action"))
        summary = str(info.get("summary", ""))
        title = "🛡 Guardian review" if guardian else "⚠ Approval needed"
        try:
            self.query_one("#approval-card-title", Static).update(
                f"{title} — {self._action[:120]}")
            self.query_one("#approval-card-summary", Static).update(summary[:200])
            fix_widget = self.query_one("#approval-card-fix", Static)
            suggested = info.get("suggested_args")
            if guardian and suggested is not None:
                try:
                    # Compact single line — the strip is only 5 rows tall.
                    fix_text = json.dumps(suggested, ensure_ascii=False)
                except Exception:
                    fix_text = str(suggested)
                if len(fix_text) > 120:
                    fix_text = fix_text[:120] + " …"
                fix_widget.update(f"fix → {fix_text}")
                fix_widget.display = True
            else:
                fix_widget.update("")
                fix_widget.display = False
            buttons = self.query_one("#approval-card-buttons", Horizontal)
            buttons.remove_children()
            if guardian:
                specs = [("ap-approve", "✓ Approve (a)"),
                         ("ap-use-fix", "✓ Use fix (u)"),
                         ("ap-reject", "✕ Reject (r)")]
            else:
                specs = [("ap-allow", "✓ Allow (y)"),
                         ("ap-deny", "✕ Deny (n)")]
            for bid, label in specs:
                buttons.mount(Button(label, id=bid))
            self.set_class(True, "visible")
            self.set_class(guardian, "guardian")
        except Exception:
            pass

    def hide(self) -> None:
        try:
            self.set_class(False, "visible")
            self.set_class(False, "guardian")
        except Exception:
            pass
        self._pending = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Mouse/touch path — keyboard goes through key_decision()."""
        bid = event.button.id or ""
        mapping = {
            "ap-allow": "allow", "ap-deny": "deny",
            "ap-approve": "approve", "ap-use-fix": "use_fix",
            "ap-reject": "reject",
        }
        decision = mapping.get(bid)
        if decision is None:
            return
        if self._pending is not None:
            self._pending.resolve(decision)
        event.stop()
