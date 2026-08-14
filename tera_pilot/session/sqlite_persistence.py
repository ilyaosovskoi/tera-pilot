"""SQLite-backed persistence for conversation memory.

Issue #6: the existing ``ContextMemory`` persists to a single JSON file
via :py:meth:`ContextMemory.save`. That works for short sessions but
has three structural problems:

1. **O(N) write cost**: every ``add()`` rewrites the entire history,
   so a 1000-message session does 1000 full rewrites.
2. **No indexing**: there is no way to query "messages 200–300 of
   session X" without loading the whole file.
3. **No multi-session support**: the JSON file is single-session;
   switching sessions requires loading a different file path.

This module ships a ``SQLitePersistence`` adapter that exposes the
same load/save contract as the JSON path, but stores each message as
a separate row. The adapter is intentionally drop-in: ``ContextMemory``
can use it transparently by setting ``persist_path`` to a ``*.db``
file and the load/save dispatch picks the right backend.

Schema (v1):

    sessions(
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        title TEXT,
        compaction_summary TEXT NOT NULL DEFAULT ''
    )

    messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,              -- 0-based monotonic order
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(session_id, seq)
    )

    schema_version(version INTEGER PRIMARY KEY)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1


def _now_iso() -> str:
    # Microsecond precision so two sessions created in the same call
    # stack still get distinct timestamps (and deterministic ordering
    # by ``updated_at``).
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + (
        f".{int((t % 1) * 1_000_000):06d}Z"
    )


# ── Schema bootstrap ──────────────────────────────────────────────────────


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT,
    compaction_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq
    ON messages(session_id, seq);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing and record the schema version."""
    conn.executescript(_SCHEMA_SQL)
    cur = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
        )
        conn.commit()


# ── Adapter ───────────────────────────────────────────────────────────────


@dataclass
class StoredMessage:
    """A message row from the database."""

    seq: int
    role: str
    content: str
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "metadata": self.metadata,
        }


class SQLitePersistence:
    """SQLite-backed persistence adapter for ``ContextMemory``.

    The adapter is thread-safe (one connection guarded by a lock).
    Each call opens a short transaction so concurrent callers don't
    block each other for long.

    Public API mirrors the JSON persistence contract:

    - ``save(session_id, messages, compaction_summary)``
    - ``load(session_id) -> (messages, compaction_summary)``
    - ``list_sessions() -> List[SessionInfo]``
    - ``delete_session(session_id)``
    - ``create_session(title=None) -> session_id``
    """

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._lock = threading.RLock()
        # check_same_thread=False: we use our own lock.
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        _ensure_schema(self._conn)

    # ── connection helpers ─────────────────────────────────────────────

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── session lifecycle ──────────────────────────────────────────────

    def create_session(self, title: Optional[str] = None) -> str:
        """Insert a new session row and return its id."""
        session_id = uuid.uuid4().hex
        now = _now_iso()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO sessions(id, created_at, updated_at, title) "
                "VALUES (?, ?, ?, ?)",
                (session_id, now, now, title),
            )
        return session_id

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages. Returns True if a row was deleted."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cur.rowcount > 0

    def list_sessions(self) -> List[Dict[str, Any]]:
        """Return metadata for all sessions, newest first."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, created_at, updated_at, title, "
                "       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS msg_count "
                "FROM sessions s ORDER BY updated_at DESC"
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "created_at": r[1],
                "updated_at": r[2],
                "title": r[3],
                "message_count": r[4],
            }
            for r in rows
        ]

    def set_compaction_summary(self, session_id: str, summary: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE sessions SET compaction_summary = ?, updated_at = ? "
                "WHERE id = ?",
                (summary, _now_iso(), session_id),
            )

    # ── message write ──────────────────────────────────────────────────

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Append a single message and return its seq number."""
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        now = _now_iso()
        with self._cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            )
            seq = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO messages(session_id, seq, role, content, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, seq, role, content, meta_json, now),
            )
            cur.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return seq

    def save(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        compaction_summary: str = "",
    ) -> None:
        """Replace all messages for ``session_id`` with ``messages``.

        This is the drop-in replacement for the JSON ``save()`` call.
        It's an O(N) full rewrite *of a single session*, but each
        subsequent ``append_message`` is O(log N).
        """
        now = _now_iso()
        with self._cursor() as cur:
            # Wrap in an explicit transaction so a crash after DELETE
            # but before the INSERTs doesn't lose all messages.
            # Since isolation_level=None (autocommit), we must use
            # explicit BEGIN/COMMIT/ROLLBACK.
            cur.execute("BEGIN")
            try:
                # Make sure the session row exists.
                cur.execute("SELECT id FROM sessions WHERE id = ?", (session_id,))
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO sessions(id, created_at, updated_at, title, compaction_summary) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (session_id, now, now, None, compaction_summary),
                    )
                else:
                    cur.execute(
                        "UPDATE sessions SET compaction_summary = ?, updated_at = ? WHERE id = ?",
                        (compaction_summary, now, session_id),
                    )
                # Wipe messages and re-insert.
                cur.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                for seq, msg in enumerate(messages):
                    cur.execute(
                        "INSERT INTO messages(session_id, seq, role, content, metadata_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            seq,
                            msg.get("role", "user"),
                            msg.get("content", ""),
                            json.dumps(msg.get("metadata", {}), ensure_ascii=False),
                            now,
                        ),
                    )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    # ── message read ───────────────────────────────────────────────────

    def load(self, session_id: str) -> Tuple[List[Dict[str, Any]], str]:
        """Load all messages for ``session_id`` plus the compaction summary.

        Returns ``([], "")`` if the session does not exist.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT compaction_summary FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                return [], ""
            summary = row[0] or ""
            cur.execute(
                "SELECT seq, role, content, metadata_json FROM messages "
                "WHERE session_id = ? ORDER BY seq ASC",
                (session_id,),
            )
            rows = cur.fetchall()
        messages: List[Dict[str, Any]] = []
        for _seq, role, content, meta_json in rows:
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except Exception:
                meta = {}
            messages.append(
                {"role": role, "content": content, "metadata": meta}
            )
        return messages, summary

    def load_range(
        self, session_id: str, offset: int = 0, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Load a slice of messages — useful for paginated UIs.

        SQLite requires ``LIMIT`` before ``OFFSET``; when only an offset
        is requested we emit ``LIMIT -1 OFFSET ?`` (``-1`` means "no
        upper bound" in SQLite).
        """
        sql = (
            "SELECT seq, role, content, metadata_json FROM messages "
            "WHERE session_id = ? ORDER BY seq ASC"
        )
        params: List[Any] = [session_id]
        if limit is not None and offset:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        with self._cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for _seq, role, content, meta_json in rows:
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except Exception:
                meta = {}
            out.append({"role": role, "content": content, "metadata": meta})
        return out

    def message_count(self, session_id: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (session_id,),
            )
            return int(cur.fetchone()[0])

    # ── compaction support ─────────────────────────────────────────────

    def trim_to_keep_recent(self, session_id: str, keep_recent: int) -> int:
        """Delete all but the last ``keep_recent`` messages.

        Returns the number of deleted rows. Mirrors the JSON
        ``compact()`` behaviour where old messages are dropped.
        """
        if keep_recent < 0:
            keep_recent = 0
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (session_id,),
            )
            total = int(cur.fetchone()[0])
            if total <= keep_recent:
                return 0
            cutoff = total - keep_recent  # delete seq < cutoff
            cur.execute(
                "DELETE FROM messages WHERE session_id = ? AND seq < ?",
                (session_id, cutoff),
            )
            deleted = cur.rowcount
            cur.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_now_iso(), session_id),
            )
            return deleted


# ── Dispatch helper ───────────────────────────────────────────────────────


def is_sqlite_path(path: str | Path) -> bool:
    """Return True if ``path`` looks like a SQLite database file.

    Used by ``ContextMemory`` to decide whether to dispatch to
    :class:`SQLitePersistence` or to the legacy JSON path.
    """
    p = str(path).lower()
    return p.endswith(".db") or p.endswith(".sqlite") or p.endswith(".sqlite3")
