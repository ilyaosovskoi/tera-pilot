"""
OutputParser — parses LLM output into structured tool calls.

Handles:
- extracting balanced-JSON tool calls from the model's reply,
- decoding escape sequences while preserving Unicode,
- lifting top-level args (some models emit {tool, args} with
  args as siblings instead of nested),
- detecting "final answer" tokens,
- extracting the model's thought before the tool call,
- detecting write intent (so the runtime can ask for diff
  review before writing).

_warn_unknown_tools() emits a logger.warning for any tool name
in the plan that isn't in ToolName.
"""

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .types import ToolCall, ToolName, AgentEvent

logger = logging.getLogger(__name__)


class OutputParser:
    """Parse JSON-based tool calls instead of fragile XML regex.

    Includes self-correction for common malformed-JSON patterns that small
    local models produce.
    """

    TOOL_ARG_HINTS = {
        "read_file":              ["path"],
        "write_file":             ["path", "content"],
        "str_replace":            ["path", "old_str", "new_str", "replace_all"],
        "apply_diff":             ["path", "diff"],
        "run_code":               ["code", "language"],
        "search_project":         ["query", "directory", "file_pattern"],
        "list_files":             ["directory", "pattern"],
        "execute_command":        ["command"],
        "get_project_structure":  ["directory"],
        "delete_file":            ["path"],
        "rename_file":            ["old_path", "new_path"],
        "mkdir":                  ["path"],
        "read_binary_file":       ["path"],
        "write_binary_file":      ["path", "content"],
        "file_info":              ["path"],
        "undo_write":             ["path"],
        # v1.0.11: git + skill tools
        "git_status":             [],
        "git_diff":               ["staged", "path"],
        "git_stage":              ["paths"],
        "git_commit":             ["message", "paths"],
        "get_skill":              ["id"],
        # v1.1.0: MCP + multi-agent tools
        "call_mcp_tool":          ["server", "tool", "args"],
        "spawn_subagent":         ["goal", "role", "max_iterations"],
        "spawn_multi_agents":     ["tasks"],
        # v1.2.0: Office Worker tools — listed here so the parser's
        # last-resort _extract_args_by_name fallback works for these
        # tools too (when small models emit slightly malformed JSON).
        "office_create":          ["path", "template"],
        "office_view":            ["path", "mode"],
        "office_add_paragraph":   ["path", "text", "style", "bold", "italic", "color", "size"],
        "office_add_heading":     ["path", "text", "level"],
        "office_add_table":       ["path", "rows", "cols", "data", "header"],
        "office_fill_table":      ["path", "table_index", "data"],
        "office_add_sheet":       ["path", "name"],
        "office_set_cell":        ["path", "sheet", "cell", "value"],
        "office_set_cell_format": ["path", "sheet", "cell", "bold", "italic",
                                   "font_color", "bg_color", "font_size", "align"],
        "office_add_chart":       ["path", "sheet", "chart_type", "data_range",
                                   "anchor", "title"],
        "office_fill_sheet":      ["path", "sheet", "data", "start_cell"],
        "office_add_slide":       ["path", "layout", "title", "subtitle"],
        "office_add_text":        ["path", "slide", "text", "x", "y", "w", "h",
                                   "bold", "italic", "color", "size", "align"],
        "office_add_shape":       ["path", "slide", "shape_type", "x", "y", "w", "h",
                                   "text", "fill_color", "line_color"],
        "office_find_replace":    ["path", "find", "replace", "sheet", "slide"],
        "office_save_as":         ["path", "new_path"],
        # v1.2.0: self_verify
        "self_verify":            ["goal", "touched_files", "mode", "run_tests"],
        # v1.2.1-fix (review §4.2): watchdog probe
        "watchdog_check":         [],
        # v1.2.1-fix (review §4.4): agentic-search tools
        "grep":                   ["pattern", "path", "include", "max_results", "case_sensitive"],
        "glob":                   ["pattern", "path", "max_results"],
        # v1.2.1-fix (review §4.5): MCP lazy-loading
        "list_mcp_tools":         ["offset", "limit"],
    }

    @classmethod
    def tool_calls_from_native(cls, native_calls: List[Dict[str, Any]]) -> List[ToolCall]:
        """Convert API-level native ``tool_calls`` into ToolCall objects.

        v2.3.5-fix (small-model support): the native-history loop keeps
        the conversation as real OpenAI-style messages (assistant
        ``tool_calls`` + ``role="tool"`` results) instead of embedding
        everything in one user prompt — small models trained on native
        tool calling (e.g. LFM 2.6B) follow their plan correctly in
        that format, while they degenerate into a ``read_file`` loop
        when observations are buried in prose. This converter turns the
        API's native tool_calls into the ToolCall objects the runtime
        executes, preserving each call's ``id`` for the matching
        ``tool_call_id`` in the follow-up tool message.
        """
        out: List[ToolCall] = []
        for tc in native_calls or []:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            if not name:
                continue
            try:
                args = json.loads(fn.get("arguments", "{}") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            try:
                call = ToolCall(name=ToolName(name), args=args)
            except (ValueError, KeyError):
                logger.warning(f"[parser] native tool name lookup failed: {name}")
                continue
            call.id = tc.get("id")
            out.append(call)
        return out

    # ── v2.3.5-fix: LFM-native text tool calls ──────────────────────
    # The LFM 2.5 chat template emits tool calls as
    # ``<|tool_call_start|>[name(arg1='v1', arg2='v2')]<|tool_call_end|>``
    # (Python-style args, single-quoted strings with \' escapes). These
    # helpers parse that format so the runtime can execute the model's
    # plan WITHOUT advertising the ``tools`` schema — LM Studio's engine
    # rejects generated native tool calls whose content contains quotes
    # ("Invalid diff", server_error 500), which the text path bypasses.

    _LFM_START = "<|tool_call_start|>"
    _LFM_END = "<|tool_call_end|>"

    @classmethod
    def _iter_lfm_calls(cls, text: str):
        """Yield ``(name, args_dict)`` for every LFM-native block, in order."""
        pos = 0
        while True:
            start = text.find(cls._LFM_START, pos)
            if start == -1:
                return
            end = text.find(cls._LFM_END, start)
            if end == -1:
                return
            body = text[start + len(cls._LFM_START):end].strip()
            if body.startswith("[") and body.endswith("]"):
                body = body[1:-1].strip()
            parsed = cls._parse_lfm_call(body)
            if parsed is not None:
                yield parsed
            pos = end + len(cls._LFM_END)

    @classmethod
    def _parse_lfm_call(cls, body: str):
        """Parse ``name(arg1='v1', arg2='v2')`` → (name, args dict).

        Never raises; returns None for unparseable bodies. Args may be
        named (``key=value``) or positional (bare values), matching the
        loose output small LFM models produce."""
        open_paren = body.find("(")
        if open_paren == -1:
            name = body.strip()
            return (name, {}) if name and name.isidentifier() else None
        name = body[:open_paren].strip()
        inner = body[open_paren + 1:]
        # Find the matching closing paren (balanced, string-aware).
        depth = 1
        close = -1
        in_str = None
        i = 0
        while i < len(inner):
            ch = inner[i]
            if in_str is not None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            elif ch in ("'", '"'):
                in_str = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
            i += 1
        if close == -1:
            return None
        args = cls._parse_lfm_args(inner[:close], name)
        return (name, args)

    @classmethod
    def _parse_lfm_args(cls, args_text: str, tool_name: str) -> dict:
        """Split ``key='v', 'positional'`` into an args dict.

        Named args keep their key; positional values are mapped to the
        tool's parameter order (``_TOOL_ARGS``), falling back to
        ``arg0/arg1`` for unknown tools."""
        parts = cls._split_lfm_args(args_text)
        ordered = cls.TOOL_ARG_HINTS.get(tool_name, [])
        args: dict = {}
        used = set()
        positional_i = 0
        for part in parts:
            eq = part.find("=")
            if eq != -1 and part[:eq].strip().isidentifier():
                key = part[:eq].strip()
                value = cls._parse_lfm_value(part[eq + 1:].strip())
                if value is not None:
                    args[key] = value
                    used.add(key)
            else:
                value = cls._parse_lfm_value(part)
                if value is None:
                    continue
                # Assign a positional value to the FIRST parameter slot
                # not already filled by a named arg (the model often
                # mixes ``write_file(path='x.py', 'content...')`` — the
                # positional content must land on ``content``, never
                # overwrite ``path``).
                while positional_i < len(ordered) and ordered[positional_i] in used:
                    positional_i += 1
                if positional_i < len(ordered):
                    args[ordered[positional_i]] = value
                    used.add(ordered[positional_i])
                    positional_i += 1
                else:
                    args[f"arg{positional_i}"] = value
                    positional_i += 1
        return args

    @staticmethod
    def _parse_lfm_value(raw: str):
        """Parse one LFM arg value: '...' / "..." / bare token.

        Returns None for empty/unknown forms so the caller can skip.
        """
        raw = raw.strip()
        if not raw:
            return None
        if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
            quote = raw[0]
            inner = raw[1:-1]
            out = []
            i = 0
            while i < len(inner):
                ch = inner[i]
                if ch == "\\" and i + 1 < len(inner):
                    nxt = inner[i + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", "\\": "\\"}.get(nxt, nxt))
                    i += 2
                else:
                    out.append(ch)
                    i += 1
            return "".join(out)
        # Bare token: number / true / false / identifier.
        low = raw.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in ("none", "null"):
            return None
        try:
            if raw.count(".") == 1:
                return float(raw)
            return int(raw)
        except ValueError:
            pass
        return raw

    @classmethod
    def _split_lfm_args(cls, args_text: str) -> List[str]:
        """Split on top-level commas (string- and paren-aware)."""
        parts: List[str] = []
        cur: List[str] = []
        in_str = None
        depth = 0
        i = 0
        while i < len(args_text):
            ch = args_text[i]
            if in_str is not None:
                cur.append(ch)
                if ch == "\\" and i + 1 < len(args_text):
                    cur.append(args_text[i + 1])
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            elif ch in ("'", '"'):
                in_str = ch
                cur.append(ch)
            elif ch == "(":
                depth += 1
                cur.append(ch)
            elif ch == ")":
                depth -= 1
                cur.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
            i += 1
        if cur:
            parts.append("".join(cur).strip())
        return [p for p in parts if p]

    @classmethod
    def _tool_call_from_parts(cls, name: str, args: dict) -> Optional[ToolCall]:
        """Build a ToolCall from a parsed LFM (name, args) pair."""
        # v2.3.10-fix: ``final_answer`` is the terminal marker, not a real
        # tool. It's handled by ``is_final`` / ``parse_final_answer`` (it
        # may be the response's only LFM block and then finalizes the run).
        # Returning None here — without the confusing "Tool name lookup
        # failed" warning — lets a response that mixes a real call with a
        # trailing final_answer still execute the call and drop the marker.
        if name == "final_answer":
            return None
        try:
            if name.startswith("mcp__") or name == "list_mcp_tools":
                return ToolCall(name=name, args=args or {})
            return ToolCall(name=ToolName(name), args=args or {})
        except (ValueError, KeyError) as e:
            logger.warning(f"[parser] Tool name lookup failed: {e}")
            return None

    @classmethod
    def parse_tool_call(cls, text: str) -> Optional[ToolCall]:
        """Extract the FIRST JSON tool call from text, with self-correction.

        v1.0.5-correctness: the old regex used ``[^{}]*`` and ``[^}]*``
        which exclude braces — so any tool call whose ``args`` contained
        a brace inside a string value (e.g.
        ``{"tool": "write_file", "args": {"path": "x.py",
        "content": "d = {'a': 1}"}}``) failed to match. We now extract
        the JSON object with a brace-balanced scan that ignores braces
        inside string literals, then try ``json.loads`` on the result
        (BUGS_REPORT M-RT-10).
        """
        cleaned = cls._strip_code_fence(text)

        # v1.0.5-correctness: brace-balanced scan that respects string
        # literals. Finds the first ``{...}`` block that contains
        # ``"tool"`` and is brace-balanced (ignoring braces inside
        # string literals). This handles tool calls whose args contain
        # braces in string values (e.g. Python dict literals in
        # `content`).
        raw = cls._extract_balanced_json(cleaned)
        if raw is None:
            # Fall back to the old simple regex for non-JSON cases.
            match = re.search(r'\{.*"tool"\s*:\s*"([^"]+)".*\}', cleaned, re.DOTALL)
            if not match:
                return None
            raw = match.group(0)
        return cls._tool_call_from_json(raw)

    @classmethod
    def parse_tool_calls(cls, text: str) -> List[ToolCall]:
        """Extract EVERY JSON tool call from the model response, in order.

        v2.3.5-fix (small-model support): small but capable agent models
        (e.g. LFM 2.5 2.6B) emit their whole PLAN as several tool-call
        JSON blocks in a single response — read file A, read file B,
        then write file C. The runtime previously executed only the
        FIRST call and silently dropped the rest, so the model saw its
        other calls "not happen" and repeated them forever (the
        degenerate ``read_file`` loop observed with LFM 2.6B). Parsing
        every call lets the runtime execute the model's plan in order.

        Consecutive identical calls (same tool + same args) are
        deduplicated, so a model that repeats ``read_file x.py`` three
        times in one response only reads it once.
        """
        cleaned = cls._strip_code_fence(text)
        calls: List[ToolCall] = []
        last_key: Optional[Tuple] = None
        # v2.3.5-fix (LM Studio / LFM 2.5): the model ALSO emits tool
        # calls in its NATIVE text format ``<|tool_call_start|>[name(arg=
        # 'value')]<|tool_call_end|>`` — the LFM chat template injects
        # those tokens even when no ``tools`` schema is advertised. LM
        # Studio's engine VALIDATES generated native tool calls and
        # hard-400s quote-heavy content ("Invalid diff"), so Tera Pilot
        # deliberately stops advertising tools to LM Studio and parses
        # this text format instead. If any LFM blocks are present they
        # win (the model's whole plan is in them); otherwise fall back
        # to the JSON paths below.
        lfm_found = False
        for _name, _args in cls._iter_lfm_calls(cleaned):
            lfm_found = True
            call = cls._tool_call_from_parts(_name, _args)
            if call is None:
                continue
            _n = call.name.value if isinstance(call.name, ToolName) else call.name
            key = (_n, json.dumps(call.args, sort_keys=True, ensure_ascii=False))
            if key == last_key:
                continue
            last_key = key
            calls.append(call)
        if lfm_found:
            return calls
        for raw, _end in cls._iter_balanced_json(cleaned):
            call = cls._tool_call_from_json(raw)
            if call is None:
                continue
            name = call.name.value if isinstance(call.name, ToolName) else call.name
            key = (name, json.dumps(call.args, sort_keys=True, ensure_ascii=False))
            if key == last_key:
                continue
            last_key = key
            calls.append(call)
        if not calls:
            single = cls.parse_tool_call(cleaned)
            if single is not None:
                calls.append(single)
        return calls

    @classmethod
    def _tool_call_from_json(cls, raw: str) -> Optional[ToolCall]:
        """Build a ToolCall from one raw JSON block, with self-correction.

        Shared by parse_tool_call / parse_tool_calls so single- and
        multi-call parsing apply the exact same recovery logic:
        safe JSON load, last-resort arg extraction by name, top-level
        arg lifting, and the dynamic MCP-tool name pass-through.
        """
        data = cls._safe_json(raw)
        if data is None:
            # _safe_json failed — try extracting tool name + args by name
            # as a last resort instead of repeating the same call.
            tool_name_match = re.search(r'"tool"\s*:\s*"([^"]+)"', raw)
            if not tool_name_match:
                logger.warning(f"[parser] No tool name in: {raw[:200]}")
                return None
            tool_name_str = tool_name_match.group(1)
            args = cls._extract_args_by_name(raw, tool_name_str)
            if args is None:
                return None
            data = {"tool": tool_name_str, "args": args}

        if "args" not in data or not isinstance(data["args"], dict):
            tool_name_str = data.get("tool", "")
            lifted = cls._lift_top_level_args(raw, tool_name_str)
            if lifted:
                data["args"] = lifted

        try:
            tool_name_str = data.get("tool", "")
            args = data.get("args", {}) or {}
            # v1.2.1-fix (review §4.5): typed MCP tools (``mcp__*``)
            # and ``list_mcp_tools`` are NOT in the ToolName enum —
            # they're dynamically discovered at runtime. We accept
            # them by storing the raw string in ToolCall.name (which
            # is typed as Union[ToolName, str] in practice — _dispatch
            # handles both). This avoids forcing every MCP tool name
            # into the enum (which would require re-importing MCPManager
            # at module load time, creating a circular dependency).
            if tool_name_str.startswith("mcp__") or tool_name_str == "list_mcp_tools":
                return ToolCall(name=tool_name_str, args=args)
            name = ToolName(tool_name_str)
            return ToolCall(name=name, args=args)
        except (ValueError, KeyError) as e:
            # v2.3.10-fix: ``final_answer`` (JSON: no ``tool`` key) and the
            # empty tool-name case are terminal markers the runtime handles
            # via is_final / parse_final_answer — they aren't a real tool
            # and don't warrant an error log line for every final answer.
            if tool_name_str not in ("", "final_answer"):
                logger.warning(f"[parser] Tool name lookup failed: {e}")
            return None

    @classmethod
    def _iter_balanced_json(cls, text: str):
        """Yield ``(raw, end_index)`` for every balanced ``{...}`` with ``"tool"``.

        Scans forward through *text*, yielding each brace-balanced block
        that contains the key ``"tool"`` (ignoring braces inside string
        literals), in order of appearance. Scanning resumes AFTER the
        previously yielded block, so nested ``"tool"`` keys inside an
        already-matched block (e.g. the inner tool of ``call_mcp_tool``'s
        ``args``) are never matched as separate calls.
        """
        marker = '"tool"'
        search_from = 0
        n = len(text)
        while True:
            idx = text.find(marker, search_from)
            if idx == -1:
                return
            # Walk backwards from idx to find the enclosing opening `{`.
            open_idx = -1
            depth = 0
            in_str: Optional[str] = None
            j = idx - 1
            while j >= 0:
                ch = text[j]
                if in_str is not None:
                    if ch == in_str:
                        in_str = None
                    j -= 1
                    continue
                if ch in ('"', "'"):
                    in_str = ch
                    j -= 1
                    continue
                if ch == '}':
                    depth += 1
                elif ch == '{':
                    if depth == 0:
                        open_idx = j
                        break
                    depth -= 1
                j -= 1
            if open_idx < 0:
                search_from = idx + len(marker)
                continue
            # Walk forwards from open_idx to find the matching `}`.
            depth = 0
            in_str = None
            i = open_idx
            while i < n:
                ch = text[i]
                if in_str is not None:
                    if ch == '\\':
                        i += 2
                        continue
                    if ch == in_str:
                        in_str = None
                    i += 1
                    continue
                if ch in ('"', "'"):
                    in_str = ch
                    i += 1
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        yield text[open_idx:i + 1], i + 1
                        search_from = i + 1
                        break
                i += 1
            else:
                # Unbalanced — try the next `"tool"` occurrence.
                search_from = idx + len(marker)

    @classmethod
    def _extract_balanced_json(cls, text: str) -> Optional[str]:
        """Find the first brace-balanced ``{...}`` block containing ``"tool"``.

        Ignores braces inside string literals (single and double quoted,
        with backslash escapes). Returns the raw substring (including
        the outer braces) or ``None`` if no balanced block with
        ``"tool"`` is found.

        v1.0.5-perf: O(n) instead of O(n²). We first locate every
        occurrence of ``"tool"`` in the text, then for each we walk
        backwards to find the enclosing ``{`` and forwards to find the
        matching ``}`` (respecting string literals). The old code
        restarted a full forward scan from every ``{`` in the text,
        which was O(n²) and noticeably slow on long model responses.
        """
        n = len(text)
        # Find every occurrence of `"tool"` — these are cheap to locate
        # and almost always few (typically exactly one).
        search_from = 0
        tool_marker = '"tool"'
        marker_len = len(tool_marker)
        while True:
            idx = text.find(tool_marker, search_from)
            if idx == -1:
                return None
            # Walk backwards from idx to find the enclosing opening `{`.
            # We track string literals so braces inside strings don't
            # confuse the depth count.
            open_idx = -1
            depth = 0
            in_str: Optional[str] = None
            j = idx - 1
            while j >= 0:
                ch = text[j]
                # Note: when walking backwards, escape detection is
                # approximate (we'd need to count preceding backslashes
                # to know if a quote is escaped). For tool-call parsing
                # this is good enough — the model rarely emits escaped
                # quotes before a `"tool"` key.
                if in_str is not None:
                    if ch == in_str:
                        in_str = None
                    j -= 1
                    continue
                if ch in ('"', "'"):
                    in_str = ch
                    j -= 1
                    continue
                if ch == '}':
                    depth += 1
                elif ch == '{':
                    if depth == 0:
                        open_idx = j
                        break
                    depth -= 1
                j -= 1
            if open_idx < 0:
                search_from = idx + marker_len
                continue
            # Walk forwards from open_idx to find the matching `}`.
            depth = 0
            in_str = None
            i = open_idx
            while i < n:
                ch = text[i]
                if in_str is not None:
                    if ch == '\\':
                        i += 2
                        continue
                    if ch == in_str:
                        in_str = None
                    i += 1
                    continue
                if ch in ('"', "'"):
                    in_str = ch
                    i += 1
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return text[open_idx:i + 1]
                i += 1
            # Unbalanced — try the next `"tool"` occurrence.
            search_from = idx + marker_len
        return None

    @classmethod
    def _strip_code_fence(cls, text: str) -> str:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            return text.replace(m.group(0), m.group(1))
        return text

    @classmethod
    def _safe_json(cls, raw: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        try:
            return json.loads(raw.replace("'", '"'))
        except json.JSONDecodeError:
            pass
        try:
            cleaned = re.sub(r',\s*([}\]])', r'\1', raw)
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.debug(f"[parser] JSON gave up: {e}")
            return None

    @classmethod
    def _lift_top_level_args(cls, raw: str, tool_name: str) -> Dict[str, Any]:
        hints = cls.TOOL_ARG_HINTS.get(tool_name)
        if not hints:
            return {}
        data = cls._safe_json(raw)
        if not data:
            return {}
        lifted = {}
        for k in hints:
            if k in data and k not in ("tool", "args"):
                lifted[k] = data[k]
        return lifted

    @classmethod
    def _extract_args_by_name(cls, raw: str,
                              tool_name: str) -> Optional[Dict[str, Any]]:
        """Last-resort arg extractor for malformed JSON tool calls.

        v1.0.5-correctness: the old implementation did
        ``m.group(1).encode("utf-8").decode("unicode_escape")`` which is
        a classic Python footgun — it encodes the str to UTF-8 bytes,
        then decodes those bytes as Latin-1 + unicode-escape. For any
        non-ASCII content this mangles the string: ``"你好"`` (6 UTF-8
        bytes) becomes ``"ä½ å¥½"`` (6 Latin-1 chars). Even
        ``codecs.decode(s, 'unicode_escape')`` has the same problem
        because it internally encodes the str to bytes first.

        The correct fix is a manual escape-sequence decoder that only
        transforms ``\\n``, ``\\"``, ``\\\\``, ``\\uXXXX`` etc. and
        leaves existing Unicode characters (CJK, emoji, etc.) untouched
        (BUGS_REPORT H-RT-6).

        v1.1.3-fix (bug 1.6): the non-string regex matched only the
        first non-comma/non-space char, so for ``"tasks": [{"goal": "x"}]``
        it captured ``[`` and then ``int("[")`` / ``float("[")`` raised
        ValueError — the except clause then stored the literal string
        ``"["`` in args. Downstream code saw ``tasks="["`` and returned
        a confusing "tasks must be a non-empty list" error. We now try
        JSON parsing on the captured token first (handles true/false/
        null/numbers), and skip the key entirely if the token is not a
        valid JSON literal (e.g. it's the start of an array/object).
        """
        hints = cls.TOOL_ARG_HINTS.get(tool_name)
        if not hints:
            return {}
        args: Dict[str, Any] = {}
        for key in hints:
            m = re.search(
                rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL
            )
            if m:
                # v1.0.5-correctness: decode only JSON/Python escape
                # sequences, preserving existing Unicode chars.
                raw_val = m.group(1)
                args[key] = cls._decode_escapes_preserving_unicode(raw_val)
                continue
            # v1.1.3-fix (bug 1.6): try to capture the full non-string
            # value (up to the next comma at the same brace level, or
            # the closing brace). The old regex ``([^",\s]+)`` only
            # captured one char for arrays/objects.
            m = re.search(rf'"{key}"\s*:\s*([^",\s]+)', raw)
            if m:
                val = m.group(1).rstrip(",}")
                # v1.1.3-fix (bug 1.6): only accept valid JSON literals
                # (true/false/null/number). Anything else (like ``[`` or
                # ``{``) means the value is a complex type we can't
                # extract with this regex — skip the key rather than
                # storing garbage.
                if val.lower() in ("true", "false", "null"):
                    if val.lower() == "true":
                        args[key] = True
                    elif val.lower() == "false":
                        args[key] = False
                    else:
                        args[key] = None
                    continue
                # Try int, then float. If both fail, skip (don't store
                # the raw string — that's what caused bug 1.6).
                try:
                    args[key] = int(val)
                    continue
                except ValueError:
                    pass
                try:
                    args[key] = float(val)
                    continue
                except ValueError:
                    pass
                # v1.1.3-fix (bug 1.6): not a valid JSON literal — skip
                # this key entirely. Storing the raw string caused
                # downstream type errors (e.g. ``tasks="["``).
                logger.debug(
                    "[parser] _extract_args_by_name: skipping key %r — "
                    "value %r is not a valid JSON literal", key, val,
                )
                continue
        return args if args else None

    @staticmethod
    def _decode_escapes_preserving_unicode(s: str) -> str:
        """Decode escape sequences in *s* without mangling Unicode.

        Handles: ``\\n``, ``\\r``, ``\\t``, ``\\b``, ``\\f``, ``\\"``,
        ``\\\\``, ``\\/``, ``\\uXXXX``. Leaves all other characters
        (including CJK and emoji) untouched.
        """
        out: List[str] = []
        i = 0
        n = len(s)
        simple_escapes = {
            'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
            '"': '"', '\\': '\\', '/': '/', "'": "'",
        }
        while i < n:
            ch = s[i]
            if ch == '\\' and i + 1 < n:
                nxt = s[i + 1]
                if nxt in simple_escapes:
                    out.append(simple_escapes[nxt])
                    i += 2
                    continue
                if nxt == 'u' and i + 5 < n:
                    hex_str = s[i + 2:i + 6]
                    try:
                        code = int(hex_str, 16)
                        out.append(chr(code))
                        i += 6
                        continue
                    except ValueError:
                        pass
                if nxt == 'U' and i + 9 < n:
                    hex_str = s[i + 2:i + 10]
                    try:
                        code = int(hex_str, 16)
                        out.append(chr(code))
                        i += 10
                        continue
                    except ValueError:
                        pass
                # Unknown escape — keep the backslash and the next char.
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            i += 1
        return ''.join(out)

    @classmethod
    def _lfm_final_answer(cls, text: str) -> Optional[str]:
        """Extract the terminal answer when ``text`` is an LFM-native
        ``final_answer(...)`` block (v2.3.10-fix).

        The LFM 2.5 chat template (LM Studio) emits the terminal in the
        same ``<|tool_call_start|>[name(args)]<|tool_call_end|>`` text
        format as its tool calls, so ``parse_final_answer`` (which looks
        for ``{"final_answer": ...}``) misses it: the runtime then saw the
        model answer with no tool call AND no recognized final answer and
        reported ``final_output`` empty ("no content returned").

        To stay consistent with the JSON path (where a response carrying a
        tool call AND a final_answer finalizes immediately), we only treat
        the LFM block as terminal when it is the response's SOLE block. If
        real tool calls precede it, the runtime executes them first and
        waits for a dedicated terminal turn instead of dropping the work.
        """
        blocks = list(cls._iter_lfm_calls(text or ""))
        if len(blocks) != 1:
            return None
        name, args = blocks[0]
        if name != "final_answer":
            return None
        parts = [v for v in args.values() if isinstance(v, str)]
        if not parts:
            return None
        joined = "\n".join(parts).strip()
        return joined or None

    @classmethod
    def parse_final_answer(cls, text: str) -> Optional[str]:
        """Extract the ``final_answer`` field from a model response.

        v1.0.5-correctness: the old regex used a non-greedy ``(.*?)``
        which stopped at the first ``"`` — so for input
        ``{"final_answer": "He said \\"hi\\" to me"}`` it returned just
        ``"He said \\"``, silently truncating the answer. The new
        implementation tries proper JSON parsing first, and only falls
        back to regex on JSON that won't parse (BUGS_REPORT H-RT-5).
        """
        # Try to find a JSON object containing "final_answer" and parse
        # it properly — this handles escaped quotes, nested objects, etc.
        # Search for the outermost {...} that contains final_answer.
        for candidate in re.finditer(r'\{[^{}]*"final_answer"[^{}]*\}', text, re.DOTALL):
            try:
                data = json.loads(candidate.group(0))
                if isinstance(data, dict) and "final_answer" in data:
                    val = data["final_answer"]
                    if isinstance(val, str):
                        return val.strip()
                    return str(val).strip()
            except json.JSONDecodeError:
                continue
        # Fallback: extract the value with an escape-aware regex.
        # ``"(?:[^"\\]|\\.)*"`` matches a JSON-style string literal
        # including escaped quotes.
        m = re.search(
            r'"final_answer"\s*:\s*"((?:[^"\\]|\\.)*)"',
            text, re.DOTALL,
        )
        if m:
            # Unescape JSON string escapes (\n, \", \\, \uXXXX, etc.).
            raw_val = m.group(1)
            try:
                return json.loads(f'"{raw_val}"')
            except json.JSONDecodeError:
                return raw_val.strip()
        # v2.3.10-fix: the LFM-native text form `final_answer('...')`.
        lfm_text = cls._lfm_final_answer(text)
        if lfm_text is not None:
            return lfm_text
        return None

    @classmethod
    def is_final(cls, text: str) -> bool:
        if '"final_answer"' in text:
            return True
        # v2.3.10-fix: LFM-native text terminal.
        return cls._lfm_final_answer(text) is not None

    @classmethod
    def extract_thought(cls, text: str) -> str:
        """Extract the 'thought' portion of a model response.

        v1.0.5-hotfix: when the model's response is short prose with no
        JSON (the "no tool call" failure mode), the old code returned
        the full text — which is correct. But when the response starts
        with ``{`` (pure JSON, no prose preamble), the old code returned
        an empty string. We now return the full text in that case too,
        so the UI has something to show. The thought is only trimmed
        when there's actual prose before the JSON.
        """
        json_start = text.find("{")
        if json_start > 0:
            return text[:json_start].strip()
        # No JSON, or JSON at position 0 — return the full text.
        return text.strip()

    # ── v1.0.5: [WRITE_FILE] special-token parsing ─────────────────
    # The token tells the runtime "the next tool call is a file write
    # targeting <path>" so the UI can pre-fetch the original content
    # for diff review and warm up the project-tree watcher. The tool
    # call itself is still a normal JSON object — the token is a hint,
    # not a substitute for the call.
    _WRITE_TOKEN_RE = re.compile(
        r"^\s*\[WRITE_FILE\]\s*(\S+)\s*$",
        re.MULTILINE,
    )

    @classmethod
    def extract_write_intent(cls, text: str) -> Optional[Tuple[str, str]]:
        """Return (path, raw_token_line) if the model emitted a
        ``[WRITE_FILE] <path>`` token anywhere in the response, else None.

        The runtime uses this to:
          * pre-load the original file content for diff review
          * emit a TOOL_CALLED event with the target path before the
            agent thread blocks on the write
          * detect mismatches (token path != tool-arg path) and warn
        """
        m = cls._WRITE_TOKEN_RE.search(text)
        if not m:
            return None
        return (m.group(1), m.group(0).strip())

    @classmethod
    def strip_write_token(cls, text: str) -> str:
        """Remove the [WRITE_FILE] line so the remaining text can be
        parsed cleanly for the JSON tool call."""
        return cls._WRITE_TOKEN_RE.sub("", text)


# ── Agent Runtime ────────────────────────────────────────────────────────

EventCallback = Callable[[AgentEvent, Dict[str, Any]], None]


def _warn_unknown_tools(plan: str) -> None:
    """Log a warning if the plan references tool names that don't exist
    in ToolName (M-RT-8). This doesn't block execution — it just helps
    the developer notice when the model hallucinates tools."""
    valid_tools = {t.value for t in ToolName}
    # Match patterns like "use the X tool" or "call X" or "X tool"
    for word in re.findall(r'\b([a-z_]+)\b', plan.lower()):
        if word in ("the", "a", "an", "to", "for", "and", "or", "with",
                     "use", "call", "tool", "step", "file", "then",
                     "via", "using", "run", "check"):
            continue
        if word in valid_tools:
            continue
        # Only warn on words that look like tool names (underscored, or
        # common tool-like suffixes)
        if "_" in word or word.endswith("_file") or word.endswith("_code"):
            if word not in valid_tools:
                logger.debug("[agent] plan references unknown tool-like word: %s", word)


