"""command_palette.py - Slash command popup overlay for the TUI.

Used for Ctrl+P full-palette view and sub-selection palettes (e.g.
/section, /model, /chat, /cd). For the inline "/" autocomplete, see
command_suggestions.py.

The palette uses Textual's OptionList for fast keyboard navigation.
Both Enter key and mouse clicks properly select and dismiss the screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static, Input

from .motion import entrance


@dataclass
class CommandEntry:
    """A single slash command entry in the palette."""
    id: str
    label: str
    description: str
    category: str
    has_sub_options: bool


SECTIONS = [
    {"id": "general", "label": "General", "desc": "General-purpose assistant"},
    {"id": "heavy_code", "label": "Heavy Code", "desc": "Advanced autonomous coding with subagents"},
    {"id": "office", "label": "Office Worker", "desc": "Create and edit .docx/.xlsx/.pptx files"},
]

BUILTIN_COMMANDS: List[CommandEntry] = [
    CommandEntry("section", "/section", "Switch runtime section (General / Heavy Code / Office)", "navigation", True),
    CommandEntry("model", "/model", "Switch AI provider/model", "navigation", True),
    CommandEntry("settings", "/settings", "Quick settings (provider, model, API key, theme)", "navigation", False),
    CommandEntry("chat", "/chat", "List and switch to saved chats", "navigation", True),
    CommandEntry("cd", "/cd", "Change workspace directory", "navigation", True),
    CommandEntry("usage", "/usage", "Show token usage and cost for this session", "info", False),
    CommandEntry("files", "/files", "List files in the current workspace", "info", False),
    CommandEntry("clear", "/clear", "Clear the chat log", "action", False),
    CommandEntry("help", "/help", "Show available slash commands", "info", False),
    CommandEntry("planning", "/planning", "Toggle planning mode on/off", "toggle", False),
    CommandEntry("gui", "/gui", "Launch the Tera Pilot GUI window (Ctrl+G)", "action", False),
    # v2.0.0 — Guardian level control
    CommandEntry("guardian", "/guardian", "Set Guardian safety level (off/dangerous_only/all)", "toggle", True),
    # v2.0.0 — Collaboration modes
    CommandEntry("collab", "/collab", "Run a collaboration-mode task (Reviewer/Codegen/Pair/Observer)", "action", True),
    # v2.0.0 — Request queue monitoring
    CommandEntry("queue", "/queue", "Show request queue stats (cooldown, retries, in-flight)", "info", False),
    # v2.0.0 — Persistence backend selector
    CommandEntry("storage", "/storage", "Choose chat storage backend (JSON/SQLite)", "toggle", True),
    # v2.0.0 — SQLite sessions browser
    CommandEntry("sessions", "/sessions", "List SQLite-stored chat sessions", "info", False),
    # v2.0.0 — Context fragments / compaction view
    CommandEntry("context", "/context", "View context fragments & compaction stats", "info", False),
    # v2.0.0 — Progressive tools catalog
    CommandEntry("tools", "/tools", "Browse loaded & available progressive tools", "info", False),
    # v2.0.1 (G7) — Capability catalog
    CommandEntry("capabilities", "/capabilities", "Browse & run pre-built capability templates", "action", True),
    # v2.0.1 (M1) — Second Opinion
    CommandEntry("second_opinion", "/second_opinion", "Configure cross-model Second Opinion (Pro)", "toggle", False),
    # v2.0.1 (G3) — Token budget
    CommandEntry("budget", "/budget", "Configure token budget & efficiency policy", "toggle", False),
    # v2.0.1 (G4) — Cross-model verification
    CommandEntry("verify", "/verify", "Cross-model verification of the last response", "action", False),
    # v2.0.2 (G5) — Agent identity + tool-call audit
    CommandEntry("agents", "/agents", "List agents + their audit stats (G5)", "info", False),
    CommandEntry("audit", "/audit", "Export audit trail JSON / CSV (G5)", "info", False),
    # v2.0.2 (G6) — Post-task handoff
    CommandEntry("handoff", "/handoff", "Create / edit / list handoff docs (G6)", "action", True),
    # v2.0.2 (M2) — Cost-aware provider routing
    CommandEntry("cost", "/cost", "Cost-aware provider routing (M2)", "toggle", False),
    # v2.0.2 (M3) — Team spend dashboard
    CommandEntry("spend", "/spend", "Team spend dashboard (M3)", "info", False),
]


class CommandPalette(ModalScreen):
    """Modal overlay for Ctrl+P command palette and sub-selection menus."""

    BINDINGS = [
        Binding("escape", "close_palette", "Close", show=False),
        Binding("up", "navigate_up", "Up", show=False),
        Binding("down", "navigate_down", "Down", show=False),
        Binding("enter", "select_item", "Select", show=False),
    ]

    CSS = """
    CommandPalette {
        align: center bottom;
    }

    #palette-container {
        width: 70%;
        max-width: 80;
        height: auto;
        max-height: 22;
        background: #111114;
        border: solid #2e2e33;
        padding: 0;
        margin-bottom: 4;
    }

    
    #palette-header {
        background: #d77757 20%;
        color: $text;
        padding: 0 1;
        height: 1;
        text-style: bold;
    }

    #palette-filter {
        height: 3;
        border: solid #2e2e33;
        margin: 0 1;
        padding: 0;
    }

    #palette-list {
        height: auto;
        max-height: 16;
        margin: 0 1;
    }

    #palette-list OptionList {
        height: auto;
        max-height: 16;
    }

    #palette-footer {
        height: 1;
        color: $text-muted;
        padding: 0 1;
        text-style: italic;
    }
    """

    def __init__(
        self,
        custom_commands: Optional[List[CommandEntry]] = None,
        sub_options: Optional[List[Dict[str, Any]]] = None,
        sub_prompt: Optional[str] = None,
        on_select: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> None:
        super().__init__()
        self._custom_commands = custom_commands or []
        self._sub_options = sub_options
        self._sub_prompt = sub_prompt
        self._on_select = on_select
        self._all_commands: List[CommandEntry] = []
        self._filtered_commands: List[CommandEntry] = []
        # Parallel list mapping OptionList index -> command id (for sub-options)
        self._option_ids: List[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-container"):
            yield Static(
                self._sub_prompt or "Slash Commands",
                id="palette-header"
            )
            yield Input(
                placeholder="Filter commands...",
                id="palette-filter",
                value="",
            )
            yield OptionList(id="palette-list")
            yield Static(
                "Up/Down navigate, Enter select, Esc close",
                id="palette-footer"
            )

    def on_mount(self) -> None:
        try:
            self._populate_list()
            # v2.3.1: entrance animation — fade + slight rise.
            entrance(self.query_one("#palette-container"))
            self.query_one("#palette-filter", Input).focus()
        except Exception:
            self.dismiss(result=None)

    def _populate_list(self) -> None:
        list_widget = self.query_one("#palette-list", OptionList)
        list_widget.clear_options()
        self._option_ids = []

        if self._sub_options:
            for opt in self._sub_options:
                label = opt.get("label", "")
                desc = opt.get("desc", "")
                opt_id = opt.get("id", label)
                self._option_ids.append(opt_id)
                list_widget.add_option(f"{label}  -  {desc}")
            if list_widget.option_count > 0:
                list_widget.highlighted = 0
            return

        self._all_commands = list(BUILTIN_COMMANDS) + list(self._custom_commands)
        self._filtered_commands = list(self._all_commands)

        categories: Dict[str, List[CommandEntry]] = {}
        for cmd in self._filtered_commands:
            cat = cmd.category
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(cmd)

        cat_labels = {
            "navigation": "Navigation & Selection",
            "info": "Information",
            "action": "Actions",
            "toggle": "Toggles",
            "custom": "Custom Commands",
        }

        first_real_index = 0
        for cat, cmds in categories.items():
            label = cat_labels.get(cat, cat.title())
            self._option_ids.append("")  # category header - no id
            list_widget.add_option(f"-- {label} --")
            for cmd in cmds:
                if first_real_index == 0:
                    first_real_index = list_widget.option_count
                self._option_ids.append(cmd.id)
                list_widget.add_option(f"{cmd.label}  -  {cmd.description}")

        if first_real_index < list_widget.option_count:
            list_widget.highlighted = first_real_index

    def on_input_changed(self, event: Input.Changed) -> None:
        try:
            input_id = event.input.id if event.input else None
        except Exception:
            return
        if input_id != "palette-filter":
            return
        query = event.value.lower().strip()

        if self._sub_options:
            list_widget = self.query_one("#palette-list", OptionList)
            list_widget.clear_options()
            self._option_ids = []
            for opt in self._sub_options:
                label = opt.get("label", "").lower()
                desc = opt.get("desc", "").lower()
                id_val = opt.get("id", "").lower()
                if not query or query in label or query in desc or query in id_val:
                    opt_id = opt.get("id", opt.get("label", ""))
                    self._option_ids.append(opt_id)
                    list_widget.add_option(
                        f"{opt.get('label', '')}  -  {opt.get('desc', '')}"
                    )
            if list_widget.option_count > 0:
                list_widget.highlighted = 0
            return

        list_widget = self.query_one("#palette-list", OptionList)
        list_widget.clear_options()
        self._option_ids = []

        if not query:
            self._filtered_commands = list(self._all_commands)
        else:
            self._filtered_commands = [
                cmd for cmd in self._all_commands
                if query in cmd.id.lower()
                or query in cmd.label.lower()
                or query in cmd.description.lower()
            ]

        categories: Dict[str, List[CommandEntry]] = {}
        for cmd in self._filtered_commands:
            cat = cmd.category
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(cmd)

        cat_labels = {
            "navigation": "Navigation & Selection",
            "info": "Information",
            "action": "Actions",
            "toggle": "Toggles",
            "custom": "Custom Commands",
        }

        first_real_index = 0
        for cat, cmds in categories.items():
            label = cat_labels.get(cat, cat.title())
            self._option_ids.append("")  # header
            list_widget.add_option(f"-- {label} --")
            for cmd in cmds:
                if first_real_index == 0:
                    first_real_index = list_widget.option_count
                self._option_ids.append(cmd.id)
                list_widget.add_option(f"{cmd.label}  -  {cmd.description}")

        if first_real_index < list_widget.option_count:
            list_widget.highlighted = first_real_index

    def on_option_list_selected(self, event: OptionList.Selected) -> None:
        """Handle mouse click on an option."""
        idx = event.option_index
        if idx is None or idx < 0 or idx >= len(self._option_ids):
            return
        selected_id = self._option_ids[idx]
        if not selected_id:
            return  # category header clicked - ignore

        if self._sub_options:
            self._on_select_and_close(selected_id)
        else:
            cmd = None
            for c in self._filtered_commands:
                if c.id == selected_id:
                    cmd = c
                    break
            if cmd and cmd.has_sub_options:
                self._on_select_and_close(selected_id, needs_sub=True)
            else:
                self._on_select_and_close(selected_id)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key pressed while the filter Input has focus.

        This is the EXACT bug issue #17 exists to catch going forward:
        the ``enter`` binding on the ModalScreen only fires when focus
        is NOT on the Input. When the user types a filter string and
        presses Enter, Textual delivers it as ``Input.Submitted`` to
        the Input, NOT as a binding to the screen. Without this
        handler, the palette was functionally dead for keyboard users
        — they could filter and navigate with Up/Down, but pressing
        Enter did nothing.

        The fix: handle ``Input.Submitted`` the same way as the
        ``action_select_item`` binding — call ``action_select_item()``
        so the highlighted option is selected.

        Mouse clicks go through ``on_option_list_selected`` and are
        unaffected.
        """
        # Don't let Input's default Submitted handler also fire
        # (which would close the palette via the parent App's
        # on_input_submitted).
        event.prevent_default()
        event.stop()
        self.action_select_item()

    def _on_select_and_close(
        self, command_id: str, needs_sub: bool = False
    ) -> None:
        """Dismiss the palette and notify the app."""
        if self._on_select:
            self._on_select(command_id, needs_sub)
        self.dismiss(result=(command_id, needs_sub))

    def action_close_palette(self) -> None:
        self.dismiss(result=None)

    def action_navigate_up(self) -> None:
        list_widget = self.query_one("#palette-list", OptionList)
        try:
            current = list_widget.highlighted
            if current is not None and current > 0:
                list_widget.highlighted = current - 1
        except Exception:
            pass

    def action_navigate_down(self) -> None:
        list_widget = self.query_one("#palette-list", OptionList)
        try:
            current = list_widget.highlighted
            count = list_widget.option_count
            if current is not None and current < count - 1:
                list_widget.highlighted = current + 1
        except Exception:
            pass

    def action_select_item(self) -> None:
        """Enter key handler - select the currently highlighted option."""
        try:
            list_widget = self.query_one("#palette-list", OptionList)
        except Exception:
            self.dismiss(result=None)
            return
        highlighted = list_widget.highlighted
        if highlighted is not None and highlighted < len(self._option_ids):
            selected_id = self._option_ids[highlighted]
            if not selected_id:
                # Category header - skip to next
                return
            if self._sub_options:
                self._on_select_and_close(selected_id)
            else:
                cmd = None
                for c in self._filtered_commands:
                    if c.id == selected_id:
                        cmd = c
                        break
                if cmd and cmd.has_sub_options:
                    self._on_select_and_close(selected_id, needs_sub=True)
                else:
                    self._on_select_and_close(selected_id)
            return
        self.dismiss(result=None)
