"""
Tera Pilot v1.1 — Token Intelligence Service.

Tracks every token_in / token_out with timestamps, provider, model, chat_id.
Calculates running cost, burn rate, and budget projections.
Persists to ~/.tera_pilot/token_history.jsonl.
"""

from __future__ import annotations

import json
import logging
import time
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Pricing per 1K tokens (USD) ──────────────────────────────────────
# Sources: provider pricing pages. Bundled table is a fallback snapshot —
# fetch_live_pricing() in web_bridge.py refreshes this at runtime from
# OpenRouter's public models endpoint when the user asks for it.

MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Anthropic
    "claude-sonnet-5":              {"in": 0.003, "out": 0.015},
    "claude-opus-4-8":              {"in": 0.005, "out": 0.025},
    "claude-haiku-4-5-20251001":    {"in": 0.001, "out": 0.005},
    "claude-fable-5":               {"in": 0.003, "out": 0.015},
    "claude-opus-5":                {"in": 0.015, "out": 0.075},
    # OpenAI
    "gpt-5.5":                      {"in": 0.005, "out": 0.030},
    "gpt-5.4":                      {"in": 0.0025, "out": 0.015},
    "gpt-5.4-mini":                 {"in": 0.00075, "out": 0.0045},
    "gpt-5.4-nano":                 {"in": 0.0002, "out": 0.00125},
    # Google
    "gemini-3.1-pro":               {"in": 0.002, "out": 0.012},
    "gemini-3.5-flash":             {"in": 0.0003, "out": 0.0025},
    # xAI
    "grok-4.3":                     {"in": 0.002, "out": 0.006},
    "grok-4.5":                     {"in": 0.002, "out": 0.006},
    "grok-4.6":                     {"in": 0.002, "out": 0.006},
    # Z.ai
    "glm-5.1":                      {"in": 0.0006, "out": 0.0022},
    # Mistral
    "mistral-large-latest":         {"in": 0.002, "out": 0.006},
    # DeepSeek
    "deepseek-v4-pro":              {"in": 0.00028, "out": 0.00042},
    "deepseek-v4-flash":            {"in": 0.00007, "out": 0.00014},
    # Open-weight (Groq / Together / Fireworks / Cerebras / SambaNova)
    "meta-llama/llama-4-maverick-17b-128e-instruct": {"in": 0.00022, "out": 0.00088},
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8": {"in": 0.00022, "out": 0.00088},
    "accounts/fireworks/models/llama4-maverick-instruct-basic": {"in": 0.00022, "out": 0.00088},
    "Meta-Llama-4-Maverick-17B-128E-Instruct": {"in": 0.00022, "out": 0.00088},
    "llama-4-scout-17b-16e-instruct": {"in": 0.00011, "out": 0.00034},
    # Local — free
    "local":                        {"in": 0.0, "out": 0.0},
    "llama3.3":                     {"in": 0.0, "out": 0.0},
    "llama4":                       {"in": 0.0, "out": 0.0},
    # Legacy models kept for historical entries already on disk
    "claude-3-5-sonnet-20241022": {"in": 0.003, "out": 0.015},
    "claude-3-5-haiku-20241022":  {"in": 0.001, "out": 0.005},
    "claude-3-opus-20240229":      {"in": 0.015, "out": 0.075},
    "claude-sonnet-4-20250514":    {"in": 0.003, "out": 0.015},
    "claude-opus-4-20250514":      {"in": 0.015, "out": 0.075},
    "gpt-4o":                      {"in": 0.0025, "out": 0.01},
    "gpt-4o-mini":                 {"in": 0.00015, "out": 0.0006},
    "gpt-4-turbo":                 {"in": 0.01, "out": 0.03},
    "llama-3.3-70b-versatile":     {"in": 0.00059, "out": 0.00079},
    "llama-3.1-8b-instant":        {"in": 0.00005, "out": 0.00008},
    "deepseek-chat":               {"in": 0.00014, "out": 0.00028},
}

# Fallback pricing when model is not in the table
DEFAULT_PRICING = {"in": 0.003, "out": 0.015}

# Default monthly budget in USD, overridable via TokenTracker.stats(budget=...)
DEFAULT_BUDGET_USD = 20.0


# Populated at runtime by TokenTracker.set_live_pricing() when the app
# fetches current prices from the internet. Takes priority over the
# bundled MODEL_PRICING snapshot above.
_LIVE_PRICING: Dict[str, Dict[str, float]] = {}
_LIVE_PRICING_FETCHED_AT: Optional[float] = None


def _effective_pricing(model: str) -> Dict[str, float]:
    if model in _LIVE_PRICING:
        return _LIVE_PRICING[model]
    return MODEL_PRICING.get(model, DEFAULT_PRICING)


@dataclass
class TokenEntry:
    """One token usage record."""
    ts: float                    # unix timestamp
    provider: str                # "anthropic" | "openai" | "openrouter" | "groq" | "local"
    model: str                   # model identifier
    tokens_in: int
    tokens_out: int
    chat_id: str = ""
    session_id: str = ""

    @property
    def cost(self) -> float:
        pricing = _effective_pricing(self.model)
        return (self.tokens_in * pricing["in"] + self.tokens_out * pricing["out"]) / 1000.0

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_jsonl(self) -> str:
        d = self.to_dict()
        d["cost"] = round(self.cost, 6)
        return json.dumps(d)


class TokenTracker:
    """
    Records token usage, computes running stats, projects burn rate.
    Thread-safe. Persists to disk.

    v1.1.5-fix (tera_pilot_bug_report.md bug #13): unlike ``quota.py`` (which
    prunes records older than 30 days) and ``memory_service.py`` (which
    caps the number of sessions at 200), the token history had NO
    rotation or cap — ``token_history.jsonl`` grew forever, and
    ``stats()`` (called on every UI panel refresh) walked the entire
    list every time, so the UI got slower the longer Tera Pilot was used.

    We now:
    * Prune entries older than ``ROTATION_DAYS`` (default 30) on every
      ``record()`` call (cheap — we just check the newest vs oldest
      entry's timestamp; full sweep only triggers when the file grows
      past ``ROTATION_CHECK_INTERVAL`` entries).
    * Cap the in-memory list at ``MAX_ENTRIES`` (default 10 000) so
      that even a runaway script recording thousands of entries per
      second can't blow up memory.
    * Rewrite the JSONL file atomically after pruning so the on-disk
      file also stays bounded.
    """

    # v1.1.5-fix (bug #13): records older than this are pruned. Same
    # value as ``quota.py::_ROTATION_DAYS`` for consistency.
    ROTATION_DAYS = 30

    # v1.1.5-fix (bug #13): hard cap on the number of entries kept
    # in memory AND on disk. 10k entries × ~200 bytes/entry ≈ 2 MB —
    # comfortably small, but enough for ~50 requests/day × 30 days
    # (= 1500 entries), with plenty of headroom for bursts.
    MAX_ENTRIES = 10_000

    # v1.1.5-fix (bug #13): only run the rotation sweep when the
    # entry count grows past this multiple of the last sweep's count.
    # This avoids running an O(n) sweep on every single record().
    # 1.25 → sweep runs at most once every 25 % growth, i.e. if the
    # last sweep left 1000 entries, the next sweep runs at ~1250.
    ROTATION_CHECK_GROWTH = 1.25

    def __init__(self, persist_path: Optional[Path] = None):
        self._lock = threading.RLock()
        self._entries: List[TokenEntry] = []
        self._persist_path = persist_path or (Path.home() / ".tera_pilot" / "token_history.jsonl")
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        # v1.1.5-fix (bug #13): bookkeeping for lazy rotation. After a
        # sweep we remember how many entries were left, so we don't
        # sweep again until the list has grown materially.
        self._last_sweep_count: int = 0
        self._load()

    # ── Recording ──────────────────────────────────────────────────

    def record(
        self,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        chat_id: str = "",
        session_id: str = "",
    ) -> TokenEntry:
        entry = TokenEntry(
            ts=time.time(),
            provider=provider,
            model=model,
            tokens_in=max(0, tokens_in),
            tokens_out=max(0, tokens_out),
            chat_id=chat_id,
            session_id=session_id,
        )
        with self._lock:
            self._entries.append(entry)
            self._persist_entry(entry)
            # v1.1.5-fix (bug #13): opportunistic rotation. Cheap check
            # — we only run the O(n) sweep when the list has grown by
            # ROTATION_CHECK_GROWTH since the last sweep, so the
            # per-record overhead is typically just a length comparison.
            self._maybe_rotate()
        return entry

    # ── v1.1.5-fix (bug #13): rotation ────────────────────────────

    def _maybe_rotate(self) -> None:
        """Prune entries older than ``ROTATION_DAYS`` and cap the list
        at ``MAX_ENTRIES``. Must be called under ``self._lock``.

        Cheap in the common case: we only do an O(n) sweep when the list
        has grown by ``ROTATION_CHECK_GROWTH`` (25 %) since the last
        sweep, otherwise we just compare lengths and return.
        """
        n = len(self._entries)
        if n == 0:
            self._last_sweep_count = 0
            return
        # Skip the sweep until we've grown past the threshold. The +1
        # handles the "last sweep left 0 entries" edge case.
        threshold = max(1, int(self._last_sweep_count * self.ROTATION_CHECK_GROWTH)) + 1
        if n < threshold and n < self.MAX_ENTRIES:
            return

        cutoff = time.time() - (self.ROTATION_DAYS * 86400.0)
        before = n
        kept = [e for e in self._entries if e.ts >= cutoff]
        # Also enforce the hard cap (keep the most recent ones).
        if len(kept) > self.MAX_ENTRIES:
            kept = kept[-self.MAX_ENTRIES:]
        pruned = before - len(kept)
        if pruned > 0:
            self._entries = kept
            # Rewrite the on-disk file so it shrinks too. We do this
            # synchronously because (a) it's rare (only when pruning
            # actually happened) and (b) we're already under the lock,
            # so a deferred write would have to take the lock again.
            try:
                self._rewrite_persist_file_locked()
            except Exception as e:
                logger.warning(f"[token_tracker] rewrite-after-rotate failed: {e}")
        self._last_sweep_count = len(self._entries)

    def _rewrite_persist_file_locked(self) -> None:
        """Atomically rewrite the JSONL file from ``self._entries``.

        Must be called under ``self._lock``. Used by ``_maybe_rotate``
        and by ``clear_history`` (via the empty-list path).

        v1.1.5-fix (bug #13): writes to a tempfile in the same directory
        and ``os.replace``s it into place, so a crash mid-write can't
        leave a half-written file (which would silently corrupt the
        token history).
        """
        import os as _os
        import tempfile as _tf
        parent = self._persist_path.parent
        try:
            _os.makedirs(str(parent), exist_ok=True)
        except OSError:
            pass
        fd, tmp_path = _tf.mkstemp(prefix=".tok_", suffix=".tmp", dir=str(parent))
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                for e in self._entries:
                    f.write(e.to_jsonl() + "\n")
            _os.replace(tmp_path, self._persist_path)
        except Exception:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ── Queries ────────────────────────────────────────────────────

    def stats(self, *, last_seconds: Optional[float] = None, budget: float = DEFAULT_BUDGET_USD) -> Dict[str, Any]:
        """Aggregate stats, optionally filtered to a recent window.

        v1.0.5-correctness: the old ``burn_rate`` calculation divided
        the ALL-TIME total cost by a window clamped to 600 seconds,
        overstating the burn rate by up to N× (e.g. 12× for a 2-hour
        session). The clamp only makes sense if ``total_cost`` is also
        recomputed over the same window. We now compute both ``window``
        and ``window_cost`` from the same entries (BUGS_REPORT H-TOK-1).

        Also: ``budget_used_pct`` used the all-time total against a
        monthly budget, so after the first month it was permanently
        clamped at 100%. We now filter to the current calendar month
        for the budget calculation.
        """
        with self._lock:
            if last_seconds:
                cutoff = time.time() - last_seconds
                entries = [e for e in self._entries if e.ts >= cutoff]
            else:
                entries = list(self._entries)

            if not entries:
                return {
                    "total_tokens_in": 0, "total_tokens_out": 0, "total_tokens": 0,
                    "total_cost": 0.0, "request_count": 0, "entries": [],
                    "burn_rate_per_min": 0.0, "budget_minutes_left": None,
                    "budget_usd": budget, "budget_used_pct": 0.0,
                }

            total_in = sum(e.tokens_in for e in entries)
            total_out = sum(e.tokens_out for e in entries)
            total_cost = sum(e.cost for e in entries)

            # v1.0.5-correctness: compute burn rate over the SAME window
            # as the cost sum. Previously the window was clamped to 600s
            # but total_cost was the all-time sum — so a 2h session got
            # burn_rate = total_cost / 10min (12× overstatement).
            #
            # Burn rate is meant to be a short-term signal ("am I
            # spending fast right now?"), so we use the last 10 minutes
            # of activity (or the available window if shorter).
            burn_window_seconds = 600.0
            now = time.time()
            recent = [e for e in entries if e.ts >= now - burn_window_seconds]
            if len(recent) > 1:
                actual_window = max(1.0, recent[-1].ts - recent[0].ts)
                # Cap to burn_window_seconds so a single very old entry
                # in the window doesn't inflate the divisor.
                actual_window = min(actual_window, burn_window_seconds)
                window_cost = sum(e.cost for e in recent)
                burn_rate = window_cost / (actual_window / 60.0)
            elif len(entries) > 1:
                # Fewer than 2 entries in the last 10 min — fall back to
                # the full-window calculation, but use the actual span.
                actual_window = max(1.0, entries[-1].ts - entries[0].ts)
                window_cost = total_cost
                burn_rate = window_cost / (actual_window / 60.0)
            else:
                burn_rate = 0.0

            # Budget projection: how many minutes left at the current burn rate,
            # against the user-configured monthly budget (default $20).
            minutes_left = (budget / burn_rate) if burn_rate > 0 else None

            # v1.0.5-correctness: budget_used_pct should reflect the
            # CURRENT MONTH's spend against the monthly budget, not the
            # all-time spend. Without this, after the first month the
            # percentage was permanently clamped at 100%.
            import calendar
            import datetime as _dt
            now_dt = _dt.datetime.now()
            month_start_ts = _dt.datetime(now_dt.year, now_dt.month, 1).timestamp()
            month_end_ts = (
                _dt.datetime(now_dt.year, now_dt.month,
                             calendar.monthrange(now_dt.year, now_dt.month)[1], 23, 59, 59).timestamp()
            )
            month_cost = sum(e.cost for e in self._entries
                             if month_start_ts <= e.ts <= month_end_ts)
            budget_used_pct = (
                round(min(100.0, (month_cost / budget) * 100), 1)
                if budget > 0 else 0.0
            )

            return {
                "total_tokens_in": total_in,
                "total_tokens_out": total_out,
                "total_tokens": total_in + total_out,
                "total_cost": round(total_cost, 4),
                "request_count": len(entries),
                "burn_rate_per_min": round(burn_rate, 4),
                "budget_minutes_left": round(minutes_left, 1) if minutes_left else None,
                "budget_usd": budget,
                "budget_used_pct": budget_used_pct,
                "month_cost": round(month_cost, 4),
                "entries": [e.to_dict() | {"cost": round(e.cost, 6)} for e in entries[-50:]],  # last 50 for UI
            }

    def session_stats(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            entries = [e for e in self._entries if e.session_id == session_id]
        if not entries:
            return {"total_cost": 0.0, "total_tokens": 0, "request_count": 0}
        return {
            "total_tokens": sum(e.total_tokens for e in entries),
            "total_cost": round(sum(e.cost for e in entries), 4),
            "request_count": len(entries),
        }

    def provider_breakdown(self) -> List[Dict[str, Any]]:
        """Cost breakdown by provider."""
        with self._lock:
            by_provider: Dict[str, Dict] = {}
            for e in self._entries:
                if e.provider not in by_provider:
                    by_provider[e.provider] = {"provider": e.provider, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "requests": 0}
                p = by_provider[e.provider]
                p["tokens_in"] += e.tokens_in
                p["tokens_out"] += e.tokens_out
                p["cost"] += e.cost
                p["requests"] += 1
        return sorted(
            [{**v, "cost": round(v["cost"], 4)} for v in by_provider.values()],
            key=lambda x: x["cost"],
            reverse=True,
        )

    def model_pricing_info(self, model: str) -> Dict[str, Any]:
        pricing = _effective_pricing(model)
        return {"model": model, "price_per_1k_in": pricing["in"], "price_per_1k_out": pricing["out"]}

    def pricing_table(self) -> Dict[str, Any]:
        """Return the full effective pricing table (live overrides merged over defaults)."""
        merged = dict(MODEL_PRICING)
        merged.update(_LIVE_PRICING)
        return {
            "pricing": merged,
            "live": bool(_LIVE_PRICING),
            "fetched_at": _LIVE_PRICING_FETCHED_AT,
        }

    def set_live_pricing(self, pricing: Dict[str, Dict[str, float]]) -> None:
        """Called after a successful internet pricing fetch."""
        global _LIVE_PRICING, _LIVE_PRICING_FETCHED_AT
        with self._lock:
            _LIVE_PRICING = dict(pricing or {})
            _LIVE_PRICING_FETCHED_AT = time.time()

    # ── Persistence ────────────────────────────────────────────────

    def _persist_entry(self, entry: TokenEntry) -> None:
        try:
            with open(self._persist_path, "a", encoding="utf-8") as f:
                f.write(entry.to_jsonl() + "\n")
        except OSError as e:
            logger.warning(f"[token_tracker] persist error: {e}")

    def _load(self) -> None:
        """Load history from disk on startup.

        v1.1.5-fix (bug #13): drop entries older than ``ROTATION_DAYS``
        on load, and cap the in-memory list at ``MAX_ENTRIES``. The
        on-disk file is rewritten atomically if any pruning happened,
        so old bloated histories self-heal on the first launch after
        the fix.
        """
        if not self._persist_path.exists():
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                entries_to_add = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        entry = TokenEntry(
                            ts=d["ts"], provider=d["provider"], model=d["model"],
                            tokens_in=d["tokens_in"], tokens_out=d["tokens_out"],
                            chat_id=d.get("chat_id", ""), session_id=d.get("session_id", ""),
                        )
                        entries_to_add.append(entry)
                    except (json.JSONDecodeError, KeyError):
                        continue

            with self._lock:
                self._entries.extend(entries_to_add)
                # v1.1.5-fix (bug #13): prune-on-load. This both
                # bounds memory and self-heals bloated JSONL files left
                # behind by older versions of Tera Pilot (which had no cap).
                # `_maybe_rotate` is normally called from `record()`,
                # but we force a sweep here by setting the threshold
                # artificially low.
                self._last_sweep_count = 0  # force the sweep
                pre = len(self._entries)
                self._maybe_rotate()
                post = len(self._entries)
                if pre != post:
                    logger.info(
                        "[token_tracker] pruned %d stale entries on load (%d → %d)",
                        pre - post, pre, post,
                    )
            logger.info(f"[token_tracker] loaded {len(entries_to_add)} entries from {self._persist_path}")
        except OSError as e:
            logger.warning(f"[token_tracker] load error: {e}")

    def clear_history(self) -> None:
        with self._lock:
            self._entries.clear()
            # v1.1.5-fix (bug #13): reset rotation bookkeeping so the
            # next record() doesn't try to compare against a stale
            # _last_sweep_count.
            self._last_sweep_count = 0
            try:
                self._persist_path.write_text("")
            except OSError:
                pass


# ── Module-level singleton ──────────────────────────────────────────

_tracker: Optional[TokenTracker] = None
_tracker_lock = threading.Lock()


def get_token_tracker() -> TokenTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = TokenTracker()
    return _tracker