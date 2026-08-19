"""
Tera Pilot v1.0.4 — Multi-Provider Auto-Router.

Automatically selects the best provider/model for each task
based on complexity analysis, cost constraints, and speed requirements.
Implements fallback chains for resilience.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# G20c — router modes. Default is "single" (today's AutoRouter.route()
# behavior). "decompose" routes through TaskDecompositionRouter (G20)
# which breaks the task into subtasks and routes each one separately.
MODE_SINGLE = "single"
MODE_DECOMPOSE = "decompose"
ALL_MODES: Tuple[str, ...] = (MODE_SINGLE, MODE_DECOMPOSE)


class TaskComplexity(str, Enum):
    TRIVIAL = "trivial"       # "fix typo", "what does this do?"
    SIMPLE = "simple"         # small edit, single-file change
    MODERATE = "moderate"     # multi-file edit, refactor
    COMPLEX = "complex"       # architecture, migration, multi-service
    EXPERT = "expert"         # novel algorithm, deep reasoning


# ── Model tiers: (provider_id, model, max_tokens, cost_category) ───

@dataclass
class ModelTier:
    provider_id: str
    model: str
    max_tokens: int
    cost_per_1k_in: float
    cost_per_1k_out: float
    speed: str  # "fast" | "medium" | "slow"
    capabilities: List[str]
    # G20a — free-text specialty description used by the task-decomposition
    # router to match subtask needs against a tier's strengths. Examples:
    #   "strong at algorithmic/mathematical reasoning"
    #   "best for large-context codebase navigation and refactors"
    #   "cheap and fast for boilerplate/CRUD/formatting"
    #   "strong at frontend/visual/design-oriented tasks"
    #   "best for long structured document generation"
    # Empty string is allowed for backward compat (overrides can fill it in).
    specialty: str = ""


# Default tier catalog — users can override via settings
# v1.1.4-fix (bug 5.1): previously only referenced 5 of the app's 15
# providers, plus a "local" provider_id that was never registered
# anywhere (has_provider("local") is always False — see the v1.0.6
# comment in route()). Anyone whose only configured provider was e.g.
# DeepSeek, Mistral, Fireworks, Cerebras, SambaNova, xAI, Together, or
# Z.ai would never get auto-routed to it, defeating the whole point of
# "don't make the person think about it". Every registered provider now
# has an entry somewhere in the tier catalog.
#
# G20a: every entry now carries a `specialty` free-text description.
# These are intentionally short and concrete ("strong at X") so the
# task-decomposition router can match subtask needs against them with a
# simple keyword/semantic match. Override via ~/.tera_pilot/model_capabilities.json.
DEFAULT_TIERS = {
    TaskComplexity.TRIVIAL: [
        ModelTier("groq", "meta-llama/llama-4-scout-17b-16e-instruct", 8192, 0.00005, 0.00008, "fast", ["chat"],
                 "cheap and fast for boilerplate/CRUD/formatting"),
        ModelTier("cerebras", "llama-4-scout-17b-16e-instruct", 8192, 0.0, 0.0, "fast", ["chat"],
                 "free-tier inference for short tasks"),
        ModelTier("ollama", "llama4", 4096, 0.0, 0.0, "medium", ["chat"],
                 "local, free, private — for offline/short tasks"),
        ModelTier("lmstudio", "", 4096, 0.0, 0.0, "medium", ["chat"],
                 "local, free, private — for offline/short tasks"),
        ModelTier("openrouter", "deepseek/deepseek-v4-flash", 8192, 0.00014, 0.00028, "fast", ["chat"],
                 "cheap and fast for boilerplate/CRUD/formatting"),
    ],
    TaskComplexity.SIMPLE: [
        ModelTier("groq", "llama-3.3-70b-versatile", 16384, 0.00059, 0.00079, "fast", ["chat", "tool_calling"],
                 "cheap and fast for boilerplate/CRUD/formatting"),
        ModelTier("deepseek", "deepseek-v4-pro", 16384, 0.00027, 0.0011, "fast", ["chat", "tool_calling"],
                 "strong at algorithmic/mathematical reasoning"),
        ModelTier("zai", "glm-5.1", 16384, 0.0002, 0.0008, "fast", ["chat", "tool_calling"],
                 "balanced for general coding and tool use"),
        ModelTier("sambanova", "Meta-Llama-4-Maverick-17B-128E-Instruct", 16384, 0.0, 0.0, "fast", ["chat", "tool_calling"],
                 "free-tier fast inference for general coding"),
        ModelTier("openrouter", "deepseek/deepseek-v4-flash", 16384, 0.00014, 0.00028, "fast", ["chat", "tool_calling"],
                 "cheap and fast for boilerplate/CRUD/formatting"),
        ModelTier("openai", "gpt-5.5", 16384, 0.00015, 0.0006, "fast", ["chat", "tool_calling"],
                 "cheap and fast for boilerplate/CRUD/formatting"),
        ModelTier("together", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8", 16384, 0.0002, 0.0006, "fast", ["chat", "tool_calling"],
                 "open-source fast inference for general coding"),
        ModelTier("fireworks", "accounts/fireworks/models/llama4-maverick-instruct-basic", 16384, 0.00022, 0.00088, "fast", ["chat", "tool_calling"],
                 "open-source fast inference for general coding"),
        ModelTier("mistral", "mistral-large-latest", 16384, 0.002, 0.006, "medium", ["chat", "tool_calling"],
                 "balanced for general coding and tool use"),
    ],
    TaskComplexity.MODERATE: [
        ModelTier("anthropic", "claude-sonnet-5", 8192, 0.003, 0.015, "medium", ["chat", "tool_calling", "vision"],
                 "strong at frontend/visual/design-oriented tasks and large-context codebase navigation"),
        ModelTier("openai", "gpt-5.5", 8192, 0.0025, 0.01, "medium", ["chat", "tool_calling", "vision"],
                 "balanced for general coding, vision, and tool use"),
        ModelTier("gemini", "gemini-3.1-pro", 8192, 0.00125, 0.005, "medium", ["chat", "tool_calling", "vision"],
                 "strong at long structured document generation and multimodal tasks"),
        ModelTier("xai", "grok-4.5", 8192, 0.002, 0.01, "medium", ["chat", "tool_calling"],
                 "balanced for general coding and tool use"),
        ModelTier("openrouter", "anthropic/claude-sonnet-5", 8192, 0.003, 0.015, "medium", ["chat", "tool_calling"],
                 "strong at frontend/visual/design-oriented tasks and large-context codebase navigation"),
    ],
    TaskComplexity.COMPLEX: [
        ModelTier("anthropic", "claude-sonnet-5", 16384, 0.003, 0.015, "medium", ["chat", "tool_calling", "vision"],
                 "best for large-context codebase navigation and refactors"),
        ModelTier("openai", "gpt-5.5", 16384, 0.0025, 0.01, "medium", ["chat", "tool_calling", "vision"],
                 "balanced for general coding, vision, and tool use"),
        ModelTier("gemini", "gemini-3.1-pro", 16384, 0.00125, 0.005, "medium", ["chat", "tool_calling", "vision"],
                 "strong at long structured document generation and multimodal tasks"),
        ModelTier("anthropic", "claude-opus-5", 16384, 0.003, 0.015, "medium", ["chat", "tool_calling"],
                 "strong at frontend/visual/design-oriented tasks and large-context codebase navigation"),
    ],
    TaskComplexity.EXPERT: [
        ModelTier("anthropic", "claude-opus-5", 16384, 0.015, 0.075, "slow", ["chat", "tool_calling", "vision"],
                 "strong at algorithmic/mathematical reasoning and complex multi-step planning"),
        ModelTier("openai", "o4-mini", 32768, 0.01, 0.04, "slow", ["chat"],
                 "strong at algorithmic/mathematical reasoning and complex multi-step planning"),
        ModelTier("anthropic", "claude-sonnet-5", 4096, 0.015, 0.075, "slow", ["chat", "tool_calling"],
                 "strong at algorithmic/mathematical reasoning and complex multi-step planning"),
    ],
}


# ── G20a — ~/.tera_pilot/model_capabilities.json override loader ────────────
#
# Mirrors the capability_catalog.py pattern: user can override or add
# entries without touching the shipped catalog. The override file is a
# JSON object keyed by provider_id, with each value being a list of
# {model, specialty, ...} dicts that REPLACE the corresponding entries
# in DEFAULT_TIERS (matched by (provider_id, model) tuple). Entries
# that don't exist in DEFAULT_TIERS are added to the SIMPLE tier.
#
# Example ~/.tera_pilot/model_capabilities.json:
#   {
#     "openai": [
#       {"model": "gpt-4o", "specialty": "best for vision-heavy tasks"}
#     ],
#     "ollama": [
#       {"model": "qwen2.5-coder:32b", "specialty": "strong at code generation", "max_tokens": 16384}
#     ]
#   }
# NOTE: the path is computed lazily via _override_path() rather than
# captured at module import time, so tests that monkeypatch
# ``Path.home()`` (via the standard ``_isolated_home`` fixture) actually
# see the redirected path. Capturing at import time would freeze the
# real home dir and break test isolation.
_OVERRIDE_FILENAME = "model_capabilities.json"


def _override_path() -> Path:
    """Return the override path (recomputed each call for testability)."""
    return Path.home() / ".tera_pilot" / _OVERRIDE_FILENAME


def _load_overrides() -> Dict[str, Any]:
    """Load the user's model capability overrides.

    Returns an empty dict on any error (missing file, invalid JSON,
    permission denied). The dict shape is {provider_id: [{model, ...}]}.
    """
    try:
        path = _override_path()
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        logger.debug("[auto_router] override load failed: %s", e)
        return {}


def _apply_overrides(
    tiers: Dict[TaskComplexity, List[ModelTier]],
    overrides: Dict[str, Any],
) -> Dict[TaskComplexity, List[ModelTier]]:
    """Apply user overrides on top of the built-in DEFAULT_TIERS.

    Overrides are matched by (provider_id, model) tuple. If the tuple
    exists in DEFAULT_TIERS, the override's non-None fields REPLACE the
    built-in fields (specialty, max_tokens, cost_per_1k_in, cost_per_1k_out,
    speed, capabilities). The override applies to EVERY matching entry
    (some (provider, model) tuples appear in multiple complexity tiers —
    e.g. openai/gpt-4o is in both MODERATE and COMPLEX). If the tuple
    doesn't exist, the entry is appended to the SIMPLE tier.

    This function is pure (no I/O) and does NOT mutate the input — it
    returns a new dict so the original DEFAULT_TIERS is never modified.
    """
    if not overrides:
        return tiers
    # Deep-ish copy: new dict, new lists, but the ModelTier dataclass
    # instances themselves are replaced (not mutated) on override.
    new_tiers: Dict[TaskComplexity, List[ModelTier]] = {
        complexity: list(tier_list) for complexity, tier_list in tiers.items()
    }
    # Track which (provider_id, model) tuples we've already added via an
    # override (so we don't add the same new entry multiple times if the
    # user lists it twice). Existing entries in DEFAULT_TIERS are matched
    # by iterating (a (provider_id, model) tuple can legitimately appear
    # in multiple complexity tiers — we want the override to apply to
    # ALL of them, not just the first one we find).
    seen_new_keys: set = set()
    for provider_id, entries in overrides.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or "model" not in entry:
                continue
            model = entry["model"]
            key = (provider_id, model)
            # Try to update every existing entry with this (provider, model).
            # If none exist, append to SIMPLE (once per key).
            matched = False
            for complexity, tier_list in new_tiers.items():
                for i, t in enumerate(tier_list):
                    if t.provider_id == provider_id and t.model == model:
                        # Replace fields only when the override explicitly sets them.
                        new_tier = ModelTier(
                            provider_id=t.provider_id,
                            model=t.model,
                            max_tokens=int(entry.get("max_tokens", t.max_tokens)),
                            cost_per_1k_in=float(entry.get("cost_per_1k_in", t.cost_per_1k_in)),
                            cost_per_1k_out=float(entry.get("cost_per_1k_out", t.cost_per_1k_out)),
                            speed=str(entry.get("speed", t.speed)),
                            capabilities=list(entry.get("capabilities", t.capabilities)),
                            specialty=str(entry.get("specialty", t.specialty)),
                        )
                        tier_list[i] = new_tier
                        matched = True
            if not matched and key not in seen_new_keys:
                # New entry — append to SIMPLE. Use sensible defaults
                # for any missing fields so the user only has to specify
                # what they care about.
                new_tier = ModelTier(
                    provider_id=provider_id,
                    model=model,
                    max_tokens=int(entry.get("max_tokens", 8192)),
                    cost_per_1k_in=float(entry.get("cost_per_1k_in", 0.0)),
                    cost_per_1k_out=float(entry.get("cost_per_1k_out", 0.0)),
                    speed=str(entry.get("speed", "medium")),
                    capabilities=list(entry.get("capabilities", ["chat", "tool_calling"])),
                    specialty=str(entry.get("specialty", "")),
                )
                new_tiers[TaskComplexity.SIMPLE].append(new_tier)
                seen_new_keys.add(key)
    return new_tiers


def _build_default_tiers_with_overrides() -> Dict[TaskComplexity, List[ModelTier]]:
    """Build the effective tier catalog: DEFAULT_TIERS + user overrides."""
    overrides = _load_overrides()
    if not overrides:
        return DEFAULT_TIERS
    return _apply_overrides(DEFAULT_TIERS, overrides)


class AutoRouter:
    """
    Analyzes the user's prompt and selects the optimal provider/model.

    Decision factors:
    1. Task complexity (estimated from prompt length, keywords, presence of code)
    2. Required capabilities (vision, tool_calling)
    3. Cost ceiling (per-request budget from user)
    4. Provider availability (API key present, not rate-limited)
    5. Fallback chain (if primary fails, try next)
    """

    def __init__(self):
        # G20a — load user overrides (~/.tera_pilot/model_capabilities.json)
        # on top of DEFAULT_TIERS. Reloaded on every __init__ so changes
        # to the override file take effect on the next router instance
        # (cheap: one stat() + one json.load if the file exists).
        self._tiers = _build_default_tiers_with_overrides()
        self._per_request_budget: Optional[float] = None  # max $ per request
        self._force_provider: Optional[str] = None        # user override
        self._force_model: Optional[str] = None
        self._provider_available: Dict[str, bool] = {}    # track which providers work
        self._provider_available_ts: Dict[str, float] = {}  # timestamp of last mark
        self._provider_cache_ttl: float = 300.0  # 5 minutes (M-AUTO-2)
        # G20c — router mode. Default "single" preserves today's behavior.
        # "decompose" routes through TaskDecompositionRouter (G20) which
        # breaks the task into subtasks and routes each one separately.
        self._mode: str = MODE_SINGLE

    # ── Configuration ──────────────────────────────────────────────

    def set_budget(self, max_usd: float) -> None:
        self._per_request_budget = max_usd

    def set_force_provider(self, provider_id: str) -> None:
        self._force_provider = provider_id

    def set_force_model(self, model: str) -> None:
        self._force_model = model

    def clear_overrides(self) -> None:
        self._force_provider = None
        self._force_model = None

    def mark_provider_available(self, provider_id: str, available: bool) -> None:
        import time as _time
        self._provider_available[provider_id] = available
        self._provider_available_ts[provider_id] = _time.time()

    # G20c — router mode (single | decompose)
    def set_mode(self, mode: str) -> None:
        """Set the router mode.

        - ``"single"`` (default): today's AutoRouter.route() — one model
          for the whole task.
        - ``"decompose"`` (G20): route through TaskDecompositionRouter
          which breaks the task into subtasks, picks the best model for
          each, dispatches them as subagents (in parallel where
          possible), and merges the results.

        Any unknown value is normalised to ``"single"`` so a typo can't
        leave the router in an undefined state.
        """
        if mode not in ALL_MODES:
            logger.warning("[auto_router] unknown mode %r, falling back to 'single'", mode)
            self._mode = MODE_SINGLE
        else:
            self._mode = mode

    def get_mode(self) -> str:
        """Return the current router mode."""
        return self._mode

    # ── Routing ────────────────────────────────────────────────────

    def classify_task(self, prompt: str) -> TaskComplexity:
        """Classify task complexity. Returns the complexity enum."""
        complexity, _ = self._classify_task_impl(prompt)
        return complexity

    def classify_explain(self, prompt: str) -> Dict[str, Any]:
        """
        Classify task and return a human-readable explanation.
        Returns {complexity, explanation, signals} — designed for UI display.
        """
        complexity, signals = self._classify_task_impl(prompt)

        DESCRIPTIONS = {
            TaskComplexity.TRIVIAL: "Short question — a fast, cheap model is sufficient.",
            TaskComplexity.SIMPLE: "Single-file task — a capable mid-range model handles this.",
            TaskComplexity.MODERATE: "Multi-step feature — needs a strong model with tool support.",
            TaskComplexity.COMPLEX: "Cross-file work — routing to a top-tier reasoning model.",
            TaskComplexity.EXPERT: "Deep reasoning task — using the most powerful model available.",
        }

        return {
            "complexity": complexity.value,
            "explanation": DESCRIPTIONS.get(complexity, complexity.value),
            "signals": signals,
        }

    def _classify_task_impl(self, prompt: str) -> Tuple[TaskComplexity, List[str]]:
        """
        Internal: classify and return (complexity, signal_list).
        """
        text = prompt.lower()
        lines = prompt.split("\n")
        word_count = len(text.split())
        has_code = "```" in prompt or any(l.strip().startswith(("#", "//", "import ", "from ")) for l in lines[:5])

        # Count file references
        file_refs = len(re.findall(r"[\w./\-]+\.\w{1,5}", prompt))

        # Expert signals
        expert_keywords = [
            "architecture", "migration", "redesign", "rewrite from scratch",
            "optimization", "algorithm", "novel", "research", "prove",
            "complex reasoning", "multi-service", "distributed",
        ]
        expert_score = sum(1 for kw in expert_keywords if kw in text)

        # Complex signals
        complex_keywords = [
            "refactor", "restructure", "multi-file", "across files",
            "test suite", "all tests", "integration", "api design",
        ]
        complex_score = sum(1 for kw in complex_keywords if kw in text)

        # Moderate signals
        moderate_keywords = [
            "feature", "add", "implement", "create", "build",
            "function", "class", "component", "endpoint",
        ]
        moderate_score = sum(1 for kw in moderate_keywords if kw in text)

        # Decision tree
        if expert_score >= 2 or (word_count > 500 and file_refs > 5):
            complexity = TaskComplexity.EXPERT
        elif complex_score >= 2 or file_refs > 3 or (word_count > 200 and has_code):
            complexity = TaskComplexity.COMPLEX
        elif moderate_score >= 1 or (word_count > 50 and has_code):
            complexity = TaskComplexity.MODERATE
        elif word_count > 20 or has_code:
            complexity = TaskComplexity.SIMPLE
        else:
            complexity = TaskComplexity.TRIVIAL

        # Build explanation of why this complexity was chosen
        signals = []
        if has_code:
            signals.append("contains code")
        if file_refs > 0:
            signals.append(f"{file_refs} file reference(s)")
        if word_count > 200:
            signals.append(f"long prompt ({word_count} words)")
        elif word_count > 50:
            signals.append(f"medium prompt ({word_count} words)")
        if expert_score > 0:
            matched = [kw for kw in expert_keywords if kw in text]
            signals.append(f"expert keyword(s): {', '.join(matched)}")
        if complex_score > 0:
            matched = [kw for kw in complex_keywords if kw in text]
            signals.append(f"complex keyword(s): {', '.join(matched)}")
        if moderate_score > 0:
            matched = [kw for kw in moderate_keywords if kw in text]
            signals.append(f"action keyword(s): {', '.join(matched)}")

        return complexity, signals

    def route(
        self,
        prompt: str,
        required_capabilities: Optional[List[str]] = None,
        configured_providers: Optional[set] = None,
    ) -> Dict[str, Any]:
        """
        Select the best provider/model for this prompt.
        Returns {provider_id, model, max_tokens, complexity, cost_estimate, fallbacks, reasoning}.

        v1.1.4-fix (bug 5.1): ``configured_providers`` — the set of
        provider ids that actually have an API key / config saved right
        now (from ProviderRegistry.list_providers()) — used to be
        ignored entirely. ``mark_provider_available()`` exists but was
        never called anywhere in the app, so ``_is_available()`` always
        returned True and the router would happily pick a provider the
        person never configured, producing a confusing auth error on
        the very first message. Passing this set makes routing actually
        respect what's set up.
        """
        # If user forced a specific provider/model
        if self._force_provider:
            return {
                "provider_id": self._force_provider,
                "model": self._force_model or "",
                "max_tokens": 8192,
                "complexity": "forced",
                "cost_estimate": 0.0,
                "fallbacks": [],
                "reasoning": f"Forced to {self._force_provider}",
                "speed": "unknown",
            }

        complexity = self._classify_task_impl(prompt)[0]
        candidates = list(self._tiers.get(complexity, []))

        # Filter by required capabilities
        if required_capabilities:
            candidates = [
                t for t in candidates
                if all(cap in t.capabilities for cap in required_capabilities)
            ]

        # Filter by provider availability (with TTL — M-AUTO-2): a
        # provider that just failed a real request is skipped for a
        # while even if it's configured.
        import time as _time
        now = _time.time()
        def _is_available(pid: str) -> bool:
            ts = self._provider_available_ts.get(pid)
            if ts is None:
                return True  # never marked — assume available
            if now - ts > self._provider_cache_ttl:
                return True  # TTL expired — retry
            return self._provider_available.get(pid, True)
        candidates = [t for t in candidates if _is_available(t.provider_id)]

        # v1.1.4-fix: filter by whether the provider is actually
        # configured (has a key / is a no-key local provider). Skipped
        # only when the caller explicitly passes None, so existing
        # tests / other callers keep working unchanged.
        if configured_providers is not None:
            candidates = [t for t in candidates if t.provider_id in configured_providers]

        # Filter by budget
        if self._per_request_budget is not None:
            candidates = [
                t for t in candidates
                if self._estimate_cost(t, prompt) <= self._per_request_budget
            ]

        if not candidates:
            # v1.1.4-fix: search the *entire* tier catalog (every
            # complexity level), not just SIMPLE — this is what makes
            # "as long as you've configured one provider, it always
            # works" actually true, regardless of which tier that
            # provider happens to live in.
            logger.warning(f"[router] no candidates for {complexity.value}, searching full catalog")
            seen_pids: set = set()
            all_candidates: List[ModelTier] = []
            for tier_list in DEFAULT_TIERS.values():
                for t in tier_list:
                    if t.provider_id not in seen_pids:
                        seen_pids.add(t.provider_id)
                        all_candidates.append(t)
            candidates = [
                t for t in all_candidates
                if _is_available(t.provider_id)
                and (configured_providers is None or t.provider_id in configured_providers)
            ]

        if not candidates:
            logger.warning("[router] no providers available at all")
            return {
                "provider_id": "",
                "model": "",
                "max_tokens": 4096,
                "complexity": complexity.value,
                "cost_estimate": 0.0,
                "fallbacks": [],
                "reasoning": (
                    "No configured providers available. Open Settings → "
                    "Providers and add an API key (or use a local model "
                    "with Ollama / LM Studio — no key needed)."
                ),
                "speed": "unknown",
            }

        # Pick the first (best) candidate
        primary = candidates[0]
        fallbacks = [
            {"provider_id": t.provider_id, "model": t.model}
            for t in candidates[1:4]
        ]

        cost_est = self._estimate_cost(primary, prompt)

        return {
            "provider_id": primary.provider_id,
            "model": primary.model,
            "max_tokens": primary.max_tokens,
            "complexity": complexity.value,
            "cost_estimate": round(cost_est, 4),
            "fallbacks": fallbacks,
            "reasoning": (
                f"Classified as {complexity.value}. "
                f"Routed to {primary.provider_id}/{primary.model} "
                f"({primary.speed}, ~${cost_est:.4f} est.)"
            ),
            "speed": primary.speed,
            # G20a — include the specialty so the task-decomposition
            # router (and the UI) can show why this model was picked.
            "specialty": primary.specialty,
        }

    def _estimate_cost(self, tier: ModelTier, prompt: str) -> float:
        """Rough cost estimate for a single request."""
        approx_tokens_in = len(prompt) // 4
        # Assume output is roughly 2x input for code tasks
        approx_tokens_out = approx_tokens_in * 2
        return (approx_tokens_in * tier.cost_per_1k_in + approx_tokens_out * tier.cost_per_1k_out) / 1000

    # ── Info ───────────────────────────────────────────────────────

    def get_tier_info(self) -> Dict[str, Any]:
        """Return the current routing configuration for the UI."""
        return {
            complexity.value: [
                {
                    "provider_id": t.provider_id,
                    "model": t.model,
                    "speed": t.speed,
                    "est_cost_in": t.cost_per_1k_in,
                    "est_cost_out": t.cost_per_1k_out,
                    # G20a — surface the specialty in the UI so users
                    # can see WHY each model is in each tier.
                    "specialty": t.specialty,
                }
                for t in tiers
            ]
            for complexity, tiers in self._tiers.items()
        }

    # G20a — expose all tiers (with overrides applied) for the
    # task-decomposition router to score against. Returns a flat list
    # of (complexity, tier) tuples so the caller can iterate without
    # caring about the complexity-bucketed structure.
    def all_tiers(self) -> List[Tuple[TaskComplexity, "ModelTier"]]:
        """Return every tier across all complexity levels (with overrides applied)."""
        out: List[Tuple[TaskComplexity, ModelTier]] = []
        for complexity, tier_list in self._tiers.items():
            for t in tier_list:
                out.append((complexity, t))
        return out


# ── Module-level singleton (lazy) — mirrors get_activity_log() ────────
# Pattern from cost_router.py: lazy-init under a Lock so the first
# caller pays the init cost (one stat + one json.load if the override
# file exists), every subsequent caller gets the same instance. Tests
# can call reset_auto_router_for_test() to get a fresh one.
_AUTO_ROUTER: Optional["AutoRouter"] = None
_AUTO_ROUTER_LOCK = threading.Lock()


def get_auto_router() -> "AutoRouter":
    """Return the process-wide AutoRouter singleton."""
    global _AUTO_ROUTER
    if _AUTO_ROUTER is None:
        with _AUTO_ROUTER_LOCK:
            if _AUTO_ROUTER is None:
                _AUTO_ROUTER = AutoRouter()
    return _AUTO_ROUTER


def reset_auto_router_for_test() -> "AutoRouter":
    """Replace the singleton with a fresh AutoRouter. Test-only."""
    global _AUTO_ROUTER
    with _AUTO_ROUTER_LOCK:
        _AUTO_ROUTER = AutoRouter()
    return _AUTO_ROUTER