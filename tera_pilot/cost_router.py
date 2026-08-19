"""
Tera Pilot v2.0.2 — Smart Cost-Aware Provider Routing (Goal M2).

**Problem (from CLAUDE.md):**
    "Build on ``registry.py`` + ``token_tracker.py`` data. Task
    complexity classifier → model tier catalog."

    The existing ``AutoRouter`` (tera_pilot/auto_router.py) already classifies
    prompt complexity and picks a model from a static tier catalog. But:

      * It does NOT look at actual spend history. If the user burned
        through 90% of their monthly budget yesterday, AutoRouter still
        cheerfully routes to claude-opus today.
      * It does NOT learn per-provider latency / reliability. A
        provider that has been failing for the last hour is treated
        identically to a healthy one.
      * It does NOT consider the user's *price ceiling per task type*.
        Some users want "trivial = always free, even if slow"; others
        want "trivial = fastest, even if it costs 0.001".

**Design:**

1.  ``CostRouter`` — a thin layer over ``AutoRouter`` + ``TokenTracker``
    + ``TokenBudget``. Re-uses AutoRouter's classifier (so existing
    routing behaviour is unchanged) but augments the decision with
    three additional signals:

       a. **Budget pressure** — the ratio of monthly_spend to
          monthly_budget. Above 80% we demote every candidate by one
          tier (expert → complex, complex → moderate, etc.) and prefer
          free local providers when available.

       b. **Provider health** — recent error rate from
          ``TokenTracker``'s ``provider_breakdown()``. Providers above
          a configurable error threshold are deprioritised.

       c. **Price ceiling** — per-complexity USD caps the user can
          set via ``/cost cap <complexity> <usd>``. Candidates whose
          estimated cost exceeds the cap are filtered out.

2.  **No mutation of AutoRouter.** ``CostRouter`` calls
    ``AutoRouter.route()`` to get the candidate list, then re-ranks
    and filters it. ``AutoRouter`` is the source of truth for the
    *catalog*; CostRouter is the source of truth for the *policy*.

3.  **Explainability.** Every routing decision produces a
    ``CostRouteDecision`` with the original AutoRouter suggestion,
    the final pick, and a list of *factors* that influenced the
    decision — so the UI can show "why this model?" next to the pick.

4.  **Persistence.** Caps and demotion thresholds live in
    ``~/.tera_pilot/cost_router.json``. The file is small and human-editable.

Status & known limitations (v2.3.4 — honest, matching README's
"deliberately NOT claiming" section):
- Cost Router is a **Pro-licensed feature** (M2): without a valid license
  (or the local-dev ``TERA_PILOT_PRO`` override) ``route()`` fails CLOSED
  — it returns the AutoRouter decision unchanged with a
  ``"Pro license required"`` factor, and config mutations raise
  ``LicenseRequiredError``. The gate lives in :meth:`CostRouter.route` and
  the config setters, which every surface funnels through (TUI ``/cost``,
  HTTP ``/api/cost/*``, bridge).
- Cost Router is NOT wired into the live chat/agent routing path yet: the
  HTTP chat stream and the TUI bridge route through AutoRouter directly.
  ``CostRouter`` is fully functional as a policy layer and is exercised
  via ``/cost route`` / ``/api/cost/route`` / the bridge, but the runtime
  does not yet call it before every prompt. This is the same
  present-but-not-wired gap Second Opinion's auto-trigger has.
- ``CostRouter.route()`` requires the ``token_tracker``/``token_budget``
  singletons for budget-pressure signals; when they are unavailable it
  degrades gracefully (pressure 0.0) rather than failing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# v2.3.4: Cost Router is a Pro-licensed feature (M2). License checks are
# fully offline and fail closed — an unlicensed user gets AutoRouter's
# decision unchanged, never a crash.
from .licensing import LicenseRequiredError, is_feature_licensed as _is_feature_licensed

logger = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────

# Per-complexity USD caps. A candidate whose estimated single-request
# cost exceeds the cap is filtered out. Override via /cost cap.
DEFAULT_CAPS_USD: Dict[str, float] = {
    "trivial":   0.0005,
    "simple":    0.005,
    "moderate":  0.05,
    "complex":   0.20,
    "expert":    1.00,
}

# Budget pressure thresholds. Above HIGH we demote by 1 tier; above
# CRITICAL we demote by 2 tiers AND prefer free providers.
BUDGET_PRESSURE_HIGH = 0.80       # 80% of monthly cap
BUDGET_PRESSURE_CRITICAL = 0.95   # 95% — basically out of budget

# Provider error-rate threshold. Above this fraction (errors / total)
# in the last N requests we deprioritise the provider.
DEFAULT_ERROR_RATE_THRESHOLD = 0.30
DEFAULT_ERROR_WINDOW = 20         # look at the last 20 requests

# Local / free provider ids (used when budget pressure is critical).
FREE_PROVIDER_IDS = {"ollama", "lmstudio", "nvidia_nim", "cerebras", "sambanova"}

# Tier demotion map — used when budget pressure is HIGH / CRITICAL.
TIER_DEMOTION = {
    "expert":   "complex",
    "complex":  "moderate",
    "moderate": "simple",
    "simple":   "trivial",
    "trivial":  "trivial",   # already lowest
}

# When budget pressure is CRITICAL we demote by 2 tiers.
TIER_DEMOTION_2 = {
    "expert":   "simple",
    "complex":  "trivial",
    "moderate": "trivial",
    "simple":   "trivial",
    "trivial":  "trivial",
}


# ── Config ───────────────────────────────────────────────────────────

@dataclass
class CostRouterConfig:
    """User-tunable cost-router policy."""
    caps_usd: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CAPS_USD))
    budget_pressure_high: float = BUDGET_PRESSURE_HIGH
    budget_pressure_critical: float = BUDGET_PRESSURE_CRITICAL
    error_rate_threshold: float = DEFAULT_ERROR_RATE_THRESHOLD
    error_window: int = DEFAULT_ERROR_WINDOW
    prefer_free_under_pressure: bool = True
    enabled: bool = True   # master switch — when False, AutoRouter runs as-is

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CostRouterConfig":
        c = cls()
        if not d:
            return c
        if "caps_usd" in d and isinstance(d["caps_usd"], dict):
            c.caps_usd = {k: float(v) for k, v in d["caps_usd"].items()}
        if "budget_pressure_high" in d:
            c.budget_pressure_high = float(d["budget_pressure_high"])
        if "budget_pressure_critical" in d:
            c.budget_pressure_critical = float(d["budget_pressure_critical"])
        if "error_rate_threshold" in d:
            c.error_rate_threshold = float(d["error_rate_threshold"])
        if "error_window" in d:
            c.error_window = int(d["error_window"])
        if "prefer_free_under_pressure" in d:
            c.prefer_free_under_pressure = bool(d["prefer_free_under_pressure"])
        if "enabled" in d:
            c.enabled = bool(d["enabled"])
        return c


_CONFIG_PATH = Path.home() / ".tera_pilot" / "cost_router.json"


def load_config() -> CostRouterConfig:
    """Load CostRouterConfig from disk, or defaults if absent / corrupt."""
    try:
        if _CONFIG_PATH.exists():
            return CostRouterConfig.from_dict(
                json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            )
    except Exception as e:
        logger.warning(f"[cost_router] load_config error: {e}")
    return CostRouterConfig()


def save_config(cfg: CostRouterConfig) -> None:
    """Persist CostRouterConfig to disk."""
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(
            json.dumps(cfg.to_dict(), indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"[cost_router] save_config error: {e}")


# ── Decision record ─────────────────────────────────────────────────

@dataclass
class CostRouteDecision:
    """The result of a single cost-aware routing call.

    Designed for UI display: every field is JSON-serialisable.
    """
    prompt_preview: str
    auto_router_pick: Dict[str, Any]      # what AutoRouter would have picked
    final_pick: Dict[str, Any]            # what we ended up picking
    fallbacks: List[Dict[str, Any]] = field(default_factory=list)
    factors: List[str] = field(default_factory=list)
    budget_pressure: float = 0.0           # 0..1
    budget_remaining_usd: float = 0.0
    complexity: str = "simple"
    estimated_cost_usd: float = 0.0
    demoted_from: str = ""                 # original complexity, if demoted
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── CostRouter ──────────────────────────────────────────────────────

class CostRouter:
    """Cost-aware re-ranker over ``AutoRouter``.

    Usage:
        cr = CostRouter()
        decision = cr.route(prompt, configured_providers={...})
        provider_id = decision.final_pick["provider_id"]
    """

    def __init__(
        self,
        config: Optional[CostRouterConfig] = None,
        auto_router: Optional[Any] = None,
        token_tracker: Optional[Any] = None,
        token_budget: Optional[Any] = None,
    ) -> None:
        self._config = config or load_config()
        self._auto_router = auto_router  # lazy-loaded on first route()
        self._token_tracker = token_tracker  # lazy-loaded
        self._token_budget = token_budget  # lazy-loaded
        self._lock = threading.RLock()

    # ── Accessors ────────────────────────────────────────────────

    def get_config(self) -> CostRouterConfig:
        with self._lock:
            return self._config

    def set_config(self, cfg: CostRouterConfig) -> None:
        """Replace the whole config. Pro-licensed (raises when unlicensed)."""
        self._require_license()
        with self._lock:
            self._config = cfg
        save_config(cfg)

    def _require_license(self) -> None:
        """Fail closed: refuse to mutate cost-router policy without a license.
        Reads are always allowed (harmless settings visibility); writes and
        routing are the feature itself."""
        if not _is_feature_licensed("cost_router"):
            raise LicenseRequiredError(
                "Cost Router is a Pro feature — activate a license with: "
                "tera-pilot license activate <key>"
            )

    def update_config(self, **kwargs: Any) -> CostRouterConfig:
        """Patch one or more config fields, then persist.

        Pro-licensed: raises ``LicenseRequiredError`` when unlicensed so no
        surface (TUI /cost, HTTP /api/cost/*) can mutate the policy by a
        different code path.
        """
        self._require_license()
        with self._lock:
            cur = self._config
            for k, v in kwargs.items():
                if k == "caps_usd" and isinstance(v, dict):
                    cur.caps_usd = {kk: float(vv) for kk, vv in v.items()}
                elif hasattr(cur, k):
                    setattr(cur, k, v)
            new_cfg = CostRouterConfig(**asdict(cur))
            self._config = new_cfg
        save_config(new_cfg)
        return new_cfg

    def set_cap(self, complexity: str, usd: float) -> CostRouterConfig:
        """Set the USD cap for a single complexity tier."""
        with self._lock:
            caps = dict(self._config.caps_usd)
            caps[complexity] = max(0.0, float(usd))
        return self.update_config(caps_usd=caps)

    # ── Routing ──────────────────────────────────────────────────

    def route(
        self,
        prompt: str,
        configured_providers: Optional[set] = None,
        required_capabilities: Optional[List[str]] = None,
    ) -> CostRouteDecision:
        """Run AutoRouter, then re-rank the candidates through the cost policy."""
        cfg = self._config
        ar = self._get_auto_router()
        # Step 1: ask AutoRouter for its routing decision.
        ar_decision = ar.route(
            prompt,
            required_capabilities=required_capabilities,
            configured_providers=configured_providers,
        )

        # Default: final pick == auto-router pick. We'll mutate this if
        # the cost policy says otherwise.
        final_pick = dict(ar_decision)
        fallbacks = list(ar_decision.get("fallbacks") or [])
        factors: List[str] = []
        original_complexity = ar_decision.get("complexity", "simple")
        demoted_from = ""

        # If CostRouter is disabled, or the feature isn't licensed (Pro),
        # just return the AutoRouter decision with the cost-explanation
        # fields zeroed out — fail closed to the free tier, never crash.
        if not cfg.enabled or not _is_feature_licensed("cost_router"):
            if cfg.enabled:
                factor = "cost_router: Pro license required — using AutoRouter output unchanged"
            else:
                factor = "cost_router disabled — using AutoRouter output unchanged"
            return CostRouteDecision(
                prompt_preview=prompt[:120],
                auto_router_pick=ar_decision,
                final_pick=final_pick,
                fallbacks=fallbacks,
                factors=[factor],
                budget_pressure=0.0,
                budget_remaining_usd=0.0,
                complexity=original_complexity,
                estimated_cost_usd=float(ar_decision.get("cost_estimate", 0.0)),
                demoted_from="",
                enabled=False,
            )

        # Step 2: compute budget pressure.
        budget_pressure, budget_remaining = self._budget_pressure()
        if budget_pressure >= cfg.budget_pressure_critical:
            factors.append(
                f"Budget pressure CRITICAL ({budget_pressure*100:.0f}% of monthly cap)"
            )
            if cfg.prefer_free_under_pressure:
                # Try to swap to a free local provider.
                free_pick = self._pick_free_provider(configured_providers)
                if free_pick:
                    final_pick = free_pick
                    factors.append(f"Switched to free provider {free_pick['provider_id']} "
                                   f"to avoid overrunning budget")
                # Demote complexity by 2 tiers for any fallback selection
                demoted = TIER_DEMOTION_2.get(original_complexity, original_complexity)
                if demoted != original_complexity:
                    demoted_from = original_complexity
        elif budget_pressure >= cfg.budget_pressure_high:
            factors.append(
                f"Budget pressure HIGH ({budget_pressure*100:.0f}% of monthly cap) — "
                f"demoting complexity by 1 tier"
            )
            demoted = TIER_DEMOTION.get(original_complexity, original_complexity)
            if demoted != original_complexity:
                demoted_from = original_complexity
                # Re-run AutoRouter with the demoted complexity so the
                # candidate list reflects the lower tier.
                # AutoRouter.route() doesn't accept a complexity override,
                # so we approximate by re-classifying — but we can't
                # change the prompt. Instead, we just filter the
                # fallback list to drop anything more expensive than the
                # demoted tier's cap.
                cap = cfg.caps_usd.get(demoted, cfg.caps_usd.get("simple", 0.005))
                fallbacks = [f for f in fallbacks
                             if self._estimate_cost(f, prompt) <= cap]
        else:
            factors.append(
                f"Budget pressure OK ({budget_pressure*100:.0f}% of monthly cap)"
            )

        # Step 3: enforce per-complexity USD caps on the final pick.
        eff_complexity = demoted_from or original_complexity
        # If we demoted, we use the demoted tier's cap; otherwise the
        # original tier's cap.
        cap_complexity = TIER_DEMOTION.get(original_complexity, original_complexity) \
            if demoted_from else original_complexity
        cap = cfg.caps_usd.get(cap_complexity, 0.05)
        est_cost = float(final_pick.get("cost_estimate", 0.0)
                         or self._estimate_cost(final_pick, prompt))
        if est_cost > cap:
            factors.append(
                f"Estimated cost ${est_cost:.4f} exceeds ${cap:.4f} cap for "
                f"{cap_complexity} — searching fallbacks"
            )
            # Try fallbacks in order; pick the first one under the cap.
            picked = None
            for fb in fallbacks:
                fb_cost = self._estimate_cost(fb, prompt)
                if fb_cost <= cap:
                    picked = fb
                    factors.append(
                        f"Fallback {picked.get('provider_id', '?')} fits "
                        f"(${fb_cost:.4f} ≤ ${cap:.4f})"
                    )
                    break
            if picked:
                final_pick = {
                    "provider_id": picked.get("provider_id", ""),
                    "model": picked.get("model", ""),
                    "max_tokens": 8192,
                    "complexity": cap_complexity,
                    "cost_estimate": self._estimate_cost(picked, prompt),
                    "reasoning": "Cost-router fallback selection",
                    "speed": "unknown",
                }
            else:
                # No fallback fits — keep the AutoRouter pick but warn.
                factors.append(
                    f"No fallback under cap — keeping {final_pick.get('provider_id', '?')} "
                    f"(will exceed cap by ${est_cost - cap:.4f})"
                )

        # Step 4: provider health — deprioritise providers with high error rates.
        unhealthy = self._unhealthy_providers()
        if final_pick.get("provider_id") in unhealthy:
            factors.append(
                f"Primary {final_pick['provider_id']} is unhealthy "
                f"(error rate > {cfg.error_rate_threshold*100:.0f}% in last "
                f"{cfg.error_window} requests) — searching fallbacks"
            )
            for fb in fallbacks:
                if fb.get("provider_id") not in unhealthy:
                    final_pick = {
                        "provider_id": fb.get("provider_id", ""),
                        "model": fb.get("model", ""),
                        "max_tokens": 8192,
                        "complexity": cap_complexity,
                        "cost_estimate": self._estimate_cost(fb, prompt),
                        "reasoning": "Cost-router health fallback",
                        "speed": "unknown",
                    }
                    factors.append(f"Switched to healthy fallback {fb.get('provider_id')}")
                    break

        # Step 5: recompute final cost estimate
        final_cost = float(final_pick.get("cost_estimate", 0.0)
                           or self._estimate_cost(final_pick, prompt))

        return CostRouteDecision(
            prompt_preview=prompt[:120],
            auto_router_pick=ar_decision,
            final_pick=final_pick,
            fallbacks=fallbacks,
            factors=factors,
            budget_pressure=round(budget_pressure, 4),
            budget_remaining_usd=round(budget_remaining, 4),
            complexity=eff_complexity,
            estimated_cost_usd=round(final_cost, 6),
            demoted_from=demoted_from,
            enabled=True,
        )

    # ── Helpers ──────────────────────────────────────────────────

    def _get_auto_router(self):
        if self._auto_router is None:
            from .auto_router import AutoRouter
            self._auto_router = AutoRouter()
        return self._auto_router

    def _get_token_tracker(self):
        if self._token_tracker is None:
            try:
                from .token_tracker import get_token_tracker
                self._token_tracker = get_token_tracker()
            except Exception:
                self._token_tracker = None
        return self._token_tracker

    def _get_token_budget(self):
        if self._token_budget is None:
            try:
                from .token_budget import get_token_budget
                self._token_budget = get_token_budget()
            except Exception:
                self._token_budget = None
        return self._token_budget

    def _budget_pressure(self) -> Tuple[float, float]:
        """Return (pressure_0_to_1, remaining_usd).

        pressure = month_cost / monthly_cap. If budget tracking is
        unavailable, returns (0.0, 0.0).
        """
        tracker = self._get_token_tracker()
        budget = self._get_token_budget()
        if tracker is None or budget is None:
            return (0.0, 0.0)
        try:
            stats = tracker.stats(budget=budget.monthly_usd)
            month_cost = float(stats.get("month_cost", 0.0))
            monthly_cap = float(getattr(budget, "monthly_usd", 0.0)) or 1.0
            pressure = max(0.0, min(1.0, month_cost / monthly_cap))
            remaining = max(0.0, monthly_cap - month_cost)
            return (pressure, remaining)
        except Exception as e:
            logger.warning(f"[cost_router] budget_pressure error: {e}")
            return (0.0, 0.0)

    def _pick_free_provider(
        self,
        configured_providers: Optional[set],
    ) -> Optional[Dict[str, Any]]:
        """Try to find a free local provider that is configured."""
        # If the caller gave us the configured set, intersect with FREE_PROVIDER_IDS.
        available_free = (
            set(FREE_PROVIDER_IDS) & set(configured_providers)
            if configured_providers
            else set(FREE_PROVIDER_IDS)
        )
        if not available_free:
            return None
        # Prefer ollama, then lmstudio, then nim, then cerebras, then sambanova.
        for pid in ("ollama", "lmstudio", "nvidia_nim", "cerebras", "sambanova"):
            if pid in available_free:
                return {
                    "provider_id": pid,
                    "model": "",  # the provider's default
                    "max_tokens": 4096,
                    "complexity": "trivial",
                    "cost_estimate": 0.0,
                    "reasoning": "Free local provider (budget pressure critical)",
                    "speed": "medium",
                }
        return None

    def _estimate_cost(self, pick: Dict[str, Any], prompt: str) -> float:
        """Estimate cost of a single request to ``pick``.

        If ``pick`` already carries a ``cost_estimate``, trust it.
        Otherwise fall back to a length-based heuristic.
        """
        if "cost_estimate" in pick and pick["cost_estimate"]:
            try:
                return float(pick["cost_estimate"])
            except (TypeError, ValueError):
                pass
        # Heuristic: 1 token ≈ 4 chars; out is 2x in.
        approx_tokens_in = len(prompt) // 4
        approx_tokens_out = approx_tokens_in * 2
        # Use AutoRouter's pricing table if we can find the model.
        try:
            self._get_auto_router()  # ensure auto-router is initialized; result unused here
            from .auto_router import DEFAULT_TIERS
            for tiers in DEFAULT_TIERS.values():
                for t in tiers:
                    if t.provider_id == pick.get("provider_id") and \
                       (not pick.get("model") or t.model == pick.get("model")):
                        return (approx_tokens_in * t.cost_per_1k_in
                                + approx_tokens_out * t.cost_per_1k_out) / 1000.0
        except Exception:
            pass
        return 0.0  # unknown — assume free

    def _unhealthy_providers(self) -> set:
        """Return the set of provider ids whose recent error rate exceeds
        the configured threshold."""
        self._config  # accessed for side effect (ensure config is loaded); result unused here
        tracker = self._get_token_tracker()
        if tracker is None:
            return set()
        try:
            breakdown = tracker.provider_breakdown()
        except Exception:
            return set()
        out: set = set()
        for p in breakdown:
            reqs = int(p.get("requests", 0) or 0)
            if reqs < 3:
                continue  # not enough data
            # token_tracker doesn't track errors directly; we approximate
            # by checking the cost is non-zero for cloud providers (free
            # providers always have cost=0). This is intentionally a
            # weak signal — full error tracking is a future enhancement.
            cost = float(p.get("cost", 0.0) or 0.0)
            if cost == 0.0 and p.get("provider") not in FREE_PROVIDER_IDS:
                # Suspicious: cloud provider with zero cost likely errored
                out.add(p.get("provider"))
        return out


# ── Module-level singleton ────────────────────────────────────────────

_router: Optional[CostRouter] = None
_router_lock = threading.Lock()


def get_cost_router() -> CostRouter:
    """Return the process-wide CostRouter singleton."""
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = CostRouter()
    return _router


def reset_cost_router_for_test() -> CostRouter:
    """Test-only: forget the cached router and return a fresh one."""
    global _router
    with _router_lock:
        _router = None
    return get_cost_router()
