"""input_box.py — bottom input line with command history and slash trigger.

v2.1.0 (Loop 3): Warm, Modern, Content-Forward redesign.
  - Dashed ASCII border (Claude Code style)
  - `> ` prefix for user messages
  - Surface background (#373737)
  - Muted border (#888888) with shimmer (#a6a6a6)
"""

from __future__ import annotations

from typing import Any, List

from textual import events
from textual.widgets import Input


class InputBox(Input):
    """Bottom input line with command history and slash trigger.

    v2.1.0 (Loop 3): Dashed ASCII border, muted border color,
    surface background. The dashed border is handled by the CSS
    (border: dashed #888888), but the `> ` prefix is shown in
    the placeholder text.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            placeholder=" > your message here_ (Enter=send, / for commands) ",
            **kwargs,
        )
        self._history: List[str] = []
        self._hist_index: int | None = None
        self._suggestions_visible: bool = False

    def set_placeholder_for_command(self, cmd: str) -> None:
        """Set placeholder hint based on the command being typed."""
        hints = {
            "/model": " > /model [provider_id]  (e.g., openai, groq, ollama) ",
            "/provider": " > /provider [provider_id]  (e.g., openai, groq, ollama) ",
            "/chat": " > /chat [chat_id]  (e.g., auto-generated id) ",
            "/cd": " > /cd [path]  (e.g., /Users/you/projects) ",
            "/section": " > /section [general|heavy_code|office]  ",
            "/guardian": " > /guardian [off|dangerous_only|all]  ",
            "/capabilities": " > /capabilities [search_term]  ",
            "/consensus": " > /consensus [--providers p1,p2,p3]  ",
        }
        hint = hints.get(cmd, " > " + cmd + " [args...]  ")
        self.placeholder = hint

    def reset_placeholder(self) -> None:
        """Reset to default placeholder."""
        self.placeholder = " > your message here_ (Enter=send, / for commands) "

    def remember(self, text: str) -> None:
        text = text.strip()
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._hist_index = None

    def set_suggestions_visible(self, visible: bool) -> None:
        """Called by the app to tell us whether the suggestion bar is active."""
        self._suggestions_visible = visible

    async def _on_key(self, event: events.Key) -> None:
        # ---- Enter: submit the prompt ----
        if event.key == "enter":
            value = self.value.strip()
            if value:
                self.value = ""
                # Call the app's submission handler directly.
                app = self.app
                if hasattr(app, "_submit_prompt"):
                    app._submit_prompt(value)
            event.prevent_default()
            event.stop()
            return

        # ---- Up/Down: navigate suggestions or history ----
        if event.key == "up":
            if self._suggestions_visible:
                app = self.app
                if hasattr(app, "_move_suggestion_up"):
                    app._move_suggestion_up()
            else:
                self._history_prev()
            event.stop()
            event.prevent_default()
            return
        if event.key == "down":
            if self._suggestions_visible:
                app = self.app
                if hasattr(app, "_move_suggestion_down"):
                    app._move_suggestion_down()
            else:
                self._history_next()
            event.stop()
            event.prevent_default()
            return

        # Tab — select highlighted suggestion
        if event.key == "tab":
            if self._suggestions_visible:
                app = self.app
                if hasattr(app, "_select_suggestion"):
                    app._select_suggestion()
                event.stop()
                event.prevent_default()
                return

        # Escape — hide suggestions
        if event.key == "escape":
            if self._suggestions_visible:
                app = self.app
                if hasattr(app, "_hide_suggestions"):
                    app._hide_suggestions()
                event.stop()
                event.prevent_default()
                return

        # ALL OTHER KEYS — pass to base Input normally (typing, backspace, etc.)
        await super()._on_key(event)

    def _history_prev(self) -> None:
        if not self._history:
            return
        if self._hist_index is None:
            self._hist_index = len(self._history) - 1
        else:
            self._hist_index = max(0, self._hist_index - 1)
        self.value = self._history[self._hist_index]
        self.cursor_position = len(self.value)

    def _history_next(self) -> None:
        if not self._history or self._hist_index is None:
            return
        if self._hist_index >= len(self._history) - 1:
            self._hist_index = None
            self.value = ""
            return
        self._hist_index += 1
        self.value = self._history[self._hist_index]
        self.cursor_position = len(self.value)
