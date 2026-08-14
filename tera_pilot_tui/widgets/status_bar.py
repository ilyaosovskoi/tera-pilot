"""status_bar.py — status header with section indicator, provider info,
session tokens, and animated spinner.

v2.1.0 (Loop 3): Warm, Modern, Content-Forward redesign.
  - Terracotta (#d77757) as primary accent
  - Right-aligned: model · tokens · cost · time · mode
  - Muted (#888888) for secondary info
  - Whimsical verbs for thinking indicator (when used standalone)
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from textual.widgets import Static


SECTION_LABELS = {
    "general": "General",
    "heavy_code": "Heavy Code",
    "office": "Office",
}

GUARDIAN_LABELS = {
    "off": ("off", "grey62"),
    "dangerous_only": ("dangerous", "yellow"),
    "all": ("all", "red"),
}

# Braille spinner frames for thinking/running animation
_SPINNER_FRAMES = [
    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
]

# v2.1.0 (Loop 3): Terracotta accent color
_TERRACOTTA = "#d77757"
_SHIMMER = "#eb9f7f"


class StatusBar(Static):
    """Top status bar with animated state indicators.

    v2.1.0 (Loop 3): Warm terracotta primary + muted secondary.
    Right-aligned: model · tokens · cost · time · mode.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._provider: str = "?"
        self._model: str = "?"
        self._tokens: int = 0
        self._cost: float = 0.0
        self._section: str = "general"
        self._state: str = "idle"
        self._guardian: str = "off"
        self._spinner_task: Optional[asyncio.Task] = None
        self._spinner_frame: int = 0
        # Set initial content so _render() never returns None
        self.update(
            f" [{_TERRACOTTA}]General[/{_TERRACOTTA}]  "
            f" [green]●[/green] idle  |  [b]?[/b]/[dim]?[/dim]  "
            f" [dim]0 tok | $0.0000[/dim]\n"
            "[dim]Enter=send | /=cmds | Ctrl+C=stop | Ctrl+D=quit[/dim]"
        )

    def update_status(
        self,
        status: Dict[str, Any],
        state: str = "idle",
        section: str = "general",
        guardian: Optional[str] = None,
    ) -> None:
        """Update all status fields and refresh the display.

        v2.2.4-fix: thread-safe — if called from a non-TUI thread,
        schedules the update on the async loop instead of calling
        self.update() directly (BUGS_REPORT — StatusBar thread safety).
        """
        self._provider = status.get("provider") or "?"
        self._model = status.get("model") or "?"
        self._tokens = int(status.get("tokens", 0) or 0)
        self._cost = float(status.get("cost", 0.0) or 0.0)
        self._section = section
        if guardian is not None:
            self._guardian = guardian

        old_state = self._state
        self._state = state

        # Start/stop spinner animation based on state change
        if state in ("thinking", "running") and old_state not in ("thinking", "running"):
            self._start_spinner()
        elif state == "idle" and old_state in ("thinking", "running"):
            self._stop_spinner()

        # Thread-safe display refresh
        try:
            app = self.app
            if app and app.is_running:
                app.call_from_thread(self._refresh_display)
                return
        except Exception:
            pass
        # Fallback: direct call (on TUI thread)
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Rebuild and update the status bar text.

        v2.1.0 (Loop 3): Terracotta primary + muted layout.
        """
        state = self._state

        # State indicator with spinner or static icon
        if state == "thinking":
            icon = _SPINNER_FRAMES[self._spinner_frame % len(_SPINNER_FRAMES)]
            state_markup = f"[{_TERRACOTTA}]{icon} thinking[/{_TERRACOTTA}]"
        elif state == "running":
            icon = _SPINNER_FRAMES[self._spinner_frame % len(_SPINNER_FRAMES)]
            state_markup = f"[{_TERRACOTTA}]{icon} tool running[/{_TERRACOTTA}]"
        else:
            state_markup = "[green]●[/green] idle"

        # Section badge — terracotta accent
        section_label = SECTION_LABELS.get(self._section, self._section.title())
        section_style = {
            "general": _TERRACOTTA,
            "heavy_code": "magenta",
            "office": "yellow",
        }.get(self._section, _TERRACOTTA)

        # Guardian badge
        g_label, g_color = GUARDIAN_LABELS.get(self._guardian, ("off", "grey62"))
        guardian_markup = f"[{g_color}]guardian:{g_label}[/{g_color}]"

        left = f" [{section_style}]{section_label}[/{section_style}]  {guardian_markup} "
        center = f" {state_markup}  |  [b]{self._provider}[/b]/[dim]{self._model}[/dim] "
        right = f" [dim]{self._tokens:,} tok | ${self._cost:.4f}[/dim] "

        hints = "[dim]Enter=send | /=cmds | Ctrl+C=stop | Ctrl+D=quit[/dim]"

        self.update(f"{left}{center}{right}\n{hints}")

    # ---- spinner animation ----

    def _start_spinner(self) -> None:
        """Start the braille spinner animation loop."""
        self._stop_spinner()
        try:
            self._spinner_task = asyncio.create_task(self._spin_loop())
        except RuntimeError:
            pass  # no event loop yet

    def _stop_spinner(self) -> None:
        """Stop the spinner animation."""
        if self._spinner_task and not self._spinner_task.done():
            self._spinner_task.cancel()
        self._spinner_task = None
        self._spinner_frame = 0

    async def _spin_loop(self) -> None:
        """Animate the spinner — updates the status bar every 80ms."""
        try:
            while True:
                self._spinner_frame = (self._spinner_frame + 1) % len(_SPINNER_FRAMES)
                self._refresh_display()
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            pass

    def on_unmount(self) -> None:
        """Clean up spinner on widget removal."""
        self._stop_spinner()
