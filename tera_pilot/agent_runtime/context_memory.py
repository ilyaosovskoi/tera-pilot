"""
ContextMemory — sliding-window conversation memory with persistence.

Wraps the message list and provides:
- token / char budget trimming,
- LLM-driven compaction (compact() replaces old messages with a
  summary while keeping the last N),
- JSON-file and SQLite persistence,
- prompt-history serialisation.

SQLite persistence is optional and used by tera_pilot.session subpackage
for long-term session storage.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .types import ConversationMessage

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English/code,
    ~2 chars per token for CJK. We use a blended 3.5 chars/token
    approximation which is close enough for budgeting purposes
    without pulling in a full tokenizer."""
    if not text:
        return 0
    # Count CJK characters (they tokenize denser)
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or
              '\u0400' <= c <= '\u04ff')  # also Cyrillic
    non_cjk = len(text) - cjk
    return (cjk // 2) + (non_cjk // 4) + 1


class ContextMemory:
    """Sliding-window conversation memory with JSON save/load.

    v1.0.9: now tracks token estimates (not just char count) and supports
    explicit compaction via the /compact command. Auto-compaction kicks
    in when the context approaches the configured token budget, so the
    agent doesn't silently lose early details.
    """

    def __init__(self, max_messages: int = 20, max_chars: int = 12000,
                 max_tokens: int = 8000,
                 persist_path: Optional[str] = None):
        self.messages: List[ConversationMessage] = []
        self.max_messages = max_messages
        self.max_chars = max_chars
        self.max_tokens = max_tokens  # v1.0.9
        self.persist_path = Path(persist_path) if persist_path else None
        # v1.0.6: lock for thread-safe message list mutations (M-RT-1).
        self._lock = threading.Lock()
        # v1.0.9: compaction summary — if set, prepended to prompt history
        # so the agent retains key decisions after old messages are dropped.
        self.compaction_summary: str = ""

    def add(self, role: str, content: str, **meta):
        # v2.2.4-fix: hold lock for add + trim + save to prevent TOCTOU
        # race where another thread can add between our append and _trim
        # (BUGS_REPORT CM-1).
        with self._lock:
            self.messages.append(ConversationMessage(role=role, content=content, metadata=meta))
            self._trim_locked()
            self._save_locked()

    def _trim(self):
        """Drop oldest messages until under all three limits.

        v1.0.9: the token limit is now the primary constraint. When we
        trim, we keep a rolling window of the most recent messages
        plus any compaction summary, so early decisions aren't lost
        without a trace.
        """
        with self._lock:
            self._trim_locked()

    def _trim_locked(self):
        """Internal trim — caller must hold self._lock."""
        while len(self.messages) > self.max_messages:
            self.messages.pop(0)
        while self._total_chars() > self.max_chars and len(self.messages) > 1:
            self.messages.pop(0)
        # v1.0.9: token-based trim
        while self._total_tokens() > self.max_tokens and len(self.messages) > 1:
            self.messages.pop(0)

    def _total_chars(self) -> int:
        return sum(len(m.content) for m in self.messages)

    def _total_tokens(self) -> int:
        """v1.0.9: estimated token count of all messages + summary."""
        total = _estimate_tokens(self.compaction_summary)
        for m in self.messages:
            total += _estimate_tokens(m.content)
        return total

    def token_breakdown(self) -> Dict[str, int]:
        """v1.0.9: per-message token counts for the /context command."""
        breakdown: Dict[str, int] = {}
        if self.compaction_summary:
            breakdown["__compaction_summary__"] = _estimate_tokens(self.compaction_summary)
        for i, m in enumerate(self.messages):
            label = f"{i:03d}_{m.role}"
            breakdown[label] = _estimate_tokens(m.content)
        return breakdown

    def should_compact(self, threshold: float = 0.85) -> bool:
        """v1.0.9: return True if context is over `threshold` of budget.

        Called by the agent loop before each LLM call. If True, the loop
        triggers auto-compaction (summarise old messages, keep only the
        most recent few + the summary).
        """
        if self.max_tokens <= 0:
            return False
        return self._total_tokens() > int(self.max_tokens * threshold)

    def compact(self, summary: str, keep_recent: int = 4) -> None:
        """v1.0.9: replace old messages with a summary, keep the most recent.

        Called by:
          - the /compact command (user-initiated)
          - auto-compaction in the agent loop (when should_compact() is True)

        The summary is prepended to to_prompt_history() so the agent still
        has access to the key decisions from the dropped messages.

        v1.0.5-correctness: cap the compaction_summary size. Previously
        every compaction prepended the previous summary verbatim, so
        after N auto-compactions in a long session the summary was N×
        the size of a single summary. ``_trim()`` only trims
        ``self.messages``, not ``compaction_summary`` — so the summary
        could blow the token budget with no recourse. We now keep only
        the most recent summary and cap its size (BUGS_REPORT M-RT-5).
        """
        if keep_recent < 0:
            keep_recent = 0
        # v1.0.5-correctness: don't accumulate summaries verbatim — each
        # compaction produces a fresh summary that already incorporates
        # the prior context (the summariser sees the previous summary
        # via `to_prompt_history()`). Only keep the latest, and cap it.
        # If the caller's summary is empty (rare), fall back to the
        # previous one so we don't lose context.
        with self._lock:
            new_summary = summary or self.compaction_summary
            # Cap at a generous 4000 chars (~1000 tokens) so a runaway
            # summariser can't blow the budget.
            _MAX_SUMMARY_CHARS = 4000
            if len(new_summary) > _MAX_SUMMARY_CHARS:
                new_summary = new_summary[-_MAX_SUMMARY_CHARS:]
                logger.warning(
                    "[memory] compaction summary truncated to %d chars",
                    _MAX_SUMMARY_CHARS,
                )
            self.compaction_summary = new_summary
            # Keep only the most recent `keep_recent` messages
            if keep_recent == 0:
                self.messages = []
            elif len(self.messages) > keep_recent:
                self.messages = self.messages[-keep_recent:]
        self.save()
        logger.info(
            "[memory] compacted: kept %d recent messages, summary=%d chars",
            len(self.messages), len(self.compaction_summary),
        )

    def to_prompt_history(self) -> str:
        """Render the retained messages as prompt text.

        v1.2.2-fix: this used to hardcode ``self.messages[-10:]``,
        which silently overrode ``max_messages`` — even after
        ``_sync_context_budgets()`` (review §4.3) raised
        ``max_messages`` to as much as 500 for large-context
        providers, only the last 10 messages ever actually reached
        the prompt. ``self.messages`` is already kept within
        ``max_messages`` / ``max_chars`` / ``max_tokens`` by
        ``_trim()`` on every ``add()``, so there is no need for a
        second, stricter cap here — we just render what ``_trim()``
        decided to keep.
        """
        parts: List[str] = []
        # v1.0.9: prepend compaction summary if present
        if self.compaction_summary:
            parts.append(f"[COMPACTION SUMMARY]\n{self.compaction_summary}")
        for m in self.messages:
            role_label = {"user": "USER", "assistant": "TERA PILOT", "tool": "TOOL"}.get(m.role, m.role.upper())
            parts.append(f"[{role_label}]\n{m.content}")
        return "\n".join(parts)

    def clear(self):
        """v1.0.9: clear messages AND compaction summary (full reset)."""
        with self._lock:
            self.messages.clear()
            self.compaction_summary = ""
        self.save()

    def status(self) -> Dict[str, Any]:
        """v1.0.9: status dict for /context command."""
        return {
            "message_count": len(self.messages),
            "total_chars": self._total_chars(),
            "total_tokens": self._total_tokens(),
            "max_messages": self.max_messages,
            "max_chars": self.max_chars,
            "max_tokens": self.max_tokens,
            "compaction_summary_chars": len(self.compaction_summary),
            "compaction_summary_tokens": _estimate_tokens(self.compaction_summary),
            "utilization": (
                self._total_tokens() / self.max_tokens if self.max_tokens > 0 else 0.0
            ),
        }

    def save(self):
        """Persist memory to JSON. v1.0.9: also saves compaction_summary.

        v1.0.5-security: atomic write via tempfile + os.replace, so a crash
        (OOM, kill, power loss) mid-write can't leave a truncated/garbled
        JSON file (BUGS_REPORT H-RT-2). Previously `open(..., "w")`
        truncated first, then `json.dump` wrote; a crash between truncate
        and the end of `json.dump` would have left the persist file in an
        unrecoverable state, losing the entire conversation history.

        Issue #6: if ``persist_path`` ends in ``.db`` / ``.sqlite`` /
        ``.sqlite3``, dispatch to :class:`tera_pilot.session.sqlite_persistence.
        SQLitePersistence` instead of writing JSON. The SQLite backend
        scales to long sessions (O(log N) appends vs O(N) rewrites).
        """
        with self._lock:
            self._save_locked()

    def _save_locked(self):
        """Internal save — caller must hold self._lock (BUGS_REPORT CM-3)."""
        if not self.persist_path:
            return
        # Issue #6: SQLite dispatch
        try:
            from tera_pilot.session.sqlite_persistence import is_sqlite_path
            if is_sqlite_path(self.persist_path):
                self._save_sqlite()
                return
        except Exception as e:
            logger.warning("[memory] SQLite dispatch failed (%s); falling back to JSON", e)

        import os as _os
        import tempfile as _tf
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "messages": [m.to_dict() for m in self.messages],
                "compaction_summary": self.compaction_summary,
            }
            payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
            fd, tmp_path = _tf.mkstemp(prefix='.mem_', suffix='.tmp',
                                       dir=str(self.persist_path.parent))
            try:
                with _os.fdopen(fd, 'wb') as f:
                    f.write(payload)
                _os.replace(tmp_path, self.persist_path)
            except Exception:
                try: _os.unlink(tmp_path)
                except OSError: pass
                raise
        except Exception as e:
            logger.warning(f"[memory] Failed to save: {e}")

    def load(self):
        """Load memory from JSON. v1.0.9: also loads compaction_summary.

        Issue #6: dispatches to SQLite if ``persist_path`` looks like a
        SQLite database file.
        """
        if not self.persist_path or not self.persist_path.exists():
            return
        # Issue #6: SQLite dispatch
        try:
            from tera_pilot.session.sqlite_persistence import is_sqlite_path
            if is_sqlite_path(self.persist_path):
                self._load_sqlite()
                return
        except Exception as e:
            logger.warning("[memory] SQLite load dispatch failed (%s); falling back to JSON", e)
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # v1.0.9: handle both old format (list) and new format (dict)
            if isinstance(data, list):
                # Old format — just messages
                new_messages = [ConversationMessage.from_dict(d) for d in data]
                new_summary = ""
            elif isinstance(data, dict):
                new_messages = [ConversationMessage.from_dict(d) for d in data.get("messages", [])]
                new_summary = data.get("compaction_summary", "")
            else:
                new_messages = []
                new_summary = ""
            with self._lock:
                self.messages = new_messages
                self.compaction_summary = new_summary
            self._trim()
            logger.info(f"[memory] Loaded {len(self.messages)} messages "
                        f"(summary: {len(self.compaction_summary)} chars)")
        except Exception as e:
            logger.warning(f"[memory] Failed to load: {e}")

    # ── Issue #6: SQLite helpers ──────────────────────────────────────

    _SQLITE_SESSION_ID_KEY = "_sqlite_session_id"

    def _get_sqlite_store(self):
        """Lazily instantiate and cache a SQLitePersistence adapter."""
        from tera_pilot.session.sqlite_persistence import SQLitePersistence
        cached = getattr(self, "_sqlite_store", None)
        if cached is None:
            cached = SQLitePersistence(self.persist_path)
            self._sqlite_store = cached
        return cached

    def _get_or_create_session_id(self) -> str:
        """Return the session id stored in metadata, creating one if needed."""
        # The session id is stashed as a private attribute on the memory
        # instance so that all subsequent saves reuse the same row.
        sid = getattr(self, "_sqlite_session_id", None)
        if sid:
            return sid
        store = self._get_sqlite_store()
        # Pick the most recently updated session that has no other owner.
        sessions = store.list_sessions()
        if sessions:
            sid = sessions[0]["id"]
        else:
            sid = store.create_session(title="default")
        self._sqlite_session_id = sid
        return sid

    def _save_sqlite(self):
        store = self._get_sqlite_store()
        sid = self._get_or_create_session_id()
        store.save(
            sid,
            [m.to_dict() for m in self.messages],
            self.compaction_summary,
        )

    def _load_sqlite(self):
        store = self._get_sqlite_store()
        sid = self._get_or_create_session_id()
        messages_dicts, summary = store.load(sid)
        self.messages = [ConversationMessage.from_dict(d) for d in messages_dicts]
        self.compaction_summary = summary
        self._trim()
        logger.info(
            "[memory] Loaded %d messages from SQLite (summary: %d chars)",
            len(self.messages), len(self.compaction_summary),
        )


# ── Tool Engine (Secure) ─────────────────────────────────────────────────

