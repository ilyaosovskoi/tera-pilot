"""
Token Budget — predictable limits & token efficiency.

Goal (G3): "Predictable limits / token efficiency — limits break
workflows, 4x tokens vs Codex wasted."

Three orthogonal knobs, all stored in ``~/.tera_pilot/config.json`` under
the ``token_budget`` key:

1. **Hard cost caps** — daily_usd, monthly_usd. When the cap is hit,
   ``check_budget()`` returns ``exceeded=True`` with a friendly reason
   and the AgentRuntime short-circuits the next iteration with a
   visible error (instead of silently failing on the provider call).

2. **Per-turn efficiency knobs** — max_tokens_per_turn (output cap
   passed to the provider so the model stops generating runaway text),
   max_iterations (caps the ReAct loop independently of max_tokens),
   compaction_threshold_pct (lower = compacter context sooner, but
   more LLM calls for summaries; higher = bigger prompts but fewer
   summary calls).

3. **Predictable-mode flag** — when ON, the runtime behaves the same
   way every turn:
     - max_iterations is enforced strictly (no "+1 if it looks like
       the model is about to finish").
     - compaction fires at a fixed threshold (no adaptive backoff).
     - the system prompt is identical every turn (no dynamic tool
       catalog changes mid-turn).
   This costs a bit of flexibility but makes token usage predictable
   enough to plan around — the original "4x vs Codex" complaint was
   largely because of adaptive behaviours that made each turn a
   surprise.

4. **Prompt caching flag** — when ON (default), stable parts of the
   prompt (system prompt, tool catalog, project instructions) are
   marked as cacheable. Providers that support prompt caching
   (Anthropic, OpenAI, Gemini) skip re-tokenizing them on every call,
   which cuts input-token cost by 60-90% on long sessions. The flag
   is a no-op for providers without cache support.

The module is data-only — it doesn't touch the AgentRuntime directly.
``AgentRuntime`` reads the config on each turn via
``get_token_budget()``, and the TUI / GUI bridges expose setter
methods that persist to disk.
"""

from __future__ import annotations

import calendar
import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────

# Cost caps: $0 means "no cap" (unlimited). Most users won't set a
# daily cap but will set a monthly one as a sanity check.
_DEFAULT_DAILY_USD: float = 0.0
_DEFAULT_MONTHLY_USD: float = 20.0

# Per-turn knobs. max_tokens_per_turn = 0 means "use provider default".
# max_iterations matches AgentRuntime's default of 8 — overriding
# here lets the user trim it without touching the runtime constructor.
_DEFAULT_MAX_TOKENS_PER_TURN: int = 0
_DEFAULT_MAX_ITERATIONS: int = 8
_DEFAULT_COMPACTION_THRESHOLD_PCT: int = 85

# Efficiency flags. Both default ON — they're pure wins for most users.
_DEFAULT_PROMPT_CACHING: bool = True
_DEFAULT_PREDICTABLE_MODE: bool = False


@dataclass
class TokenBudget:
    """User-facing token budget configuration."""
    daily_usd: float = _DEFAULT_DAILY_USD
    monthly_usd: float = _DEFAULT_MONTHLY_USD
    max_tokens_per_turn: int = _DEFAULT_MAX_TOKENS_PER_TURN
    max_iterations: int = _DEFAULT_MAX_ITERATIONS
    compaction_threshold_pct: int = _DEFAULT_COMPACTION_THRESHOLD_PCT
    prompt_caching: bool = _DEFAULT_PROMPT_CACHING
    predictable_mode: bool = _DEFAULT_PREDICTABLE_MODE

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TokenBudget":
        return cls(
            daily_usd=float(d.get("daily_usd", _DEFAULT_DAILY_USD)),
            monthly_usd=float(d.get("monthly_usd", _DEFAULT_MONTHLY_USD)),
            max_tokens_per_turn=int(d.get("max_tokens_per_turn", _DEFAULT_MAX_TOKENS_PER_TURN)),
            max_iterations=int(d.get("max_iterations", _DEFAULT_MAX_ITERATIONS)),
            compaction_threshold_pct=int(d.get("compaction_threshold_pct", _DEFAULT_COMPACTION_THRESHOLD_PCT)),
            prompt_caching=bool(d.get("prompt_caching", _DEFAULT_PROMPT_CACHING)),
            predictable_mode=bool(d.get("predictable_mode", _DEFAULT_PREDICTABLE_MODE)),
        )


@dataclass
class BudgetCheckResult:
    """Result of a budget check — returned by ``check_budget()``."""
    exceeded: bool = False
    reason: str = ""
    daily_used: float = 0.0
    daily_cap: float = 0.0
    monthly_used: float = 0.0
    monthly_cap: float = 0.0
    day_used_pct: float = 0.0
    month_used_pct: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Config persistence ───────────────────────────────────────────────

_CONFIG_KEY = "token_budget"
_config_lock = threading.RLock()


def _config_path() -> Path:
    return Path.home() / ".tera_pilot" / "config.json"


def _load_full_config() -> Dict[str, Any]:
    try:
        if _config_path().exists():
            with open(_config_path(), "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def _save_full_config(cfg: Dict[str, Any]) -> None:
    try:
        _config_path().parent.mkdir(parents=True, exist_ok=True)
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.warning("[token_budget] failed to save config: %s", e)


def get_token_budget() -> TokenBudget:
    """Load the token budget from disk (or defaults if unset)."""
    with _config_lock:
        cfg = _load_full_config()
        raw = cfg.get(_CONFIG_KEY, {}) or {}
        return TokenBudget.from_dict(raw)


def set_token_budget(
    *,
    daily_usd: Optional[float] = None,
    monthly_usd: Optional[float] = None,
    max_tokens_per_turn: Optional[int] = None,
    max_iterations: Optional[int] = None,
    compaction_threshold_pct: Optional[int] = None,
    prompt_caching: Optional[bool] = None,
    predictable_mode: Optional[bool] = None,
) -> TokenBudget:
    """Update fields of the token budget. Only non-None fields change.

    Returns the new TokenBudget after persisting.
    """
    with _config_lock:
        cur = get_token_budget()
        if daily_usd is not None:
            cur.daily_usd = max(0.0, float(daily_usd))
        if monthly_usd is not None:
            cur.monthly_usd = max(0.0, float(monthly_usd))
        if max_tokens_per_turn is not None:
            cur.max_tokens_per_turn = max(0, int(max_tokens_per_turn))
        if max_iterations is not None:
            cur.max_iterations = max(1, min(50, int(max_iterations)))
        if compaction_threshold_pct is not None:
            cur.compaction_threshold_pct = max(50, min(95, int(compaction_threshold_pct)))
        if prompt_caching is not None:
            cur.prompt_caching = bool(prompt_caching)
        if predictable_mode is not None:
            cur.predictable_mode = bool(predictable_mode)

        cfg = _load_full_config()
        cfg[_CONFIG_KEY] = cur.to_dict()
        _save_full_config(cfg)
        return cur


def reset_token_budget() -> TokenBudget:
    """Restore defaults."""
    with _config_lock:
        cfg = _load_full_config()
        cfg[_CONFIG_KEY] = TokenBudget().to_dict()
        _save_full_config(cfg)
        return TokenBudget()


# ── Budget check (called by AgentRuntime before each LLM call) ───────

def _day_window() -> tuple[float, float]:
    """Return (start_ts, end_ts) for the current local day."""
    now = datetime.now()
    start = datetime(now.year, now.month, now.day, 0, 0, 0).timestamp()
    end = datetime(now.year, now.month, now.day, 23, 59, 59).timestamp()
    return start, end


def _month_window() -> tuple[float, float]:
    """Return (start_ts, end_ts) for the current calendar month."""
    now = datetime.now()
    start = datetime(now.year, now.month, 1, 0, 0, 0).timestamp()
    last_day = calendar.monthrange(now.year, now.month)[1]
    end = datetime(now.year, now.month, last_day, 23, 59, 59).timestamp()
    return start, end


def check_budget(
    budget: Optional[TokenBudget] = None,
    token_tracker: Optional[Any] = None,
) -> BudgetCheckResult:
    """Check whether the user has blown a daily or monthly cap.

    Args:
        budget: the TokenBudget to check against. If None, loads from disk.
        token_tracker: a TokenTracker with .stats() returning cost data.
            If None, returns ``exceeded=False`` (can't check without data).

    Returns:
        BudgetCheckResult. ``exceeded=True`` means the AgentRuntime
        should short-circuit with a friendly error.
    """
    if budget is None:
        budget = get_token_budget()
    if token_tracker is None:
        return BudgetCheckResult(
            daily_cap=budget.daily_usd,
            monthly_cap=budget.monthly_usd,
        )

    try:
        token_tracker.stats()  # verify tracker is functional; result unused
    except Exception as e:
        logger.debug("[token_budget] tracker stats failed: %s", e)
        return BudgetCheckResult(
            daily_cap=budget.daily_usd,
            monthly_cap=budget.monthly_usd,
        )

    # The token tracker keeps a flat list of entries with .ts and .cost.
    # We recompute day and month windows from the raw entries to avoid
    # depending on the tracker's stats() format (which has changed
    # before).
    try:
        with token_tracker._lock:
            entries = list(token_tracker._entries)
    except Exception:
        entries = []

    day_start, day_end = _day_window()
    month_start, month_end = _month_window()
    day_cost = sum(e.cost for e in entries if day_start <= e.ts <= day_end)
    month_cost = sum(e.cost for e in entries if month_start <= e.ts <= month_end)

    day_pct = (day_cost / budget.daily_usd * 100) if budget.daily_usd > 0 else 0.0
    month_pct = (month_cost / budget.monthly_usd * 100) if budget.monthly_usd > 0 else 0.0

    exceeded = False
    reason = ""
    if budget.daily_usd > 0 and day_cost >= budget.daily_usd:
        exceeded = True
        reason = (
            f"Daily token budget reached: ${day_cost:.4f} / ${budget.daily_usd:.2f}. "
            f"Resets at local midnight."
        )
    elif budget.monthly_usd > 0 and month_cost >= budget.monthly_usd:
        exceeded = True
        reason = (
            f"Monthly token budget reached: ${month_cost:.4f} / ${budget.monthly_usd:.2f}. "
            f"Resets on the 1st of next month."
        )

    return BudgetCheckResult(
        exceeded=exceeded,
        reason=reason,
        daily_used=round(day_cost, 6),
        daily_cap=budget.daily_usd,
        monthly_used=round(month_cost, 6),
        monthly_cap=budget.monthly_usd,
        day_used_pct=round(day_pct, 1),
        month_used_pct=round(month_pct, 1),
    )


# ── Helpers for AgentRuntime integration ────────────────────────────

def get_max_tokens_for_provider(provider_default: int = 4096) -> int:
    """Return the per-turn output cap, falling back to the provider's default.

    The AgentRuntime calls this when constructing the ProviderConfig so
    the user's ``max_tokens_per_turn`` setting actually takes effect
    instead of being silently overridden by the provider's default.
    """
    budget = get_token_budget()
    if budget.max_tokens_per_turn > 0:
        return budget.max_tokens_per_turn
    return provider_default


def should_compact_now(used_pct: float, budget: Optional[TokenBudget] = None) -> bool:
    """Decide whether the runtime should auto-compact right now.

    ``used_pct`` is the ContextMemory's current utilization
    (0-100). We compare against the configured threshold.

    In predictable mode this is a hard cutoff. In normal mode we add
    a 5% hysteresis band — if the user just compacted, we don't
    re-compact at 86% just because the threshold is 85%.
    """
    if budget is None:
        budget = get_token_budget()
    threshold = budget.compaction_threshold_pct
    if budget.predictable_mode:
        return used_pct >= threshold
    return used_pct >= threshold + 5.0  # hysteresis
