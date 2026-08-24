"""app.py — TeraPilotTUIApp, the Textual entry point.

Layout: status bar on top, scrollable chat log in the middle, inline
command-suggestion bar above the input, input line at the bottom.

The original Input.Submitted mechanism is preserved — Enter works natively.
Inline suggestions appear when "/" is typed but do NOT intercept Enter.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input

from .bridge import TeraPilotBridge
from .widgets.approval_modal import ApprovalModal, GuardianModal
from .widgets.chat_log import ChatLog
from .widgets.command_palette import CommandPalette, CommandEntry, SECTIONS, BUILTIN_COMMANDS
from .widgets.command_suggestions import CommandSuggestions, SuggestionItem
from .widgets.input_box import InputBox
from .widgets.info_box import InfoBox
from .widgets.model_selector_modal import ModelSelectorModal
from .widgets.verification_modal import VerificationModal
from .widgets.task_canvas_view import TaskCanvasView
from .widgets.settings_modal import QuickSettingsModal


class TeraPilotTUIApp(App):
    CSS_PATH = "styles_dark.tcss"
    TITLE = "tera_pilot"

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True, show=True),
        Binding("ctrl+d", "quit", "Quit", priority=True, show=True),
        Binding("ctrl+g", "launch_gui", "GUI", show=True),
        # v2.3.5-fix: priority=True — Textual's App.__init__ auto-binds
        # ctrl+p → command_palette with priority=True UNLESS the app
        # already binds an action named ``command_palette``. Our action
        # is named differently (open_command_palette), so Textual added
        # its own system palette and (being priority=True) it won the
        # key — Ctrl+P showed Textual's Maximize/Quit/Screenshot instead
        # of the project's slash-command palette. Marking ours priority
        # makes our binding resolve first.
        Binding("ctrl+p", "open_command_palette", "Commands", priority=True, show=True),
        Binding("ctrl+t", "toggle_theme", "Theme", show=True),
    ]

    def __init__(self, bridge: Optional[TeraPilotBridge] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bridge = bridge or TeraPilotBridge()
        self._turn_running = False
        self._last_prompt: str = ""
        self._suggestions_active: bool = False
        self._dark_theme: bool = True  # Start with dark theme
        # v2.3.4-fix: track the open approval/guardian modal so it can be
        # popped when the turn ends, errors, or is interrupted. Previously
        # the modal stayed on screen forever once the agent moved on
        # (stale dialog over an idle app).
        self._approval_modal = None
        # v2.3.5-fix: last error already rendered via the agent's ERROR
        # event. The runtime emits an ERROR event AND returns
        # success=False for the same terminal failure, so without this
        # the ChatLog showed every run-ending error twice (once from the
        # event, once from _on_turn_done).
        self._last_event_error: Optional[str] = None
        # v2.3.6: animated "thinking…" status line while a turn runs.
        self._status_state: str = "idle"
        self._status_frame: int = 0

    # ---------------------------------------------------------------- compose
    def compose(self) -> ComposeResult:
        yield InfoBox(id="info")
        yield ChatLog(id="chat")
        yield CommandSuggestions(id="suggestions")
        yield InputBox(id="input")

    def on_mount(self) -> None:
        self.bridge.set_event_sink(self._sink)
        self.bridge.set_confirm_handler(self._confirm)
        self.bridge.set_guardian_handler(self._confirm)

        try:
            from tera_pilot import __version__ as _tera_pilot_version
        except Exception:
            _tera_pilot_version = "2.3.7"

        # Initialize InfoBox with current state
        info = self.query_one(InfoBox)
        status = self.bridge.status()
        info.update_info(
            model=status.get("model", "unknown"),
            provider=status.get("provider", "unknown"),
            directory=self.bridge.workspace,
            version=_tera_pilot_version,
        )

        # Initialize chat
        chat = self.query_one(ChatLog)
        chat.add_system(
            f"Type a request and press Enter.\n"
            f"Type / for slash commands (/model, /provider, /capabilities, etc.).\n"
            f"Ctrl+C to interrupt, Ctrl+D to quit, Ctrl+G to launch GUI."
        )

        # Set up suggestions
        sug = self.query_one(CommandSuggestions)
        sug.set_commands(BUILTIN_COMMANDS)
        sug.set_on_select(self._on_suggestion_selected)

        # v2.3.6: drive the animated "thinking…" status line.
        self.set_interval(0.15, self._tick_status_animation)

        self.query_one(InputBox).focus()

    # --------------------------------------------------------- suggestions
    def _show_suggestions(self, query: str = "") -> None:
        sug = self.query_one(CommandSuggestions)
        custom_cmds = self.bridge.list_slash_commands()
        custom_entries = []
        for c in custom_cmds:
            custom_entries.append(
                CommandEntry(
                    id=c["id"],
                    label=f"/{c['id']}",
                    description=c.get("description", c.get("name", "")),
                    category="custom",
                    has_sub_options=False,
                )
            )
        sug.set_commands(BUILTIN_COMMANDS, custom_entries=custom_entries)
        sug.show_suggestions(query)
        self._suggestions_active = sug.is_visible
        self.query_one(InputBox).set_suggestions_visible(self._suggestions_active)

    def _hide_suggestions(self) -> None:
        sug = self.query_one(CommandSuggestions)
        sug.hide()
        self._suggestions_active = False
        self.query_one(InputBox).set_suggestions_visible(False)

    def _move_suggestion_up(self) -> None:
        self.query_one(CommandSuggestions).move_up()

    def _move_suggestion_down(self) -> None:
        self.query_one(CommandSuggestions).move_down()

    def _select_suggestion(self) -> Optional[SuggestionItem]:
        sug = self.query_one(CommandSuggestions)
        return sug.select_highlighted()

    def _on_suggestion_selected(self, item: SuggestionItem) -> None:
        """Called when user picks a suggestion (Tab or click)."""
        self._hide_suggestions()
        box = self.query_one(InputBox)
        box.value = ""
        if item.needs_sub:
            self._open_sub_palette_for_cmd(item.id)
        else:
            # Show command hint for commands that need parameters
            if item.id in ["/model", "/provider", "/chat", "/cd", "/section", "/guardian", "/capabilities", "/consensus"]:
                box.set_placeholder_for_command(item.id)
            else:
                box.reset_placeholder()
            self._execute_builtin_cmd(item.id)
        box.focus()

    # ------------------------------------------------------------- user input
    def _submit_prompt(self, prompt: str) -> None:
        """Core submission logic — called directly from InputBox on Enter."""
        if not prompt:
            return
        if self._turn_running:
            self.bell()
            return

        # If it's a slash command (even without suggestions), handle it directly
        if prompt.startswith("/"):
            self._handle_slash_input(prompt)
            return

        # If suggestions visible, try selecting highlighted one
        if self._suggestions_active:
            selected = self._select_suggestion()
            if selected is not None:
                return  # _on_suggestion_selected handled it
            self._hide_suggestions()

        # v2.1.0 (Loop 1): inline section switch — parse leading
        # {section} or /mode section tokens before the message reaches
        # the LLM. If a section token is found, switch section and show
        # a toast, then pass the cleaned message to the bridge.
        from tera_pilot.agent_runtime.section_parser import parse_section_switch
        new_section, cleaned_prompt = parse_section_switch(prompt)
        if new_section is not None:
            result = self.bridge.set_section(new_section.value)
            chat = self.query_one(ChatLog)
            if result.get("ok"):
                name = {"general": "General", "heavy_code": "Heavy Code", "office": "Office Worker"}.get(
                    new_section.value, new_section.value)
                chat.add_system(f"Section switched to: [b]{name}[/b]")
                self._refresh_status("idle")
            else:
                chat.add_error(f"Failed to switch section: {result.get('error', 'unknown')}")
            # If the cleaned message is empty (user just typed {office}
            # or /mode office), don't send a turn — just switch section.
            if not cleaned_prompt:
                box = self.query_one(InputBox)
                box.remember(prompt)
                box.value = ""
                box.focus()
                return
            # Otherwise, proceed with the cleaned message
            prompt = cleaned_prompt

        # Normal message
        box = self.query_one(InputBox)
        box.remember(prompt)
        box.value = ""
        self.query_one(ChatLog).add_user(prompt)
        self._last_prompt = prompt
        self._turn_running = True
        self._refresh_status("thinking")
        self._run_turn(prompt)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Fallback handler — intentionally disabled.

        InputBox._on_key intercepts Enter before Input.Submitted can fire.
        Keeping this enabled caused DOUBLE submission: the call from the
        key handler set _running=True, then this handler fired again,
        saw _running=True, and silently dropped the message — meanwhile
        the user's prompt was lost on the first call.
        """
        return

    def on_input_changed(self, event: Input.Changed) -> None:
        """Show/hide inline suggestions when input starts/stop with '/'."""
        if event.input.id != "input":
            return
        val = event.value
        if val.startswith("/"):
            self._show_suggestions(val)
        elif self._suggestions_active:
            self._hide_suggestions()

    # --------------------------------------------------------- slash commands
    def _handle_slash_input(self, prompt: str) -> None:
        parts = prompt.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        # Bare "/" — open full command palette
        if cmd == "/":
            self.open_command_palette()
            return

        # Custom .md commands
        resolved = self.bridge.resolve_slash_command(prompt)
        if resolved:
            expanded = resolved.get("expanded", prompt)
            box = self.query_one(InputBox)
            box.remember(prompt)
            self.query_one(ChatLog).add_user(prompt)
            self._last_prompt = expanded
            self._turn_running = True
            self._refresh_status("thinking")
            self._run_turn(expanded)
            return

        # Built-in commands
        if cmd == "/section":
            if arg:
                self._exec_section(arg)
            else:
                self._open_sub_palette("section", SECTIONS)
        elif cmd == "/model":
            self.query_one(ChatLog).add_user(prompt)
            if arg:
                self._exec_model(arg)
            else:
                self._open_model_palette()
        elif cmd == "/settings":
            self._open_quick_settings()
        elif cmd == "/provider":
            # /provider is an alias for /model — switch provider and/or
            # set the model. _exec_model handles "<provider>",
            # "<provider> <model>" and a bare "<model>" (applied to the
            # currently active provider), so both aliases share one path.
            self.query_one(ChatLog).add_user(prompt)
            if arg:
                self._exec_model(arg)
            else:
                self._open_model_palette()
        elif cmd == "/chat":
            if arg:
                self._exec_chat(arg)
            else:
                self._open_chat_palette()
        elif cmd == "/cd":
            if arg:
                self._exec_cd(arg)
            else:
                self._open_cd_palette()
        elif cmd == "/usage":
            self._exec_usage()
        elif cmd == "/files":
            self._exec_files()
        elif cmd == "/clear":
            self._exec_clear()
        elif cmd == "/help":
            self._exec_help()
        elif cmd == "/planning":
            self._exec_planning()
        elif cmd == "/gui":
            self.action_launch_gui()
        elif cmd == "/guardian":
            self._exec_guardian(arg)
        elif cmd == "/collab":
            self._exec_collab(arg)
        elif cmd == "/queue":
            self._exec_queue()
        elif cmd == "/storage":
            self._exec_storage(arg)
        elif cmd == "/sessions":
            self._exec_sessions()
        elif cmd == "/context":
            self._exec_context()
        elif cmd == "/tools":
            self._exec_tools()
        elif cmd == "/capabilities":
            self._exec_capabilities(arg)
        elif cmd == "/second_opinion":
            self._exec_second_opinion(arg)
        elif cmd == "/verify":
            self._exec_verify(arg)
        elif cmd == "/budget":
            self._exec_budget(arg)
        elif cmd == "/agents":
            self._exec_agents(arg)
        elif cmd == "/audit":
            self._exec_audit(arg)
        elif cmd == "/handoff":
            self._exec_handoff(arg)
        elif cmd == "/cost":
            self._exec_cost(arg)
        elif cmd == "/spend":
            self._exec_spend(arg)
        elif cmd == "/hooks":
            self._exec_hooks(arg)
        elif cmd == "/checkpoint":
            self._exec_checkpoint(arg)
        elif cmd == "/rewind":
            self._exec_rewind(arg)
        elif cmd == "/github":
            self._exec_github(arg)
        elif cmd == "/mcp-server":
            self._exec_mcp_server(arg)
        elif cmd == "/notify":
            self._exec_notify(arg)
        elif cmd == "/daemon":
            self._exec_daemon(arg)
        # v2.1.0 (G15): multi-provider consensus engine
        elif cmd == "/consensus":
            self._exec_consensus(arg)
        # v2.1.0 (G16): signed audit trail — /audit verify <file>
        elif cmd == "/audit-signed":
            self._exec_audit_signed(arg)
        # v2.1.0 (G17): automatic learning loop
        elif cmd == "/learnings":
            self._exec_learnings(arg)
        # v2.1.0 (G18): web search backend status
        elif cmd == "/websearch":
            self._exec_websearch(arg)
        # v2.1.0 (Loop 1): /mode slash command for section switching
        elif cmd == "/mode":
            self._exec_mode(arg)
        # v2.1.0 (Loop 3): /theme slash command for theme switching
        elif cmd == "/theme":
            self._exec_theme(arg)
        # G19a: task canvas view
        elif cmd == "/canvas":
            self._exec_canvas(arg)
        # G19b: persona memory
        elif cmd == "/persona":
            self._exec_persona(arg)
        # G20c: router mode
        elif cmd == "/router-mode":
            self._exec_router_mode(arg)
        else:
            self.query_one(ChatLog).add_system(
                f"Unknown command: {cmd}. Type /help for available commands."
            )

    # ── Command palette (Ctrl+P) ──────────

    def open_command_palette(self) -> None:
        custom_cmds = self.bridge.list_slash_commands()
        custom_entries = []
        for c in custom_cmds:
            custom_entries.append(
                CommandEntry(
                    id=c["id"],
                    label=f"/{c['id']}",
                    description=c.get("description", c.get("name", "")),
                    category="custom",
                    has_sub_options=False,
                )
            )
        palette = CommandPalette(custom_commands=custom_entries)

        def on_result(result: Optional[Tuple[str, bool]]) -> None:
            if result is None:
                box = self.query_one(InputBox)
                if box.value.startswith("/"):
                    box.value = ""
                box.focus()
                return
            cmd_id, needs_sub = result
            box = self.query_one(InputBox)
            box.value = ""
            if needs_sub:
                self._open_sub_palette_for_cmd(cmd_id)
            else:
                self._execute_builtin_cmd(cmd_id)

        self.push_screen(palette, on_result)

    def action_open_command_palette(self) -> None:
        self.open_command_palette()

    # ── Sub-selection palettes ────────────────────────────────────

    def _open_sub_palette_for_cmd(self, cmd_id: str) -> None:
        if cmd_id == "section":
            self._open_sub_palette("section", SECTIONS)
        elif cmd_id == "model":
            self._open_model_palette()
        elif cmd_id == "chat":
            self._open_chat_palette()
        elif cmd_id == "cd":
            self._open_cd_palette()
        elif cmd_id == "guardian":
            self._open_guardian_palette()
        elif cmd_id == "collab":
            self._open_collab_palette()
        elif cmd_id == "storage":
            self._open_storage_palette()
        elif cmd_id == "capabilities":
            # v2.1.1 (G22b): route through _exec_capabilities("") so
            # selecting /capabilities from the main palette actually
            # opens the browse palette. Without this, the main-palette
            # selection fell through to the "needs a parameter" branch
            # even though the entry is marked has_sub_options=True —
            # same class of dead-binding bug issue #17 was opened for.
            self._exec_capabilities("")
        elif cmd_id == "handoff":
            # v2.1.1 (G22b): same fix — list_handoffs() shows the
            # handoff browser. If there are no handoffs, _exec_handoff
            # already prints a helpful message.
            self._exec_handoff("list")
        else:
            self.query_one(ChatLog).add_system(
                f"Command /{cmd_id} needs a parameter. Type /{cmd_id} <value> directly."
            )
            self.query_one(InputBox).focus()

    def _open_sub_palette(self, cmd_name: str, options: List[Dict[str, Any]]) -> None:
        palette = CommandPalette(
            sub_options=options,
            sub_prompt=f"Select {cmd_name}...",
        )

        def on_result(result: Optional[Tuple[str, bool]]) -> None:
            if result is None:
                self.query_one(InputBox).focus()
                return
            selected_id, _ = result
            if cmd_name == "section":
                self._exec_section(selected_id)
            elif cmd_name == "model":
                self._exec_model(selected_id)
            elif cmd_name == "chat":
                self._exec_chat(selected_id)
            elif cmd_name == "cd":
                self._exec_cd(selected_id)
            elif cmd_name == "guardian":
                self._exec_guardian(selected_id)
            elif cmd_name == "collab":
                self._exec_collab(selected_id)
            elif cmd_name == "storage":
                self._exec_storage(selected_id)
            elif cmd_name == "capability":
                # Treat palette-pick as "/capabilities <id>" — will
                # either run it (no required placeholders) or show
                # detail with the missing ones.
                self._exec_capabilities(selected_id)

        self.push_screen(palette, on_result)

    # ── v2.2.4: Quick Settings (/settings) ──────────────────────────

    def _open_quick_settings(self) -> None:
        """Open the simplified two-tier settings modal.

        v2.3.5-fix: the modal's "Advanced…" button dismisses the quick
        settings and hands off to the full model palette — pass the
        callback here (previously the modal dismissed to nothing).
        """

        def _open_full_settings() -> None:
            self._open_model_palette()

        self.push_screen(QuickSettingsModal(self.bridge, on_advanced=_open_full_settings))

    def _open_model_palette(self) -> None:
        providers = self.bridge.list_providers()
        if not providers:
            self.query_one(ChatLog).add_error("No providers available")
            return

        def on_select(provider_id: str) -> None:
            result = self.bridge.set_provider(provider_id)
            chat = self.query_one(ChatLog)
            if result.get("ok"):
                chat.add_system(
                    f"Provider switched to: [b]{result.get('provider', provider_id)}[/b] "
                    f"model: [dim]{result.get('model', '?')}[/dim]"
                )
                self._refresh_status("idle")
            else:
                chat.add_error(f"Failed to switch provider: {result.get('error', 'unknown')}")

        modal = ModelSelectorModal(providers, on_select)
        self.app.push_screen(modal)

    def _open_chat_palette(self) -> None:
        chats = self.bridge.list_chats()
        options = []
        for c in chats:
            status_icon = {"done": "done", "error": "err", "running": "run", "idle": "-"}.get(
                c.get("status", "idle"), "-"
            )
            options.append({
                "id": c.get("id", ""),
                "label": f"[{status_icon}] {c.get('title', 'Untitled')}",
                "desc": f"{c.get('message_count', 0)} msgs",
            })
        if not options:
            self.query_one(ChatLog).add_system("No saved chats found.")
            self.query_one(InputBox).focus()
            return
        self._open_sub_palette("chat", options)

    def _open_cd_palette(self) -> None:
        import json
        config_path = os.path.expanduser("~/.tera_pilot/config.json")
        recent_dirs = []
        try:
            with open(config_path, "r") as f:
                cfg = json.load(f)
            root = cfg.get("project_root")
            if root:
                recent_dirs.append(root)
        except Exception:
            pass
        recent_dirs.append(self.bridge.workspace)
        home = os.path.expanduser("~")
        recent_dirs.append(home)
        seen = set()
        unique_dirs = []
        for d in recent_dirs:
            if d not in seen:
                seen.add(d)
                unique_dirs.append(d)
        options = []
        for d in unique_dirs:
            basename = os.path.basename(d) or d
            options.append({"id": d, "label": basename, "desc": d})
        self._open_sub_palette("cd", options)

    # ── Command execution ─────────────────────────────────────────

    def _execute_builtin_cmd(self, cmd_id: str) -> None:
        dispatch = {
            "usage": self._exec_usage,
            "files": self._exec_files,
            "clear": self._exec_clear,
            "help": self._exec_help,
            "planning": self._exec_planning,
            "gui": lambda: self.action_launch_gui(),
        }
        handler = dispatch.get(cmd_id)
        if handler:
            handler()
        else:
            self._open_sub_palette_for_cmd(cmd_id)
        box = self.query_one(InputBox)
        if box.value.startswith("/"):
            box.value = ""
        box.focus()

    def _exec_section(self, section_id: str) -> None:
        result = self.bridge.set_section(section_id)
        chat = self.query_one(ChatLog)
        if result.get("ok"):
            name = {"general": "General", "heavy_code": "Heavy Code", "office": "Office Worker"}.get(
                section_id, section_id)
            chat.add_system(f"Section switched to: [b]{name}[/b]")
            self._refresh_status("idle")
        else:
            chat.add_error(f"Failed to switch section: {result.get('error', 'unknown')}")
        self.query_one(InputBox).focus()

    def _exec_mode(self, arg: str) -> None:
        """v2.1.0 (Loop 1): /mode slash command — show or switch section.

        /mode              — show current section
        /mode general      — switch to general
        /mode heavy_code   — switch to heavy_code
        /mode office       — switch to office
        """
        arg = (arg or "").strip()
        if not arg:
            # Show current section
            chat = self.query_one(ChatLog)
            name = {"general": "General", "heavy_code": "Heavy Code", "office": "Office Worker"}.get(
                self.bridge.section, self.bridge.section)
            chat.add_system(f"Current section: [b]{name}[/b]")
            self.query_one(InputBox).focus()
            return
        # Reuse the section parser for consistency
        from tera_pilot.agent_runtime.section_parser import parse_section_switch
        # Use /mode prefix so the parser can handle it
        new_section, _ = parse_section_switch(f"/mode {arg}")
        if new_section is not None:
            self._exec_section(new_section.value)
        else:
            self.query_one(ChatLog).add_error(
                f"Unknown section: {arg}. Valid: general, heavy_code, office"
            )
            self.query_one(InputBox).focus()

    def _exec_theme(self, arg: str) -> None:
        """v2.1.0 (Loop 3): /theme slash command — switch or show theme.

        /theme              — show current theme
        /theme dark         — switch to dark theme
        /theme light        — switch to light theme
        """
        arg = (arg or "").strip().lower()
        if not arg:
            # Show current theme
            theme = "dark" if self._dark_theme else "light"
            self.query_one(ChatLog).add_system(f"Current theme: [b]{theme}[/b]")
            self.query_one(InputBox).focus()
            return
        if arg == "dark":
            self._dark_theme = True
            self.CSS_PATH = "styles_dark.tcss"
            try:
                self.reload_css()
            except Exception:
                pass
            self.query_one(ChatLog).add_system("Theme switched to: [b]dark[/b]")
        elif arg == "light":
            self._dark_theme = False
            self.CSS_PATH = "styles_light.tcss"
            try:
                self.reload_css()
            except Exception:
                pass
            self.query_one(ChatLog).add_system("Theme switched to: [b]light[/b]")
        else:
            self.query_one(ChatLog).add_error(
                f"Unknown theme: {arg}. Valid: dark, light"
            )
        self.query_one(InputBox).focus()

    def _exec_model(self, arg: str = "") -> None:
        """Switch provider and/or set a custom model.

        /model                       — interactive picker
        /model <provider_id>         — switch provider (keeps its model)
        /model <provider_id> <model> — switch provider AND set the model
        /model <model>               — set the model on the currently
                                       active provider (e.g. /model ox-alpha)
        """
        chat = self.query_one(ChatLog)

        # No arg — show the interactive provider picker.
        if not arg or arg.strip() == "":
            self._open_model_palette()
            return

        parts = arg.split(None, 1)
        first = parts[0]
        rest = parts[1] if len(parts) > 1 else None
        provider_ids = {p.get("id") for p in self.bridge.list_providers()}

        if first in provider_ids:
            # Provider-id form: switch, optionally with a model.
            r = self.bridge.set_provider(first)
            if not r.get("ok"):
                chat.add_error(f"Failed to switch provider: {r.get('error', 'unknown')}")
                self.query_one(InputBox).focus()
                return
            model = r.get("model", "?")
            if rest:
                # v2.3.5-fix: apply the model through configure_provider
                # which preserves api_base/api_key (set_provider(model=...)
                # builds a bare ProviderConfig and would wipe them).
                r2 = self.bridge.configure_provider(first, model=rest)
                if not r2.get("ok"):
                    chat.add_error(
                        f"Provider switched, but model failed: {r2.get('error', 'unknown')}"
                    )
                    self.query_one(InputBox).focus()
                    return
                model = r2.get("model", rest)
            chat.add_system(
                f"Provider switched to: [b]{r.get('provider', first)}[/b] "
                f"model: [dim]{model}[/dim]"
            )
            self._refresh_status("idle")
        else:
            # Model-name form: set the model on the currently active provider.
            active_pid = self.bridge.get_active_provider_id()
            if not active_pid:
                chat.add_error(
                    "No active provider. Pick one first: /model <provider_id> "
                    f"(e.g. {', '.join(sorted(provider_ids)) or 'none configured'})"
                )
                self.query_one(InputBox).focus()
                return
            r = self.bridge.configure_provider(active_pid, model=arg)
            if r.get("ok"):
                chat.add_system(
                    f"Model set on [b]{active_pid}[/b]: [dim]{r.get('model', arg)}[/dim]"
                )
                self._refresh_status("idle")
            else:
                chat.add_error(f"Failed to set model: {r.get('error', 'unknown')}")
        self.query_one(InputBox).focus()

    def _exec_chat(self, chat_id: str) -> None:
        chats = self.bridge.list_chats()
        target = None
        for c in chats:
            if c.get("id") == chat_id:
                target = c
                break
        if target:
            self.query_one(ChatLog).add_system(
                f"Chat: [b]{target.get('title', 'Untitled')}[/b] "
                f"({target.get('message_count', 0)} messages, "
                f"status: {target.get('status', 'idle')})\n"
                f"Full chat restore not yet supported in TUI."
            )
        else:
            self.query_one(ChatLog).add_error(f"Chat not found: {chat_id}")
        self.query_one(InputBox).focus()

    def _exec_cd(self, path: str) -> None:
        result = self.bridge.change_workspace(path)
        chat = self.query_one(ChatLog)
        if result.get("ok"):
            chat.add_system(f"Workspace changed to: [b]{result.get('workspace', path)}[/b]")
            self._refresh_status("idle")
        else:
            chat.add_error(f"Failed to change workspace: {result.get('error', 'unknown')}")
        self.query_one(InputBox).focus()

    def _exec_usage(self) -> None:
        s = self.bridge.get_usage()
        chat = self.query_one(ChatLog)
        chat.add_system(
            f"[b]Session Usage[/b]\n"
            f"  Provider: {s.get('provider', '?')}\n"
            f"  Model:    {s.get('model', '?')}\n"
            f"  Tokens:   {s.get('tokens', 0):,}\n"
            f"  Cost:     ${s.get('cost', 0.0):.4f}\n"
            f"  Section:  {self.bridge.section}"
        )
        self.query_one(InputBox).focus()

    def _exec_files(self) -> None:
        names = self.bridge.list_workspace_files()
        chat = self.query_one(ChatLog)
        if names:
            dirs = [n for n in names if n.endswith("/")]
            files = [n for n in names if not n.endswith("/")]
            listing = ""
            if dirs:
                listing += "[b]Directories[/b]\n  " + "  ".join(d.rstrip("/") for d in dirs[:20]) + "\n"
            if files:
                listing += "[b]Files[/b]\n  " + "  ".join(files[:30])
                if len(files) > 30:
                    listing += f"\n  ... and {len(files) - 30} more"
            chat.add_system(f"[b]Workspace: {self.bridge.workspace}[/b]\n{listing}")
        else:
            chat.add_system(f"Workspace: {self.bridge.workspace} - (empty or unreadable)")
        self.query_one(InputBox).focus()

    def _exec_clear(self) -> None:
        chat = self.query_one(ChatLog)
        chat.clear()
        chat.add_system("Chat log cleared.")
        self.query_one(InputBox).focus()

    def _exec_help(self) -> None:
        chat = self.query_one(ChatLog)
        lines = [
            "[b]Slash Commands[/b]",
            "",
            "  [cyan]/section[/cyan]   Switch section (General / Heavy Code / Office)",
            "  [cyan]/model[/cyan]     Switch provider or set model (e.g. /model ox-alpha)",
            "  [cyan]/provider[/cyan]  Switch provider (alias for /model)",
            "  [cyan]/chat[/cyan]      List and browse saved chats",
            "  [cyan]/cd[/cyan]        Change workspace directory",
            "  [cyan]/usage[/cyan]     Show session token usage & cost",
            "  [cyan]/files[/cyan]     List files in workspace",
            "  [cyan]/clear[/cyan]     Clear the chat log",
            "  [cyan]/planning[/cyan]  Toggle planning mode",
            "  [cyan]/guardian[/cyan]  Set Guardian safety level (off/dangerous_only/all)",
            "  [cyan]/collab[/cyan]    Run a collaboration mode (reviewer/codegen/pair/observer)",
            "  [cyan]/queue[/cyan]     Show request queue stats (cooldown, retries)",
            "  [cyan]/storage[/cyan]   Choose chat storage backend (JSON/SQLite)",
            "  [cyan]/sessions[/cyan]  List SQLite-stored chat sessions",
            "  [cyan]/context[/cyan]   View context fragments & compaction stats",
            "  [cyan]/tools[/cyan]     Browse loaded & available progressive tools",
            "  [cyan]/capabilities[/cyan]  Browse & run pre-built capability templates",
            "  [cyan]/second_opinion[/cyan] Toggle cross-model review before risky actions (Pro)",
            "  [cyan]/verify[/cyan]    Cross-model verification of the last response",
            "  [cyan]/budget[/cyan]    Configure token budget & efficiency policy",
            "  [cyan]/agents[/cyan]    List agents + their audit stats (G5)",
            "  [cyan]/audit[/cyan]     Export audit trail JSON / CSV (G5)",
            "  [cyan]/handoff[/cyan]   Create / edit / list handoff docs (G6)",
            "  [cyan]/cost[/cyan]      Cost-aware provider routing (M2)",
            "  [cyan]/spend[/cyan]     Team spend dashboard (M3)",
            "  [cyan]/consensus[/cyan] Run a prompt on 2–3 providers in parallel (G15)",
            "  [cyan]/audit-signed[/cyan] Verify a signed/chained audit export (G16)",
            "  [cyan]/learnings[/cyan] List / scan / dismiss auto-learning entries (G17)",
            "  [cyan]/websearch[/cyan] Web search backend status (G18)",
            "  [cyan]/gui[/cyan]       Launch the Tera Pilot GUI window",
            "  [cyan]/help[/cyan]      Show this help",
            "",
            "Type / to see inline suggestions, Ctrl+P for full command palette.",
            "Ctrl+C=interrupt | Ctrl+D=quit | Ctrl+G=GUI | Ctrl+P=commands | Ctrl+T=theme",
            "",
            "[dim]Custom .md commands from .claude/commands/ also appear.[/dim]",
        ]
        custom = self.bridge.list_slash_commands()
        if custom:
            lines.append("")
            lines.append("[b]Custom Commands[/b]")
            for c in custom:
                lines.append(f"  [cyan]/{c['id']}[/cyan]  {c.get('description', c.get('name', ''))}")
        chat.add_system("\n".join(lines))
        self.query_one(InputBox).focus()

    def _exec_planning(self) -> None:
        result = self.bridge.toggle_planning()
        chat = self.query_one(ChatLog)
        state = "ON" if result.get("planning") else "OFF"
        chat.add_system(f"Planning mode: [b]{state}[/b]")
        self._refresh_status("idle")
        self.query_one(InputBox).focus()

    def _exec_guardian(self, arg: str) -> None:
        arg = arg.strip().lower()
        valid = {"off", "dangerous_only", "all"}
        if arg not in valid:
            chat = self.query_one(ChatLog)
            chat.add_system(
                f"Usage: /guardian <level>\n"
                f"  Levels: off | dangerous_only | all\n"
                f"  Current: {self.bridge.get_guardian_level().get('level', 'off')}"
            )
            self.query_one(InputBox).focus()
            return
        result = self.bridge.set_guardian_level(arg)
        chat = self.query_one(ChatLog)
        if result.get("ok"):
            chat.add_system(f"Guardian level set to: [b]{result['level']}[/b]")
        else:
            chat.add_error(f"Failed to set Guardian level: {result.get('error', 'unknown')}")
        self.query_one(InputBox).focus()

    def _open_guardian_palette(self) -> None:
        options = [
            {"id": "off", "label": "Off",
             "desc": "Guardian disabled — fastest, no LLM safety review"},
            {"id": "dangerous_only", "label": "Dangerous tools only",
             "desc": "Review only high-risk tool calls (recommended)"},
            {"id": "all", "label": "All tools",
             "desc": "Review every medium+ risk tool call"},
        ]
        self._open_sub_palette("guardian", options)

    def _open_collab_palette(self) -> None:
        modes = self.bridge.list_collaboration_modes()
        options = [{"id": m["id"], "label": m["label"], "desc": m["desc"]}
                   for m in modes]
        self._open_sub_palette("collab", options)

    def _open_storage_palette(self) -> None:
        current = self.bridge.get_persistence_backend()
        options = [
            {"id": "json", "label": "JSON files",
             "desc": "~/.tera_pilot/chats/*.json  (default)" + ("  [active]" if current == "json" else "")},
            {"id": "sqlite", "label": "SQLite database",
             "desc": "~/.tera_pilot/chats.sqlite3  (single-file, O(log N) append)" + ("  [active]" if current == "sqlite" else "")},
        ]
        self._open_sub_palette("storage", options)

    def _exec_collab(self, arg: str) -> None:
        """Run a collaboration-mode task. arg = '<mode> <task text>' or '<mode>'."""
        arg = arg.strip()
        if not arg:
            self._open_collab_palette()
            return
        parts = arg.split(None, 1)
        mode = parts[0].lower()
        task = parts[1].strip() if len(parts) > 1 else ""
        valid_modes = {"single", "reviewer", "codegen", "pair", "observer"}
        if mode not in valid_modes:
            self.query_one(ChatLog).add_system(
                f"Unknown collaboration mode: {mode}\n"
                f"Valid: {', '.join(sorted(valid_modes))}"
            )
            self.query_one(InputBox).focus()
            return
        if mode == "single":
            self.query_one(ChatLog).add_system(
                "Single mode = no collaboration. Just type your task as a normal prompt."
            )
            self.query_one(InputBox).focus()
            return
        if not task:
            self.query_one(ChatLog).add_system(
                f"Usage: /collab {mode} <task description>\n"
                f"Example: /collab {mode} Refactor the auth module to use async/await"
            )
            self.query_one(InputBox).focus()
            return
        # Render the task as a user message, then run collaboration in a worker
        chat = self.query_one(ChatLog)
        chat.add_user(f"[collab:{mode}] {task}")
        self._turn_running = True
        self._refresh_status("thinking")
        self._run_collaboration(mode, task)

    @work(thread=True, exclusive=True)
    def _run_collaboration(self, mode: str, task: str) -> None:
        try:
            result = self.bridge.run_collaboration(mode, task)
            self.call_from_thread(self._on_collab_done, result)
        except Exception as e:
            self.call_from_thread(self._on_turn_error, str(e))

    def _on_collab_done(self, result: Dict[str, Any]) -> None:
        chat = self.query_one(ChatLog)
        if not result.get("ok"):
            chat.add_error(f"Collaboration failed: {result.get('error', 'unknown')}")
            self._turn_running = False
            self._refresh_status("idle")
            return
        output = result.get("output", "") or ""
        if output:
            chat.add_final(output)
        metadata = result.get("metadata", {}) or {}
        verdict = metadata.get("verdict")
        iterations = result.get("iterations", 0)
        if verdict:
            feedback = metadata.get("feedback", "") or metadata.get("reason", "")
            chat.add_reviewer_verdict(verdict, feedback, iterations)
        observer_warnings = metadata.get("observer_warnings") or []
        if observer_warnings:
            chat.add_observer_warnings(observer_warnings)
        self._turn_running = False
        self._refresh_status("idle")
        self.query_one(InputBox).focus()

    def _exec_queue(self) -> None:
        stats = self.bridge.get_queue_stats()
        chat = self.query_one(ChatLog)
        if not stats:
            chat.add_system(
                "[b]Request Queues[/b]\n"
                "  No provider queues registered yet.\n"
                "  Queues are created on first provider call."
            )
        else:
            lines = ["[b]Request Queues[/b]", ""]
            for pid, s in stats.items():
                # v2.3.4-fix: use the ACTUAL keys RequestQueue.stats()
                # returns (cooldown_remaining_secs, max_concurrency,
                # pending, retried, errors) — the old code read
                # in_flight/max_in_flight/total_retries/cooldown_until,
                # which don't exist, so /queue always showed zeros.
                cooldown = s.get("cooldown_remaining_secs", 0) or 0
                cooldown_str = f"{int(cooldown)}s" if cooldown > 0 else "-"
                max_conc = s.get("max_concurrency", 1)
                lines.append(
                    f"  [cyan]{pid}[/cyan]:  "
                    f"max_concurrency {max_conc}  "
                    f"pending {s.get('pending', 0)}  "
                    f"completed {s.get('completed', 0)}  "
                    f"retries {s.get('retried', 0)}  "
                    f"errors {s.get('errors', 0)}  "
                    f"cooldown: {cooldown_str}"
                )
            chat.add_system("\n".join(lines))
        self.query_one(InputBox).focus()

    def _exec_storage(self, arg: str) -> None:
        arg = arg.strip().lower()
        if not arg:
            self._open_storage_palette()
            return
        result = self.bridge.set_persistence_backend(arg)
        chat = self.query_one(ChatLog)
        if result.get("ok"):
            chat.add_system(f"Storage backend set to: [b]{result['backend']}[/b]")
        else:
            chat.add_error(f"Failed: {result.get('error', 'unknown')}")
        self.query_one(InputBox).focus()

    def _exec_sessions(self) -> None:
        sessions = self.bridge.list_sqlite_sessions()
        chat = self.query_one(ChatLog)
        if not sessions:
            chat.add_system(
                "[b]SQLite Sessions[/b]\n"
                "  No sessions found. Switch to SQLite storage via /storage "
                "and run some chats to populate the database."
            )
        else:
            lines = ["[b]SQLite Sessions[/b] (~/.tera_pilot/chats.sqlite3)", ""]
            for s in sessions[:50]:
                lines.append(
                    f"  [{s.get('id', '?')[:8]}]  "
                    f"{s.get('title', 'Untitled')}  "
                    f"({s.get('message_count', 0)} msgs)"
                )
            if len(sessions) > 50:
                lines.append(f"  ... and {len(sessions) - 50} more")
            chat.add_system("\n".join(lines))
        self.query_one(InputBox).focus()

    def _exec_context(self) -> None:
        stats = self.bridge.get_compaction_stats()
        chat = self.query_one(ChatLog)
        if not stats:
            chat.add_system(
                "[b]Context Fragments[/b]\n"
                "  No compaction has run yet this session."
            )
        else:
            chat.add_system(
                "[b]Context Fragments (last compaction)[/b]\n"
                f"  Original fragments:  {stats.get('original_fragments', '?')}\n"
                f"  Kept:                {stats.get('kept_fragments', '?')}\n"
                f"  Dropped (tombstoned): {stats.get('dropped_fragments', '?')}\n"
                f"  Chars saved:         {stats.get('chars_saved', 0):,}"
            )
        self.query_one(InputBox).focus()

    def _exec_tools(self) -> None:
        state = self.bridge.get_tool_catalog_state()
        chat = self.query_one(ChatLog)
        loaded = state.get("loaded", [])
        available = state.get("available", [])
        saved = state.get("prompt_chars_saved", 0)
        chat.add_system(
            f"[b]Progressive Tools Catalog[/b]\n"
            f"  Loaded:   {len(loaded)}  (full schemas shipped to the model)\n"
            f"  Available: {len(available)}  (callable via select_tools)\n"
            f"  Prompt chars saved: {saved:,}\n"
            f"\n"
            f"  Loaded: {', '.join(loaded[:20])}{'...' if len(loaded) > 20 else ''}\n"
            f"  Sample available: {', '.join(available[:20])}{'...' if len(available) > 20 else ''}"
        )
        self.query_one(InputBox).focus()

    # ── v2.0.1 (G7) — Capability catalog ──────────────────────────

    def _exec_capabilities(self, arg: str) -> None:
        """Browse the capability catalog and optionally run one.

        Usage:
            /capabilities                — open browse palette
            /capabilities <id>           — show capability detail
            /capabilities <id> k=v ...   — fill placeholders and run
        """
        arg = arg.strip()
        if not arg:
            self._open_capability_palette()
            return

        parts = arg.split(None, 1)
        cap_id = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""

        cap = self.bridge.get_capability(cap_id)
        if cap is None:
            self.query_one(ChatLog).add_system(
                f"Unknown capability: {cap_id}\n"
                f"Type /capabilities (no arg) to browse the catalog."
            )
            self.query_one(InputBox).focus()
            return

        # If the capability has no required placeholders, run it now.
        placeholders = cap.get("placeholders", []) or []
        required = [p["name"] for p in placeholders if p.get("required", True)]

        if not required and not rest:
            self._run_capability(cap_id, {})
            return

        # If the user passed inline values, parse "k=v k2=v2 ..."
        values: Dict[str, str] = {}
        if rest:
            for token in self._split_kv_tokens(rest):
                if "=" in token:
                    k, _, v = token.partition("=")
                    values[k.strip()] = v.strip()

        # Validate
        missing = [r for r in required if not values.get(r)]
        if missing:
            self._show_capability_detail(cap, missing, values)
            return

        self._run_capability(cap_id, values)

    def _split_kv_tokens(self, s: str) -> List[str]:
        """Split a string on whitespace, honouring quoted substrings."""
        import shlex
        try:
            return shlex.split(s)
        except ValueError:
            return s.split()

    def _open_capability_palette(self) -> None:
        """Open a palette to browse capabilities, grouped by category."""
        caps = self.bridge.list_capabilities()
        if not caps:
            self.query_one(ChatLog).add_system(
                "[b]Capability Catalog[/b]\n  No capabilities available."
            )
            self.query_one(InputBox).focus()
            return
        # Group by category for the palette
        options: List[Dict[str, Any]] = []
        for c in caps:
            builtin_tag = " [dim](builtin)[/dim]" if c.get("builtin") else ""
            options.append({
                "id": c["id"],
                "label": f"[{c.get('category', '?')}] {c.get('name', c['id'])}{builtin_tag}",
                "desc": c.get("description", "")[:120],
            })
        self._open_sub_palette("capability", options)

    def _show_capability_detail(
        self,
        cap: Dict[str, Any],
        missing: List[str],
        values: Dict[str, str],
    ) -> None:
        """Show a capability's body + placeholders and prompt for the missing ones."""
        chat = self.query_one(ChatLog)
        lines = [
            f"[b]Capability: {cap.get('name', cap.get('id'))}[/b]",
            f"  Category: {cap.get('category', '?')}",
            f"  {cap.get('description', '')}",
            "",
            "[b]Placeholders[/b]",
        ]
        for p in cap.get("placeholders", []):
            req = "required" if p.get("required", True) else "optional"
            default = p.get("default", "")
            default_str = f", default: {default}" if default else ""
            cur = values.get(p["name"], "")
            cur_str = f"  [green]= {cur}[/green]" if cur else f"  [red]missing ({req}{default_str})[/red]"
            lines.append(f"  ${p['name']}$ — {p.get('description', '')}{cur_str}")
        if missing:
            lines.append("")
            lines.append(
                f"[yellow]Fill the missing placeholders and re-run:[/yellow]\n"
                f"  /capabilities {cap['id']} " +
                " ".join(f"{m}=..." for m in missing)
            )
        chat.add_system("\n".join(lines))
        self.query_one(InputBox).focus()

    def _run_capability(self, cap_id: str, values: Dict[str, str]) -> None:
        """Fill the template and run the resulting prompt as a normal turn."""
        result = self.bridge.fill_capability_template(cap_id, values)
        chat = self.query_one(ChatLog)
        if not result.get("ok"):
            chat.add_error(
                f"Failed to fill capability template: {result.get('error', 'unknown')}"
            )
            self.query_one(InputBox).focus()
            return
        prompt = result.get("prompt", "")
        cap_meta = result.get("capability", {})
        chat.add_user(
            f"[capability:{cap_id}] {cap_meta.get('name', cap_id)}\n"
            f"[dim](filled template — placeholders: {dict(values) or 'none'})[/dim]"
        )
        self._last_prompt = prompt
        self._turn_running = True
        self._refresh_status("thinking")
        self._run_turn(prompt)

    # ── v2.0.1 (M1) — Second Opinion ──────────────────────────────

    def _exec_second_opinion(self, arg: str) -> None:
        """Configure or inspect the Second Opinion feature.

        Usage:
            /second_opinion                       — show current state
            /second_opinion on|off                — enable / disable
            /second_opinion pro on|off            — toggle Tera Pilot Pro flag
            /second_opinion provider <pid> [model]— pick the second model
            /second_opinion risk low|medium|high  — min risk to trigger
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()
        if not arg:
            cfg = self.bridge.get_second_opinion_config()
            pro = "ON" if cfg.get("pro_enabled") else "OFF"
            so = "ON" if cfg.get("enabled") else "OFF"
            chat.add_system(
                f"[b]Second Opinion[/b] (Tera Pilot Pro feature)\n"
                f"  Tera Pilot Pro:        [b]{pro}[/b]\n"
                f"  Second Opinion:  [b]{so}[/b]\n"
                f"  Second provider: {cfg.get('provider_id', 'auto')}\n"
                f"  Second model:    {cfg.get('model', 'auto')}\n"
                f"  Min risk level:  {cfg.get('min_risk_level', 'medium')}\n\n"
                f"  Usage:\n"
                f"    /second_opinion on|off\n"
                f"    /second_opinion pro on|off\n"
                f"    /second_opinion provider <pid> [model]\n"
                f"    /second_opinion risk low|medium|high"
            )
            self.query_one(InputBox).focus()
            return

        parts = arg.split()
        sub = parts[0].lower()
        if sub in ("on", "off", "enable", "disable"):
            enabled = sub in ("on", "enable")
            r = self.bridge.set_second_opinion_config(enabled=enabled)
            if r.get("ok"):
                state = "ON" if r.get("enabled") else "OFF"
                chat.add_system(f"Second Opinion: [b]{state}[/b]")
                if not r.get("pro_enabled"):
                    chat.add_system(
                        "[yellow]Note:[/yellow] Tera Pilot Pro is OFF. "
                        "Enable with /second_opinion pro on"
                    )
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
        elif sub == "pro":
            if len(parts) < 2:
                chat.add_system("Usage: /second_opinion pro on|off")
            else:
                # v2.3.4: Pro is license-based — the toggle no longer grants
                # access; show the REAL status and a hint when it's off.
                v = parts[1].lower() in ("on", "1", "true", "yes")
                r = self.bridge.set_pro_enabled(v)
                if r.get("ok"):
                    state = "ON" if r.get("pro") else "OFF"
                    chat.add_system(f"Tera Pilot Pro: [b]{state}[/b]")
                    if not r.get("pro") and r.get("note"):
                        chat.add_system(f"[yellow]Note:[/yellow] {r['note']}")
                else:
                    chat.add_error(f"Failed: {r.get('error', 'unknown')}")
        elif sub == "provider":
            if len(parts) < 2:
                # Show available providers
                providers = self.bridge.list_second_opinion_providers()
                chat.add_system(
                    "[b]Available second-opinion providers[/b]\n" +
                    "\n".join(
                        f"  {p.get('id')} — {p.get('label', '')} "
                        f"(model: {p.get('model', p.get('default_model', '?'))})"
                        for p in providers[:20]
                    )
                )
            else:
                pid = parts[1]
                model = parts[2] if len(parts) > 2 else "auto"
                r = self.bridge.set_second_opinion_config(provider_id=pid, model=model)
                if r.get("ok"):
                    chat.add_system(
                        f"Second Opinion provider: [b]{r.get('provider_id')}[/b] "
                        f"model: [dim]{r.get('model')}[/dim]"
                    )
                else:
                    chat.add_error(f"Failed: {r.get('error', 'unknown')}")
        elif sub in ("risk", "min_risk", "threshold"):
            if len(parts) < 2:
                chat.add_system("Usage: /second_opinion risk low|medium|high")
            else:
                lvl = parts[1].lower()
                if lvl not in ("low", "medium", "high"):
                    chat.add_error(f"Invalid risk level: {lvl}")
                else:
                    r = self.bridge.set_second_opinion_config(min_risk_level=lvl)
                    if r.get("ok"):
                        chat.add_system(f"Second Opinion min risk: [b]{r.get('min_risk_level')}[/b]")
                    else:
                        chat.add_error(f"Failed: {r.get('error', 'unknown')}")
        else:
            chat.add_system(
                f"Unknown subcommand: {sub}\n"
                f"Usage: /second_opinion [on|off|pro|provider|risk] ..."
            )
        self.query_one(InputBox).focus()

    # ── v2.0.1 (G3) — Token budget ────────────────────────────────

    def _exec_budget(self, arg: str) -> None:
        """Configure or inspect the token budget / efficiency policy.

        Usage:
            /budget                — show current budget
            /budget daily <usd>    — set daily USD cap
            /budget monthly <usd>  — set monthly USD cap
            /budget per_turn <tok> — max tokens per agentic turn
            /budget compaction <pct> — auto-compact threshold (50-95)
            /budget reset          — reset to defaults
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()
        if not arg:
            b = self.bridge.get_token_budget()
            if not b.get("ok"):
                chat.add_error(f"Failed: {b.get('error', 'unknown')}")
                self.query_one(InputBox).focus()
                return
            chat.add_system(
                f"[b]Token Budget & Efficiency[/b]\n"
                f"  Daily cap:        ${b.get('daily_usd', 0):.2f}\n"
                f"  Monthly cap:      ${b.get('monthly_usd', 0):.2f}\n"
                f"  Max tokens/turn:  {b.get('max_tokens_per_turn', 0):,}\n"
                f"  Max iterations:   {b.get('max_iterations', 0)}\n"
                f"  Auto-compact at:  {b.get('compaction_threshold_pct', 85)}%\n"
                f"  Prompt caching:   {'ON' if b.get('prompt_caching') else 'OFF'}\n"
                f"  Predictable mode: {'ON' if b.get('predictable_mode') else 'OFF'}\n\n"
                f"  Usage this month: ${b.get('month_cost', 0):.4f} / "
                f"${b.get('monthly_usd', 0):.2f} "
                f"({b.get('month_used_pct', 0)}%)\n"
                f"  Today: ${b.get('day_cost', 0):.4f}\n\n"
                f"  Commands:\n"
                f"    /budget daily|monthly <usd>\n"
                f"    /budget per_turn <tokens>\n"
                f"    /budget iterations <n>\n"
                f"    /budget compaction <50-95>\n"
                f"    /budget caching on|off\n"
                f"    /budget predictable on|off\n"
                f"    /budget reset"
            )
            self.query_one(InputBox).focus()
            return

        parts = arg.split()
        sub = parts[0].lower()
        try:
            if sub == "daily":
                self.bridge.set_token_budget(daily_usd=float(parts[1]))
            elif sub == "monthly":
                self.bridge.set_token_budget(monthly_usd=float(parts[1]))
            elif sub in ("per_turn", "per-turn"):
                self.bridge.set_token_budget(max_tokens_per_turn=int(parts[1]))
            elif sub == "iterations":
                self.bridge.set_token_budget(max_iterations=int(parts[1]))
            elif sub == "compaction":
                pct = max(50, min(95, int(parts[1])))
                self.bridge.set_token_budget(compaction_threshold_pct=pct)
            elif sub == "caching":
                self.bridge.set_token_budget(
                    prompt_caching=(parts[1].lower() in ("on", "1", "true", "yes"))
                )
            elif sub == "predictable":
                self.bridge.set_token_budget(
                    predictable_mode=(parts[1].lower() in ("on", "1", "true", "yes"))
                )
            elif sub == "reset":
                self.bridge.reset_token_budget()
            else:
                chat.add_system(f"Unknown subcommand: {sub}")
                self.query_one(InputBox).focus()
                return
            chat.add_system(f"Budget updated: [b]{sub}[/b]")
        except (IndexError, ValueError) as e:
            chat.add_error(f"Bad argument: {e}")
        except Exception as e:
            chat.add_error(f"Failed: {e}")
        self.query_one(InputBox).focus()

    # ── v2.0.1 (G4) — Cross-model verification ────────────────────

    def _exec_verify(self, arg: str) -> None:
        """Verify the last assistant response with a different model.

        Usage:
            /verify                          — auto-pick a cross-family verifier
            /verify <provider_id>            — use a specific provider
            /verify <provider_id> <model>    — use a specific provider + model
        """
        if self._turn_running:
            self.query_one(ChatLog).add_system(
                "Wait for the current turn to finish before running /verify."
            )
            self.query_one(InputBox).focus()
            return

        parts = arg.strip().split()
        v_pid = parts[0] if parts else None
        v_model = parts[1] if len(parts) > 1 else None

        chat = self.query_one(ChatLog)
        chat.add_system(
            "[dim]Running cross-model verification...[/dim]"
        )
        self._refresh_status("thinking")
        self._run_verification(v_pid, v_model)

    @work(thread=True, exclusive=True)
    def _run_verification(
        self,
        v_pid: Optional[str],
        v_model: Optional[str],
    ) -> None:
        try:
            result = self.bridge.verify_last_response(
                verifier_provider_id=v_pid,
                verifier_model=v_model,
            )
            self.call_from_thread(self._on_verify_done, result)
        except Exception as e:
            self.call_from_thread(self._on_turn_error, str(e))

    def _on_verify_done(self, result: Dict[str, Any]) -> None:
        chat = self.query_one(ChatLog)
        self._refresh_status("idle")
        if not result.get("ok"):
            chat.add_error(
                f"Verification failed: {result.get('error', 'unknown')}"
            )
            self.query_one(InputBox).focus()
            return
        # Show the verification modal
        self.push_screen(VerificationModal(result), lambda _: self._after_verify_modal())

    def _after_verify_modal(self) -> None:
        """Called after the verification modal is dismissed."""
        self.query_one(InputBox).focus()

    # ── v2.0.2 (G5) — Agent identity + audit ──────────────────────

    def _exec_agents(self, arg: str) -> None:
        """List every agent that has acted in this process, with audit stats.

        Usage:
            /agents            — list all agents
            /agents <agent_id> — show entries produced by one agent
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()
        if arg:
            # Show entries for one agent
            r = self.bridge.filter_audit_by_agent(arg, include_children=True)
            if not r.get("ok"):
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
                self.query_one(InputBox).focus()
                return
            entries = r.get("entries") or []
            if not entries:
                chat.add_system(f"No audit entries found for agent [b]{arg}[/b].")
            else:
                lines = [f"[b]Audit entries for agent {arg}[/b]  ({r.get('count', 0)} entries)", ""]
                for e in entries[-25:]:
                    ts = e.get("ts_iso", "")
                    tool = e.get("tool") or e.get("kind") or "?"
                    status = e.get("status", "?")
                    title = (e.get("title") or "")[:80]
                    lines.append(f"  [{ts}]  {tool}  [{status}]  {title}")
                if r.get("count", 0) > 25:
                    lines.append(f"  ... and {r.get('count', 0) - 25} more (showing last 25)")
                chat.add_system("\n".join(lines))
            self.query_one(InputBox).focus()
            return

        agents = self.bridge.list_agents()
        ident = self.bridge.get_agent_identity()
        if not agents:
            chat.add_system(
                "[b]Agents[/b]  (no activity yet)\n"
                f"  Root agent id: [dim]{ident.get('id', '?')}[/dim]  role: {ident.get('role', '?')}"
            )
            self.query_one(InputBox).focus()
            return
        lines = [
            f"[b]Agents in this process[/b]  (root: {ident.get('id', '?')})",
            "",
            f"  {'ID':<18}  {'Role':<12}  {'Calls':>6}  {'Errs':>5}  {'Reject':>6}  {'Dur(ms)':>8}  Name",
            f"  {'-'*18}  {'-'*12}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*8}  {'-'*20}",
        ]
        for a in agents:
            lines.append(
                f"  {a['id']:<18}  {a['role']:<12}  {a['tool_calls']:>6}  "
                f"{a['errors']:>5}  {a['rejections']:>6}  "
                f"{a['total_duration_ms']:>8}  {a.get('name', '')}"
            )
        chat.add_system("\n".join(lines))
        self.query_one(InputBox).focus()

    def _exec_audit(self, arg: str) -> None:
        """Export the audit trail.

        Usage:
            /audit             — show summary + first 20 entries
            /audit json        — print the full JSON export
            /audit csv         — print the CSV export
            /audit verify      — re-verify SHA-256 fingerprints (integrity check)
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip().lower()
        if arg == "json":
            r = self.bridge.export_audit_json(with_fingerprints=True)
            if r.get("ok"):
                chat.add_system(f"[b]Audit JSON export[/b]\n```json\n{r.get('json', '')[:8000]}\n```")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        if arg == "csv":
            r = self.bridge.export_audit_csv()
            if r.get("ok"):
                chat.add_system(f"[b]Audit CSV export[/b]\n```csv\n{r.get('csv', '')[:8000]}\n```")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        if arg == "verify":
            from tera_pilot.agent_identity import get_audit_trail
            trail = get_audit_trail()
            with trail._log._lock:
                entries = list(trail._log._entries)
            ok = 0
            bad = 0
            for e in entries:
                fp = e.get("fingerprint")
                if not fp:
                    # In-memory entries don't have fingerprints; only exports do.
                    continue
                if trail.verify_fingerprint(e):
                    ok += 1
                else:
                    bad += 1
            chat.add_system(
                f"[b]Audit integrity check[/b]\n"
                f"  Verified OK:      {ok}\n"
                f"  Tampered entries: {bad}"
            )
            self.query_one(InputBox).focus()
            return
        # Default: summary
        summary = self.bridge.get_agent_audit_summary()
        if not summary.get("ok"):
            chat.add_error(f"Failed: {summary.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        s = summary.get("summary") or {}
        if not s:
            chat.add_system("[b]Audit Trail[/b]\n  No entries yet.")
        else:
            lines = ["[b]Audit Trail Summary[/b]  (per-agent)", ""]
            for aid, row in s.items():
                lines.append(
                    f"  [cyan]{aid}[/cyan]  ({row.get('role', '?')})  "
                    f"calls: {row.get('tool_calls', 0)}  "
                    f"errors: {row.get('errors', 0)}  "
                    f"rejects: {row.get('rejections', 0)}  "
                    f"dur: {row.get('total_duration_ms', 0)}ms"
                )
            lines.append("")
            lines.append("  Use [cyan]/audit json[/cyan] or [cyan]/audit csv[/cyan] to export.")
            lines.append("  Use [cyan]/audit-signed verify <file>[/cyan] to verify a signed export (G16).")
            chat.add_system("\n".join(lines))
        self.query_one(InputBox).focus()

    # ── v2.1.0 (G15) — Multi-provider consensus engine ────────────

    def _exec_consensus(self, arg: str) -> None:
        """Run a prompt on 2–3 providers in parallel and show a structured diff.

        Usage:
            /consensus <prompt>          — run with default providers
            /consensus providers p1,p2   — set the provider triplet
            /consensus min_agreement 0.6 — set the agreement threshold
            /consensus timeout 30        — per-provider timeout (seconds)
        """
        chat = self.query_one(ChatLog)
        arg = (arg or "").strip()
        if not arg:
            chat.add_system(
                "[b]Consensus Engine (G15)[/b]\n\n"
                "  Run the same prompt on 2–3 providers in parallel and produce a\n"
                "  structured diff/comparison between the approaches.\n\n"
                "  Usage:\n"
                "    [cyan]/consensus <prompt>[/cyan]          — run with default providers\n"
                "    [cyan]/consensus providers p1,p2,p3[/cyan] — set the provider triplet\n"
                "    [cyan]/consensus min_agreement 0.6[/cyan] — set the agreement threshold\n"
                "    [cyan]/consensus timeout 30[/cyan]        — per-provider timeout (seconds)\n"
                "    [cyan]/consensus config[/cyan]            — show current config"
            )
            self.query_one(InputBox).focus()
            return
        parts = arg.split(None, 1)
        sub = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""
        if sub == "config":
            r = self.bridge.get_consensus_config()
            if r.get("ok"):
                cfg = r
                chat.add_system(
                    f"[b]Consensus config[/b]\n"
                    f"  providers: {', '.join(cfg.get('providers', [])) or '(auto — picks 3 from different families)'}\n"
                    f"  min_agreement: {cfg.get('min_agreement', 0.4)}\n"
                    f"  timeout_s: {cfg.get('timeout_s', 60.0)}\n"
                    f"  max_chars_per_response: {cfg.get('max_chars_per_response', 8000)}"
                )
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        if sub == "providers":
            if not sub_arg:
                chat.add_system("Usage: /consensus providers p1,p2,p3")
                self.query_one(InputBox).focus()
                return
            pids = [p.strip() for p in sub_arg.split(",") if p.strip()]
            r = self.bridge.set_consensus_config(providers=tuple(pids))
            if r.get("ok"):
                chat.add_system(f"Consensus providers set to: {', '.join(pids)}")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        if sub == "min_agreement":
            try:
                val = float(sub_arg)
            except ValueError:
                chat.add_system("Usage: /consensus min_agreement 0.6")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.set_consensus_config(min_agreement=val)
            if r.get("ok"):
                chat.add_system(f"Consensus min_agreement set to: {val}")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        if sub == "timeout":
            try:
                val = float(sub_arg)
            except ValueError:
                chat.add_system("Usage: /consensus timeout 30")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.set_consensus_config(timeout_s=val)
            if r.get("ok"):
                chat.add_system(f"Consensus timeout set to: {val}s")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        # Otherwise: treat the whole arg as a prompt to run consensus on.
        # Run in a worker so we don't block the UI.
        prompt = arg
        chat.add_user(f"[consensus] {prompt}")
        self._turn_running = True
        self._refresh_status("thinking")
        self._run_consensus(prompt)

    @work(thread=True, exclusive=True)
    def _run_consensus(self, prompt: str) -> None:
        try:
            result = self.bridge.run_consensus(prompt)
            self.call_from_thread(self._on_consensus_done, result)
        except Exception as e:
            self.call_from_thread(self._on_turn_error, str(e))

    def _on_consensus_done(self, result: Dict[str, Any]) -> None:
        chat = self.query_one(ChatLog)
        self._turn_running = False
        self._refresh_status("idle")
        if not result.get("ok"):
            chat.add_error(f"Consensus failed: {result.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        text = result.get("text", "")
        if text:
            chat.add_final(text)
        self.query_one(InputBox).focus()

    # ── v2.1.0 (G16) — Signed audit trail verification ────────────

    def _exec_audit_signed(self, arg: str) -> None:
        """Verify a signed/chained audit export (G16).

        Usage:
            /audit-signed                  — show help
            /audit-signed export           — export the current log as signed JSON
            /audit-signed verify <file>    — verify a signed export file
        """
        chat = self.query_one(ChatLog)
        arg = (arg or "").strip()
        if not arg:
            chat.add_system(
                "[b]Signed Audit Trail (G16)[/b]\n\n"
                "  Ed25519 signatures + SHA-256 hash chaining for tamper-evidence.\n\n"
                "  Usage:\n"
                "    [cyan]/audit-signed export[/cyan]           — export current log as signed JSON\n"
                "    [cyan]/audit-signed verify <file>[/cyan]    — verify a signed export file\n\n"
                "  The keypair is stored at [dim]~/.tera_pilot/audit_key[/dim] (private) and\n"
                "  [dim]~/.tera_pilot/audit_key.pub[/dim] (public). Generated on first use.\n"
                "  Zero-cloud — keys never leave the user's machine."
            )
            self.query_one(InputBox).focus()
            return
        parts = arg.split(None, 1)
        sub = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""
        if sub == "export":
            r = self.bridge.export_audit_signed_json()
            if r.get("ok"):
                signed = r.get("signed_json", "")
                # Show a truncated preview + offer to save to file.
                preview = signed[:4000] + (f"\n... [{len(signed)} total chars]" if len(signed) > 4000 else "")
                chat.add_system(
                    f"[b]Signed audit export[/b] ({len(signed)} chars)\n"
                    f"```json\n{preview}\n```\n\n"
                    f"To verify later: [cyan]/audit-signed verify <file>[/cyan]"
                )
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        if sub == "verify":
            if not sub_arg:
                chat.add_system("Usage: /audit-signed verify <path-to-signed-json>")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.verify_audit_signed_file(sub_arg)
            if not r.get("ok"):
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
                self.query_one(InputBox).focus()
                return
            report = r.get("report", {})
            ok = report.get("ok")
            checked = report.get("entries_checked", 0)
            valid = report.get("signatures_valid", 0)
            invalid = report.get("signatures_invalid", 0)
            breaks = report.get("chain_breaks", 0)
            failure = report.get("first_failure", "")
            status_line = "[green]OK[/green]" if ok else "[red]FAILED[/red]"
            chat.add_system(
                f"[b]Audit verification[/b]  {status_line}\n"
                f"  entries checked:   {checked}\n"
                f"  signatures valid:  {valid}\n"
                f"  signatures invalid: {invalid}\n"
                f"  chain breaks:      {breaks}"
                + (f"\n  [red]first failure:[/red] {failure}" if failure else "")
            )
            self.query_one(InputBox).focus()
            return
        chat.add_system(f"Unknown /audit-signed subcommand: {sub}")
        self.query_one(InputBox).focus()

    # ── v2.1.0 (G17) — Automatic learning loop ────────────────────

    def _exec_learnings(self, arg: str) -> None:
        """List / scan / dismiss auto-learning entries (G17).

        Usage:
            /learnings                  — list recent entries
            /learnings show <id>        — show full body
            /learnings dismiss <id>     — stop injecting an entry
            /learnings restore <id>     — un-dismiss
            /learnings scan             — manually run trigger detection
            /learnings dismissed        — list dismissed only
        """
        chat = self.query_one(ChatLog)
        workspace = self.bridge.workspace or os.getcwd()
        r = self.bridge.handle_learnings_command(workspace, arg or "")
        if r.get("ok"):
            chat.add_system(r.get("text", ""))
        else:
            chat.add_system(f"[red]Error:[/red] {r.get('error', 'unknown')}")
        self.query_one(InputBox).focus()

    # ── v2.1.0 (G18) — Web search backend status ──────────────────

    def _exec_websearch(self, arg: str) -> None:
        """Show web search backend status (G18).

        Usage:
            /websearch            — show backend health + last probe
            /websearch backends   — alias for /websearch
            /websearch scan       — re-discover search backends now
        """
        chat = self.query_one(ChatLog)
        r = self.bridge.get_websearch_status()
        if not r.get("ok"):
            chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        status = r.get("status", {})
        active = status.get("active_backend", "")
        msg = status.get("last_status_msg", "")
        backends = status.get("backends", {})
        mcp_servers = status.get("mcp_servers", [])
        lines = ["[b]Web Search Backend Status (G18)[/b]", ""]
        lines.append(f"  Active backend: [cyan]{active or '(none)'}[/cyan]")
        if msg:
            lines.append(f"  Last status:    {msg}")
        if backends:
            lines.append("")
            lines.append("  [b]Backend health[/b]")
            for name, h in backends.items():
                ok = h.get("last_probe_ok", False)
                mark = "[green]ok[/green]" if ok else "[red]fail[/red]"
                err = h.get("last_probe_error", "")
                lines.append(f"    {name}: {mark}" + (f" ({err[:80]})" if err else ""))
        else:
            lines.append("  No backend probes yet — run a web_search to populate.")
        if mcp_servers:
            lines.append("")
            lines.append(f"  [b]MCP servers[/b] ({len(mcp_servers)} configured)")
            for s in mcp_servers[:10]:
                running = s.get("running", False)
                rmark = "[green]running[/green]" if running else "[red]stopped[/red]"
                lines.append(f"    {s.get('name', '?')}: {rmark} ({s.get('tool_count', 0)} tools)")
        else:
            lines.append("")
            lines.append("  [dim]No MCP servers configured. See .tera_pilot/skills/web-research/SKILL.md[/dim]")
            lines.append("  [dim]for a no-API-key search backend template.[/dim]")
        chat.add_system("\n".join(lines))
        self.query_one(InputBox).focus()

    # ── v2.0.2 (G6) — Post-task handoff ───────────────────────────

    def _exec_handoff(self, arg: str) -> None:
        """Create / list / edit / export handoff documents.

        Usage:
            /handoff                       — list saved handoffs
            /handoff create                — parse the LAST agent response into a handoff
            /handoff show <doc_id>         — show blocks + statuses
            /handoff accept <doc> <block>  — accept a block
            /handoff reject <doc> <block> [comment]
            /handoff edit <doc> <block> <replacement text...>
            /handoff todo <doc> <block>    — toggle a todo block
            /handoff revisions <doc>       — compile revisions into a prompt
            /handoff markdown <doc>        — export as Markdown
            /handoff delete <doc>
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()
        if not arg:
            docs = self.bridge.list_handoffs(limit=50)
            if not docs:
                chat.add_system(
                    "[b]Handoffs[/b]\n"
                    "  No saved handoffs.\n\n"
                    "  Use [cyan]/handoff create[/cyan] to parse the last agent response into an editable handoff."
                )
            else:
                lines = ["[b]Handoffs[/b]  (~/.tera_pilot/handoffs/)", ""]
                for d in docs:
                    lines.append(
                        f"  [{d['id']}]  {d.get('title', 'Untitled')[:60]}\n"
                        f"     blocks: {d.get('block_count', 0)}  "
                        f"updated: {d.get('updated_at', '?')}"
                    )
                lines.append("")
                lines.append("  Use [cyan]/handoff show <id>[/cyan] to view blocks.")
                chat.add_system("\n".join(lines))
            self.query_one(InputBox).focus()
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "create":
            # Grab the last assistant message
            last_output = ""
            try:
                cl = self.query_one(ChatLog)
                # ChatLog doesn't expose messages; use the bridge's
                # memory if available, else fall back to a hint.
                agent = self.bridge._agent
                if agent is not None and hasattr(agent, "memory") and agent.memory is not None:
                    messages = getattr(agent.memory, "messages", None)
                    if messages is not None:
                        for m in reversed(messages):
                            if getattr(m, "role", "") == "assistant" and getattr(m, "content", ""):
                                last_output = m.content
                                break
            except Exception:
                pass
            if not last_output:
                chat.add_error("No prior assistant response to convert into a handoff.")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.create_handoff(
                output=last_output,
                prompt=self._last_prompt or "",
                title=self._last_prompt[:60] if self._last_prompt else "Untitled handoff",
            )
            if r.get("ok"):
                doc = r.get("doc") or {}
                chat.add_system(
                    f"[b]Handoff created:[/b] {doc.get('id')}\n"
                    f"  Title: {doc.get('title', 'Untitled')}\n"
                    f"  Blocks: {len(doc.get('blocks') or [])}\n\n"
                    f"  Use [cyan]/handoff show {doc.get('id')}[/cyan] to view and edit blocks."
                )
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "show":
            if not rest:
                chat.add_system("Usage: /handoff show <doc_id>")
                self.query_one(InputBox).focus()
                return
            doc = self.bridge.get_handoff(rest)
            if doc is None:
                chat.add_error(f"Handoff not found: {rest}")
                self.query_one(InputBox).focus()
                return
            lines = [
                f"[b]Handoff: {doc.get('title', 'Untitled')}[/b]  (id: {doc.get('id')})",
                f"  Created: {doc.get('created_at', '?')}  Updated: {doc.get('updated_at', '?')}",
                "",
            ]
            for i, b in enumerate(doc.get("blocks") or [], 1):
                status = b.get("status", "pending")
                tag = {"pending": "[dim]pending[/dim]",
                       "accepted": "[green]accepted[/green]",
                       "rejected": "[red]rejected[/red]",
                       "edited": "[yellow]edited[/yellow]"}.get(status, status)
                btype = b.get("type", "?")
                path = b.get("path", "")
                path_str = f"  [dim]{path}[/dim]" if path else ""
                lines.append(f"  {i}. [{btype}]  {tag}{path_str}  id={b.get('id')}")
                content = (b.get("content") or "")
                preview = content[:200].replace("\n", " ")
                if preview:
                    lines.append(f"     [dim]{preview}[/dim]")
                if b.get("comment"):
                    lines.append(f"     [italic]comment: {b['comment']}[/italic]")
            lines.append("")
            lines.append(
                "  Edit with: [cyan]/handoff accept|reject|edit|todo[/cyan] <doc> <block_id>"
            )
            chat.add_system("\n".join(lines))
            self.query_one(InputBox).focus()
            return

        if sub in ("accept", "reject"):
            # /handoff accept <doc> <block_id>
            args = rest.split(None, 1)
            if len(args) < 2:
                chat.add_system(f"Usage: /handoff {sub} <doc_id> <block_id> [comment]")
                self.query_one(InputBox).focus()
                return
            doc_id, block_and_comment = args[0], args[1]
            comment = ""
            if " " in block_and_comment:
                block_id, comment = block_and_comment.split(None, 1)
            else:
                block_id = block_and_comment
            r = self.bridge.set_handoff_block_status(
                doc_id=doc_id, block_id=block_id,
                status="accepted" if sub == "accept" else "rejected",
                comment=comment,
            )
            if r.get("ok"):
                chat.add_system(f"Block [b]{block_id}[/b] marked [b]{sub}[/b].")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "edit":
            # /handoff edit <doc> <block_id> <replacement text>
            args = rest.split(None, 2)
            if len(args) < 3:
                chat.add_system("Usage: /handoff edit <doc_id> <block_id> <replacement text>")
                self.query_one(InputBox).focus()
                return
            doc_id, block_id, replacement = args[0], args[1], args[2]
            r = self.bridge.set_handoff_block_status(
                doc_id=doc_id, block_id=block_id,
                status="edited", replacement=replacement,
            )
            if r.get("ok"):
                chat.add_system(f"Block [b]{block_id}[/b] edited — replacement recorded.")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "todo":
            args = rest.split(None, 1)
            if len(args) < 2:
                chat.add_system("Usage: /handoff todo <doc_id> <block_id>")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.toggle_handoff_todo(args[0], args[1])
            if r.get("ok"):
                chat.add_system(f"Todo block [b]{args[1]}[/b] toggled.")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "revisions":
            if not rest:
                chat.add_system("Usage: /handoff revisions <doc_id>")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.build_handoff_revision_prompt(rest)
            if not r.get("ok"):
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
                self.query_one(InputBox).focus()
                return
            prompt = r.get("prompt") or ""
            if not prompt:
                chat.add_system("No pending revisions — every block is accepted.")
            else:
                chat.add_system(
                    f"[b]Revision prompt for {rest}:[/b]\n\n" + prompt +
                    "\n\n[dim]Send this back to the agent by typing a normal message, "
                    "or use /handoff send <doc_id> to auto-dispatch.[/dim]"
                )
            self.query_one(InputBox).focus()
            return

        if sub == "markdown":
            if not rest:
                chat.add_system("Usage: /handoff markdown <doc_id>")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.export_handoff_markdown(rest)
            if r.get("ok"):
                chat.add_system(f"```markdown\n{r.get('markdown', '')}\n```")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "delete":
            if not rest:
                chat.add_system("Usage: /handoff delete <doc_id>")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.delete_handoff(rest)
            if r.get("ok"):
                chat.add_system(f"Handoff [b]{rest}[/b] deleted.")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        chat.add_system(
            f"Unknown /handoff subcommand: {sub}\n"
            "Subcommands: create | show | accept | reject | edit | todo | "
            "revisions | markdown | delete"
        )
        self.query_one(InputBox).focus()

    # ── v2.0.2 (M2) — Cost-aware provider routing ────────────────

    def _exec_cost(self, arg: str) -> None:
        """Cost-aware provider routing.

        Usage:
            /cost                       — show current config + budget pressure
            /cost route <prompt text>   — run the router on a prompt
            /cost cap <complexity> <usd>— set per-complexity USD cap
            /cost enable on|off         — toggle cost-router master switch
            /cost threshold high|critical <pct>
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()
        if not arg:
            cfg = self.bridge.get_cost_router_config()
            if not cfg.get("ok"):
                chat.add_error(f"Failed: {cfg.get('error', 'unknown')}")
                self.query_one(InputBox).focus()
                return
            caps = cfg.get("caps_usd", {})
            chat.add_system(
                f"[b]Cost-Aware Router[/b]  (M2)\n"
                f"  Enabled:          {'ON' if cfg.get('enabled') else 'OFF'}\n"
                f"  Budget pressure:  HIGH ≥ {cfg.get('budget_pressure_high', 0)*100:.0f}%  "
                f"CRITICAL ≥ {cfg.get('budget_pressure_critical', 0)*100:.0f}%\n"
                f"  Error threshold:  {cfg.get('error_rate_threshold', 0)*100:.0f}% "
                f"(window: {cfg.get('error_window', 0)} requests)\n"
                f"  Prefer free under pressure: "
                f"{'ON' if cfg.get('prefer_free_under_pressure') else 'OFF'}\n\n"
                f"  [b]Per-complexity USD caps[/b]\n"
                f"    trivial:   ${caps.get('trivial', 0):.4f}\n"
                f"    simple:    ${caps.get('simple', 0):.4f}\n"
                f"    moderate:  ${caps.get('moderate', 0):.4f}\n"
                f"    complex:   ${caps.get('complex', 0):.4f}\n"
                f"    expert:    ${caps.get('expert', 0):.4f}\n\n"
                f"  Commands:\n"
                f"    /cost route <prompt>\n"
                f"    /cost cap <complexity> <usd>\n"
                f"    /cost enable on|off\n"
                f"    /cost threshold high|critical <pct>"
            )
            self.query_one(InputBox).focus()
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "route":
            if not rest:
                chat.add_system("Usage: /cost route <prompt text>")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.cost_route(rest)
            if not r.get("ok"):
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
                self.query_one(InputBox).focus()
                return
            final = r.get("final_pick") or {}
            factors = r.get("factors") or []
            chat.add_system(
                f"[b]Cost-aware routing decision[/b]\n"
                f"  Prompt:          {r.get('prompt_preview', '')[:60]}...\n"
                f"  Complexity:      {r.get('complexity', '?')}"
                f"{'  (demoted from ' + r.get('demoted_from', '') + ')' if r.get('demoted_from') else ''}\n"
                f"  Final pick:      [b]{final.get('provider_id', '?')}[/b] / "
                f"[dim]{final.get('model', '?')}[/dim]\n"
                f"  Est. cost:       ${r.get('estimated_cost_usd', 0):.4f}\n"
                f"  Budget pressure: {r.get('budget_pressure', 0)*100:.0f}%  "
                f"(remaining ${r.get('budget_remaining_usd', 0):.2f})\n"
                f"  AutoRouter pick: {(r.get('auto_router_pick') or {}).get('provider_id', '?')} / "
                f"{(r.get('auto_router_pick') or {}).get('model', '?')}\n\n"
                f"  [b]Factors[/b]\n" +
                "\n".join(f"    - {f}" for f in factors)
            )
            self.query_one(InputBox).focus()
            return

        if sub == "cap":
            args = rest.split()
            if len(args) < 2:
                chat.add_system("Usage: /cost cap <complexity> <usd>\n"
                                "  complexity: trivial|simple|moderate|complex|expert")
                self.query_one(InputBox).focus()
                return
            complexity = args[0].lower()
            valid = {"trivial", "simple", "moderate", "complex", "expert"}
            if complexity not in valid:
                chat.add_error(f"Invalid complexity: {complexity}. Valid: {', '.join(sorted(valid))}")
                self.query_one(InputBox).focus()
                return
            try:
                usd = float(args[1])
            except ValueError:
                chat.add_error(f"Invalid USD value: {args[1]}")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.set_cost_cap(complexity, usd)
            if r.get("ok"):
                chat.add_system(f"Cap for [b]{complexity}[/b] set to [b]${usd:.4f}[/b].")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "enable":
            v = rest.lower() in ("on", "1", "true", "yes")
            r = self.bridge.set_cost_router_config(enabled=v)
            if r.get("ok"):
                chat.add_system(f"Cost-aware router: [b]{'ON' if v else 'OFF'}[/b]")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "threshold":
            args = rest.split()
            if len(args) < 2 or args[0] not in ("high", "critical"):
                chat.add_system("Usage: /cost threshold high|critical <pct>")
                self.query_one(InputBox).focus()
                return
            try:
                pct = float(args[1]) / 100.0
            except ValueError:
                chat.add_error(f"Invalid pct: {args[1]}")
                self.query_one(InputBox).focus()
                return
            kwarg = {"budget_pressure_high": pct} if args[0] == "high" \
                else {"budget_pressure_critical": pct}
            r = self.bridge.set_cost_router_config(**kwarg)
            if r.get("ok"):
                chat.add_system(f"{args[0]} threshold set to [b]{args[1]}%[/b].")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        chat.add_system(
            f"Unknown /cost subcommand: {sub}\n"
            "Subcommands: route | cap | enable | threshold"
        )
        self.query_one(InputBox).focus()

    # ── v2.0.2 (M3) — Team spend dashboard ────────────────────────

    def _exec_spend(self, arg: str) -> None:
        """Team spend dashboard.

        Usage:
            /spend                  — show team spend summary
            /spend team <name>      — set local user's team
            /spend budget <usd>     — set team monthly budget
            /spend sources          — list token_history sources
            /spend add <path>       — add a source (file or dir of *.jsonl)
            /spend json|csv         — export report
            /spend identity         — show local user identity
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()
        if not arg:
            r = self.bridge.get_team_spend_report(days=30)
            if not r.get("ok"):
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
                self.query_one(InputBox).focus()
                return
            by_user = r.get("by_user") or []
            by_provider = r.get("by_provider") or []
            chat.add_system(
                f"[b]Team Spend Dashboard[/b]  (team: {r.get('team', '?')})\n"
                f"  Generated at:  {r.get('generated_at_iso', '?')}\n"
                f"  Sources:       {r.get('sources_scanned', 0)}  "
                f"Entries processed: {r.get('entries_processed', 0)}\n"
                f"  Total cost:    [b]${r.get('total_cost_usd', 0):.4f}[/b]\n"
                f"  Total tokens:  in={r.get('total_tokens_in', 0):,}  "
                f"out={r.get('total_tokens_out', 0):,}\n"
                f"  Requests:      {r.get('total_request_count', 0)}\n"
                f"  Team budget:   ${r.get('team_budget_usd', 0):.2f}  "
                f"used: {r.get('team_budget_used_pct', 0)}%\n"
                f"  Top consumer:  {r.get('top_consumer_user_id', 'n/a')}\n\n"
                f"  [b]By user[/b] (top 5)\n" +
                "\n".join(
                    f"    {u.get('user_id', '?')[:18]:<18}  "
                    f"${u.get('cost_usd', 0):.4f}  "
                    f"{u.get('request_count', 0)} reqs  "
                    f"{u.get('name', '')}"
                    for u in by_user[:5]
                ) +
                f"\n\n  [b]By provider[/b] (top 5)\n" +
                "\n".join(
                    f"    {p.get('provider', '?'):<18}  "
                    f"${p.get('cost_usd', 0):.4f}  "
                    f"{p.get('request_count', 0)} reqs"
                    for p in by_provider[:5]
                ) +
                "\n\n  Commands: /spend team|budget|sources|add|json|csv|identity"
            )
            self.query_one(InputBox).focus()
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "team":
            if not rest:
                chat.add_system("Usage: /spend team <name>")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.set_user_team(rest)
            if r.get("ok"):
                chat.add_system(f"Local user team set to: [b]{rest}[/b]")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "budget":
            if not rest:
                chat.add_system("Usage: /spend budget <usd>")
                self.query_one(InputBox).focus()
                return
            try:
                usd = float(rest)
            except ValueError:
                chat.add_error(f"Invalid USD: {rest}")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.set_team_budget(usd)
            if r.get("ok"):
                chat.add_system(f"Team budget set to [b]${usd:.2f}/mo[/b].")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "sources":
            r = self.bridge.list_spend_sources()
            sources = r.get("sources") or []
            if sources:
                chat.add_system(
                    "[b]Token history sources[/b]\n" +
                    "\n".join(f"  - {s}" for s in sources)
                )
            else:
                chat.add_system("No token history sources configured.")
            self.query_one(InputBox).focus()
            return

        if sub == "add":
            if not rest:
                chat.add_system("Usage: /spend add <path-to-jsonl-or-dir>")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.add_spend_source(rest)
            if r.get("ok"):
                chat.add_system(f"Source added. Now tracking: {len(r.get('sources', []))} sources.")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "json":
            r = self.bridge.export_spend_report_json(days=30)
            if r.get("ok"):
                chat.add_system(f"```json\n{r.get('json', '')[:8000]}\n```")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "csv":
            r = self.bridge.export_spend_report_csv(days=30)
            if r.get("ok"):
                chat.add_system(f"```csv\n{r.get('csv', '')[:8000]}\n```")
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        if sub == "identity":
            r = self.bridge.get_user_identity()
            if r.get("ok"):
                chat.add_system(
                    f"[b]Local User Identity[/b]\n"
                    f"  user_id: {r.get('user_id', '?')}\n"
                    f"  name:    {r.get('name', '?')}\n"
                    f"  team:    {r.get('team', '?')}\n"
                    f"  email:   {r.get('email', '') or '(not shared)'}"
                )
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        chat.add_system(
            f"Unknown /spend subcommand: {sub}\n"
            "Subcommands: team | budget | sources | add | json | csv | identity"
        )
        self.query_one(InputBox).focus()

    # --------------------------------------------------------------- worker
    @work(thread=True, exclusive=True)
    def _run_turn(self, prompt: str) -> None:
        try:
            result = self.bridge.run_prompt(prompt)
            self.call_from_thread(self._on_turn_done, result)
        except Exception as e:
            self.call_from_thread(self._on_turn_error, str(e))

    # ----------------------------------------------------------- agent events
    def _sink(self, kind: str, data: Dict[str, Any]) -> None:
        self.call_from_thread(self._handle_event, kind, data)

    def action_toggle_theme(self) -> None:
        """Toggle between light and dark themes."""
        self._dark_theme = not self._dark_theme
        theme = "dark" if self._dark_theme else "light"
        self.notify(f"Theme switched to {theme}")
        # Update CSS path to switch themes, then reload so the new
        # stylesheet actually applies. Without reload_css() Textual keeps
        # the previously-loaded stylesheet cached.
        if self._dark_theme:
            self.CSS_PATH = "styles_dark.tcss"
        else:
            self.CSS_PATH = "styles_light.tcss"
        try:
            self.reload_css()
        except Exception:
            pass

    def _handle_event(self, kind: str, data: Dict[str, Any]) -> None:
        chat = self.query_one(ChatLog)
        # v2.0.0: surface subagent events distinctly so the user can tell
        # which agent produced a thought / tool call / result.
        sub_label = data.get("parent_label") or data.get("subagent_label")
        if kind == "plan_created":
            chat.add_plan(str(data.get("plan", "")))
        elif kind == "thought":
            text = str(data.get("thought", ""))
            if sub_label:
                chat.add_thought(f"[subagent {sub_label}] {text}")
            else:
                chat.add_thought(text)
        elif kind == "token_delta":
            chat.append_token_delta(str(data.get("delta", "")))
        elif kind == "tool_called":
            self._refresh_status("running")
            tool = str(data.get("tool", "?"))
            args = data.get("args") or {}
            if sub_label:
                chat.add_tool_call(tool, args, sub_label=sub_label)
            else:
                chat.add_tool_call(tool, args)
        elif kind == "tool_result":
            chat.add_tool_result(str(data.get("tool", "?")), str(data.get("result", "")))
            self._refresh_status("thinking")
        elif kind == "iteration_start":
            self._refresh_status("thinking")
        elif kind == "iteration_end":
            # v2.0.0 fix: transition back to "thinking" between iterations
            # so the StatusBar doesn't get stuck on "tool running".
            self._refresh_status("thinking")
        elif kind == "error":
            # v2.3.5-fix: remember the error the event surfaced so
            # _on_turn_done can skip its duplicate (the runtime emits the
            # ERROR event AND returns success=False for the same failure).
            self._last_event_error = str(data.get("error", "unknown error"))
            chat.add_error(self._last_event_error)
        elif kind == "done":
            pass

    def _confirm(self, info: Dict[str, Any]) -> None:
        self.call_from_thread(self._show_confirm, dict(info))

    def _show_confirm(self, info: Dict[str, Any]) -> None:
        def _answer(result: bool | str | None) -> None:
            self._approval_modal = None
            # Guardian modal returns "approve" | "reject" | "use_fix"
            # Legacy modal returns True/False
            if isinstance(result, str):
                if result == "use_fix":
                    self.bridge.answer_guardian_verdict("use_fix")
                elif result == "approve":
                    self.bridge.answer_confirmation(True)
                elif result == "reject":
                    self.bridge.answer_confirmation(False)
                else:
                    self.bridge.answer_confirmation(False)
            elif isinstance(result, bool):
                self.bridge.answer_confirmation(result)
            else:
                self.bridge.answer_confirmation(False)

        # Check if this is a Guardian review event
        if info.get("guardian_verdict") == "MODIFY" or info.get("suggested_args") is not None:
            self._approval_modal = GuardianModal(info)
            self.push_screen(self._approval_modal, _answer)
        else:
            self._approval_modal = ApprovalModal(info)
            self.push_screen(self._approval_modal, _answer)

    def _close_stale_approval(self) -> None:
        """Pop a confirmation modal that is no longer relevant.

        Called when the turn ends / errors / is interrupted while an
        approval dialog is still open (the agent already moved on — the
        dialog would otherwise stay on screen forever, blocking further
        input). Safe no-op when no modal is tracked.
        """
        modal = self._approval_modal
        self._approval_modal = None
        if modal is None:
            return
        try:
            if modal in self.screen_stack:
                self.pop_screen()
        except Exception:
            pass

    # --------------------------------------------------------------- lifecycle
    def _on_turn_done(self, result: Any) -> None:
        chat = self.query_one(ChatLog)
        was_streaming = chat._streaming_active

        # v2.3.4-fix: the agent thread may have moved on (or the run ended)
        # while an approval dialog was still open — close the stale dialog.
        self._close_stale_approval()

        error = getattr(result, "error", None)
        metadata = getattr(result, "metadata", {})
        if error == "awaiting_plan_approval":
            plan_text = metadata.get("plan", "")
            chat.add_plan(plan_text)
            self._show_plan_approval(plan_text)
            self._turn_running = False
            self._refresh_status("idle")
            return

        output = getattr(result, "output", "") or ""
        success = getattr(result, "success", True)
        if success:
            # v2.3.1-fix: always render the final answer via add_final().
            # When streaming was active, add_final() rolls the live
            # streaming entry back and replaces it with the Markdown-
            # rendered answer. Previously the final answer was dropped
            # entirely on streamed turns (end_streaming() discarded the
            # buffer and add_final was skipped), so streamed responses
            # stayed as raw plain text and never rendered Markdown.
            chat.add_final(output)
        else:
            err = getattr(result, "error", None) or output or "task failed"
            if was_streaming:
                chat.abort_streaming()
            # v2.3.6: when the run hit the iteration cap, don't throw the
            # partial work away — surface whatever the agent produced so
            # far as the answer (the error line already says why it
            # stopped).
            if "max iterations" in str(err).lower() and output and output.strip():
                chat.add_system(
                    "[dim]Hit the iteration cap — showing the partial result below.[/dim]"
                )
                chat.add_final(output)
            # v2.3.5-fix: skip the duplicate error. The runtime already
            # emitted this exact error via the ERROR event (rendered by
            # _handle_event) before returning success=False — adding it
            # again here made every run-ending failure appear twice in
            # the ChatLog.
            if str(err) != (self._last_event_error or ""):
                chat.add_error(str(err))
        self._last_event_error = None
        self._turn_running = False
        self._refresh_status("idle")

    def _show_plan_approval(self, plan_text: str) -> None:
        info = {
            "action": "Execute plan",
            "summary": plan_text[:500] + ("..." if len(plan_text) > 500 else ""),
        }

        def _answer(accepted: bool | None) -> None:
            if accepted:
                self._run_turn_with_plan_approval()
            else:
                self.query_one(ChatLog).add_system(
                    "Plan rejected. Type new instructions or feedback."
                )
                self.query_one(InputBox).focus()

        self.push_screen(ApprovalModal(info), _answer)

    @work(thread=True, exclusive=True)
    def _run_turn_with_plan_approval(self) -> None:
        # v2.3.4-fix: mark the turn as running BEFORE the LLM work starts.
        # Previously _turn_running stayed False after the user approved the
        # plan, so: a second prompt submitted while the approved plan was
        # executing queued silently behind the busy lock (no "thinking"
        # status, no feedback), and Ctrl+C reported "nothing running".
        self.call_from_thread(self._mark_turn_running)
        try:
            result = self.bridge.run_prompt(
                self._last_prompt, plan_approved=True
            )
            self.call_from_thread(self._on_turn_done, result)
        except Exception as e:
            self.call_from_thread(self._on_turn_error, str(e))

    def _mark_turn_running(self) -> None:
        self._turn_running = True
        self._refresh_status("thinking")

    def _on_turn_error(self, message: str) -> None:
        chat = self.query_one(ChatLog)
        # v2.3.1-fix: discard any partial streamed text instead of leaving
        # half-written plain text above the error message.
        if chat._streaming_active:
            chat.abort_streaming()
        # v2.3.4-fix: a run that crashed must not leave a stale approval
        # dialog on screen.
        self._close_stale_approval()
        chat.add_error(message)
        self._turn_running = False
        self._refresh_status("idle")

    def _refresh_status(self, state: str) -> None:
        """Update InfoBox with current status (model, provider, etc)."""
        self._status_state = state
        try:
            status = self.bridge.status()
            info = self.query_one(InfoBox)
            info.update_info(
                model=status.get("model", "unknown"),
                provider=status.get("provider", "unknown"),
                # v2.3.4-fix: refresh the directory too — /cd changed the
                # workspace but the InfoBox kept showing the old path.
                directory=self.bridge.workspace,
            )
            if state not in ("thinking", "running"):
                info.clear_status()
        except Exception:
            pass
        # v2.3.1: mark the input box with the "working" class while the
        # agent is running so its border glows with the accent color.
        try:
            box = self.query_one(InputBox)
            working = state in ("thinking", "running")
            if working != box.has_class("working"):
                box.set_class(working, "working")
        except Exception:
            pass

    def _tick_status_animation(self) -> None:
        """v2.3.6: animate the status line ("thinking…") while a turn runs.

        Runs on a 0.15s timer; no-ops when idle. The word follows the
        current phase (thinking / running), the dots cycle 0→3.
        """
        if not self._turn_running or self._status_state not in ("thinking", "running"):
            return
        try:
            info = self.query_one(InfoBox)
        except Exception:
            return
        self._status_frame += 1
        word = "thinking" if self._status_state == "thinking" else "running"
        dots = "." * (self._status_frame % 4)
        info.update_status(f"{word}{dots:<3}")

    # ------------------------------------------------------------------ actions
    def action_interrupt(self) -> None:
        if self._turn_running:
            self.bridge.request_stop()
            # v2.3.4-fix: if an approval dialog is open, close it — the
            # stop request already released the agent's confirmation wait.
            self._close_stale_approval()
            self.query_one(ChatLog).add_system("interrupt requested...")
        else:
            self.query_one(ChatLog).add_system("(nothing running - Ctrl+D to quit)")

    def action_launch_gui(self) -> None:
        import subprocess
        import sys
        try:
            subprocess.Popen(
                [sys.executable, "-m", "tera_pilot", "--project", self.bridge.workspace]
            )
            self.query_one(ChatLog).add_system("launching GUI...")
            config_path = os.path.expanduser("~/.tera_pilot/config.json")
            close_on_switch = False
            try:
                import json
                with open(config_path, "r") as f:
                    cfg = json.load(f)
                close_on_switch = bool(cfg.get("close_on_switch", False))
            except Exception:
                pass
            if close_on_switch:
                self.exit()
        except Exception as e:
            self.query_one(ChatLog).add_error(f"Failed to launch GUI: {e}")


    # ── G9/G10/G11/G13 slash commands ──────────────────────────

    # ── G9: /hooks ──────────────────────────────────────────────────────────

    def _exec_hooks(self, arg: str) -> None:
        """Manage hook system.

        Usage:
            /hooks                    — list all hooks
            /hooks enable <id>        — enable a hook
            /hooks disable <id>       — disable a hook
            /hooks remove <id>        — remove a hook
            /hooks test <id> <type>   — dry-run a hook
            /hooks stats              — show hook statistics
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()

        if not arg or arg == "stats":
            if arg == "stats":
                r = self.bridge.get_hook_stats()
                if r.get("ok"):
                    data = r.get("hooks", {})
                    lines = ["[b]Hook System Stats[/b]", ""]
                    for ht, info in data.items():
                        lines.append(
                            f"  {ht}: {info['enabled']} enabled / {info['total']} total"
                        )
                    chat.add_system("\n".join(lines))
                else:
                    chat.add_error(f"Error: {r.get('error', 'unknown')}")
            else:
                hooks = self.bridge.list_hooks()
                if not hooks:
                    chat.add_system(
                        "[b]Hook System[/b]\n\n"
                        "  No hooks registered.\n\n"
                        "  Add Python modules to [cyan]~/.tera_pilot/hooks/[/cyan] to register hooks.\n"
                        "  Each module may define [cyan]register_hooks(manager)[/cyan]."
                    )
                else:
                    lines = ["[b]Hook System[/b]  (~/.tera_pilot/hooks/)", ""]
                    for h in hooks:
                        status = "[green]ON[/green]" if h["enabled"] else "[red]OFF[/red]"
                        lines.append(
                            f"  [{h['id']}] {h['name']}  ({h['hook_type']})  {status}\n"
                            f"     priority: {h['priority']}  source: {h.get('source', 'api')}"
                        )
                    lines.append("")
                    lines.append("  Use [cyan]/hooks enable|disable|remove <id>[/cyan] to manage.")
                    chat.add_system("\n".join(lines))
            self.query_one(InputBox).focus()
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "enable":
            if not rest:
                chat.add_system("Usage: /hooks enable <hook_id>")
            else:
                r = self.bridge.set_hook_enabled(rest, True)
                if r.get("ok"):
                    chat.add_system(f"Hook [cyan]{rest}[/cyan] enabled.")
                else:
                    chat.add_error(f"Hook {rest} not found.")
        elif sub == "disable":
            if not rest:
                chat.add_system("Usage: /hooks disable <hook_id>")
            else:
                r = self.bridge.set_hook_enabled(rest, False)
                if r.get("ok"):
                    chat.add_system(f"Hook [cyan]{rest}[/cyan] disabled.")
                else:
                    chat.add_error(f"Hook {rest} not found.")
        elif sub == "remove":
            if not rest:
                chat.add_system("Usage: /hooks remove <hook_id>")
            else:
                r = self.bridge.remove_hook(rest)
                if r.get("ok"):
                    chat.add_system(f"Hook [cyan]{rest}[/cyan] removed.")
                else:
                    chat.add_error(f"Hook {rest} not found.")
        elif sub == "test":
            test_parts = rest.split(None, 1)
            if len(test_parts) < 2:
                chat.add_system("Usage: /hooks test <hook_id> <event_type>")
            else:
                r = self.bridge.test_hook(test_parts[0], test_parts[1])
                if r.get("ok"):
                    result = r.get("result", {})
                    chat.add_system(f"Hook test result: {result.get('action', 'unknown')} — {result.get('message', '')}")
                else:
                    chat.add_error(f"Test failed: {r.get('error', 'unknown')}")
        else:
            chat.add_system(f"Unknown subcommand: {sub}. Use enable|disable|remove|test|stats.")

        self.query_one(InputBox).focus()


    # ── G10: /checkpoint ────────────────────────────────────────────────────

    def _exec_checkpoint(self, arg: str) -> None:
        """Create / manage checkpoints.

        Usage:
            /checkpoint                — list checkpoints
            /checkpoint save [label]   — create a manual checkpoint
            /checkpoint auto [on|off]  — toggle auto-checkpointing
            /checkpoint stats          — show checkpoint statistics
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()

        if not arg:
            cps = self.bridge.list_checkpoints(limit=20)
            if not cps:
                chat.add_system(
                    "[b]Checkpoints[/b]\n\n"
                    "  No checkpoints yet.\n\n"
                    "  Use [cyan]/checkpoint save [label][/cyan] to create one.\n"
                    "  Auto-checkpointing is enabled by default."
                )
            else:
                lines = ["[b]Checkpoints[/b]  (~/.tera_pilot/checkpoints/)", ""]
                for cp in cps:
                    label = f"  [{cp.get('label', '')}] " if cp.get('label') else "  "
                    lines.append(
                        f"{label}{cp['id']}  turn={cp['turn_number']}  "
                        f"msg={cp['message_count']}  files={len(cp.get('file_manifest', []))}"
                    )
                lines.append("")
                lines.append("  Use [cyan]/rewind <n>[/cyan] to go back to a checkpoint.")
                chat.add_system("\n".join(lines))
            self.query_one(InputBox).focus()
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "save":
            # Get current message count from the agent
            msg_count = 0
            try:
                agent = self.bridge._agent
                if agent is not None:
                    msg_count = len(agent.memory.messages)
            except Exception:
                pass
            # Get touched files. v2.3.6-fix: the runtime's ToolEngine lives
            # at ``agent.tools`` (NOT ``agent._tool_engine``, which never
            # existed) — so /checkpoint save used to record 0 touched
            # files even after the agent wrote files, and the backup
            # manifest was always empty.
            touched = []
            try:
                agent = self.bridge._agent
                if agent is not None:
                    touched = list(getattr(agent.tools, "_touched_files", []) or [])
            except Exception:
                pass
            r = self.bridge.create_checkpoint(
                message_count=msg_count,
                touched_files=touched,
                label=rest,
            )
            if r.get("ok"):
                cp = r.get("checkpoint", {})
                chat.add_system(
                    f"[b]Checkpoint saved:[/b] {cp.get('id')}\n"
                    f"  Turn: {cp.get('turn_number')}  Messages: {cp.get('message_count')}\n"
                    f"  Files backed up: {len(cp.get('file_manifest', []))}"
                )
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
        elif sub == "auto":
            if rest.lower() in ("off", "false", "no"):
                r = self.bridge.set_auto_checkpoint(False)
                chat.add_system("Auto-checkpointing [red]disabled[/red].")
            else:
                r = self.bridge.set_auto_checkpoint(True)
                chat.add_system("Auto-checkpointing [green]enabled[/green].")
        elif sub == "stats":
            r = self.bridge.get_checkpoint_stats()
            if r.get("ok"):
                lines = ["[b]Checkpoint Statistics[/b]", ""]
                lines.append(f"  Session: {r.get('session_id', '?')}")
                lines.append(f"  Total checkpoints: {r.get('total_checkpoints', 0)}")
                lines.append(f"  Current turn: {r.get('current_turn', 0)}")
                lines.append(f"  Auto-checkpoint: {r.get('auto_checkpoint_enabled', True)}")
                lines.append(f"  Total files backed up: {r.get('total_files_backed_up', 0)}")
                chat.add_system("\n".join(lines))
            else:
                chat.add_error(f"Error: {r.get('error', 'unknown')}")
        else:
            chat.add_system(f"Unknown subcommand: {sub}. Use save|auto|stats.")

        self.query_one(InputBox).focus()


    # ── G10: /rewind ────────────────────────────────────────────────────────

    def _exec_rewind(self, arg: str) -> None:
        """Rewind to a previous checkpoint.

        Usage:
            /rewind <n>          — rewind N steps
            /rewind to <id>      — rewind to a specific checkpoint
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()

        if not arg:
            chat.add_system("Usage: /rewind <n>  or  /rewind to <checkpoint_id>")
            self.query_one(InputBox).focus()
            return

        parts = arg.split(None, 1)
        if parts[0].lower() == "to" and len(parts) > 1:
            r = self.bridge.rewind_to_checkpoint(parts[1].strip())
        else:
            try:
                n = int(parts[0])
            except ValueError:
                chat.add_error(f"Invalid number: {parts[0]}")
                self.query_one(InputBox).focus()
                return
            r = self.bridge.rewind_checkpoint(n)

        if r.get("ok"):
            cp = r.get("checkpoint", {})
            files = r.get("files_restored", [])
            errors = r.get("errors", [])
            lines = [
                f"[b]Rewound to checkpoint:[/b] {cp.get('id')}",
                f"  Turn: {cp.get('turn_number')}  Messages: {cp.get('message_count')}",
                f"  Files restored: {len(files)}",
            ]
            if errors:
                lines.append(f"  [yellow]Warnings:[/yellow] {len(errors)}")
                for e in errors[:5]:
                    lines.append(f"    - {e}")
            lines.append("")
            lines.append("[yellow]Note:[/yellow] You may need to restart the conversation to see the full effect.")
            chat.add_system("\n".join(lines))
        else:
            chat.add_error(f"Rewind failed: {r.get('error', 'unknown')}")

        self.query_one(InputBox).focus()


    # ── G11: /github ────────────────────────────────────────────────────────

    def _exec_github(self, arg: str) -> None:
        """GitHub automation commands.

        Usage:
            /github                         — show status
            /github auth <token>            — set GitHub token
            /github repo <owner/repo>       — set repository
            /github detect                  — auto-detect repo from git remote
            /github prs [state]             — list PRs
            /github pr <num>                — get PR details
            /github pr <num> implement      — get PR context for implementation
            /github issues [state]          — list issues
            /github issue <num>             — get issue details
            /github action [trigger]        — generate GitHub Action template
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()

        if not arg:
            r = self.bridge.github_status()
            if r.get("ok"):
                lines = ["[b]GitHub Automation[/b]", ""]
                lines.append(f"  Token: {'[green]configured[/green]' if r.get('has_token') else '[red]not set[/red]'}")
                lines.append(f"  Repo: {r.get('repo', 'not set')}")
                lines.append("")
                lines.append("  Use [cyan]/github auth <token>[/cyan] to set your token.")
                lines.append("  Use [cyan]/github detect[/cyan] to auto-detect the repo.")
                chat.add_system("\n".join(lines))
            else:
                chat.add_error(f"Error: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "auth":
            if not rest:
                chat.add_system("Usage: /github auth <token>")
            else:
                r = self.bridge.github_set_token(rest)
                if r.get("ok"):
                    chat.add_system("[green]GitHub token set.[/green]")
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "repo":
            if not rest:
                chat.add_system("Usage: /github repo <owner/repo>")
            else:
                r = self.bridge.github_set_repo(rest)
                if r.get("ok"):
                    chat.add_system(f"Repo set to [cyan]{rest}[/cyan].")
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "detect":
            r = self.bridge.github_auto_detect_repo()
            if r.get("ok"):
                chat.add_system(f"Detected repo: [cyan]{r['repo']}[/cyan]")
            else:
                chat.add_error(f"Auto-detect failed: {r.get('error')}")
        elif sub == "prs":
            r = self.bridge.github_list_prs(state=rest or "open")
            if r.get("ok"):
                prs = r.get("prs", [])
                if not prs:
                    chat.add_system("No pull requests found.")
                else:
                    lines = ["[b]Pull Requests[/b]", ""]
                    for pr in prs:
                        draft = " [dim](draft)[/dim]" if pr.get("draft") else ""
                        lines.append(f"  #{pr['number']} {pr['title']}{draft}")
                        lines.append(f"     {pr['head_ref']} → {pr['base_ref']}  by {pr['author']}")
                    chat.add_system("\n".join(lines))
            else:
                chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "pr":
            pr_parts = rest.split(None, 1)
            if not pr_parts:
                chat.add_system("Usage: /github pr <number> [implement]")
            else:
                try:
                    pr_num = int(pr_parts[0])
                except ValueError:
                    chat.add_error(f"Invalid PR number: {pr_parts[0]}")
                    self.query_one(InputBox).focus()
                    return
                if len(pr_parts) > 1 and pr_parts[1].lower() == "implement":
                    r = self.bridge.github_get_pr_context(pr_num)
                    if r.get("ok"):
                        prompt = r.get("implement_prompt", "")
                        chat.add_system(f"[b]PR #{pr_num} Implementation Context[/b]\n\n{prompt[:3000]}")
                    else:
                        chat.add_error(f"Failed: {r.get('error')}")
                else:
                    r = self.bridge.github_get_pr(pr_num)
                    if r.get("ok"):
                        pr = r["pr"]
                        lines = [
                            f"[b]PR #{pr['number']}: {pr['title']}[/b]",
                            f"  State: {pr['state']}  Author: {pr['author']}",
                            f"  Branch: {pr['head_ref']} → {pr['base_ref']}",
                            f"  URL: {pr['url']}",
                            "",
                            f"  {pr.get('body', '(no description)')[:500]}",
                        ]
                        chat.add_system("\n".join(lines))
                    else:
                        chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "issues":
            r = self.bridge.github_list_issues(state=rest or "open")
            if r.get("ok"):
                issues = r.get("issues", [])
                if not issues:
                    chat.add_system("No issues found.")
                else:
                    lines = ["[b]Issues[/b]", ""]
                    for issue in issues:
                        labels = ", ".join(issue.get("labels", []))
                        lines.append(f"  #{issue['number']} {issue['title']}  [{labels}]")
                    chat.add_system("\n".join(lines))
            else:
                chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "issue":
            if not rest:
                chat.add_system("Usage: /github issue <number>")
            else:
                try:
                    issue_num = int(rest)
                except ValueError:
                    chat.add_error(f"Invalid issue number: {rest}")
                    self.query_one(InputBox).focus()
                    return
                r = self.bridge.github_get_issue(issue_num)
                if r.get("ok"):
                    issue = r["issue"]
                    lines = [
                        f"[b]Issue #{issue['number']}: {issue['title']}[/b]",
                        f"  State: {issue['state']}  Author: {issue['author']}",
                        f"  Labels: {', '.join(issue.get('labels', []))}",
                        f"  URL: {issue['url']}",
                        "",
                        f"  {issue.get('body', '(no description)')[:500]}",
                    ]
                    chat.add_system("\n".join(lines))
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "action":
            r = self.bridge.github_generate_action(trigger=rest or "pull_request")
            if r.get("ok"):
                chat.add_system(f"[b]GitHub Action Template[/b]\n\n```yaml\n{r['yaml']}\n```")
            else:
                chat.add_error(f"Failed: {r.get('error')}")
        else:
            chat.add_system(f"Unknown subcommand: {sub}. Use auth|repo|detect|prs|pr|issues|issue|action.")

        self.query_one(InputBox).focus()


    # ── G13: /mcp-server ────────────────────────────────────────────────────

    def _exec_mcp_server(self, arg: str) -> None:
        """MCP Server mode information.

        Usage:
            /mcp-server          — show status and available tools
            /mcp-server start    — show how to start the MCP server
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()

        if arg == "start":
            chat.add_system(
                "[b]MCP Server Mode[/b]\n\n"
                "  Start the MCP server with:\n"
                "    [cyan]tera-pilot-acp --mcp-server --workspace /path/to/project[/cyan]\n\n"
                "  For write access:\n"
                "    [cyan]tera-pilot-acp --mcp-server --allow-writes[/cyan]\n\n"
                "  For custom tools:\n"
                "    [cyan]tera-pilot-acp --mcp-server --tools read_file write_file search_project[/cyan]\n\n"
                "  Other agents can then connect to Tera Pilot as an MCP tool provider."
            )
        else:
            r = self.bridge.mcp_server_status()
            if r.get("ok"):
                tools = r.get("available_tools", 0)
                lines = [
                    "[b]MCP Server Mode[/b]",
                    "",
                    f"  Version: {r.get('version', '?')}",
                    f"  Protocol: {r.get('protocol_version', '?')}",
                    f"  Workspace: {r.get('workspace', '?')}",
                    f"  Write mode: {r.get('allow_writes', False)}",
                    f"  Available tools: {tools}",
                    "",
                    "  Use [cyan]/mcp-server start[/cyan] for launch instructions.",
                ]
                chat.add_system("\n".join(lines))
            else:
                chat.add_error(f"Error: {r.get('error', 'unknown')}")

        self.query_one(InputBox).focus()

    # ── G18: /notify ────────────────────────────────────────────────

    def _exec_notify(self, arg: str) -> None:
        """Manage notification backends.

        Usage:
            /notify                        — show status
            /notify test <backend>         — send test notification
            /notify test-all               — test all backends
            /notify enable <backend>       — enable a backend
            /notify disable <backend>      — disable a backend
            /notify events <backend> <evts> — set events (comma-separated)
            /notify remove <backend>       — remove a backend
            /notify telegram <token> <chat_id> — configure Telegram
            /notify discord <webhook_url>  — configure Discord
            /notify slack <webhook_url>    — configure Slack
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()

        if not arg:
            r = self.bridge.notify_status()
            if r.get("ok"):
                backends = r.get("backends", [])
                lines = ["[b]Notification Backends[/b]  (~/.tera_pilot/notifiers.json)", ""]
                if not backends:
                    lines.append("  No backends configured.")
                    lines.append("")
                    lines.append("  Configure with:")
                    lines.append("    [cyan]/notify telegram <bot_token> <chat_id>[/cyan]")
                    lines.append("    [cyan]/notify discord <webhook_url>[/cyan]")
                    lines.append("    [cyan]/notify slack <webhook_url>[/cyan]")
                else:
                    for b in backends:
                        status = "[green]ON[/green]" if b["enabled"] else "[red]OFF[/red]"
                        events = ", ".join(b.get("events", []))
                        lines.append(f"  {b['name']}  {status}  events: {events}")
                lines.append("")
                lines.append("  Use [cyan]/notify test <backend>[/cyan] to test.")
                chat.add_system("\n".join(lines))
            else:
                chat.add_error(f"Error: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "test-all":
            r = self.bridge.notify_test_all()
            if r.get("ok"):
                results = r.get("results", {})
                lines = ["[b]Test Results[/b]", ""]
                for name, res in results.items():
                    status = "[green]OK[/green]" if res.get("ok") else f"[red]FAIL[/red] {res.get('error', '')}"
                    lines.append(f"  {name}: {status}")
                chat.add_system("\n".join(lines))
            else:
                chat.add_error(f"Error: {r.get('error', 'unknown')}")
        elif sub == "test":
            if not rest:
                chat.add_system("Usage: /notify test <backend_name>")
            else:
                r = self.bridge.notify_test(rest)
                if r.get("ok"):
                    chat.add_system(f"[green]Test notification sent to {rest}.[/green]")
                else:
                    chat.add_error(f"Test failed: {r.get('error', 'unknown')}")
        elif sub == "enable":
            if not rest:
                chat.add_system("Usage: /notify enable <backend_name>")
            else:
                r = self.bridge.notify_set_enabled(rest, True)
                if r.get("ok"):
                    chat.add_system(f"Backend [cyan]{rest}[/cyan] [green]enabled[/green].")
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "disable":
            if not rest:
                chat.add_system("Usage: /notify disable <backend_name>")
            else:
                r = self.bridge.notify_set_enabled(rest, False)
                if r.get("ok"):
                    chat.add_system(f"Backend [cyan]{rest}[/cyan] [red]disabled[/red].")
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "remove":
            if not rest:
                chat.add_system("Usage: /notify remove <backend_name>")
            else:
                r = self.bridge.notify_remove_backend(rest)
                if r.get("ok"):
                    chat.add_system(f"Backend [cyan]{rest}[/cyan] removed.")
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "events":
            ev_parts = rest.split(None, 1)
            if len(ev_parts) < 2:
                chat.add_system("Usage: /notify events <backend> done,error,checkpoint")
            else:
                events = [e.strip() for e in ev_parts[1].split(",")]
                r = self.bridge.notify_set_events(ev_parts[0], events)
                if r.get("ok"):
                    chat.add_system(f"Events for [cyan]{ev_parts[0]}[/cyan] set to: {', '.join(events)}")
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "telegram":
            tg_parts = rest.split()
            if len(tg_parts) < 2:
                chat.add_system("Usage: /notify telegram <bot_token> <chat_id>")
            else:
                config = {
                    "enabled": True,
                    "bot_token": tg_parts[0],
                    "chat_id": tg_parts[1],
                    "events": ["done", "error"],
                }
                r = self.bridge.notify_configure_backend("telegram", config)
                if r.get("ok"):
                    chat.add_system(f"[green]Telegram configured.[/green] Use [cyan]/notify test telegram[/cyan] to verify.")
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "discord":
            if not rest:
                chat.add_system("Usage: /notify discord <webhook_url>")
            else:
                config = {
                    "enabled": True,
                    "webhook_url": rest,
                    "events": ["done", "error"],
                }
                r = self.bridge.notify_configure_backend("discord", config)
                if r.get("ok"):
                    chat.add_system(f"[green]Discord configured.[/green] Use [cyan]/notify test discord[/cyan] to verify.")
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        elif sub == "slack":
            if not rest:
                chat.add_system("Usage: /notify slack <webhook_url>")
            else:
                config = {
                    "enabled": True,
                    "webhook_url": rest,
                    "events": ["done", "error"],
                }
                r = self.bridge.notify_configure_backend("slack", config)
                if r.get("ok"):
                    chat.add_system(f"[green]Slack configured.[/green] Use [cyan]/notify test slack[/cyan] to verify.")
                else:
                    chat.add_error(f"Failed: {r.get('error')}")
        else:
            chat.add_system(f"Unknown subcommand: {sub}. Use test|enable|disable|remove|events|telegram|discord|slack.")

        self.query_one(InputBox).focus()

    # ── G18: /daemon ────────────────────────────────────────────────

    def _exec_daemon(self, arg: str) -> None:
        """Daemon status and information.

        Usage:
            /daemon          — show daemon status
            /daemon start    — show how to start the daemon
        """
        chat = self.query_one(ChatLog)
        arg = arg.strip()

        if arg == "start":
            chat.add_system(
                "[b]Tera Pilot Daemon[/b]\n\n"
                "  Start the daemon server with:\n"
                "    [cyan]tera-pilot-daemon --port 8765 --notify telegram[/cyan]\n\n"
                "  Run a single task with notification:\n"
                "    [cyan]tera-pilot-daemon task \"Refactor auth\" --notify telegram[/cyan]\n\n"
                "  Submit tasks via API:\n"
                "    [cyan]curl -X POST http://localhost:8765/task \\\\[/cyan]\n"
                "    [cyan]  -H \"Authorization: Bearer <token>\" \\\\[/cyan]\n"
                "    [cyan]  -d '{\"prompt\": \"Fix bug #42\"}'[/cyan]\n\n"
                "  Stream task output:\n"
                "    [cyan]curl -N http://localhost:8765/stream/<task_id>[/cyan]\n\n"
                "  Config stored in [cyan]~/.tera_pilot/daemon.json[/cyan]"
            )
        else:
            r = self.bridge.daemon_status()
            if r.get("ok"):
                lines = [
                    "[b]Tera Pilot Daemon[/b]",
                    "",
                    f"  Configured: {'[green]yes[/green]' if r.get('configured') else '[red]no[/red]'}",
                    "",
                    "  Use [cyan]/daemon start[/cyan] for launch instructions.",
                ]
                chat.add_system("\n".join(lines))
            else:
                chat.add_error(f"Error: {r.get('error', 'unknown')}")

        self.query_one(InputBox).focus()

    # ── G19a: /canvas ───────────────────────────────────────────────

    def _exec_canvas(self, arg: str) -> None:
        """Show / reset the task canvas.

        Usage:
            /canvas           — show the current canvas (nodes + counts)
            /canvas reset     — drop every node (start a fresh canvas)
        """
        chat = self.query_one(ChatLog)
        arg = (arg or "").strip()
        if arg == "reset":
            r = self.bridge.reset_task_canvas()
            if r.get("ok"):
                chat.add_system("[green]Task canvas reset.[/green]")
                self._refresh_task_canvas_view()
            else:
                chat.add_error(f"Reset failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        r = self.bridge.get_task_canvas()
        if not r.get("ok"):
            chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        canvas = r.get("canvas", {})
        nodes = canvas.get("nodes", [])
        counts = canvas.get("counts", {})
        total = canvas.get("total", 0)
        lines = ["[b]Task Canvas (G19a)[/b]", ""]
        if not nodes:
            lines.append("  [dim](empty — no nodes yet)[/dim]")
        else:
            lines.append(
                f"  Total: {total} nodes — "
                f"[green]{counts.get('done', 0)} done[/green], "
                f"[#d77757]{counts.get('running', 0)} running[/#d77757], "
                f"[yellow]{counts.get('pending', 0)} pending[/yellow], "
                f"[red]{counts.get('failed', 0)} failed[/red]"
            )
            lines.append("")
            for n in nodes:
                status = n.get("status", "?")
                color = {
                    "done": "green",
                    "running": "#d77757",
                    "pending": "yellow",
                    "failed": "red",
                }.get(status, "white")
                label = n.get("label", "?")
                if len(label) > 60:
                    label = label[:59] + "…"
                line = f"  [{color}][{status}][/{color}] {label}"
                if n.get("model"):
                    line += f" [dim]-> {n['model']}[/dim]"
                if n.get("depends_on"):
                    line += f" [dim](depends: {', '.join(n['depends_on'])})[/dim]"
                lines.append(line)
        chat.add_system("\n".join(lines))
        self._refresh_task_canvas_view()
        self.query_one(InputBox).focus()

    def _refresh_task_canvas_view(self) -> None:
        """Refresh the sidebar TaskCanvasView widget, if present."""
        try:
            view = self.query_one(TaskCanvasView)
            view.refresh_view()
        except Exception:
            pass  # widget not mounted yet — fine

    # ── G19b: /persona ──────────────────────────────────────────────

    def _exec_persona(self, arg: str) -> None:
        """Show / edit / reset the per-user persona profile.

        Usage:
            /persona             — show current persona.md content
            /persona edit        — open $EDITOR on the persona file
            /persona reset       — delete the persona file
            /persona update      — run the maintenance LLM call now
                                   (uses the cheap tier; pass an optional
                                   summary string as the second arg)
        """
        chat = self.query_one(ChatLog)
        arg = (arg or "").strip()
        if arg == "reset":
            r = self.bridge.reset_persona()
            if r.get("ok"):
                chat.add_system("[green]Persona file deleted.[/green]")
            else:
                chat.add_error(f"Reset failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        if arg == "edit":
            import subprocess
            import os
            editor = os.environ.get("EDITOR", "nano")
            path = ""
            r = self.bridge.get_persona()
            if r.get("ok"):
                path = r.get("path", "")
            if not path:
                chat.add_error("No persona path available")
                self.query_one(InputBox).focus()
                return
            chat.add_system(f"Opening [cyan]{path}[/cyan] in {editor}…")
            try:
                subprocess.run([editor, path], check=False)
                chat.add_system("[green]Persona file edited.[/green]")
            except Exception as e:
                chat.add_error(f"Editor failed: {e}")
            self.query_one(InputBox).focus()
            return
        if arg.startswith("update"):
            summary = ""
            parts = arg.split(None, 1)
            if len(parts) > 1:
                summary = parts[1]
            chat.add_system("Running persona maintenance LLM call (cheap tier)…")
            digest_dict = {"summary": summary} if summary else None
            r = self.bridge.update_persona_from_session(digest_dict)
            if r.get("ok"):
                if r.get("unchanged"):
                    chat.add_system(
                        f"[green]Persona unchanged[/green] "
                        f"({r.get('after_chars', 0)} chars). "
                        "LLM decided no updates needed."
                    )
                else:
                    chat.add_system(
                        f"[green]Persona updated[/green]: "
                        f"{r.get('before_chars', 0)} → {r.get('after_chars', 0)} chars "
                        f"(provider: {r.get('provider_id', '?')}/{r.get('model', '?')})"
                    )
            else:
                chat.add_error(f"Update failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        # Default: show
        r = self.bridge.get_persona()
        if not r.get("ok"):
            chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        content = r.get("content", "")
        path = r.get("path", "")
        chars = r.get("chars", 0)
        soft_cap = r.get("soft_cap", 2000)
        lines = [
            "[b]Persona Memory (G19b)[/b]",
            "",
            f"  Path:  [cyan]{path}[/cyan]",
            f"  Size:  {chars} / {soft_cap} chars"
            + (" [yellow](over soft cap)[/yellow]" if chars > soft_cap else ""),
            "",
        ]
        if content:
            lines.append("[b]Content:[/b]")
            lines.append(content)
        else:
            lines.append("[dim](empty — no persona file yet)[/dim]")
            lines.append("")
            lines.append("Use [cyan]/persona update[/cyan] to run the maintenance LLM call")
            lines.append("or [cyan]/persona edit[/cyan] to write one by hand.")
        chat.add_system("\n".join(lines))
        self.query_one(InputBox).focus()

    # ── G20c: /router-mode ──────────────────────────────────────────

    def _exec_router_mode(self, arg: str) -> None:
        """Show / set the AutoRouter mode (single | decompose).

        Usage:
            /router-mode              — show current mode
            /router-mode single       — single-model routing (default)
            /router-mode decompose    — task-decomposition router (G20)
        """
        chat = self.query_one(ChatLog)
        arg = (arg or "").strip()
        if arg in ("single", "decompose"):
            r = self.bridge.set_router_mode(arg)
            if r.get("ok"):
                chat.add_system(
                    f"[green]Router mode set to:[/green] [cyan]{r.get('mode')}[/cyan]"
                )
            else:
                chat.add_error(f"Failed: {r.get('error', 'unknown')}")
            self.query_one(InputBox).focus()
            return
        r = self.bridge.get_router_mode()
        if r.get("ok"):
            mode = r.get("mode", "single")
            lines = [
                "[b]AutoRouter Mode (G20c)[/b]",
                "",
                f"  Current: [cyan]{mode}[/cyan]",
                "",
                "  Modes:",
                "    [cyan]single[/cyan]     — one model for the whole task (default)",
                "    [cyan]decompose[/cyan]  — break into subtasks, route each to the best model",
                "",
                "  Use [cyan]/router-mode single[/cyan] or [cyan]/router-mode decompose[/cyan] to switch.",
            ]
            chat.add_system("\n".join(lines))
        else:
            chat.add_error(f"Failed: {r.get('error', 'unknown')}")
        self.query_one(InputBox).focus()
