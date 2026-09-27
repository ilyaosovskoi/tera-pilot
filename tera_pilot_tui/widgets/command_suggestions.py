"""command_suggestions.py — inline slash-command suggestion bar.

Sits ABOVE the input line. When the user types "/" in the InputBox,
this widget appears showing matching commands. Up/Down arrows in the
InputBox navigate the suggestion list; Tab or Enter selects the
highlighted command.

Unlike CommandPalette (a ModalScreen), this widget does NOT steal focus
or block input - the user keeps typing to filter, and the suggestions
update in real time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import OptionList, Static

from .command_palette import CommandEntry, BUILTIN_COMMANDS
from .motion import fade_in


@dataclass
class SuggestionItem:
    """A single suggestion shown in the bar."""
    id: str
    label: str
    description: str
    needs_sub: bool  # True = command requires a parameter selection


class CommandSuggestions(Widget):
    """Inline suggestion list that appears above the input when the user
    types '/'.

    Navigation:
      - InputBox Up/Down -> highlight moves through suggestions
      - Tab or Enter on highlighted item -> select command
      - Escape -> hide suggestions, keep the typed text
    """

    DEFAULT_CSS = """
    CommandSuggestions {
        height: 0;
        /* v2.5.0: in normal flow above the composer (NOT docked —
           dock:bottom + display:none→block mislays the bar below the
           input, over the statusline — same bug as the approval card). */
        margin: 0 1 0 1;
    }

    CommandSuggestions.visible {
        height: auto;
    }

    #suggestions-box {
        height: auto;
        max-height: 12;
        background: $panel;
        border: tall $primary;
        padding: 0 1;
    }

    #suggestions-box OptionList {
        height: auto;
        max-height: 10;
    }

    #suggestions-footer {
        height: 1;
        color: $text-disabled;
        padding: 0 1;
        text-style: italic;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._all_commands: List[CommandEntry] = []
        self._items: List[SuggestionItem] = []
        self._highlighted: int = -1
        self._last_query: str = ""
        self._on_select: Any = None  # callback(item: SuggestionItem)

    def compose(self) -> ComposeResult:
        with Vertical(id="suggestions-box"):
            yield OptionList(id="suggestions-list")
            yield Static(
                " Tab=select | Esc=close | Up/Down=navigate ",
                id="suggestions-footer",
            )

    def on_mount(self) -> None:
        self.set_class(False, "visible")

    # ---- public API ---------------------------------------------------

    def set_commands(self, commands: List[CommandEntry],
                     custom_entries: Optional[List[CommandEntry]] = None) -> None:
        """Store the full command list (builtins + custom)."""
        self._all_commands = list(commands)
        if custom_entries:
            self._all_commands.extend(custom_entries)

    def show_suggestions(self, query: str = "") -> None:
        """Filter and show suggestions matching *query* (without the leading '/').

        LOCAL-PROTOTYPE: weighted fuzzy matching (name ×3, word parts
        ×2, description ×0.5) instead of plain substring — "cm" finds
        /commit and /compact, typos still match.
        """
        from tera_pilot_tui.fuzzy import match_commands
        query_lower = query.lower().lstrip("/")
        self._last_query = query_lower

        self._items = []
        for cmd, _score, _positions in match_commands(query_lower, self._all_commands):
            self._items.append(SuggestionItem(
                id=cmd.id,
                label=cmd.label,
                description=cmd.description,
                needs_sub=cmd.has_sub_options,
            ))

        self._highlighted = 0 if self._items else -1
        self._sync_highlight()

        if self._items:
            self.set_class(True, "visible")
            # v2.3.1: fade the suggestion bar in instead of popping it.
            try:
                fade_in(self.query_one("#suggestions-box"))
            except Exception:
                pass
        else:
            self.set_class(False, "visible")

    def hide(self) -> None:
        """Hide the suggestion bar."""
        self.set_class(False, "visible")
        try:
            list_w = self.query_one("#suggestions-list", OptionList)
            list_w.clear_options()
        except Exception:
            pass
        self._items = []
        self._highlighted = -1

    def set_on_select(self, callback: Any) -> None:
        """Set the callback for when a suggestion is selected."""
        self._on_select = callback

    def move_up(self) -> None:
        if self._highlighted > 0:
            self._highlighted -= 1
            self._sync_highlight()

    def move_down(self) -> None:
        if self._highlighted < len(self._items) - 1:
            self._highlighted += 1
            self._sync_highlight()

    def select_highlighted(self) -> Optional[SuggestionItem]:
        """Return the currently highlighted item (or None)."""
        if 0 <= self._highlighted < len(self._items):
            item = self._items[self._highlighted]
            if self._on_select:
                self._on_select(item)
            return item
        return None

    @property
    def is_visible(self) -> bool:
        return len(self._items) > 0

    # ---- internal ------------------------------------------------------

    def _sync_highlight(self) -> None:
        """Rebuild the list with highlight indicator and set highlight.

        LOCAL-PROTOTYPE: matched characters in the label render bold so
        the eye lands on why each row matched; descriptions stay dim and
        truncated. Rows are Rich Text objects (OptionList accepts them).
        v2.5.0: the highlighted row ALSO gets a ❯ marker + bold label so
        the choice reads without relying on the list's background tint.
        """
        from rich.text import Text
        from tera_pilot_tui.fuzzy import highlight_text, subsequence_positions
        try:
            list_w = self.query_one("#suggestions-list", OptionList)
        except Exception:
            return
        list_w.clear_options()
        current_query = getattr(self, "_last_query", "")
        for i, item in enumerate(self._items):
            selected = i == self._highlighted
            prefix = "❯ " if selected else "  "
            positions = (subsequence_positions(current_query, item.label) or []
                         if current_query else [])
            # Bold base for the selected row; matched chars stay bold
            # via the mark style in both cases.
            highlighted = highlight_text(item.label, positions,
                                         plain_style="bold" if selected else "")
            row = Text(prefix, style="bold" if selected else "")
            row.append_text(highlighted if isinstance(highlighted, Text)
                            else Text(item.label))
            desc = item.description
            if len(desc) > 60:
                desc = desc[:60] + "…"
            row.append(f"  —  {desc}", style="dim")
            list_w.add_option(row)
        if 0 <= self._highlighted < list_w.option_count:
            list_w.highlighted = self._highlighted

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle mouse click on a suggestion item.

        v2.3.4-fix: renamed from ``on_option_list_selected`` — Textual
        8.x dispatches ``OptionList.OptionSelected`` to
        ``on_option_list_option_selected``, so the old name was never
        called and clicks on suggestions did nothing.
        """
        idx = event.option_index
        if 0 <= idx < len(self._items):
            item = self._items[idx]
            # Update highlight to clicked item
            self._highlighted = idx
            self._sync_highlight()
            if self._on_select:
                self._on_select(item)
