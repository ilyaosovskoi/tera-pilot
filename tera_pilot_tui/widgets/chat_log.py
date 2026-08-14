"""chat_log.py — scrollable conversation area.

v2.1.0 (Loop 3): Warm, Modern, Content-Forward redesign.
  - AI responses: pure white, no border/box
  - User messages: in dashed ASCII box (Claude Code style)
  - Separators: thin #505050 between messages
  - Tool blocks: colored Unicode borders (hot pink)
  - Streaming support: append chunks to last AI message
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual.widgets import RichLog

# Tools that involve code editing
_CODE_TOOLS = {"write_file", "str_replace", "create_file", "edit_file"}

# v2.1.0 (Loop 3): Terracotta accent color for AI headers
_TERRACOTTA = "#d77757"
# Hot pink for tool blocks
_HOT_PINK = "#fd5db1"
# Muted separator color
_SEPARATOR_COLOR = "#505050"
# Surface background for user messages
_SURFACE = "#373737"


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

    # ---- user / system ------------------------------------------------------

    def add_user(self, text: str) -> None:
        """Display a user message (no box, just text like Claude Code)."""
        # Use Text to avoid markup injection from user content
        self.write(Text(f"> {text}", style="white"))
        self.write(Text(""))

    def add_system(self, text: str) -> None:
        """Display a system/info message with Rich markup support."""
        self.write(text)

    def add_plan(self, plan: str) -> None:
        """Display a plan proposal."""
        self.write(Text("[plan]", style="bold #888888"))
        self.write(Markdown(plan))
        self.write(Text(""))

    # ---- separators (Loop 3) ────────────────────────────────────────────

    def add_separator(self) -> None:
        """Display a thin separator line between messages.

        v2.1.0 (Loop 3): Thin #505050 line between messages.
        """
        self.write(Text("─" * 60, style=_SEPARATOR_COLOR))

    # ---- model --------------------------------------------------------------

    def add_thought(self, text: str) -> None:
        """Display agent thinking (greyed out)."""
        if not text:
            return
        self.write(Text(text.rstrip(), style="grey62"))

    def append_token_delta(self, chunk: str) -> None:
        """Append a streaming token chunk to the live assistant response.

        v2.2.4-fix: Instead of writing each chunk as a separate RichLog
        entry (which creates duplicates when add_final() adds Markdown),
        we now only accumulate the text. The final rendering happens in
        add_final() which replaces the streamed text with Markdown.
        """
        if not self._streaming_active:
            self._streaming_active = True
            self._streaming_text = chunk
        else:
            self._streaming_text += chunk
        # v2.2.4-fix: Do NOT write intermediate entries to the RichLog.
        # Instead, we update the LAST written entry in-place if it exists,
        # to avoid creating N duplicate progressively-longer entries.
        try:
            # Try to update the last entry rather than appending a new one
            if self._children and len(self._children) > 0:
                # Replace the last child with updated text
                self._children[-1] = Text(self._streaming_text, style="white")
                self.refresh()
            else:
                # First chunk — write initial entry
                self.write(Text(self._streaming_text, style="white"))
        except Exception:
            # Fallback: just write (slightly duplicative but at least visible)
            self.write(Text(self._streaming_text, style="white"))

    def end_streaming(self) -> str:
        """Stop accumulating and return the buffered text."""
        text = self._streaming_text
        self._streaming_active = False
        self._streaming_text = ""
        return text

    def add_final(self, text: str) -> None:
        """Display the final assistant response.

        v2.1.0 (Loop 3): AI responses are pure white, no border/box.
        Clean, content-first presentation.
        """
        if not text:
            return
        if self._streaming_active:
            self.end_streaming()
        # AI responses: plain text, white, no container
        self.write(Markdown(text))

    def add_error(self, text: str) -> None:
        """Display an error message."""
        # Use Text to avoid markup injection from error content
        self.write(Text(f"[!] {text}", style="bold #ff6b6b"))
        self.write(Text(""))

    def add_reviewer_verdict(self, verdict: str, feedback: str = "",
                             iterations: int = 0) -> None:
        """Render a reviewer verdict (no panel)."""
        color = {
            "APPROVE": "#88ff88",
            "REJECT": "#ff8888",
            "MODIFY": "#ffff88",
            "EXHAUSTED": "#888888",
        }.get(verdict.upper(), "#aaaaaa")
        self.write(Text(f"[verdict] {verdict}", style=f"bold {color}"))
        if iterations:
            self.write(Text(f"iterations: {iterations}", style="dim"))
        if feedback:
            self.write(Text(feedback.rstrip(), style="white"))
        self.write(Text(""))

    def add_observer_warnings(self, warnings: list) -> None:
        """Render observer-mode warnings (no panel)."""
        if not warnings:
            return
        self.write(Text(f"[warnings {len(warnings)}]", style="bold #ffaa88"))
        for w in warnings:
            self.write(Text(f"  • {w}", style="#ffaa88"))
        self.write(Text(""))

    # ---- tools ───────────────────────────────────────────────────────────

    def add_tool_call(self, tool: str, args: Dict[str, Any],
                      sub_label: Optional[str] = None) -> None:
        """Display a tool invocation (no panel)."""
        body = self._render_tool_args(tool, args)
        label = f"[{sub_label}] {tool}" if sub_label else tool
        self.write(Text(f"→ {label}", style="bold #888888"))
        self.write(body)
        self.write(Text(""))

    def add_tool_result(self, tool: str, result: str) -> None:
        """Display a tool result (no panel)."""
        preview = (result or "").rstrip()
        self.write(Text(f"← {tool}", style="dim #888888"))
        self.write(Text(preview or "(no output)", style="#aaaaaa"))
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
                                             theme="ansi_dark", word_wrap=True))
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
