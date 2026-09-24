"""input_box.py — bottom composer with command history and slash trigger.

v2.4.3: the composer is a ``TextArea`` (was a single-line ``Input``) so a
long request stays readable while you type. The box soft-wraps the text
and grows upward, up to ``max-height`` (see styles_*.tcss); past that it
scrolls internally. Keyboard contract:

  - Enter              — submit the prompt (unchanged)
  - Shift+Enter/Ctrl+J — insert a newline (compose a multi-line prompt)
  - Up/Down            — history recall while the prompt is one logical
                         line; cursor movement once it spans several
  - Tab                — select the highlighted slash suggestion
  - Escape             — hide the slash suggestions

v2.1.0 (Loop 3): Warm, Modern, Content-Forward redesign.
  - Dashed ASCII border (terminal style)
  - `> ` prefix for user messages
  - Surface background (#373737)
  - Muted border (#888888) with shimmer (#a6a6a6)
"""

from __future__ import annotations

from typing import Any, List

from textual import events
from textual.widgets import TextArea

DEFAULT_PLACEHOLDER = (
    " > your message here_ (Enter=send, Shift+Enter=newline, / for commands) "
)


class InputBox(TextArea):
    """Bottom composer with command history and slash trigger.

    v2.4.3: multi-line + auto-growing. Soft wrapping means an arbitrarily
    long prompt is visible in full instead of scrolling off a single line.
    The `value` alias keeps the ``Input.value`` API the app and tests use.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            soft_wrap=True,
            placeholder=DEFAULT_PLACEHOLDER,
            **kwargs,
        )
        self._history: List[str] = []
        self._hist_index: int | None = None
        self._suggestions_visible: bool = False

    # -------------------------------------------------------------- value API
    @property
    def value(self) -> str:
        """The composer text — same property name the old ``Input`` used."""
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = "" if text is None else text
        self._cursor_to_end()

    def _cursor_to_end(self) -> None:
        """Put the caret after the last character (Input-like behavior)."""
        lines = self.text.split("\n")
        try:
            self.move_cursor((len(lines) - 1, len(lines[-1])))
            self.scroll_cursor_visible()
        except Exception:
            pass

    def set_placeholder_for_command(self, cmd: str) -> None:
        """Set placeholder hint based on the command being typed."""
        hints = {
            "/model": " > /model [provider_id | model]  (e.g., openrouter, ox-alpha) ",
            "/provider": " > /provider [provider_id | model]  (e.g., openrouter, ox-alpha) ",
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
        self.placeholder = DEFAULT_PLACEHOLDER

    def remember(self, text: str) -> None:
        text = text.strip()
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._hist_index = None

    def set_suggestions_visible(self, visible: bool) -> None:
        """Called by the app to tell us whether the suggestion bar is active."""
        self._suggestions_visible = visible

    # ---------------------------------------------------------------- keys
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        # ---- v2.5.0: inline approval takes over the composer ----
        # While a confirmation card is pending, decision keys resolve it
        # (y/n, a/u/r, Enter/Esc per the card hint); every other key is
        # swallowed so the user can't type a prompt the turn would then
        # misread. Ctrl-combos (interrupt, quit) always pass through.
        try:
            _approval = getattr(getattr(self, "app", None), "_inline_approval", None)
        except Exception:
            _approval = None
        if _approval is not None and getattr(_approval, "active", False):
            if key.startswith("ctrl"):
                await super()._on_key(event)
                return
            from .approval_card import key_decision
            decision = key_decision(key, bool(getattr(_approval, "guardian", False)))
            if decision is not None:
                _approval.resolve(decision)
            event.prevent_default()
            event.stop()
            return

        # ---- Enter: submit the prompt ----
        if key == "enter":
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

        # ---- Shift+Enter / Ctrl+J: explicit newline in the composer ----
        if key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            self.scroll_cursor_visible()
            return

        # ---- Up/Down: navigate suggestions or history ----
        if key in ("up", "down"):
            if self._suggestions_visible:
                app = self.app
                handler = "_move_suggestion_up" if key == "up" else "_move_suggestion_down"
                if hasattr(app, handler):
                    getattr(app, handler)()
            elif "\n" in self.value:
                # Multi-line prompt: the caret moves through the text
                # instead of recalling history — don't swallow the key.
                return
            elif key == "up":
                self._history_prev()
            else:
                self._history_next()
            event.stop()
            event.prevent_default()
            return

        # Tab — select highlighted suggestion
        if key == "tab":
            if self._suggestions_visible:
                app = self.app
                if hasattr(app, "_select_suggestion"):
                    app._select_suggestion()
                event.stop()
                event.prevent_default()
                return

        # Escape — hide suggestions
        if key == "escape":
            if self._suggestions_visible:
                app = self.app
                if hasattr(app, "_hide_suggestions"):
                    app._hide_suggestions()
                event.stop()
                event.prevent_default()
                return

        # ALL OTHER KEYS — pass to the base TextArea (typing, backspace, …)
        await super()._on_key(event)

    def _history_prev(self) -> None:
        if not self._history:
            return
        if self._hist_index is None:
            self._hist_index = len(self._history) - 1
        else:
            self._hist_index = max(0, self._hist_index - 1)
        self.value = self._history[self._hist_index]

    def _history_next(self) -> None:
        if not self._history or self._hist_index is None:
            return
        if self._hist_index >= len(self._history) - 1:
            self._hist_index = None
            self.value = ""
            return
        self._hist_index += 1
        self.value = self._history[self._hist_index]
