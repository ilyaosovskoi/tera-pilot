"""Session context memory service for the Tera Pilot AI coding assistant.

Provides persistent, append-only storage of session context in a single
Markdown file.  Each session is recorded as a timestamped section so that
recent context can be retrieved efficiently without any external database
dependency.

v1.0.5 changes (cross-chat context — "лучше хранил контекст между чатами"):

* Each saved session now carries structured metadata: project_root,
  provider, files touched, chat_id, tags. Stored as a JSON header line
  at the top of every section so it is both human-readable and
  machine-parseable.
* New ``search_sessions()`` lets the agent / UI find prior context by
  keyword, file path, project root, or tag — across ALL chats, not
  just the current one.
* New ``build_context_brief()`` returns a compact (~1 KB) summary of
  the most relevant prior sessions, suitable for injection into a
  system prompt so the model has continuity across chats.
* ``load_memory()`` now optionally filters by tag/project_root.
* The Markdown file format is backward-compatible: old sections
  without the JSON header still parse, they just have empty metadata.

v1.1.5-fix (tera_pilot_bug_report.md bug #10): the old comment claimed
``_PROCESS_LOCK`` protected against "two parallel instances of Tera Pilot",
but ``threading.RLock`` only synchronises threads *inside one Python
process*. If two Tera Pilot processes were running at once (two windows, or
a packaged build + ``python -m tera_pilot`` for dev), their read-modify-write
cycles on ``tera_pilot_memory.md`` could interleave and silently drop each
other's writes. We now ALSO take a cross-process file lock
(``fcntl.flock`` on POSIX / ``msvcrt.locking`` on Windows) on a
sidecar ``.lock`` file, exactly like ``quota.py`` already does for the
quota history file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Cross-process file locking primitives ──────────────────────────
# v1.1.5-fix (bug #10): mirror the approach already used by quota.py.
# threading.RLock alone only protects against races between threads of
# the SAME process; two Tera Pilot processes need a real OS-level lock.
try:
    import fcntl as _fcntl  # POSIX
    _HAS_FCNTL = True
except ImportError:
    _fcntl = None
    _HAS_FCNTL = False

try:
    import msvcrt as _msvcrt  # Windows
    _HAS_MSVCRT = True
except ImportError:
    _msvcrt = None
    _HAS_MSVCRT = False


_DEFAULT_MEMORY_DIR = Path.home() / ".tera_pilot"
_DEFAULT_MEMORY_FILE = _DEFAULT_MEMORY_DIR / "tera_pilot_memory.md"

# Number of dashes used as a session separator
_SEPARATOR = "---"

# v1.0.5: every section begins with a `## Session: ...` header line,
# optionally followed by a `<!-- meta: {...} -->` JSON metadata line.
# We parse the metadata line if present (backward compatible: old
# sections have no metadata and parse with empty meta).
_META_LINE_RE = re.compile(r"^<!--\s*meta:\s*(\{.*?\})\s*-->\s*$", re.MULTILINE)
_SESSION_HEADER_RE = re.compile(
    r"^##\s*Session:\s*(?P<title>.+?)\s*—\s*(?P<ts>[^\n]+)\s*$",
    re.MULTILINE,
)


@dataclass
class SessionEntry:
    """One saved session — title, timestamp, metadata, body."""
    title: str
    timestamp: str
    body: str
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "timestamp": self.timestamp,
            "body": self.body,
            "meta": self.meta,
        }


class MemoryService:
    """Append-only session memory backed by a single Markdown file.

    Parameters
    ----------
    persist_path:
        Absolute or relative path to the Markdown file used for persistence.
        When *None*, defaults to ``~/.tera_pilot/tera_pilot_memory.md``.
    max_sessions:
        v1.0.5-security — cap on the number of sessions kept on disk.
        Older sessions are dropped on save when the cap is exceeded.
        Default: 200. Without this cap, ``~/.tera_pilot/tera_pilot_memory.md`` grows
        without bound forever (BUGS_REPORT H-MEM-1).

    v1.1.5-fix (bug #10): ``save_context`` now acquires a cross-process
    file lock (``fcntl`` on POSIX, ``msvcrt`` on Windows) on a sidecar
    ``.lock`` file, in addition to the in-process ``threading.RLock``.
    This makes the read-modify-write cycle atomic across multiple Tera Pilot
    processes (two windows, packaged app + ``python -m tera_pilot`` for dev,
    etc.). Without it, concurrent saves from two processes could
    interleave and silently drop each other's writes.
    """

    # Process-wide lock guarding all RMW cycles on the memory file.
    # v1.1.5-fix (bug #10): this lock ONLY serialises threads inside
    # the current process. Cross-process serialisation is handled by
    # ``_file_lock()`` below. Both must be held during the RMW cycle.
    _PROCESS_LOCK = threading.RLock()

    DEFAULT_MAX_SESSIONS = 200

    def __init__(self, persist_path: Optional[str] = None,
                 max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        self._path = Path(persist_path) if persist_path else _DEFAULT_MEMORY_FILE
        self._max_sessions = max(1, int(max_sessions))
        # v1.1.5-fix (bug #10): sidecar lock file for cross-process
        # serialisation. Same pattern as quota.py.
        self._lockfile_path = self._path.with_suffix(self._path.suffix + ".lock")
        # v1.1.5-fix: per-thread reentrancy counter so a nested
        # `with self._file_lock():` inside the SAME thread doesn't
        # try to flock() the same fd twice (which would deadlock on
        # POSIX — flock is exclusive per file description, not per
        # process). The OS-level lock is taken only on the OUTERMOST
        # call; inner calls just bump the counter.
        self._lock_reentrance = threading.local()
        logger.debug("MemoryService initialised with persist_path=%s", self._path)

    # ------------------------------------------------------------------
    # Cross-process locking
    # ------------------------------------------------------------------

    def _file_lock(self):
        """Context manager that acquires a cross-process file lock.

        v1.1.5-fix (bug #10): on POSIX uses ``fcntl.flock(LOCK_EX)``; on
        Windows uses ``msvcrt.locking`` on byte 0. Falls back to a no-op
        if neither is available (e.g. some sandboxed runtimes) — in
        that case the in-process lock still serialises threads, but
        cross-process races are not prevented.

        Reentrancy: ``threading.RLock`` lets the SAME thread acquire
        the in-process lock multiple times, but ``fcntl.flock`` on a
        freshly-opened fd would deadlock against a flock already held
        by the same process. We track a per-thread reentrance counter
        so the OS-level lock is taken only on the outermost call;
        inner calls just bump the counter and skip the flock.

        Pattern mirrors ``quota.py::_file_lock`` but with the extra
        reentrancy guard, since ``MemoryService.save_context`` may be
        called from paths that already hold the lock (e.g. internal
        helpers).
        """
        @contextlib.contextmanager
        def _cm():
            # Always acquire the in-process lock first. RLock allows
            # the same thread to re-acquire without deadlock.
            self._PROCESS_LOCK.acquire()
            # Per-thread reentrance counter.
            depth = getattr(self._lock_reentrance, "depth", 0)
            setattr(self._lock_reentrance, "depth", depth + 1)
            outermost = (depth == 0)
            try:
                if not outermost or (not _HAS_FCNTL and not _HAS_MSVCRT):
                    # Either we're inside an outer _file_lock() already,
                    # or there's no cross-process primitive available.
                    # In the first case the outer call holds the OS lock;
                    # in the second case there's nothing to take.
                    yield
                    return
                # Create/open the lock file and hold an exclusive lock.
                try:
                    self._lockfile_path.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                lock_fd = os.open(
                    str(self._lockfile_path),
                    os.O_CREAT | os.O_RDWR,
                    0o600,
                )
                try:
                    if _HAS_FCNTL:
                        _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
                    elif _HAS_MSVCRT:
                        _msvcrt.locking(lock_fd, _msvcrt.LK_LOCK, 1)
                    yield
                finally:
                    try:
                        if _HAS_FCNTL:
                            _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                        elif _HAS_MSVCRT:
                            _msvcrt.locking(lock_fd, _msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                    os.close(lock_fd)
            finally:
                # Decrement reentrance counter BEFORE releasing the
                # RLock so the next outermost call sees depth==0.
                depth = getattr(self._lock_reentrance, "depth", 1)
                setattr(self._lock_reentrance, "depth", max(0, depth - 1))
                self._PROCESS_LOCK.release()

        return _cm()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_context(self, session_id: str, title: str, content: str,
                     **meta: Any) -> dict:
        """Prepend a timestamped session section to the memory file.

        The new section is *prepended* so that the most recent session
        always appears at the top of the file, making reads efficient
        (see :meth:`load_memory`).

        Parameters
        ----------
        session_id:
            Unique identifier for the session (currently used only for
            logging / future indexing).
        title:
            Human-readable session title.
        content:
            The session context body (arbitrary text / Markdown).
        **meta:
            v1.0.5 — optional structured metadata. Recognised keys:
            ``project_root``, ``provider``, ``chat_id``, ``tags``
            (list[str]), ``files_touched`` (list[str]). Stored as a
            JSON line at the top of the section so it is searchable
            by :meth:`search_sessions`.

        Returns
        -------
        dict
            ``{"ok": True, "path": str}`` on success.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        # v1.0.5: serialise metadata as a JSON comment line so the
        # file stays valid Markdown AND is machine-parseable. We strip
        # any keys that aren't JSON-serialisable (rare, but defensive).
        meta_line = ""
        if meta:
            try:
                safe_meta = {k: v for k, v in meta.items()
                             if isinstance(v, (str, int, float, bool, list, dict, type(None)))}
                meta_line = f"<!-- meta: {json.dumps(safe_meta, ensure_ascii=False)} -->\n"
            except (TypeError, ValueError) as exc:
                logger.warning("Memory meta not JSON-serialisable: %s", exc)

        # v1.0.5-security: sanitise title — strip newlines so the session
        # header regex can always re-parse it (BUGS_REPORT M-MEM-3).
        safe_title = (title or "").replace("\r", " ").replace("\n", " ").strip()
        if not safe_title:
            safe_title = "(untitled)"

        section = (
            f"## Session: {safe_title} \u2014 {timestamp}\n"
            f"{meta_line}"
            f"\n{content}\n\n{_SEPARATOR}\n"
        )

        # Ensure the parent directory tree exists.
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # v1.1.5-fix (bug #10): cross-process RMW under both the
        # in-process RLock AND an OS-level file lock. This is the same
        # fix pattern that quota.py uses for its quota history file.
        # Without the file lock, two Tera Pilot processes could both read
        # the current file, both prepend their new section, and one
        # write would clobber the other.
        with self._file_lock():
            existing = ""
            if self._path.exists():
                try:
                    existing = self._path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning("Failed to read memory file: %s", exc)
                    existing = ""

            new_content = section + existing

            # Cap growth: keep at most `self._max_sessions` most-recent
            # sessions. Sessions are separated by `\n---\n` at the start
            # of a line; we count them and trim from the end.
            if self._max_sessions > 0:
                sessions = new_content.split(f"\n{_SEPARATOR}\n")
                # The first chunk is the most-recent session (we prepend).
                # Drop trailing empty chunk if present.
                if sessions and sessions[-1].strip() == "":
                    sessions = sessions[:-1]
                if len(sessions) > self._max_sessions:
                    sessions = sessions[:self._max_sessions]
                    new_content = f"\n{_SEPARATOR}\n".join(sessions) + f"\n{_SEPARATOR}\n"

            # Atomic write: tempfile in same dir, then os.replace.
            data = new_content.encode("utf-8")
            try:
                fd, tmp_path = tempfile.mkstemp(prefix='.mem_', suffix='.tmp',
                                                dir=str(self._path.parent))
                try:
                    with os.fdopen(fd, 'wb') as f:
                        f.write(data)
                    os.replace(tmp_path, self._path)
                except Exception:
                    try: os.unlink(tmp_path)
                    except OSError: pass
                    raise
            except OSError as exc:
                logger.error("Failed to write memory file: %s", exc)
                return {"ok": False, "error": str(exc), "path": str(self._path)}

        logger.info(
            "Saved session context: session_id=%s title=%s path=%s",
            session_id,
            safe_title,
            self._path,
        )
        return {"ok": True, "path": str(self._path)}

    def load_memory(self, max_chars: int = 8000,
                    *, project_root: Optional[str] = None,
                    tag: Optional[str] = None) -> dict:
        """Return the most recent session context from the memory file.

        Because new sessions are *prepended*, the most recent content is
        at the start of the file.  However, per the specification this
        method reads the full file and returns the **last** ``max_chars``
        characters, which in practice gives the most recent material when
        combined with the prepend-on-write strategy.

        v1.0.5: optional ``project_root`` / ``tag`` filters restrict the
        returned content to sessions whose metadata matches. This is
        what makes the memory useful *across* chats — a session saved
        while working on project X can be retrieved when the user opens
        project X again, even if they spent 5 chats on project Y in
        between.

        Parameters
        ----------
        max_chars:
            Maximum number of characters to return.
        project_root:
            If given, only sessions whose ``meta.project_root`` matches
            (suffix match) are included in the result.
        tag:
            If given, only sessions whose ``meta.tags`` list contains
            this tag are included.

        Returns
        -------
        dict
            ``{"ok": True, "content": str, "chars": int}`` when the file
            exists, or ``{"ok": True, "content": "", "chars": 0}`` when it
            does not.
        """
        if not self._path.exists():
            logger.debug("Memory file does not exist yet: %s", self._path)
            return {"ok": True, "content": "", "chars": 0}

        try:
            full_text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read memory file %s: %s", self._path, exc)
            return {"ok": True, "content": "", "chars": 0}

        # v1.0.5: filter sessions by metadata if requested.
        if project_root or tag:
            entries = self._parse_sessions(full_text)
            kept = [e for e in entries
                    if self._entry_matches(e, project_root=project_root, tag=tag)]
            full_text = "".join(self._render_entry(e) for e in kept)
            if not full_text:
                return {"ok": True, "content": "", "chars": 0}

        if len(full_text) <= max_chars:
            return {"ok": True, "content": full_text, "chars": len(full_text)}

        # Return the last `max_chars` characters as specified.
        tail = full_text[-max_chars:]
        return {"ok": True, "content": tail, "chars": len(tail)}

    def search_sessions(self, *, query: Optional[str] = None,
                        project_root: Optional[str] = None,
                        tag: Optional[str] = None,
                        chat_id: Optional[str] = None,
                        file_path: Optional[str] = None,
                        limit: int = 20) -> List[SessionEntry]:
        """v1.0.5 — find prior sessions matching one or more filters.

        All filter parameters are optional AND combined. Any session
        that satisfies ALL the supplied filters is returned, most
        recent first.

        Parameters
        ----------
        query:
            Case-insensitive substring search over the session body
            and title.
        project_root:
            Suffix match on ``meta.project_root`` (so a search for
            ``~/projects/foo`` matches sessions saved with
            ``/Users/me/projects/foo``).
        tag:
            Membership check on ``meta.tags``.
        chat_id:
            Exact match on ``meta.chat_id``.
        file_path:
            Membership check on ``meta.files_touched``.
        limit:
            Cap on the number of returned entries.

        Returns
        -------
        list of :class:`SessionEntry`
        """
        if not self._path.exists():
            return []
        try:
            full_text = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        entries = self._parse_sessions(full_text)
        q_lower = (query or "").lower()
        results: List[SessionEntry] = []
        for e in entries:
            if q_lower and q_lower not in e.body.lower() and q_lower not in e.title.lower():
                continue
            if not self._entry_matches(e, project_root=project_root, tag=tag,
                                       chat_id=chat_id, file_path=file_path):
                continue
            results.append(e)
            if len(results) >= limit:
                break
        return results

    def build_context_brief(self, *, project_root: Optional[str] = None,
                            query: Optional[str] = None,
                            max_chars: int = 1500) -> str:
        """v1.0.5 — produce a compact brief of recent relevant sessions,
        suitable for injection into a system prompt so the model has
        continuity across chats.

        The brief format is::

            Prior context (most recent first):
            1. <title> (<timestamp>) — touched: file1, file2
               <first 200 chars of body>
            2. ...

        Returns an empty string if no sessions match.
        """
        entries = self.search_sessions(project_root=project_root, query=query,
                                       limit=5)
        if not entries:
            return ""
        lines = ["Prior context (most recent first):"]
        for i, e in enumerate(entries, 1):
            files = e.meta.get("files_touched") or []
            files_str = f" — touched: {', '.join(files[:3])}" if files else ""
            tags = e.meta.get("tags") or []
            tags_str = f" [{', '.join(tags)}]" if tags else ""
            preview = e.body.strip().replace("\n", " ")[:200]
            lines.append(f"{i}. {e.title} ({e.timestamp}){files_str}{tags_str}")
            if preview:
                lines.append(f"   {preview}")
        out = "\n".join(lines)
        return out[:max_chars]

    def get_session_summary(self) -> dict:
        """Return a lightweight summary of the memory file.

        Returns
        -------
        dict
            ``{
                "total_sessions": int,
                "file_size_bytes": int,
                "last_updated": str | None,
            }``

            *total_sessions* is derived by counting the ``---`` separator
            lines in the file.  *last_updated* is the file's modification
            timestamp in ISO format, or ``None`` when the file does not
            exist.
        """
        if not self._path.exists():
            return {
                "total_sessions": 0,
                "file_size_bytes": 0,
                "last_updated": None,
            }

        file_size = self._path.stat().st_size
        mtime = datetime.fromtimestamp(self._path.stat().st_mtime, tz=timezone.utc)

        try:
            text = self._path.read_text(encoding="utf-8")
            # Count sessions by matching the session header regex, not by
            # counting "---" substrings which can appear in diffs, markdown rules,
            # YAML frontmatter, etc.
            total_sessions = len(list(_SESSION_HEADER_RE.finditer(text)))
        except OSError:
            total_sessions = 0

        return {
            "total_sessions": total_sessions,
            "file_size_bytes": file_size,
            "last_updated": mtime.isoformat(),
        }

    def clear(self) -> dict:
        """Remove the memory file entirely.

        v1.1.5-fix (bug #10): the file lock is taken here too, so
        ``clear()`` racing with a concurrent ``save_context()`` in
        another process can't corrupt the file (one process deleting
        while the other is mid-RMW would otherwise leave a partial
        file or a "file not found" error inside the save).
        """
        with self._file_lock():
            if self._path.exists():
                try:
                    self._path.unlink()
                    logger.info("Cleared memory file: %s", self._path)
                except OSError as exc:
                    logger.warning("Failed to clear memory file %s: %s", self._path, exc)
            else:
                logger.debug("Clear requested but file does not exist: %s", self._path)

        return {"ok": True, "path": str(self._path)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_matches(entry: SessionEntry, *,
                       project_root: Optional[str] = None,
                       tag: Optional[str] = None,
                       chat_id: Optional[str] = None,
                       file_path: Optional[str] = None) -> bool:
        """Check whether a single session entry satisfies ALL supplied filters."""
        meta = entry.meta or {}
        if project_root is not None:
            saved = str(meta.get("project_root", ""))
            if not saved or not (saved.endswith(project_root)
                                 or project_root.endswith(saved)):
                return False
        if tag is not None:
            tags = meta.get("tags") or []
            if not isinstance(tags, list) or tag not in tags:
                return False
        if chat_id is not None:
            if meta.get("chat_id") != chat_id:
                return False
        if file_path is not None:
            touched = meta.get("files_touched") or []
            if not isinstance(touched, list) or file_path not in touched:
                return False
        return True

    def _parse_sessions(self, full_text: str) -> List[SessionEntry]:
        """Split the markdown file into SessionEntry objects.

        Each section is delimited by `## Session: <title> — <ts>` and
        terminated by `---` on its own line.
        """
        # Find all session-header positions.
        headers = list(_SESSION_HEADER_RE.finditer(full_text))
        if not headers:
            return []
        entries: List[SessionEntry] = []
        for i, m in enumerate(headers):
            start = m.start()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(full_text)
            section = full_text[start:end]
            title = m.group("title").strip()
            ts = m.group("ts").strip()
            # Body = everything after the header line, within this section.
            # m.end() is the offset in full_text where the header ends,
            # so within `section` (which starts at `start`), the body
            # begins at offset (m.end() - start).
            body_start = m.end() - start
            body = section[body_start:]
            # Extract optional metadata line from the body's first 512 chars.
            meta: Dict[str, Any] = {}
            meta_match = _META_LINE_RE.search(body[:512])
            if meta_match:
                try:
                    meta = json.loads(meta_match.group(1))
                    if not isinstance(meta, dict):
                        meta = {}
                except json.JSONDecodeError:
                    meta = {}
                # Strip the meta line from the body so callers don't see it.
                body = body.replace(meta_match.group(0), "", 1)
            # v1.0.5-security: strip trailing `---` SEPARATOR line properly.
            # `str.rstrip("---")` strips ANY combination of `-` chars from the
            # end (BUGS_REPORT M-MEM-2). Split on lines and drop trailing `---`.
            body_lines = body.split("\n")
            while body_lines and body_lines[-1].strip() == _SEPARATOR:
                body_lines.pop()
            body = "\n".join(body_lines).strip()
            entries.append(SessionEntry(title=title, timestamp=ts,
                                        body=body, meta=meta))
        return entries

    @staticmethod
    def _render_entry(entry: SessionEntry) -> str:
        meta_line = ""
        if entry.meta:
            try:
                meta_line = f"<!-- meta: {json.dumps(entry.meta, ensure_ascii=False)} -->\n"
            except (TypeError, ValueError):
                pass
        return (
            f"## Session: {entry.title} \u2014 {entry.timestamp}\n"
            f"{meta_line}\n{entry.body}\n\n{_SEPARATOR}\n"
        )