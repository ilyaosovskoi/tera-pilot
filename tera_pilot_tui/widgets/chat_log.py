"""chat_log.py — scrollable conversation area.

v2.1.0 (Loop 3): Warm, Modern, Content-Forward redesign.
  - AI responses: pure white, no border/box
  - User messages: in dashed ASCII box
  - Separators: thin #505050 between messages
  - Tool blocks: colored Unicode borders (hot pink)
  - Streaming support: append chunks to last AI message
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual.geometry import Size
from textual.widgets import RichLog

# Tools that involve code editing
_CODE_TOOLS = {"write_file", "str_replace", "create_file", "edit_file"}

# v2.3.6-fix (rendering garbage): model-generated text can carry ANSI
# escape sequences / control characters (some models "colorize" their
# thinking). Written verbatim into a RichLog they render as stray
# fragments — e.g. isolated "u"-looking glyphs from half-consumed CSI
# sequences — and the user mistakes them for a model bug. Strip them
# before rendering; the TUI's own styles (grey thoughts, white answers)
# stay the source of color.
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OTHER = re.compile(r"\x1b[@-Z\\-_]")
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_display_text(text: str) -> str:
    """Strip ANSI escapes and stray control characters from model text."""
    if not text:
        return text
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OTHER.sub("", text)
    return _CTRL_CHARS.sub("", text)

# v2.1.0 (Loop 3): Terracotta accent color for AI headers
_TERRACOTTA = "#d77757"
# Hot pink for tool blocks
_HOT_PINK = "#fd5db1"
# Muted separator color
_SEPARATOR_COLOR = "#505050"
# Surface background for user messages
_SURFACE = "#373737"

# ── Per-theme palettes (dark / light) ──────────────────────────────
# v2.4.2 (theme fix): Rich inline styles are NOT repainted by
# reload_css() — before this map, the whole conversation body was
# hard-coded to dark-palette colors ("white" text, #aaaaaa results,
# #505050 separators), which became unreadable on the light theme.
# Mirrors the InfoBox.set_theme() pattern: the app calls
# ChatLog.set_theme() on every theme switch.
_PALETTES = {
    True: {   # dark
        "body": "white",
        "thought": "grey62",
        "result": "#aaaaaa",
        "label": "#888888",
        "separator": "#505050",
        "accent": "#d77757",
        "exec": "#fd5db1",
        "read": "#b1b9f9",
        "edit": "#e8b34b",
        "verify": "#4eba65",
        "error": "bold #ff6b6b",
        "warning": "#ffaa88",
        "warning_bold": "bold #ffaa88",
        "approve": "#88ff88",
        "reject": "#ff8888",
        "modify": "#ffff88",
        "unknown": "#aaaaaa",
        "code_theme": "ansi_dark",
    },
    False: {  # light
        "body": "#1c1c22",
        "thought": "#5a5a64",
        "result": "#55555e",
        "label": "#6e6e78",
        "separator": "#c9c9d2",
        "accent": "#b34d2e",
        "exec": "#a4138c",
        "read": "#4a54c4",
        "edit": "#9a6700",
        "verify": "#1a7f37",
        "error": "bold #d1242f",
        "warning": "#9a6700",
        "warning_bold": "bold #9a6700",
        "approve": "#1a7f37",
        "reject": "#d1242f",
        "modify": "#9a6700",
        "unknown": "#55555e",
        "code_theme": "ansi_light",
    },
}

#: Tool families → header accent (mirrors ToolBlock border hues so the
#: chat rows and any future ToolBlock render agree).
_TOOL_FAMILY = (
    (frozenset({"execute_command", "run_code", "task_spawn", "task_stop",
                "repl_run", "worktree_add", "worktree_remove"}), "exec"),
    (frozenset({"read_file", "read_binary_file", "list_files", "search_project",
                "grep", "glob", "get_project_structure", "file_info",
                "git_status", "git_diff", "code_symbols", "lsp_definition",
                "lsp_references", "lsp_symbols", "web_fetch", "web_search"}), "read"),
    (frozenset({"write_file", "str_replace", "apply_diff", "delete_file",
                "rename_file", "mkdir", "git_stage", "git_commit"}), "edit"),
    (frozenset({"self_verify", "watchdog_check", "todo_write"}), "verify"),
)

#: Tool results longer than this are truncated in the chat (the full
#: text stays in the Activity log) so one pytest dump can't bury the
#: conversation.
_RESULT_PREVIEW_LIMIT = 3000


def _tool_family(tool: str) -> str:
    for family, key in _TOOL_FAMILY:
        if tool in family:
            return key
    return "label"


def truncate_result_preview(text: str, limit: int = _RESULT_PREVIEW_LIMIT) -> str:
    """Cap a tool result for chat display (pure helper, unit-tested)."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… [{len(text) - limit:,} more chars — full output in Activity]"


class ChatLog(RichLog):
    """Scrollable chat area with support for streaming, tools, and markdown.

    v2.1.0 (Loop 3): Warm, modern, content-forward redesign.
    AI responses are plain white with no border. User messages are
    in a dashed ASCII box. Tool blocks have colored Unicode borders.

    v2.2.4-fix: markup=True so that Rich markup tags in system
    messages (e.g. [b]bold[/b]) are rendered correctly. User content
    is always passed as Text/markdown objects to avoid injection.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(highlight=True, markup=True, wrap=True, **kwargs)
        self._streaming_text: str = ""
        self._streaming_active: bool = False
        # v2.4.2: active theme (True = dark). Set by the app on mount and
        # whenever the user switches theme, so the body palette matches.
        self._dark: bool = True
        # v2.3.1-fix: line count before the in-progress streaming entry was
        # written. Used to roll the entry back and re-render it in place so
        # token chunks never pile up as N duplicate log entries (the old
        # code poked RichLog._children which does not exist on Textual 8.x,
        # so every chunk fell through to a fresh self.write() — a stream of
        # N chunks produced N progressively-longer duplicate lines).
        self._stream_baseline: Optional[int] = None
        self._stream_write_scheduled: bool = False

    # ---- theme ------------------------------------------------------------

    def set_theme(self, dark: bool) -> None:
        """v2.4.2: switch the body palette (called by the app on mount
        and on every theme change)."""
        self._dark = bool(dark)

    @property
    def dark(self) -> bool:
        return self._dark

    @property
    def _pal(self) -> Dict[str, str]:
        return _PALETTES[bool(self._dark)]

    # ---- user / system ------------------------------------------------------

    def add_user(self, text: str) -> None:
        """Display a user message: accent marker + plain body text."""
        # Use Text to avoid markup injection from user content.
        # v2.4.3: animate=True gives new messages a smooth scroll glide
        # instead of an instant jump (minimal motion language).
        # v2.5.0: the marker carries the theme accent so user turns are
        # scannable at a glance in a long log.
        self.write(Text.assemble(
            ("❯ ", f"bold {self._pal['accent']}"),
            (text, self._pal["body"]),
        ), animate=True)
        self.write(Text(""))

    def add_system(self, text: str) -> None:
        """Display a system/info message with Rich markup support."""
        self.write(text, animate=True)

    def add_plan(self, plan: str) -> None:
        """Display a plan proposal."""
        self.write(Text("[plan]", style=f"bold {self._pal['label']}"), animate=True)
        self.write(Markdown(clean_display_text(plan)), animate=True)
        self.write(Text(""))

    # ---- separators (Loop 3) ────────────────────────────────────────────

    def add_separator(self) -> None:
        """Display a thin separator line between messages.

        v2.1.0 (Loop 3): Thin separator line between messages.
        v2.4.2: color follows the active theme palette.
        """
        self.write(Text("─" * 60, style=self._pal["separator"]))

    # ---- model --------------------------------------------------------------

    def add_thought(self, text: str) -> None:
        """Display agent thinking (greyed out, italic)."""
        if not text:
            return
        self.write(Text(clean_display_text(text).rstrip(),
                        style=f"italic {self._pal['thought']}"))

    def append_token_delta(self, chunk: str) -> None:
        """Append a streaming token chunk to the live assistant response.

        v2.3.1-fix: only ONE live entry exists in the log at any moment.
        The first chunk records the line count before the entry (the
        "baseline") and writes it; every later chunk rolls the entry back
        to the baseline and re-writes the accumulated text, so a stream of
        N chunks renders as a single growing line instead of N duplicates.

        If the log width is not known yet (writes are deferred until the
        first resize), we only accumulate the text — the final rendering
        happens in add_final(), which replaces the streamed text with the
        Markdown-rendered answer.
        """
        if not self._streaming_active:
            self._streaming_active = True
            self._streaming_text = chunk
            if self._size_known:
                self._stream_baseline = len(self.lines)
                self.write(Text(clean_display_text(self._streaming_text), style=self._pal["body"]))
        else:
            self._streaming_text += chunk
            if self._size_known:
                self._rollback_stream_entry()
                self.write(Text(clean_display_text(self._streaming_text), style=self._pal["body"]))

    def _rollback_stream_entry(self) -> None:
        """Remove the in-progress streaming entry from the log.

        Truncates ``self.lines`` back to the recorded baseline so the next
        write re-renders the streamed text in place. Safe no-op when no
        streaming entry was written (e.g. writes were deferred because the
        widget size was unknown).
        """
        if self._stream_baseline is None:
            return
        try:
            del self.lines[self._stream_baseline:]
        except Exception:
            return
        self._start_line = min(self._start_line, len(self.lines))
        self._line_cache.clear()
        self.virtual_size = Size(self._widest_line_width, len(self.lines))
        self.refresh()

    def end_streaming(self) -> str:
        """Stop accumulating and return the buffered text.

        The streamed entry (if any) is left in the log as plain text — use
        ``add_final()`` to replace it with the Markdown-rendered answer, or
        ``abort_streaming()`` to discard it on error/interrupt.
        """
        text = self._streaming_text
        self._streaming_active = False
        self._streaming_text = ""
        self._stream_baseline = None
        return text

    def abort_streaming(self) -> str:
        """Discard any partial streamed text (error / interrupt paths).

        Rolls the in-progress streaming entry out of the log so a failed or
        interrupted run does not leave half-written plain text above the
        error message. Returns the discarded text for callers that want it.
        """
        if not self._streaming_active:
            return ""
        self._rollback_stream_entry()
        return self.end_streaming()

    def add_final(self, text: str) -> None:
        """Display the final assistant response.

        v2.1.0 (Loop 3): AI responses are pure white, no border/box.
        Clean, content-first presentation.

        v2.3.1-fix: if a live streaming entry exists, it is rolled back
        first so the Markdown-rendered answer REPLACES the plain streamed
        text instead of duplicating it.
        """
        if not text:
            return
        if self._streaming_active:
            self._rollback_stream_entry()
            self.end_streaming()
        # AI responses: plain text, white, no container
        # v2.3.1: smooth scroll so the final answer glides into view.
        self.write(Markdown(clean_display_text(text)), animate=True)

    def add_error(self, text: str) -> None:
        """Display an error message."""
        # Use Text to avoid markup injection from error content
        self.write(Text(f"[!] {text}", style=self._pal["error"]))
        self.write(Text(""))

    def add_reviewer_verdict(self, verdict: str, feedback: str = "",
                             iterations: int = 0) -> None:
        """Render a reviewer verdict (no panel)."""
        pal = self._pal
        color = {
            "APPROVE": pal["approve"],
            "REJECT": pal["reject"],
            "MODIFY": pal["modify"],
            "EXHAUSTED": pal["label"],
        }.get(verdict.upper(), pal["unknown"])
        self.write(Text(f"[verdict] {verdict}", style=f"bold {color}"))
        if iterations:
            self.write(Text(f"iterations: {iterations}", style="dim"))
        if feedback:
            self.write(Text(feedback.rstrip(), style=pal["body"]))
        self.write(Text(""))

    def add_observer_warnings(self, warnings: list) -> None:
        """Render observer-mode warnings (no panel)."""
        if not warnings:
            return
        self.write(Text(f"[warnings {len(warnings)}]", style=self._pal["warning_bold"]))
        for w in warnings:
            self.write(Text(f"  • {w}", style=self._pal["warning"]))
        self.write(Text(""))

    # ---- tools ───────────────────────────────────────────────────────────

    def add_tool_call(self, tool: str, args: Dict[str, Any],
                       sub_label: Optional[str] = None) -> None:
        """Display a tool invocation: family-colored header + args."""
        body = self._render_tool_args(tool, args)
        label = f"[{sub_label}] {tool}" if sub_label else tool
        color = self._pal[_tool_family(tool)]
        self.write(Text(f"→ {label}", style=f"bold {color}"))
        self.write(body)
        self.write(Text(""))

    def add_tool_result(self, tool: str, result: str) -> None:
        """Display a tool result (truncated past the preview limit)."""
        preview = truncate_result_preview(
            clean_display_text(result or "").rstrip())
        self.write(Text(f"← {tool}", style=f"dim {self._pal['label']}"))
        self.write(Text(preview or "(no output)", style=self._pal["result"]))
        self.write(Text(""))

    def _render_tool_args(self, tool: str, args: Dict[str, Any]):
        args = args or {}
        if tool in _CODE_TOOLS:
            content = (
                args.get("content")
                or args.get("new_str")
                or args.get("new_string")
                or args.get("replacement")
            )
            path = args.get("path") or args.get("file_path") or ""
            if isinstance(content, str) and content:
                lexer = _guess_lexer(path)
                header = Text(f"{path}\n", style="bold")
                return _Group(header, Syntax(content, lexer,
                                             theme=self._pal["code_theme"],
                                             word_wrap=True))
        # Fallback: compact key: value listing
        # Since RichLog was created with markup=False, we must use
        # Text objects instead of markup strings to avoid Rich trying
        # to interpret square brackets as markup tags (which causes
        # "Text markup error" when args contain literal [brackets]).
        lines = []
        for k, v in args.items():
            sv = str(v)
            if len(sv) > 500:
                sv = sv[:500] + " ..."
            lines.append(Text.assemble(
                (f"{k}", "bold"),
                (": ", ""),
                (sv, ""),
            ))
        if not lines:
            return Text("(no args)")
        return Text("\n").join(lines)


def _guess_lexer(path: str) -> str:
    p = (path or "").lower()
    for ext, lexer in (
        (".py", "python"), (".rs", "rust"), (".js", "javascript"),
        (".ts", "typescript"), (".json", "json"), (".md", "markdown"),
        (".sh", "bash"), (".toml", "toml"), (".yaml", "yaml"), (".yml", "yaml"),
        (".html", "html"), (".css", "css"), (".go", "go"),
    ):
        if p.endswith(ext):
            return lexer
    return "text"


class _Group:
    """Minimal renderable group for stacking renderables."""

    def __init__(self, *renderables: Any) -> None:
        self._renderables = renderables

    def __rich_console__(self, console, options):
        for r in self._renderables:
            yield r
