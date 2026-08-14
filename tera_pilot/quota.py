"""
Quota Tracker for Tera Pilot v1.1.0 — daily request limits per section.

Tracks how many agent/chat requests the user has made today, segmented by
section (general | heavy_code | office). Used to enforce the free-tier
"10 Heavy Code requests per day" limit. Future versions will add paid
tiers (>10/day) and per-provider quotas.

Design mirrors TokenTracker:
  - append-only JSONL log at ~/.tera_pilot/quota_history.jsonl
  - in-memory aggregation on demand via stats()
  - thread-safe (single lock around record() and stats())
  - per-section daily counters; "today" is midnight-local-time-bounded

The enforcement is opt-in: AgentRuntime calls `quota.exhausted(section)`
before each LLM call. If exhausted, the agent loop short-circuits with
a friendly error instead of making the call.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# v1.1.3-fix (bug 2.4): platform-compatible file locking for atomic
# quota check-and-record. Without this, two parallel Tera Pilot processes
# could both pass `exhausted() == False` and then both call `record()`,
# exceeding the daily limit by 1+ requests.
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

logger = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────────

# Free-tier daily limits per section. Future versions: paid users get
# higher limits via config overrides.
DEFAULT_DAILY_LIMITS: Dict[str, int] = {
    "general": 0,        # 0 = unlimited (no enforcement)
    "heavy_code": 10,    # 10 free Heavy Code runs per day
    "office": 0,         # office not yet released
}

# v1.1.3-fix (bug 2.7): valid section names. set_daily_limit() rejects
# anything outside this set so a typo or malicious API call can't
# create a phantom counter.
VALID_SECTIONS = frozenset({"general", "heavy_code", "office"})


def _tera_pilot_home() -> Path:
    p = Path.home() / ".tera_pilot"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _default_history_path() -> Path:
    return _tera_pilot_home() / "quota_history.jsonl"


def _local_midnight_epoch(ts: float, tz_offset_hours: float = 0.0) -> float:
    """Return the epoch timestamp of the most recent local midnight
    before `ts`. Used to bound "today" for daily quota counting.

    v1.1.3-fix (bug 2.5): the parameter ``tz_offset_hours`` was accepted
    but silently ignored — the function always computed UTC midnight.
    This was misleading (the docstring claimed "the user can override
    via tz_offset_hours"). We now honor the offset so callers can pass
    a non-zero value to get local-midnight bounding. The default is
    still 0.0 (UTC) to preserve existing behaviour.
    """
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # v1.1.3-fix (bug 2.5): apply the offset before computing midnight,
    # so passing tz_offset_hours=3.0 returns midnight in UTC+3.
    if tz_offset_hours:
        from datetime import timedelta
        dt = dt + timedelta(hours=tz_offset_hours)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert back: if we shifted forward, midnight is in local time —
    # subtract the offset to get the corresponding UTC epoch.
    if tz_offset_hours:
        from datetime import timedelta
        midnight = midnight - timedelta(hours=tz_offset_hours)
    return midnight.timestamp()


# ── QuotaTracker ────────────────────────────────────────────────────────

class QuotaTracker:
    """Append-only JSONL log of agent/chat requests, with per-section
    daily counters and exhaustion checks.

    Usage:
        qt = get_quota_tracker()
        qt.record(section="heavy_code", provider="groq", model="llama-3.3-70b")
        if qt.exhausted(section="heavy_code"):
            raise RuntimeError("Daily Heavy Code limit reached — resets at 00:00 UTC")

    v1.1.3-fix (bug 2.5/2.6): "today" is UTC-midnight-bounded (the
    docstring previously claimed "local-time-bounded" which was wrong).
    The history file is rotated on load — records older than 30 days
    are pruned to keep the file from growing without bound.
    """

    # v1.1.3-fix (bug 2.6): records older than this are pruned on load.
    _ROTATION_DAYS = 30

    def __init__(self, history_path: Optional[str] = None,
                 daily_limits: Optional[Dict[str, int]] = None):
        self._path = Path(history_path) if history_path else _default_history_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # v1.1.3-fix (bug 2.4): separate lock file for fcntl/msvcrt.
        # Locking the JSONL directly works on POSIX but is awkward on
        # Windows (you can't open a file for append and lock a byte
        # range atomically in a portable way). A sidecar .lock file is
        # simpler and works everywhere.
        self._lockfile_path = self._path.with_suffix(self._path.suffix + ".lock")
        # v1.1.3-fix (bug 2.4): use RLock (reentrant) so record() can
        # call _ensure_loaded() without deadlocking — both acquire
        # self._lock. A regular Lock would deadlock here.
        self._lock = threading.RLock()
        # v1.1.5-fix (tera_pilot_bug_report.md bug #3): per-thread reentrance
        # counter so a nested `with self._file_lock():` inside the SAME
        # thread doesn't try to flock() the same lock file twice (which
        # would deadlock on POSIX — flock is exclusive per file
        # description, not per process, and each _file_lock() opens a
        # fresh fd). The OS-level lock is taken only on the OUTERMOST
        # call; inner calls just bump the counter. Without this, making
        # `_ensure_loaded` acquire `_file_lock` would deadlock when
        # `record()` (which already holds `_file_lock`) calls it.
        # Pattern mirrors memory_service.py::_file_lock.
        self._lock_reentrance = threading.local()
        self._daily_limits = dict(daily_limits) if daily_limits else dict(DEFAULT_DAILY_LIMITS)
        # Cached aggregation: date_str (YYYY-MM-DD) -> section -> count
        self._counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._loaded = False

    # ── Configuration ──────────────────────────────────────────────

    def set_daily_limit(self, section: str, limit: int) -> None:
        """Override the daily limit for a section. 0 = unlimited.

        v1.1.3-fix (bug 2.7): validate ``section`` against
        ``VALID_SECTIONS``. A typo or malicious API call previously
        created a phantom counter (e.g. ``"heavy-code"`` with a hyphen)
        that showed up in stats() but was never enforced.
        """
        # v1.1.3-fix (bug 2.7): reject unknown sections.
        if section not in VALID_SECTIONS:
            raise ValueError(
                f"Invalid section {section!r} — must be one of "
                f"{sorted(VALID_SECTIONS)}."
            )
        with self._lock:
            self._daily_limits[section] = max(0, int(limit))

    def get_daily_limit(self, section: str) -> int:
        return int(self._daily_limits.get(section, 0))

    def get_all_limits(self) -> Dict[str, int]:
        return dict(self._daily_limits)

    # ── Persistence ────────────────────────────────────────────────

    def _file_lock(self):
        """Context manager that acquires a cross-process file lock.

        v1.1.3-fix (bug 2.4): on POSIX uses fcntl.flock(LOCK_EX); on
        Windows uses msvcrt.locking on byte 0. Falls back to a no-op
        if neither is available (e.g. some sandboxed runtimes) — in
        that case the in-process lock still serializes threads, but
        cross-process races are not prevented.

        v1.1.5-fix (tera_pilot_bug_report.md bug #3): added per-thread
        reentrance counter so a nested `with self._file_lock():` in
        the same thread doesn't open a second fd on the lock file and
        deadlock against the outer flock(). The OS-level lock is taken
        only on the outermost call; inner calls just bump the counter.
        This matters because `_ensure_loaded()` now wraps itself in
        `_file_lock()` (see bug #3 fix below), and `record()` already
        holds `_file_lock()` when it calls `_ensure_loaded()`.
        Pattern mirrors memory_service.py::_file_lock.
        """
        import contextlib

        @contextlib.contextmanager
        def _cm():
            # Always acquire the in-process lock first. RLock allows
            # the same thread to re-acquire without deadlock.
            self._lock.acquire()
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
                self._lock.release()

        return _cm()

    def _ensure_loaded(self) -> None:
        """Lazily load the JSONL history into the in-memory cache on
        first access. We re-read on every stats() call to pick up
        records written by other processes (e.g. multiple Tera Pilot windows).

        v1.1.3-fix (bug 2.6): records older than ``_ROTATION_DAYS``
        (default 30) are pruned on load. Without this, the JSONL file
        grew without bound (~150 bytes/request, ~5MB/year at 100
        requests/day) and every read became slow after a few years.

        v1.1.5-fix (tera_pilot_bug_report.md bug #3): the entire body —
        including the destructive rotation rewrite — is now wrapped
        in ``self._file_lock()``. Previously only ``record()`` held
        the cross-process lock, but ``_ensure_loaded`` is also called
        from ``today_counts()``, ``stats()``, and transitively
        ``exhausted()`` — all without any lock. Two parallel calls
        that both decided "time to rotate" would race on the
        ``open(self._path, "w")`` rewrite and clobber each other,
        exactly the class of bug ``_file_lock()`` was written to
        prevent. The reentrance counter in ``_file_lock`` makes this
        safe even when ``record()`` (already holding the lock) calls
        ``_ensure_loaded``.
        """
        with self._file_lock():
            self._ensure_loaded_locked()

    def _ensure_loaded_locked(self) -> None:
        """Actual load+rotation logic. Caller MUST hold ``_file_lock()``.

        Split out from ``_ensure_loaded`` so ``record()`` (which already
        holds the lock) can call this directly without a redundant
        re-acquire round-trip — keeps the call graph obvious.
        """
        if not self._path.exists():
            self._loaded = True
            return
        try:
            # Re-aggregate from disk every time — file is small (one line
            # per request, ~150 bytes each). A user would need ~100k
            # requests to make this slow.
            counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
            # v1.1.3-fix (bug 2.6): collect surviving records for rotation.
            keep_lines: List[str] = []
            rotation_cutoff = time.time() - (self._ROTATION_DAYS * 86400)
            rotated = 0
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = float(rec.get("ts", 0))
                    # v1.1.3-fix (bug 2.6): drop records older than the
                    # rotation window. They contribute nothing to today's
                    # counts and just bloat the file.
                    if ts < rotation_cutoff:
                        rotated += 1
                        continue
                    keep_lines.append(line)
                    section = rec.get("section", "general")
                    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    counts[date_str][section] += 1
            # No need for a separate `with self._lock:` — _file_lock
            # already holds it. Just assign directly.
            self._counts = counts
            self._loaded = True
            # v1.1.3-fix (bug 2.6): if we pruned any records, rewrite the
            # file in-place with only the surviving lines. This is best-
            # effort — if it fails (concurrent writer, disk full), we
            # just log and move on; the next load will try again.
            # v1.1.5-fix (bug #3): safe to do now — we hold _file_lock,
            # so no other process can be mid-write to the same file.
            if rotated > 0:
                try:
                    with open(self._path, "w", encoding="utf-8") as f:
                        for line in keep_lines:
                            f.write(line + "\n")
                    logger.info("[quota] rotated %d old records (>%d days)",
                                rotated, self._ROTATION_DAYS)
                except OSError as rot_err:
                    logger.warning("[quota] rotation failed: %s", rot_err)
        except Exception as e:
            logger.warning("[quota] failed to load history: %s", e)
            self._loaded = True

    def record(self, section: str = "general",
               provider: str = "",
               model: str = "",
               chat_id: str = "") -> bool:
        """Append a single request record to the JSONL log and bump
        the in-memory counter for today.

        v1.1.3-fix (bug 2.4): the write is now guarded by a cross-
        process file lock. Returns True if the record was written,
        False if the section's daily limit was already exhausted (so
        callers can short-circuit a redundant provider call). Callers
        that want strict enforcement should call ``exhausted()`` first
        AND check the return value of ``record()`` — the file lock
        closes the race window where two parallel processes both pass
        ``exhausted() == False`` and then both record.

        section: "general" | "heavy_code" | "office"
        """
        ts = time.time()
        rec = {
            "ts": ts,
            "section": section,
            "provider": provider,
            "model": model,
            "chat_id": chat_id,
        }
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        # v1.1.3-fix (bug 2.4): hold the cross-process lock for the
        # check-and-write so two parallel processes can't both pass
        # exhausted() and then both record(), exceeding the limit.
        try:
            with self._file_lock():
                # Re-read the current count under the lock.
                # v1.1.5-fix (bug #3): call _ensure_loaded_locked directly
                # — we already hold _file_lock, so the public wrapper
                # would just re-enter it (harmless thanks to the
                # reentrance counter, but pointless).
                self._ensure_loaded_locked()
                limit = self.get_daily_limit(section)
                if limit > 0:
                    used_today = int(self._counts.get(date_str, {}).get(section, 0))
                    if used_today >= limit:
                        logger.warning(
                            "[quota] record() rejected — %s daily limit "
                            "already exhausted (%d/%d)",
                            section, used_today, limit,
                        )
                        return False
                # Atomic append — small enough that we don't need tempfile+rename.
                try:
                    with open(self._path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError as e:
                    logger.error("[quota] failed to write history: %s", e)
                    return False
                self._counts[date_str][section] += 1
        except Exception as e:
            logger.error("[quota] record() lock/write failed: %s", e)
            return False
        logger.info(
            "[quota] recorded: section=%s provider=%s model=%s — today's count for %s = %d",
            section, provider, model, section, self._counts[date_str][section],
        )
        return True

    # ── Querying ───────────────────────────────────────────────────

    def today_counts(self) -> Dict[str, int]:
        """Return {section: count} for today (UTC midnight-bounded)."""
        self._ensure_loaded()
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            return dict(self._counts.get(today, {}))

    def count_today(self, section: str) -> int:
        """How many requests for `section` have been made today."""
        return int(self.today_counts().get(section, 0))

    def remaining(self, section: str) -> int:
        """How many requests are left for `section` today. Returns -1
        if the section has no limit (unlimited)."""
        limit = self.get_daily_limit(section)
        if limit <= 0:
            return -1  # unlimited
        used = self.count_today(section)
        return max(0, limit - used)

    def exhausted(self, section: str) -> bool:
        """True if the section's daily quota is used up."""
        limit = self.get_daily_limit(section)
        if limit <= 0:
            return False  # unlimited
        return self.count_today(section) >= limit

    def stats(self) -> Dict[str, Any]:
        """Return a full status dict for the UI."""
        self._ensure_loaded()
        today = self.today_counts()
        limits = self.get_all_limits()
        # Compute remaining + reset time
        sections: Dict[str, Any] = {}
        for sec, limit in limits.items():
            used = today.get(sec, 0)
            sections[sec] = {
                "used": used,
                "limit": limit,
                "remaining": -1 if limit <= 0 else max(0, limit - used),
                "exhausted": limit > 0 and used >= limit,
            }
        # Next reset = next UTC midnight
        now = datetime.now(tz=timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # add 1 day
        from datetime import timedelta
        tomorrow = tomorrow + timedelta(days=1)
        return {
            "today": today,
            "limits": limits,
            "sections": sections,
            "reset_at": tomorrow.isoformat(),
            "reset_in_seconds": int((tomorrow - now).total_seconds()),
        }

    def clear_history(self) -> None:
        """Wipe the JSONL log. Used by Settings → Usage → Reset quota
        (admin/debug feature)."""
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                f.write("")
            with self._lock:
                self._counts = defaultdict(lambda: defaultdict(int))
            logger.info("[quota] history cleared")
        except OSError as e:
            logger.error("[quota] failed to clear history: %s", e)


# ── Singleton ───────────────────────────────────────────────────────────

_SINGLETON: Optional[QuotaTracker] = None
_SINGLETON_LOCK = threading.Lock()


def get_quota_tracker() -> QuotaTracker:
    """Process-wide QuotaTracker singleton."""
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = QuotaTracker()
    return _SINGLETON
