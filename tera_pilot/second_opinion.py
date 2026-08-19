"""
Second Opinion — cross-model review of risky tool calls before commit.

Goal (M1): "Cross-model 'Second Opinion' before commit — Guardian +
16 providers ready; 1 extra API call + UI toggle. Gate behind
``tera_pilot_pro`` flag."

This module is the data layer for the Second Opinion feature. It does
TWO things:

1. **Pro gating (v2.3.4: license-based)** — Second Opinion only fires when
   the ``second_opinion`` feature is licensed: a valid Ed25519-signed
   license (``tera-pilot license activate <key>``) or the local-dev
   ``TERA_PILOT_PRO`` override (dev-only, never for production). The old
   forgeable ``tera_pilot_pro`` config flag no longer grants access — see
   ``licensing.py`` / ``LICENSING.md``. On top of the license, the user
   must explicitly enable Second Opinion (``second_opinion.enabled`` in
   ``~/.tera_pilot/config.json``, or ``/second_opinion on``).

2. **Cross-model review** — ``review_with_second_model()`` asks a
   DIFFERENT provider/model than the active one to review a proposed
   tool call. The second model sees the same args as the active model
   and returns a JSON verdict: APPROVE / REJECT / MODIFY with a
   rationale. The verdict is forwarded to the existing confirmation
   modal so the user can compare the two opinions before deciding.

The "second" model defaults to whatever the user picks via
``/second_opinion provider <pid> [model]``. If unset, we pick a
sensible default: if the active provider is local (Ollama / LM Studio
/ Nvidia NIM), the second opinion defaults to a free cloud provider
(Groq with llama-3.3-70b-versatile). If the active provider is already
cloud, the second opinion defaults to a different family — Anthropic
if OpenAI is active, OpenAI if Anthropic is active, etc. — so the two
opinions actually come from different training data.

Why a separate module (vs. extending ``guardian.py``):
- Guardian is rule-based risk scoring + optional LLM review of the
  SAME provider. Second Opinion is ALWAYS cross-model, has its own
  UI surface (the approval modal gets a "Second Opinion" panel), and
  is gated by a Pro flag Guardian isn't.
- The two features compose: Guardian flags a risky call, then Second
  Opinion chimes in with a different model's take. The user sees both
  rationales side-by-side.

Status & known limitations (v2.3.4 — honest, matching README's
"deliberately NOT claiming" section):
- The MANUAL run path is fully wired and gated: TUI ``/second_opinion
  run ...``-style invocation and ``POST /api/second_opinion/run`` both
  funnel through :func:`run_second_opinion` → license check →
  cross-model review.
- The AUTO-trigger path (:func:`should_run_second_opinion`, meant to
  fire before risky tool calls) is implemented and gated but is NOT yet
  wired into the agent tool-engine call flow — nothing calls it during
  a run yet. The guardian approval modal can still surface a second
  opinion when the UI explicitly invokes it.
- The second opinion issues a REAL LLM call to a second provider; that
  is the feature itself (a cloud provider call by design), not
  telemetry. License checks themselves make zero network calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Config ───────────────────────────────────────────────────────────

# Environment variable shortcut: set TERA_PILOT_PRO=1 to enable pro mode
# without touching the config file (handy for trials / CI).
_PRO_ENV_VAR = "TERA_PILOT_PRO"

# Config keys under ~/.tera_pilot/config.json
_CONFIG_KEY_PRO = "tera_pilot_pro"
_CONFIG_KEY_SECOND_OPINION = "second_opinion"

# Sensible cross-model fallbacks. We try to pick a model from a
# different family than the active one so the two opinions actually
# differ in training data and reasoning style.
_CROSS_FAMILY_DEFAULTS: Dict[str, str] = {
    # active provider family → (second provider id, second model)
    "ollama":       ("groq",      "meta-llama/llama-4-maverick-17b-128e-instruct"),
    "lmstudio":     ("groq",      "meta-llama/llama-4-maverick-17b-128e-instruct"),
    "nvidia_nim":   ("groq",      "meta-llama/llama-4-maverick-17b-128e-instruct"),
    "openai":       ("anthropic", "claude-haiku-4-5-20251001"),
    "anthropic":    ("openai",    "gpt-5.5"),
    "openrouter":   ("groq",      "meta-llama/llama-4-maverick-17b-128e-instruct"),
    "groq":         ("openai",    "gpt-5.5"),
    "deepseek":     ("openai",    "gpt-5.5"),
    "zai":          ("openai",    "gpt-5.5"),
    "gemini":       ("openai",    "gpt-5.5"),
    "mistral":      ("openai",    "gpt-5.5"),
    "together":     ("groq",      "meta-llama/llama-4-maverick-17b-128e-instruct"),
    "fireworks":    ("groq",      "meta-llama/llama-4-maverick-17b-128e-instruct"),
    "xai":          ("openai",    "gpt-5.5"),
    "cerebras":     ("openai",    "gpt-5.5"),
    "sambanova":    ("openai",    "gpt-5.5"),
}


@dataclass(frozen=True)
class SecondOpinionConfig:
    """User-facing configuration for the Second Opinion feature."""
    enabled: bool = False
    provider_id: str = "auto"   # "auto" = pick a different family
    model: str = "auto"         # "auto" = use the second provider's default
    # Risk threshold below which we SKIP the second opinion (no point
    # asking a second model for a "low-risk" call). Mirrors Guardian's
    # three levels: "off", "dangerous_only", "all".
    min_risk_level: str = "medium"


@dataclass(frozen=True)
class SecondOpinionVerdict:
    """Verdict returned by the second model."""
    verdict: str   # "APPROVE" | "REJECT" | "MODIFY"
    rationale: str
    suggested_args: Optional[Dict[str, Any]] = None
    provider_id: str = ""
    model: str = ""
    elapsed_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict":         self.verdict,
            "rationale":       self.rationale,
            "suggested_args":  self.suggested_args,
            "provider_id":     self.provider_id,
            "model":           self.model,
            "elapsed_ms":      round(self.elapsed_ms, 1),
            "error":           self.error,
        }


# ── Config persistence ───────────────────────────────────────────────

def _config_path() -> Path:
    return Path.home() / ".tera_pilot" / "config.json"


def _load_config() -> Dict[str, Any]:
    try:
        if _config_path().exists():
            with open(_config_path(), "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def _save_config(cfg: Dict[str, Any]) -> None:
    try:
        _config_path().parent.mkdir(parents=True, exist_ok=True)
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.warning("[second_opinion] failed to save config: %s", e)


def is_pro_enabled() -> bool:
    """DEPRECATED (v2.3.4): Pro is now license-based.

    Returns True only when a valid Pro license is active (or the
    ``TERA_PILOT_PRO`` local-dev override is set). The old ``tera_pilot_pro``
    config flag no longer grants anything — it was a forgeable boolean and
    is replaced by the offline license system (``licensing.py``). Kept as a
    status shim for displays; feature gating uses
    ``licensing.is_feature_licensed("second_opinion")``.
    """
    from .licensing import is_pro_enabled as _licensing_pro
    return _licensing_pro()


def set_pro_enabled(enabled: bool) -> None:
    """DEPRECATED (v2.3.4): Pro can no longer be enabled by a config flag.

    Pro requires a valid signed license (``tera-pilot license activate
    <key>``). This method is kept for API compatibility: it records the
    legacy flag for any old readers, but it has NO effect on gating —
    :func:`is_pro_enabled` ignores it.
    """
    logger.warning(
        "[second_opinion] set_pro_enabled() is deprecated — Pro requires a "
        "valid license (see tera_pilot/licensing.py and LICENSING.md). "
        "The config flag is no longer read for gating."
    )
    cfg = _load_config()
    cfg[_CONFIG_KEY_PRO] = bool(enabled)
    _save_config(cfg)


def get_second_opinion_config() -> SecondOpinionConfig:
    """Load the Second Opinion config from disk."""
    cfg = _load_config()
    so_cfg = cfg.get(_CONFIG_KEY_SECOND_OPINION, {}) or {}
    return SecondOpinionConfig(
        enabled=bool(so_cfg.get("enabled", False)),
        provider_id=str(so_cfg.get("provider_id", "auto")),
        model=str(so_cfg.get("model", "auto")),
        min_risk_level=str(so_cfg.get("min_risk_level", "medium")),
    )


def set_second_opinion_config(new_cfg: SecondOpinionConfig) -> None:
    """Persist the Second Opinion config."""
    cfg = _load_config()
    cfg[_CONFIG_KEY_SECOND_OPINION] = {
        "enabled":         new_cfg.enabled,
        "provider_id":     new_cfg.provider_id,
        "model":           new_cfg.model,
        "min_risk_level":  new_cfg.min_risk_level,
    }
    _save_config(cfg)


def resolve_second_provider(
    active_provider_id: str,
    cfg: SecondOpinionConfig,
) -> tuple[str, str]:
    """Resolve which provider+model to use for the second opinion.

    If the user pinned a specific provider/model, use that. Otherwise
    pick a different family from the cross-family defaults table.
    """
    if cfg.provider_id != "auto" and cfg.provider_id:
        pid = cfg.provider_id
        # If model is "auto", fall through to the provider's default
        # — the caller (registry.get) will supply it.
        model = cfg.model if cfg.model and cfg.model != "auto" else ""
        return pid, model

    pair = _CROSS_FAMILY_DEFAULTS.get(active_provider_id)
    if pair:
        return pair
    # Last-resort: Groq with a fast llama model.
    return "groq", "meta-llama/llama-4-maverick-17b-128e-instruct"


# ── Verdict parsing ──────────────────────────────────────────────────

def _parse_verdict(raw: str) -> Optional[SecondOpinionVerdict]:
    """Parse a JSON verdict from the second model's response."""
    # Try fenced JSON first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
    try:
        data = json.loads(raw)
        if "verdict" not in data or "rationale" not in data:
            return None
        verdict = str(data.get("verdict", "")).upper()
        if verdict not in ("APPROVE", "REJECT", "MODIFY"):
            return None
        rationale = str(data.get("rationale", ""))
        suggested = data.get("suggested_args")
        if verdict == "MODIFY" and not isinstance(suggested, dict):
            return None
        if verdict in ("APPROVE", "REJECT"):
            suggested = None
        return SecondOpinionVerdict(
            verdict=verdict,
            rationale=rationale,
            suggested_args=suggested,
        )
    except Exception:
        return None


# ── Prompt builder ───────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a Second Opinion reviewer for an AI coding agent.

The primary agent is about to execute a tool call. Your job is to give
an independent second opinion by reviewing the proposed action and
returning a verdict in STRICT JSON.

Verdict must be one of:
- "APPROVE"  — the action is safe and reasonable.
- "REJECT"   — the action should not be taken (explain why).
- "MODIFY"   — the action is OK in spirit but the args need adjustment
               (provide a corrected `suggested_args` object).

You MUST respond with a single JSON object and nothing else:
{
  "verdict": "APPROVE" | "REJECT" | "MODIFY",
  "rationale": "<one or two sentences>",
  "suggested_args": {...}   // only when verdict is MODIFY
}

Be concise. Focus on:
- Does the action match the user's stated intent?
- Are there obvious security, data-loss, or correctness risks?
- Is there a simpler / safer alternative?
"""


def _build_user_prompt(
    tool_name: str,
    args: Dict[str, Any],
    risk_level: str,
    risk_reasons: List[str],
    recent_context: str,
) -> str:
    """Build the user prompt for the second-opinion model."""
    payload = {
        "tool": tool_name,
        "args": args,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "recent_context": recent_context[:2000],  # cap to keep the call cheap
    }
    return (
        "Review the following proposed tool call. The primary agent's "
        "risk scorer has already classified it. Give your independent verdict.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


# ── Review entry point ───────────────────────────────────────────────

def review_with_second_model(
    *,
    config: SecondOpinionConfig,
    tool_name: str,
    args: Dict[str, Any],
    risk_level: str,
    risk_reasons: List[str],
    recent_context: str,
    provider_registry,
    active_provider_id: str,
    _provider_for_test: Optional[Any] = None,
) -> SecondOpinionVerdict:
    """Call a second model to review the tool call. BLOCKING.

    Args:
        config: SecondOpinionConfig (already loaded / constructed).
        tool_name: the tool the primary agent wants to call.
        args: the proposed tool args (may be modified by MODIFY verdict).
        risk_level: "low" | "medium" | "high" — from Guardian's risk scorer.
        risk_reasons: free-text reasons from the risk scorer.
        recent_context: recent conversation history, for the second
            model to understand intent.
        provider_registry: a ``ProviderRegistry`` with the second
            provider registered.
        active_provider_id: the provider the primary agent is using
            (so we can pick a DIFFERENT family).
        _provider_for_test: injected provider instance (skips registry
            lookup). Tests use this to avoid spinning up real HTTP.

    Returns:
        ``SecondOpinionVerdict``. On any error, returns a verdict with
        ``error`` set and ``verdict="APPROVE"`` so the feature fails
        OPEN (never blocks the user because of a bug in the second
        opinion itself).
    """
    import time as _time
    t0 = _time.time()

    # 1. Resolve provider + model
    second_pid, second_model = resolve_second_provider(active_provider_id, config)
    if not second_model:
        # The cross-family table always supplies both, but a user-pinned
        # provider with model="auto" needs us to ask the registry for
        # the default.
        try:
            cls = provider_registry._classes.get(second_pid)
            second_model = cls.default_model if cls else ""
        except Exception:
            second_model = ""

    if not second_pid:
        return SecondOpinionVerdict(
            verdict="APPROVE",
            rationale="Second Opinion: no second provider configured — defaulting to approve.",
            provider_id="",
            model="",
            elapsed_ms=(_time.time() - t0) * 1000,
            error="no_second_provider",
        )

    # 2. Get the provider instance
    provider = _provider_for_test
    if provider is None:
        try:
            provider = provider_registry.get(second_pid)
            if not provider.is_loaded:
                provider.load()
        except Exception as e:
            logger.warning("[second_opinion] provider %s unavailable: %s", second_pid, e)
            return SecondOpinionVerdict(
                verdict="APPROVE",
                rationale=f"Second Opinion: provider '{second_pid}' unavailable — defaulting to approve.",
                provider_id=second_pid,
                model=second_model,
                elapsed_ms=(_time.time() - t0) * 1000,
                error=str(e),
            )

    # 3. Build messages
    from tera_pilot.providers import ProviderMessage
    user_prompt = _build_user_prompt(
        tool_name, args, risk_level, risk_reasons, recent_context,
    )
    messages = [
        ProviderMessage(role="system", content=_SYSTEM_PROMPT),
        ProviderMessage(role="user", content=user_prompt),
    ]

    # 4. Call the second model. We do NOT retry — Second Opinion is a
    #    nice-to-have; a single failure should not block the user's
    #    workflow.
    try:
        resp = provider.generate(messages, model=second_model)
        raw = getattr(resp, "text", "") or ""
    except Exception as e:
        logger.warning("[second_opinion] LLM call failed: %s", e)
        return SecondOpinionVerdict(
            verdict="APPROVE",
            rationale=f"Second Opinion: LLM call failed — defaulting to approve.",
            provider_id=second_pid,
            model=second_model,
            elapsed_ms=(_time.time() - t0) * 1000,
            error=str(e),
        )

    # 5. Parse the verdict
    verdict = _parse_verdict(raw)
    if verdict is None:
        logger.warning("[second_opinion] unparseable response, defaulting to APPROVE")
        return SecondOpinionVerdict(
            verdict="APPROVE",
            rationale="Second Opinion: response unparseable — defaulting to approve.",
            provider_id=second_pid,
            model=second_model,
            elapsed_ms=(_time.time() - t0) * 1000,
            error="unparseable_response",
        )

    # 6. Fill in provider/model/elapsed and return
    return SecondOpinionVerdict(
        verdict=verdict.verdict,
        rationale=verdict.rationale,
        suggested_args=verdict.suggested_args,
        provider_id=second_pid,
        model=second_model,
        elapsed_ms=(_time.time() - t0) * 1000,
    )


def should_run_second_opinion(
    config: SecondOpinionConfig,
    risk_level: str,
) -> bool:
    """Decide whether to actually invoke the second model for this call.

    Rules:
    - The second_opinion feature must be licensed (valid Pro license or
      TERA_PILOT_PRO dev override).
    - Second Opinion must be enabled in config.
    - The risk level must meet the configured threshold.
    """
    from .licensing import is_feature_licensed
    if not is_feature_licensed("second_opinion"):
        return False
    if not config.enabled:
        return False
    threshold = config.min_risk_level.lower()
    level = (risk_level or "low").lower()
    rank = {"low": 0, "medium": 1, "high": 2}
    if rank.get(level, 0) < rank.get(threshold, 1):
        return False
    return True
