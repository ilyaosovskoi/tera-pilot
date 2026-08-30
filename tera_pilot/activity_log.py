"""
Tera Pilot v1.2.0 — Activity Log / Audit Trail

A unified, append-only audit trail of EVERYTHING the agent does on the
user's machine. Every shell command, every file write, every code edit,
every office operation, every subagent spawn, every self-verify call is
recorded here as a structured entry.

The log is:
- thread-safe (entries can be recorded from the agent worker thread
  while the UI reads them on the main thread)
- bounded (last N entries kept in memory — no unbounded growth)
- subscribable (UI gets a callback for every new entry, used to push
  via Qt signals or WebSocket)
- exportable (JSON dump for "what did the agent do?" audits)
- queryable (recent(N, category=..., status=..., search=...) for the
  Activity Stream panel's filters and search box)

Design rules:

1. **One entry per tool execution.** Every tool dispatched by
   `ToolEngine._dispatch` produces exactly one entry — no entry for
   thoughts/plans (those are separate event types in AgentEvent) — but
   we DO record them as category="thought" / "plan" so the Activity
   Stream shows the agent's reasoning interleaved with its actions.

2. **Status is parsed from the tool's return string.** Tools return
   strings like `[WRITTEN] foo.py`, `[REJECTED BY USER] ...`,
   `[SECURITY ERROR] ...`, `[CANCELLED] ...`. The status parser maps
   these to a small enum: `ok` / `error` / `rejected` / `cancelled` /
   `timeout` / `pending`.

3. **Args are sanitised before recording.** Large content fields
   (`content` in `write_file`, `code` in `run_code`, `diff` in
   `apply_diff`) are replaced with a `{len, preview}` summary — the
   full body is never stored in the activity log (it can be huge and
   the file itself is the source of truth). Paths, command strings,
   and small scalar args are kept verbatim.

4. **No PII scrubbing.** The agent runs locally; the log lives in
   memory and is never sent anywhere. If the user wants to share the
   log (e.g. in a bug report), they can use `export_json()` and
   redact manually.

5. **The log is process-scoped, not chat-scoped.** Entries from
   previous chats remain visible until the user clears the log or
   restarts Tera Pilot. This lets the user audit "what did the agent do
   across all my chats today?" without having to switch chats.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Categories ────────────────────────────────────────────────────────
# Each activity entry has exactly one category. The category drives the
# icon and color in the Activity Stream panel, and is filterable.

CATEGORY_SHELL = "shell"
CATEGORY_CODE = "code"
CATEGORY_FILE = "file"
CATEGORY_OFFICE = "office"
CATEGORY_SUBAGENT = "subagent"
CATEGORY_VERIFY = "verify"
CATEGORY_GIT = "git"
CATEGORY_MCP = "mcp"
CATEGORY_SKILL = "skill"
CATEGORY_THOUGHT = "thought"
CATEGORY_PLAN = "plan"
CATEGORY_ERROR = "error"
CATEGORY_INFO = "info"
CATEGORY_OTHER = "other"
# v2.1.0 (G18): web tools get their own category so the Activity Stream
# can show "agent reached outside the project" with a distinct icon.
CATEGORY_WEB = "web"

ALL_CATEGORIES = (
    CATEGORY_SHELL,
    CATEGORY_CODE,
    CATEGORY_FILE,
    CATEGORY_OFFICE,
    CATEGORY_SUBAGENT,
    CATEGORY_VERIFY,
    CATEGORY_GIT,
    CATEGORY_MCP,
    CATEGORY_SKILL,
    CATEGORY_THOUGHT,
    CATEGORY_PLAN,
    CATEGORY_ERROR,
    CATEGORY_INFO,
    CATEGORY_OTHER,
    CATEGORY_WEB,
)


# ── Statuses ──────────────────────────────────────────────────────────

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"
STATUS_PENDING = "pending"

ALL_STATUSES = (
    STATUS_OK,
    STATUS_ERROR,
    STATUS_REJECTED,
    STATUS_CANCELLED,
    STATUS_TIMEOUT,
    STATUS_PENDING,
)


# ── Tool-name → category mapping ─────────────────────────────────────

_TOOL_CATEGORY: Dict[str, str] = {
    "execute_command": CATEGORY_SHELL,
    "run_code": CATEGORY_CODE,
    "write_file": CATEGORY_FILE,
    "str_replace": CATEGORY_FILE,
    "apply_diff": CATEGORY_FILE,
    "read_file": CATEGORY_FILE,
    "list_files": CATEGORY_FILE,
    "get_project_structure": CATEGORY_FILE,
    "delete_file": CATEGORY_FILE,
    "rename_file": CATEGORY_FILE,
    "mkdir": CATEGORY_FILE,
    "read_binary_file": CATEGORY_FILE,
    "write_binary_file": CATEGORY_FILE,
    "file_info": CATEGORY_FILE,
    "undo_write": CATEGORY_FILE,
    "git_status": CATEGORY_GIT,
    "git_diff": CATEGORY_GIT,
    "git_stage": CATEGORY_GIT,
    "git_commit": CATEGORY_GIT,
    "get_skill": CATEGORY_SKILL,
    "call_mcp_tool": CATEGORY_MCP,
    "spawn_subagent": CATEGORY_SUBAGENT,
    "spawn_multi_agents": CATEGORY_SUBAGENT,
    "self_verify": CATEGORY_VERIFY,
    # v2.1.0 (G18): web tools — search and fetch get their own category
    # so the Activity Stream can flag "agent reached outside the project"
    # with a distinct icon. web_search results + web_fetch pages also
    # get wrapped as <context_fragment type="web_*"> by the tool engine
    # so they participate in tombstone-compaction AND are tagged as
    # untrusted external content (see ToolEngine._web_search/_web_fetch).
    "web_search": CATEGORY_WEB,
    "web_fetch": CATEGORY_WEB,
}


def categorize(tool_name: str) -> str:
    """Return the activity category for a given tool name."""
    if not tool_name:
        return CATEGORY_OTHER
    if tool_name.startswith("office_"):
        return CATEGORY_OFFICE
    return _TOOL_CATEGORY.get(tool_name, CATEGORY_OTHER)


# ── Status parser ─────────────────────────────────────────────────────
# Tools return strings like "[WRITTEN] foo.py", "[REJECTED BY USER] ...".
# We parse the leading bracketed tag to derive a status.

_STATUS_PREFIXES: List[tuple] = [
    # (prefix_lower, status)
    ("[written]", STATUS_OK),
    ("[created]", STATUS_OK),
    ("[added", STATUS_OK),       # [ADDED PARAGRAPH], [ADDED HEADING], ...
    ("[set", STATUS_OK),         # [SET CELL], [SET CELL FORMAT]
    ("[filled", STATUS_OK),      # [FILLED TABLE], [FILLED SHEET]
    ("[no output]", STATUS_OK),
    ("[ok]", STATUS_OK),
    ("[structure", STATUS_OK),
    ("[renamed]", STATUS_OK),
    ("[deleted]", STATUS_OK),
    ("[directory created]", STATUS_OK),
    ("[file info]", STATUS_OK),
    ("[saved as]", STATUS_OK),
    ("[found", STATUS_OK),       # [FOUND 3 MATCHES]
    ("[replaced", STATUS_OK),    # [REPLACED 5 OCCURRENCES]
    ("[verify ok", STATUS_OK),
    ("[self_verify ok", STATUS_OK),
    ("[spawned", STATUS_OK),     # [SPAWNED SUBAGENT]
    ("[skill loaded]", STATUS_OK),
    # v2.1.0 (G18): web tool status prefixes.
    ("[web_search]", STATUS_OK),
    ("[web_search no results]", STATUS_OK),
    ("[web_fetch]", STATUS_OK),
    ("[web_fetch empty]", STATUS_OK),
    ("[web_fetch rejected]", STATUS_REJECTED),

    ("[error]", STATUS_ERROR),
    ("[security error]", STATUS_ERROR),
    ("[office error]", STATUS_ERROR),
    ("[command error]", STATUS_ERROR),
    ("[structure error]", STATUS_ERROR),
    ("[str_replace error]", STATUS_ERROR),
    ("[file not found]", STATUS_ERROR),
    ("[read error]", STATUS_ERROR),
    ("[write error]", STATUS_ERROR),
    ("[verify failed", STATUS_ERROR),
    ("[verify error]", STATUS_ERROR),
    # v2.3.10-fix: the tool engine emits many more error-status tags
    # than this list originally covered (e.g. "[TOOL ERROR]", "[GIT ERROR]",
    # "[MCP ERROR]", "[BLOCKED]", "[REFUSED]"). Because parse_status
    # defaults unknown prefixes to STATUS_OK, every one of those FAILED
    # tool calls was recorded in the activity/audit log as "ok" — so the
    # signed audit trail showed green checks for errors. Add the concrete
    # error/rejected tags the engine actually returns so the status is
    # truthful (see agent_runtime/tool_engine/_engine.py, which documents
    # "[TOOL ERROR]" as an error status).
    ("[tool error]", STATUS_ERROR),
    ("[git error]", STATUS_ERROR),
    ("[mcp error]", STATUS_ERROR),
    ("[web_fetch error]", STATUS_ERROR),
    ("[web_search error]", STATUS_ERROR),
    ("[subagent error]", STATUS_ERROR),
    ("[multi-agents error]", STATUS_ERROR),
    ("[skill error]", STATUS_ERROR),
    ("[grep error]", STATUS_ERROR),
    ("[glob error]", STATUS_ERROR),
    ("[search error]", STATUS_ERROR),
    ("[reviewer error]", STATUS_ERROR),
    ("[search_tools error]", STATUS_ERROR),
    ("[select_tools error]", STATUS_ERROR),
    ("[self-verify error]", STATUS_ERROR),
    ("[self-verify failed]", STATUS_ERROR),
    ("[wave timeout]", STATUS_TIMEOUT),
    ("[runtime not found]", STATUS_ERROR),

    ("[rejected by user]", STATUS_REJECTED),
    ("[tool rejected]", STATUS_REJECTED),
    ("[tool denied]", STATUS_REJECTED),
    ("[blocked]", STATUS_REJECTED),
    ("[refused]", STATUS_REJECTED),
    ("[rejected]", STATUS_REJECTED),

    ("[cancelled", STATUS_CANCELLED),
    ("[cancelled by user]", STATUS_CANCELLED),

    ("[timeout]", STATUS_TIMEOUT),
    ("[timed out]", STATUS_TIMEOUT),
]


def parse_status(result: Optional[str]) -> str:
    """Derive a status enum from the leading bracketed tag of `result`."""
    if result is None:
        return STATUS_PENDING
    stripped = result.lstrip()
    if not stripped:
        return STATUS_OK
    lower = stripped.lower()
    for prefix, status in _STATUS_PREFIXES:
        if lower.startswith(prefix):
            return status
    # Default: anything that doesn't match a known prefix is treated
    # as ok — most tool results are short content strings ("Here are
    # the files: ...") that don't start with a bracketed tag.
    return STATUS_OK


# ── Args sanitiser ────────────────────────────────────────────────────
# Some tool args carry very large payloads (file contents, code blocks,
# diffs). We never store the full payload in the activity log — the
# file itself is the source of truth, and the log would balloon to MBs
# in seconds. Instead we record a {len, preview} summary.

_LARGE_ARG_KEYS = {
    "content",      # write_file
    "code",         # run_code
    "diff",         # apply_diff
    "original",     # diff_review payload
    "proposed",     # diff_review payload
    "new_str",      # str_replace
    "old_str",      # str_replace (kept short — but cap to be safe)
    "text",         # office_add_paragraph / office_add_text — keep preview
}

_PREVIEW_LEN = 240


def _sanitise_arg(key: str, value: Any) -> Any:
    """Return a sanitised copy of an arg value for activity log storage."""
    if value is None:
        return None
    # Strings: cap long ones, but keep paths/commands/names verbatim
    if isinstance(value, str):
        if key in _LARGE_ARG_KEYS:
            return {
                "_summary": True,
                "len": len(value),
                "preview": value[:_PREVIEW_LEN],
            }
        if len(value) > 800:
            return {
                "_summary": True,
                "len": len(value),
                "preview": value[:_PREVIEW_LEN],
            }
        return value
    # Lists: recurse, but cap length
    if isinstance(value, list):
        capped = value[:20]
        return [_sanitise_arg(key, v) for v in capped] + (
            [{"_truncated": True, "hidden": len(value) - 20}] if len(value) > 20 else []
        )
    # Dicts: recurse.
    #
    # v2.3.10-fix: pass the ORIGINAL top-level ``key`` down, not the
    # nested dict's own key (``k``). Only top-level args named
    # ``content``/``diff``/``code``/... are the tool's large payload, and
    # only those should be collapsed to a summary. Previously a *nested*
    # field that happened to be named ``content`` (or ``diff``) — e.g.
    # ``{"meta": {"diff": ...}}`` or ``{"response_format": {"content": ...}}``
    # — was silently summarized even though it is ordinary metadata, so
    # the activity log dropped data that should have been kept.
    if isinstance(value, dict):
        return {k: _sanitise_arg(key, v) for k, v in value.items()}
    # Scalars (int, bool, float): pass through
    return value


def sanitise_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitise a tool's args dict for activity log storage."""
    if not args:
        return {}
    return {k: _sanitise_arg(k, v) for k, v in args.items()}


# ── Title builder ─────────────────────────────────────────────────────
# A human-readable one-liner for each entry, shown in the Activity
# Stream's main row. Falls back to the tool name if no specific title
# applies.

def build_title(tool: str, args: Dict[str, Any]) -> str:
    """Build a one-line human-readable title for a tool call."""
    if not tool:
        return "Activity"
    path = args.get("path")
    command = args.get("command")
    code_lang = args.get("language")

    if tool == "execute_command":
        return f"Run: {command}" if command else "Run command"
    if tool == "run_code":
        return f"Run {code_lang or 'python'} code"
    if tool == "write_file":
        return f"Write {path}" if path else "Write file"
    if tool == "str_replace":
        return f"Edit {path}" if path else "Edit file"
    if tool == "apply_diff":
        return f"Apply diff: {path}" if path else "Apply diff"
    if tool == "read_file":
        return f"Read {path}" if path else "Read file"
    if tool == "list_files":
        d = args.get("directory", ".")
        return f"List {d}"
    if tool == "get_project_structure":
        d = args.get("directory", ".")
        return f"Structure of {d}"
    if tool == "delete_file":
        return f"Delete {path}" if path else "Delete file"
    if tool == "rename_file":
        return f"Rename {args.get('old_path')} → {args.get('new_path')}"
    if tool == "mkdir":
        return f"Make dir {path}" if path else "Make directory"
    if tool == "read_binary_file":
        return f"Read binary {path}" if path else "Read binary file"
    if tool == "write_binary_file":
        return f"Write binary {path}" if path else "Write binary file"
    if tool == "file_info":
        return f"Info: {path}" if path else "File info"
    if tool == "undo_write":
        return f"Undo write {path}" if path else "Undo write"
    if tool == "git_status":
        return "Git status"
    if tool == "git_diff":
        return f"Git diff ({'staged' if args.get('staged') else 'unstaged'})"
    if tool == "git_stage":
        return f"Stage {len(args.get('paths', []))} path(s)"
    if tool == "git_commit":
        msg = args.get("message", "")
        return f"Commit: {msg[:60]}"
    if tool == "get_skill":
        return f"Load skill: {args.get('id', '?')}"
    if tool == "call_mcp_tool":
        return f"MCP: {args.get('server', '?')}.{args.get('tool', '?')}"
    if tool == "spawn_subagent":
        goal = args.get("goal", "")
        role = args.get("role", "generalist")
        return f"Spawn {role} subagent — {goal[:80]}"
    if tool == "spawn_multi_agents":
        tasks = args.get("tasks") or []
        return f"Spawn {len(tasks)} subagents in parallel"
    if tool == "self_verify":
        return "Self-verify (closing review)"
    # v2.1.0 (G18): web tool titles.
    if tool == "web_search":
        q = args.get("query", "")
        return f"Web search: {q[:80]}" if q else "Web search"
    if tool == "web_fetch":
        u = args.get("url", "")
        return f"Web fetch: {u[:80]}" if u else "Web fetch"
    # Office tools — uniform prefix
    if tool.startswith("office_"):
        method = tool[len("office_"):]
        if path:
            return f"Office · {method} · {path}"
        return f"Office · {method}"
    return tool


# ── ActivityEntry ─────────────────────────────────────────────────────

def _new_id() -> str:
    return "evt_" + uuid.uuid4().hex[:10]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_entry(
    *,
    category: str,
    kind: str,
    tool: Optional[str] = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    status: str = STATUS_OK,
    args: Optional[Dict[str, Any]] = None,
    result_preview: Optional[str] = None,
    duration_ms: Optional[int] = None,
    section: Optional[str] = None,
    chat_id: Optional[str] = None,
    iteration: Optional[int] = None,
    path: Optional[str] = None,
    command: Optional[str] = None,
    diff_stat: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a single activity log entry dict.

    This is the canonical shape of an activity record. Both the
    ActivityLog.record() method and the bridge's activity_recorded
    signal use this shape.
    """
    safe_args = sanitise_args(args or {})
    if title is None:
        title = build_title(tool or "", args or {})
    return {
        "id": _new_id(),
        "ts": time.time(),
        "ts_iso": _now_iso(),
        "category": category,
        "kind": kind,
        "tool": tool or "",
        "title": title,
        "summary": summary or "",
        "status": status,
        "args": safe_args,
        "result_preview": (result_preview or "")[:600],
        "duration_ms": duration_ms,
        "section": section,
        "chat_id": chat_id,
        "iteration": iteration,
        "path": path,
        "command": command,
        "diff_stat": diff_stat,
        "meta": meta or {},
    }


# ── ActivityLog ───────────────────────────────────────────────────────

class ActivityLog:
    """Bounded, thread-safe, subscribable audit trail.

    The log keeps the last `max_entries` records in memory. Listeners
    are called on every new entry — they receive a single dict (the
    entry) and MUST be thread-safe (the log does not marshal the call
    back to any specific thread).

    Typical usage:

        log = ActivityLog(max_entries=2000)
        log.subscribe(lambda entry: my_signal.emit(entry))
        ...
        entry_id = log.record(category="shell", kind="command", ...)
    """

    def __init__(self, max_entries: int = 2000):
        self._entries: Deque[Dict[str, Any]] = deque(maxlen=max_entries)
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._lock = threading.RLock()
        self._counter_by_category: Dict[str, int] = {}
        self._counter_by_status: Dict[str, int] = {}

    # ── Recording ──

    def record(self, **kwargs) -> str:
        """Record a new activity entry.

        Accepts the same keyword arguments as `make_entry()`. If
        `category`, `kind` are missing they default to "info".

        Returns the entry's id.
        """
        entry = make_entry(**kwargs)
        with self._lock:
            self._entries.append(entry)
            cat = entry.get("category") or CATEGORY_INFO
            st = entry.get("status") or STATUS_OK
            self._counter_by_category[cat] = self._counter_by_category.get(cat, 0) + 1
            self._counter_by_status[st] = self._counter_by_status.get(st, 0) + 1
            listeners = list(self._listeners)
        # Call listeners OUTSIDE the lock so a slow listener can't
        # block the agent thread.
        for cb in listeners:
            try:
                cb(entry)
            except Exception as e:
                logger.warning("[activity] listener error: %s", e)
        return entry["id"]

    def record_tool_call(
        self,
        *,
        tool: str,
        args: Dict[str, Any],
        result: Optional[str],
        duration_ms: Optional[int] = None,
        section: Optional[str] = None,
        chat_id: Optional[str] = None,
        iteration: Optional[int] = None,
    ) -> str:
        """Convenience wrapper for recording a completed tool call.

        Derives category, status, title, path, command, diff_stat
        automatically from the tool name and result string.
        """
        category = categorize(tool)
        status = parse_status(result)
        title = build_title(tool, args)

        # Extract path / command / diff_stat from args/result
        path = args.get("path")
        command = args.get("command")
        diff_stat = None
        if tool in ("write_file", "str_replace", "apply_diff") and isinstance(result, str):
            # Try to extract a "diff stat" from the result line, if any
            # (write_file returns "[WRITTEN] path (N chars)" — no diff;
            # str_replace returns "[REPLACED] path (3 occurrences)").
            # For apply_diff we look at the diff itself.
            if tool == "apply_diff":
                diff_body = args.get("diff", "")
                if isinstance(diff_body, str):
                    added = sum(1 for l in diff_body.splitlines()
                                if l.startswith("+") and not l.startswith("+++"))
                    removed = sum(1 for l in diff_body.splitlines()
                                  if l.startswith("-") and not l.startswith("---"))
                    diff_stat = f"+{added} -{removed}"

        summary = ""
        if isinstance(result, str):
            # First non-empty line, capped
            first_line = result.split("\n", 1)[0].strip()
            summary = first_line[:160]

        return self.record(
            category=category,
            kind=tool,
            tool=tool,
            title=title,
            summary=summary,
            status=status,
            args=args,
            result_preview=result if isinstance(result, str) else None,
            duration_ms=duration_ms,
            section=section,
            chat_id=chat_id,
            iteration=iteration,
            path=path,
            command=command,
            diff_stat=diff_stat,
        )

    # ── Subscribing ──

    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        """Subscribe to new entries. Returns an unsubscribe callable."""
        with self._lock:
            self._listeners.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(callback)
                except ValueError:
                    pass

        return _unsubscribe

    # ── Querying ──

    def recent(
        self,
        n: int = 200,
        category: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return up to `n` recent entries, optionally filtered.

        Filters are AND-combined. `search` matches (case-insensitive)
        against title, summary, tool, path, and command.
        """
        with self._lock:
            entries = list(self._entries)
        # Newest first for filtering, then we reverse for display
        entries.reverse()
        out: List[Dict[str, Any]] = []
        search_lower = (search or "").lower().strip() if search else None
        for e in entries:
            if category and e.get("category") != category:
                continue
            if status and e.get("status") != status:
                continue
            if kind and e.get("kind") != kind:
                continue
            if search_lower:
                haystack = " ".join(
                    str(e.get(k, "") or "")
                    for k in ("title", "summary", "tool", "path", "command")
                ).lower()
                if search_lower not in haystack:
                    continue
            out.append(e)
            if len(out) >= n:
                break
        # Reverse back to chronological order (oldest first) for display
        out.reverse()
        return out

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """Return a single entry by id, or None if not found."""
        with self._lock:
            for e in self._entries:
                if e.get("id") == entry_id:
                    return e
        return None

    def stats(self) -> Dict[str, Any]:
        """Return counts by category and status, plus total."""
        with self._lock:
            total = len(self._entries)
            by_cat = dict(self._counter_by_category)
            by_st = dict(self._counter_by_status)
        return {
            "total": total,
            "by_category": by_cat,
            "by_status": by_st,
        }

    # ── Mutations ──

    def clear(self) -> int:
        """Clear all entries. Returns the number of entries removed."""
        with self._lock:
            n = len(self._entries)
            self._entries.clear()
            self._counter_by_category.clear()
            self._counter_by_status.clear()
            listeners = list(self._listeners)
        # Notify listeners of the clear so the UI can empty its view
        clear_event = make_entry(
            category=CATEGORY_INFO,
            kind="log_cleared",
            title="Activity log cleared",
            summary=f"{n} entries removed",
            status=STATUS_OK,
        )
        for cb in listeners:
            try:
                cb(clear_event)
            except Exception:
                pass
        return n

    # ── Export ──

    def export_json(self) -> str:
        """Return the full log as a JSON string."""
        import json
        with self._lock:
            entries = list(self._entries)
        return json.dumps(entries, indent=2, default=str)

    # v2.1.0 (G16): signed/chained export. Additive — the existing
    # export_json() above is unchanged and remains the default for
    # callers that don't need tamper-evidence. The new method delegates
    # to tera_pilot.audit_signing, which uses a local Ed25519 keypair stored
    # under ~/.tera_pilot/ (same directory convention as everything else) and
    # hash-chains entries so tampering/reordering/deletion is detectable.
    # The signed format is fully backward-compatible: each entry in the
    # output is the original entry dict plus three underscore-prefixed
    # fields (_signature, _hash, _prev_hash) that existing readers
    # ignore (they only add fields, never remove or rename).
    def export_signed_json(self) -> str:
        """Return the full log as a signed + hash-chained JSON string.

        Each entry gets an Ed25519 signature over its canonical payload
        + the previous entry's hash. The hash chain makes tampering,
        reordering, and deletion all detectable. Verify with
        ``tera_pilot.audit_signing.verify_signed_json`` or the
        ``/audit verify <file>`` slash command.

        Raises ``tera_pilot.audit_signing.AuditSigningError`` only if the
        cryptography backend is unavailable — in that case callers
        should fall back to ``export_json()``.
        """
        from tera_pilot.audit_signing import export_signed_json as _export_signed
        with self._lock:
            entries = list(self._entries)
        return _export_signed(entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ── Process-wide singleton ────────────────────────────────────────────
# One log per Tera Pilot process. Created lazily on first access. Used by
# the bridge, the agent runtime, and the API server so they all share
# the same audit trail.

_GLOBAL_LOG: Optional[ActivityLog] = None
_GLOBAL_LOCK = threading.Lock()


def get_activity_log() -> ActivityLog:
    """Return the process-wide ActivityLog singleton."""
    global _GLOBAL_LOG
    if _GLOBAL_LOG is None:
        with _GLOBAL_LOCK:
            if _GLOBAL_LOG is None:
                _GLOBAL_LOG = ActivityLog(max_entries=2000)
    return _GLOBAL_LOG
