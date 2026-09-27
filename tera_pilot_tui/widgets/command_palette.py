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

try:
    from textual.widgets import Option as _Option
except ImportError:  # older textual layouts
    from textual.widgets._option_list import Option as _Option

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

# Ordered command groups. The palette and /help render commands under
# these headers; keep the order — “Security & Control” first so the
# safety controls are the first thing a new user sees.
COMMAND_GROUPS: List[Dict[str, str]] = [
    {"id": "security", "label": "Security & Control"},
    {"id": "agent", "label": "Agent & Persona"},
    {"id": "flow", "label": "Review & Delivery"},
    {"id": "provider", "label": "Provider & Model"},
    {"id": "session", "label": "Session & Workspace"},
    {"id": "info", "label": "Info & Stats"},
    {"id": "actions", "label": "Actions & UI"},
    {"id": "custom", "label": "Custom Commands"},
]

COMMAND_GROUP_LABELS: Dict[str, str] = {
    g["id"]: g["label"] for g in COMMAND_GROUPS
}

# Full builtin catalog (v2.4.0: every / command the TUI handles, grouped;
# previously ~15 commands were missing from the palette and /help).
BUILTIN_COMMANDS: List[CommandEntry] = [
    # ── Security & Control ────────────────────────────────────────
    CommandEntry("section", "/section", "Switch runtime section (General / Heavy Code / Office)", "security", True),
    CommandEntry("guardian", "/guardian", "Set Guardian safety level (off/dangerous_only/all)", "security", True),
    CommandEntry("mode", "/mode", "Show / switch section ({office} inline or /mode)", "security", False),
    CommandEntry("planning", "/planning", "Toggle planning mode on/off", "security", False),
    CommandEntry("audit", "/audit", "Export audit trail JSON / CSV / verify", "security", False),
    CommandEntry("audit-signed", "/audit-signed", "Verify a signed/chained audit export", "security", False),
    CommandEntry("permissions", "/permissions", "Allow/deny rules + mode (default/plan/auto/bypass)", "security", False),
    # ── Agent & Persona ───────────────────────────────────────────
    CommandEntry("agent", "/agent", "Pick today's agent profile (code/video/reviewer/apex)", "agent", False),
    CommandEntry("collab", "/collab", "Run a collaboration-mode task (Reviewer/Codegen/Pair/Observer)", "agent", True),
    CommandEntry("handoff", "/handoff", "Create / edit / list handoff docs", "agent", True),
    CommandEntry("persona", "/persona", "Show / edit / update persona memory", "agent", False),
    CommandEntry("canvas", "/canvas", "Show / reset the task canvas", "agent", False),
    # ── Review & Delivery (v2.5.0) ──────────────────────────────────
    CommandEntry("review", "/review", "Review changes or a path (read-only)", "flow", False),
    CommandEntry("security-review", "/security-review", "Adversarial security review", "flow", False),
    CommandEntry("advisor", "/advisor", "Architecture / design advice (no changes)", "flow", False),
    CommandEntry("bughunter", "/bughunter", "Hunt for latent bugs (report only)", "flow", False),
    CommandEntry("commit", "/commit", "Stage and commit one logical change", "flow", False),
    CommandEntry("commit-push-pr", "/commit-push-pr", "Commit, push and open a PR", "flow", False),
    CommandEntry("pr-comments", "/pr-comments", "Fetch and address PR review comments", "flow", False),
    # ── Provider & Model ──────────────────────────────────────────
    CommandEntry("model", "/model", "Switch provider / set model (e.g. /model vscodeapi)", "provider", True),
    CommandEntry("provider", "/provider", "Switch provider (alias of /model)", "provider", True),
    CommandEntry("settings", "/settings", "Quick settings (provider, model, API key)", "provider", False),
    CommandEntry("key", "/key", "Save an API key for a provider", "provider", False),
    CommandEntry("effort", "/effort", "How much the agent explains (normal/brief/detailed)", "provider", False),
    CommandEntry("fast", "/fast", "Minimal chatter + minimal tool calls", "provider", False),
    CommandEntry("brief", "/brief", "Terse output mode", "provider", False),
    CommandEntry("output-style", "/output-style", "File-based output styles (list/switch)", "provider", False),
    CommandEntry("cost", "/cost", "Cost-aware provider routing", "provider", False),
    CommandEntry("budget", "/budget", "Token budget & efficiency policy", "provider", False),
    CommandEntry("endurance", "/endurance", "How long one run may keep working (limits / wall clock)", "provider", False),
    CommandEntry("spend", "/spend", "Team spend dashboard", "provider", False),
    CommandEntry("second_opinion", "/second_opinion", "Cross-model Second Opinion (Pro)", "provider", False),
    CommandEntry("verify", "/verify", "Cross-model verification of the last response", "provider", False),
    CommandEntry("websearch", "/websearch", "Web search backend status", "provider", False),
    CommandEntry("router-mode", "/router-mode", "AutoRouter mode (single / decompose)", "provider", False),
    CommandEntry("route", "/route", "Smart routing/failover status (local prototype)", "provider", False),
    CommandEntry("consensus", "/consensus", "Run a prompt on 2–3 providers in parallel", "provider", False),
    CommandEntry("mcp-server", "/mcp-server", "Manage MCP server connections", "provider", False),
    # ── Session & Workspace ───────────────────────────────────────
    CommandEntry("chat", "/chat", "List and switch to saved chats", "session", True),
    CommandEntry("resume", "/resume", "Restore a saved chat into this session", "session", False),
    CommandEntry("chat-export", "/chat-export", "Save a chat as a portable file", "session", False),
    CommandEntry("chat-import", "/chat-import", "Import a portable chat file", "session", False),
    CommandEntry("cd", "/cd", "Change workspace directory", "session", True),
    CommandEntry("files", "/files", "List files in the current workspace", "session", False),
    CommandEntry("clear", "/clear", "Clear the chat log", "session", False),
    CommandEntry("storage", "/storage", "Choose chat storage backend (JSON/SQLite)", "session", True),
    CommandEntry("sessions", "/sessions", "List SQLite-stored chat sessions", "session", False),
    CommandEntry("context", "/context", "View context fragments & compaction stats", "session", False),
    CommandEntry("checkpoint", "/checkpoint", "Create / manage checkpoints", "session", False),
    CommandEntry("rewind", "/rewind", "Rewind the workspace to a checkpoint", "session", False),
    CommandEntry("github", "/github", "GitHub automation (auth, PRs, issues)", "session", False),
    CommandEntry("daemon", "/daemon", "Remote daemon task management", "session", False),
    CommandEntry("bridge", "/bridge", "IDE bridge server (start/stop/context)", "session", False),
    CommandEntry("notify", "/notify", "Notification backends (Telegram/Discord/Slack)", "session", False),
    CommandEntry("hooks", "/hooks", "Manage the hook system", "session", False),
    CommandEntry("compact", "/compact", "Summarise old conversation, keep recent", "session", False),
    CommandEntry("export", "/export", "Save the current run as JSON", "session", False),
    CommandEntry("share", "/share", "Save the current run as Markdown", "session", False),
    CommandEntry("share-signed", "/share-signed", "Signed, tamper-evident run export", "session", False),
    CommandEntry("rename", "/rename", "Rename a saved chat", "session", False),
    CommandEntry("tag", "/tag", "Tag saved chats", "session", False),
    CommandEntry("init", "/init", "Scaffold the project memory file", "session", False),
    CommandEntry("onboarding", "/onboarding", "First-run checklist with live status", "session", False),
    CommandEntry("remember", "/remember", "Store / list / scan persistent facts", "session", False),
    CommandEntry("schedule", "/schedule", "Show scheduled tasks", "session", False),
    CommandEntry("tasks", "/tasks", "Live background tasks of this run", "session", False),
    CommandEntry("add-dir", "/add-dir", "Extra context dirs outside the workspace", "session", False),
    CommandEntry("sandbox", "/sandbox", "OS sandbox for commands (off/auto/on)", "security", False),
    # ── Info & Stats ──────────────────────────────────────────────
    CommandEntry("usage", "/usage", "Show token usage and cost for this session", "info", False),
    CommandEntry("stats", "/stats", "Session numbers (usage, schedule, chats)", "info", False),
    CommandEntry("queue", "/queue", "Request queue stats (cooldown, retries)", "info", False),
    CommandEntry("tools", "/tools", "Browse loaded & available progressive tools", "info", False),
    CommandEntry("capabilities", "/capabilities", "Browse & run pre-built capability templates", "info", True),
    CommandEntry("learnings", "/learnings", "List / scan / dismiss auto-learning entries", "info", False),
    CommandEntry("improve", "/improve", "Self-improvement backlog: review / plan / close proposals", "info", False),
    CommandEntry("agents", "/agents", "List agents + their audit stats", "info", False),
    # ── Actions & UI ──────────────────────────────────────────────
    CommandEntry("gui", "/gui", "Launch the Tera Pilot GUI window (Ctrl+G)", "actions", False),
    CommandEntry("theme", "/theme", "Switch theme (dark / light)", "actions", False),
    CommandEntry("plugin", "/plugin", "Install / remove / enable plugins", "actions", False),
    CommandEntry("mascot", "/mascot", "The 8-bit pilot, with a one-liner", "actions", False),
    CommandEntry("copy", "/copy", "Copy the last AI answer to the clipboard (Ctrl+O)", "actions", False),
    CommandEntry("help", "/help", "Show available slash commands", "actions", False),
]

# v2.4.0 — agent security levels (shared labels used by /agent)
AGENT_SECURITY_LEVELS = [
    {"id": "controlled", "label": "Controlled", "desc": "Strict — every side effect needs approval"},
    {"id": "balanced", "label": "Balanced", "desc": "New files auto-approved, dangerous actions gated"},
    {"id": "free", "label": "Free", "desc": "Maximum freedom — no approvals"},
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
        background: $surface;
        border: solid $border;
        padding: 0;
        /* v2.5.0: no bottom margin — the #palette-spacer reserves the
           composer space instead (inline margins are ignored on modal
           children in Textual 8.x). */
        margin-bottom: 0;
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
        border: solid $border;
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

    #palette-spacer {
        height: 0;
        width: 1;
    }
    """

    def __init__(
        self,
        custom_commands: Optional[List[CommandEntry]] = None,
        sub_options: Optional[List[Dict[str, Any]]] = None,
        sub_prompt: Optional[str] = None,
        on_select: Optional[Callable[[str, bool], None]] = None,
        above_y: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._custom_commands = custom_commands or []
        self._sub_options = sub_options
        self._sub_prompt = sub_prompt
        self._on_select = on_select
        # v2.5.0: terminal row the palette's bottom edge must not cross
        # (the composer's top) — the app passes the input's y so the
        # menu docks directly above the input instead of floating over
        # the chat AND the typed line.
        self._above_y = above_y
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
        # Bottom spacer — sized in on_mount to the composer+statusline
        # height so the menu docks directly above the input.
        yield Static("", id="palette-spacer")

    def on_mount(self) -> None:
        try:
            self._populate_list()
            self._anchor_above_composer()
            # v2.3.1: entrance animation — fade + slight rise.
            entrance(self.query_one("#palette-container"))
            self.query_one("#palette-filter", Input).focus()
        except Exception:
            self.dismiss(result=None)

    def _anchor_above_composer(self) -> None:
        """Reserve the composer+statusline space below the palette.

        v2.5.0: the menu used to float center-bottom over the chat AND
        the typed line. The app passes the input's y via ``above_y``;
        a bottom spacer takes exactly that many rows so the menu reads
        as an extension of the input. NOTE: implemented with a spacer
        widget (dynamic ``height``), NOT margins — Textual 8.2.8
        ignores inline margin changes on modal children (verified
        empirically), while inline height is honored. Falls back to
        flush-bottom when the geometry is unknown.
        """
        if self._above_y is None:
            return
        try:
            gap = max(0, self.size.height - int(self._above_y))
            self.query_one("#palette-spacer", Static).styles.height = gap
        except Exception:
            pass

    def _set_header(self, shown: int, total: int) -> None:
        try:
            header = self.query_one("#palette-header", Static)
            if shown == total:
                header.update(self._sub_prompt or "Slash Commands")
            else:
                header.update(f"{self._sub_prompt or 'Slash Commands'} — {shown}/{total}")
        except Exception:
            pass

    def _set_footer(self, shown: int) -> None:
        try:
            footer = self.query_one("#palette-footer", Static)
            footer.update(f"↑↓ navigate · Enter select · Esc close · {shown} matches")
        except Exception:
            pass

    def _render_rows(self, list_widget: OptionList, query: str,
                     entries: List[Any], total: int) -> None:
        """Render grouped rows with fuzzy match highlighting.

        LOCAL-PROTOTYPE: matched label characters render bold; headers
        show per-group counts; headers map to "" in _option_ids (kept
        in lock-step so clicks/Enter resolve correctly).
        """
        from rich.text import Text
        from tera_pilot_tui.fuzzy import highlight_text, subsequence_positions
        list_widget.clear_options()
        self._option_ids = []
        groups: Dict[str, List[Any]] = {}

        def _cat(entry: Any) -> str:
            if isinstance(entry, dict):
                return str(entry.get("category", "custom") or "custom")
            return str(getattr(entry, "category", "custom") or "custom")

        def _entry_id(entry: Any) -> str:
            if isinstance(entry, dict):
                return str(entry.get("id", entry.get("label", "")))
            return str(getattr(entry, "id", ""))

        def _label(entry: Any) -> str:
            if isinstance(entry, dict):
                return str(entry.get("label", ""))
            return str(getattr(entry, "label", ""))

        def _desc(entry: Any) -> str:
            if isinstance(entry, dict):
                return str(entry.get("desc", entry.get("description", "")))
            return str(getattr(entry, "description", ""))

        for entry in entries:
            groups.setdefault(_cat(entry), []).append(entry)
        first_real_index = 0
        for cat, cmds in groups.items():
            label = COMMAND_GROUP_LABELS.get(cat, cat.title())
            self._option_ids.append("")
            # v2.5.0: headers are DISABLED options — Textual grays them,
            # skips them in keyboard nav and never fires selection for
            # them, so a group title can never look (or act) clickable.
            # The "" _option_ids entry + guards stay as a second layer.
            try:
                list_widget.add_option(_Option(f"── {label} ({len(cmds)}) ──",
                                              disabled=True))
            except Exception:
                list_widget.add_option(f"── {label} ({len(cmds)}) ──")
            for cmd in cmds:
                if first_real_index == 0:
                    first_real_index = list_widget.option_count
                self._option_ids.append(_entry_id(cmd))
                text_label = _label(cmd)
                positions = (subsequence_positions(query, text_label) or []
                             if query else [])
                highlighted = highlight_text(text_label, positions)
                row = highlighted if isinstance(highlighted, Text) else Text(text_label)
                desc = _desc(cmd)
                if len(desc) > 60:
                    desc = desc[:60] + "…"
                row.append(f"  ·  {desc}", style="dim")
                list_widget.add_option(row)
        if first_real_index < list_widget.option_count:
            list_widget.highlighted = first_real_index
        shown = list_widget.option_count - len(groups) if groups else 0
        self._set_header(shown, total)
        self._set_footer(shown)

    def _populate_list(self) -> None:
        list_widget = self.query_one("#palette-list", OptionList)

        if self._sub_options:
            for opt in self._sub_options:
                label = opt.get("label", "")
                desc = opt.get("desc", "")
                opt_id = opt.get("id", label)
                self._option_ids.append(opt_id)
                list_widget.add_option(f"{label}  -  {desc}")
            if list_widget.option_count > 0:
                list_widget.highlighted = 0
            self._set_header(list_widget.option_count, len(self._sub_options))
            self._set_footer(list_widget.option_count)
            return

        self._all_commands = list(BUILTIN_COMMANDS) + list(self._custom_commands)
        self._filtered_commands = list(self._all_commands)
        self._render_rows(list_widget, "", self._filtered_commands,
                          len(self._all_commands))

    def on_input_changed(self, event: Input.Changed) -> None:
        try:
            input_id = event.input.id if event.input else None
        except Exception:
            return
        if input_id != "palette-filter":
            return
        query = event.value.lower().strip()

        if self._sub_options:
            from tera_pilot_tui.fuzzy import match_commands
            from rich.text import Text
            from tera_pilot_tui.fuzzy import highlight_text, subsequence_positions
            list_widget = self.query_one("#palette-list", OptionList)
            list_widget.clear_options()
            self._option_ids = []
            matched = match_commands(query, self._sub_options)
            for opt, _score, _pos in matched:
                opt_id = opt.get("id", opt.get("label", ""))
                self._option_ids.append(opt_id)
                label = str(opt.get("label", ""))
                positions = (subsequence_positions(query, label) or []
                             if query else [])
                highlighted = highlight_text(label, positions)
                row = highlighted if isinstance(highlighted, Text) else Text(label)
                desc = str(opt.get("desc", ""))
                if desc:
                    row.append(f"  ·  {desc[:60]}", style="dim")
                list_widget.add_option(row)
            if list_widget.option_count > 0:
                list_widget.highlighted = 0
            self._set_header(list_widget.option_count, len(self._sub_options))
            self._set_footer(list_widget.option_count)
            return

        list_widget = self.query_one("#palette-list", OptionList)
        list_widget.clear_options()
        self._option_ids = []

        from tera_pilot_tui.fuzzy import match_commands
        if not query:
            self._filtered_commands = list(self._all_commands)
        else:
            self._filtered_commands = [
                cmd for cmd, _score, _pos in match_commands(query, self._all_commands)
            ]
        self._render_rows(list_widget, query, self._filtered_commands,
                          len(self._all_commands))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle mouse click on an option.

        v2.3.4-fix: the handler was named ``on_option_list_selected``,
        which Textual 8.x never calls — the real dispatch name for
        ``OptionList.OptionSelected`` is ``on_option_list_option_selected``.
        Mouse clicks on palette options were dead; only keyboard Enter
        worked.
        """
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

        Mouse clicks go through ``on_option_list_option_selected`` and are
        unaffected (v2.3.4-fix: the old ``on_option_list_selected`` name
        never fired in Textual 8.x).
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
                nxt = current - 1
                # Skip group headers ("" in _option_ids); if nothing
                # real is above, stay where we are.
                while (nxt >= 0 and nxt < len(self._option_ids)
                        and not self._option_ids[nxt]):
                    nxt -= 1
                if nxt < 0:
                    return
                list_widget.highlighted = nxt
        except Exception:
            pass

    def action_navigate_down(self) -> None:
        list_widget = self.query_one("#palette-list", OptionList)
        try:
            current = list_widget.highlighted
            count = list_widget.option_count
            if current is not None and current < count - 1:
                nxt = current + 1
                # Skip group headers ("" in _option_ids); if nothing
                # real is below, stay where we are.
                while (nxt < count and nxt < len(self._option_ids)
                        and not self._option_ids[nxt]):
                    nxt += 1
                if nxt >= count:
                    return
                list_widget.highlighted = nxt
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
                # Category header — advance to the next real command.
                nxt = highlighted + 1
                while nxt < len(self._option_ids) and not self._option_ids[nxt]:
                    nxt += 1
                if nxt < len(self._option_ids):
                    list_widget.highlighted = nxt
                    highlighted = nxt
                    selected_id = self._option_ids[nxt]
                else:
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
