"""verification_modal.py — cross-model verification result display (G4).

Shows the result of ``bridge.verify_last_response()`` in a modal that
the user can dismiss with one keystroke. Used by ``/verify`` in the TUI
and (in the GUI) by a JS-side verification panel that calls the
``verify_last_response`` slot directly.

The modal displays:
- The verdict (PASS / WARN / FAIL) with a colour-coded banner.
- The four sub-verdicts (correctness, safety, completeness, overall)
  as a 2x2 grid.
- Issues (if any) as a bulleted list.
- Suggestions (if any) as a bulleted list.
- A summary line.
- Verifier provider + model + elapsed time.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from .motion import entrance


def _verdict_color(verdict: str) -> str:
    v = (verdict or "").upper()
    if v == "PASS":
        return "green"
    if v == "WARN":
        return "yellow"
    if v == "FAIL":
        return "red"
    return "white"


def _verdict_icon(verdict: str) -> str:
    v = (verdict or "").upper()
    if v == "PASS":
        return "OK"
    if v == "WARN":
        return "!"
    if v == "FAIL":
        return "X"
    return "?"


class VerificationModal(ModalScreen[None]):
    """Display a cross-model verification result.

    Returns ``None`` on dismiss — the modal is informational; the
    caller already has the verdict dict (it passed it in).
    """
    BINDINGS = [
        Binding("escape,enter,space,d", "dismiss_modal", "Dismiss"),
    ]

    def __init__(self, result: Dict[str, Any]) -> None:
        super().__init__()
        self._result = result

    def compose(self) -> ComposeResult:
        verification = self._result.get("verification", {}) or {}
        overall = verification.get("overall", "WARN")
        color = _verdict_color(overall)
        icon = _verdict_icon(overall)

        v_provider = self._result.get("verifier_provider", "?")
        v_model = self._result.get("verifier_model", "?")
        elapsed = self._result.get("elapsed_ms", 0)
        original_chars = self._result.get("original_chars", 0)

        with Vertical(id="verify-box"):
            yield Label(
                f"[{color}]Cross-Model Verification: {icon} {overall}[/{color}]",
                id="verify-title",
            )

            # 2x2 sub-verdict grid
            sub_verdicts = [
                ("correctness", verification.get("correctness", "?")),
                ("safety", verification.get("safety", "?")),
                ("completeness", verification.get("completeness", "?")),
                ("overall", verification.get("overall", "?")),
            ]
            with Horizontal(id="verify-grid"):
                for label, val in sub_verdicts:
                    c = _verdict_color(val)
                    yield Static(
                        f"  [{c}]{label:>14}: {val}[/{c}]",
                        classes="verify-cell",
                    )

            yield Static(
                f"\n[dim]Verifier: {v_provider} / {v_model}  "
                f"({elapsed:.0f} ms, {original_chars:,} chars reviewed)[/dim]",
                id="verify-meta",
            )

            summary = verification.get("summary", "")
            if summary:
                yield Static(
                    f"\n[b]Summary[/b]\n{summary}",
                    id="verify-summary",
                )

            issues = verification.get("issues") or []
            if issues:
                issues_text = "\n".join(f"  • {i}" for i in issues)
                yield Static(
                    f"\n[red][b]Issues ({len(issues)})[/b][/red]\n{issues_text}",
                    id="verify-issues",
                )

            suggestions = verification.get("suggestions") or []
            if suggestions:
                sugg_text = "\n".join(f"  • {s}" for s in suggestions)
                yield Static(
                    f"\n[cyan][b]Suggestions ({len(suggestions)})[/b][/cyan]\n{sugg_text}",
                    id="verify-suggestions",
                )

            # If there's a "raw" field (fallback when JSON parse failed),
            # show it so the user can read the verifier's actual output.
            raw = verification.get("raw")
            if raw:
                yield Static(
                    f"\n[dim][b]Raw verifier output[/b]\n{raw}[/dim]",
                    id="verify-raw",
                )

            with Horizontal(id="verify-buttons"):
                yield Button("Dismiss (Esc)", variant="primary", id="dismiss")

    def on_mount(self) -> None:
        """v2.3.1: entrance animation — fade + slight rise."""
        try:
            entrance(self.query_one("#verify-box"))
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
