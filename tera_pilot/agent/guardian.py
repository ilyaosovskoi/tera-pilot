"""guardian.py — LLM-based reviewer for risky tool calls (APPROVE/REJECT/MODIFY).

Rule-based risk scoring + optional LLM review with circuit breaker protection.

Issue #4 (Guardian Agent sub-reviewer): when the configured mode is
``subagent``, the LLM review is delegated to a throw-away
``GeneralPurposeSubagent`` instead of calling the provider directly. This
isolates the review prompt from the parent runtime's prompt history and
lets us enforce a read-only toolset so the reviewer cannot mutate the
workspace while assessing the call.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from tera_pilot.agent.circuit_breaker import CircuitBreakerRegistry, CircuitOpenError
from tera_pilot.agent import get_circuit_breaker_registry
from tera_pilot.agent.native import NATIVE_AVAILABLE, get_native_module
from tera_pilot.providers import ProviderMessage, ProviderResponse
from tera_pilot.providers.base import Provider

logger = logging.getLogger(__name__)

# --- Config --------------------------------------------------------------


@dataclass(frozen=True)
class GuardianConfig:
    level: str = "off"  # "off" | "dangerous_only" | "all"
    provider_id: str = "auto"  # "auto" = use parent runtime's active provider
    model: str = "auto"  # "auto" = use parent runtime's active model
    # Issue #4: when True, delegate the LLM review to a read-only subagent
    # rather than calling the provider directly. Defaults to False so
    # existing behaviour is unchanged.
    use_subagent: bool = False


# --- Risk Scoring --------------------------------------------------------


@dataclass(frozen=True)
class RiskAssessment:
    level: str  # "low" | "medium" | "high"
    reasons: list[str]


CRITICAL_PATHS = [
    os.path.expanduser("~/.ssh"),
    os.path.expanduser("~/.aws"),
    os.path.expanduser("~/.config/gcloud"),
    os.path.expanduser("~/.docker"),
    os.path.expanduser("~/.kube"),
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/sys",
    "/proc",
]

CRITICAL_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "requirements.txt",
    "requirements.lock",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "passwd",
    "shadow",
    "sudoers",
}

DANGEROUS_COMMAND_PATTERNS = [
    (re.compile(r"\brm\s+-rf\b"), "rm -rf"),
    (re.compile(r"\bgit\s+push\s+--force\b"), "git push --force"),
    (re.compile(r"\bcurl\s+.*\|\s*sh\b"), "curl | sh"),
    (re.compile(r"\bwget\s+.*\|\s*sh\b"), "wget | sh"),
    (re.compile(r"\bchmod\s+777\b"), "chmod 777"),
    (re.compile(r">\s*/etc/"), "write to /etc"),
    (re.compile(r">\s*/usr/"), "write to /usr"),
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r"\bdd\s+if="), "dd if="),
    (re.compile(r":\s*\(\)\s*{\s*:\|:\s*&\s*}\s*;\s*:"), "fork bomb"),
]


def _normalise_command(cmd: Any) -> str:
    """Best-effort flattening of a command argument to a single string.

    Tools may pass the command as ``str``, ``list[str]`` or ``tuple[str, ...]``.
    The risk scorer needs a single string so the regex patterns can run.
    """
    if cmd is None:
        return ""
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(x) for x in cmd)
    return str(cmd)


# v2.1.0 (G18): web_fetch URL risk classifier. Used by assess_risk to
# flag suspicious URLs as at-least-medium risk. Returns one of:
# "" (clean), "secret" (secret-shaped param), "base64" (long base64-like
# param), "too_long" (URL >2000 chars). Mirrors the same checks that
# ToolEngine._web_fetch uses to REJECT the fetch outright — Guardian
# flags the risk, the tool engine enforces the block.
_SECRET_PARAM_NAMES = {
    "api_key", "apikey", "token", "access_token",
    "password", "passwd", "secret", "client_secret",
}


def _check_web_fetch_url(url: str) -> str:
    """Return a risk classification for a web_fetch URL.

    Returns:
        "" — URL looks clean.
        "secret" — URL contains a secret-shaped query param (api_key=,
            token=, etc. with a non-trivial value). HIGH risk.
        "base64" — URL has a query param with a long (>=80 char) base64-
            like value. MEDIUM risk (could be benign encoded data, but
            often injection or exfiltration).
        "too_long" — URL is >2000 chars. MEDIUM risk (almost never
            legitimate).
    """
    if not url:
        return ""
    if len(url) > 2000:
        return "too_long"
    if "?" not in url:
        return ""
    query = url.split("?", 1)[1]
    for param in query.split("&"):
        if "=" not in param:
            continue
        key, _, value = param.partition("=")
        key_lower = key.lower()
        if key_lower in _SECRET_PARAM_NAMES and len(value) >= 16:
            return "secret"
        if len(value) >= 80 and re.fullmatch(r"[A-Za-z0-9+/=_\-]+", value):
            return "base64"
    return ""


def assess_risk(
    tool_name: str,
    args: dict[str, Any],
    workspace: str,
    command_policy: Optional[Any] = None,
) -> RiskAssessment:
    """Rule-based risk assessment. Returns level and reasons.

    The function is intentionally pure (no I/O, no side effects) so it
    can be unit-tested without mocks.
    """
    reasons: list[str] = []
    level = "low"

    # --- shell command execution ---------------------------------------
    if tool_name in ("execute_command", "run_shell", "bash", "shell"):
        cmd = _normalise_command(
            args.get("command") or args.get("cmd") or args.get("args")
        )

        # Check command_policy (optional). The original check called
        # ``command_policy.is_dangerous_flag(binary, "")`` with an empty
        # flag, which can never return True — dead code that silently
        # disabled this whole branch. We now flag commands the resolved
        # policy would refuse: binaries on the deny list (or absent from
        # the allowed set). That is the policy decision that is both
        # meaningful here and free of false positives for whitelisted
        # commands (e.g. `git status` / `python3 script.py` stay at the
        # default medium risk for any shell execution).
        if command_policy:
            binary = cmd.strip().split()[0] if cmd.strip() else ""
            if binary and not command_policy.is_allowed(binary):
                reasons.append(f"command_policy: {binary} is not allowed")
                level = "high"

        # Pattern matching for known-dangerous shells
        for pattern, desc in DANGEROUS_COMMAND_PATTERNS:
            if pattern.search(cmd):
                reasons.append(f"dangerous pattern: {desc}")
                level = "high"

        # Anything else with shell execution is at least medium risk
        if level == "low":
            reasons.append(f"{tool_name} operation")
            level = "medium"

    # --- file writes / edits -------------------------------------------
    elif tool_name in ("write_file", "edit_file", "write", "edit", "str_replace", "str_replace_editor"):
        path = args.get("path") or args.get("file_path") or ""
        if path:
            abs_path = os.path.abspath(os.path.join(workspace, path))
            ws_abs = os.path.abspath(workspace)

            for crit in CRITICAL_PATHS:
                try:
                    if abs_path.startswith(crit):
                        reasons.append(f"writes to critical path: {crit}")
                        level = "high"
                        break
                except Exception:
                    pass

            basename = os.path.basename(path)
            if basename in CRITICAL_FILENAMES:
                reasons.append(f"writes critical file: {basename}")
                level = "high"

            if not abs_path.startswith(ws_abs):
                reasons.append(f"writes outside workspace: {path}")
                level = "high"

        if level == "low":
            reasons.append(f"{tool_name} operation")
            level = "medium"

    # --- file deletion -------------------------------------------------
    elif tool_name in ("delete_file", "delete", "remove", "rm"):
        path = args.get("path") or args.get("file_path") or ""
        if path:
            abs_path = os.path.abspath(os.path.join(workspace, path))
            for crit in CRITICAL_PATHS:
                try:
                    if abs_path.startswith(crit):
                        reasons.append(f"deletes critical path: {crit}")
                        level = "high"
                        break
                except Exception:
                    pass
            basename = os.path.basename(path)
            if basename in CRITICAL_FILENAMES:
                reasons.append(f"deletes critical file: {basename}")
                level = "high"

        if level == "low":
            reasons.append(f"{tool_name} operation")
            level = "medium"

    # --- git operations ------------------------------------------------
    elif tool_name in ("git", "git_push", "git_commit", "git_stage"):
        subcmd = args.get("subcommand") or args.get("args") or args.get("command") or ""
        subcmd_s = _normalise_command(subcmd)
        if "push" in subcmd_s and "--force" in subcmd_s:
            reasons.append("git push --force")
            level = "high"
        elif "reset --hard" in subcmd_s:
            reasons.append("git reset --hard")
            level = "high"
        elif level == "low":
            reasons.append(f"{tool_name} operation")
            level = "medium"

    # v2.1.0 (G18): web tools — untrusted external content risk.
    # web_fetch introduces a risk class the rule-based scorer didn't
    # cover before: content fetched from the internet can contain
    # instructions that look like user commands (prompt injection) or
    # can be used to exfiltrate data (e.g. a crafted URL with secrets
    # embedded as query params). We flag:
    # - URLs with secret-shaped query params (api_key=, token=, etc.)
    #   as HIGH risk — these are exfiltration vectors.
    # - URLs with very long base64-like query params as MEDIUM risk —
    #   these are usually either injection or exfiltration attempts.
    # - All other web_fetch calls as MEDIUM (untrusted content enters
    #   the conversation).
    # web_search is always LOW (it only returns metadata + snippets,
    # not full page content) but still gets a reason so the user can
    # see the agent reached outside the project.
    # These rules are ADDITIVE to assess_risk — they don't relax any
    # existing rule. The structural "untrusted external content"
    # tagging is enforced at the tool-result layer (see
    # ToolEngine._web_search / _web_fetch — both wrap their output in
    # <context_fragment type="web_*"> so the prompt layer can tell
    # fetched content apart from user commands).
    elif tool_name == "web_fetch":
        url = args.get("url", "") or ""
        suspicious = _check_web_fetch_url(url)
        if suspicious == "secret":
            reasons.append(f"web_fetch URL contains secret-shaped param — likely exfiltration vector")
            level = "high"
        elif suspicious == "base64":
            reasons.append("web_fetch URL has long base64-like query param — possible injection/exfiltration")
            level = "high" if level == "high" else "medium"
        elif suspicious == "too_long":
            reasons.append("web_fetch URL is unusually long (>2000 chars) — often malicious")
            level = "high" if level == "high" else "medium"
        else:
            reasons.append("web_fetch — untrusted external content enters conversation")
            level = "medium"
    elif tool_name == "web_search":
        reasons.append("web_search — external content fetched from the internet")
        # Stays at "low" — snippets are metadata, not full page content.

    return RiskAssessment(level=level, reasons=reasons)


# --- Guardian LLM Review -------------------------------------------------


@dataclass(frozen=True)
class GuardianVerdict:
    verdict: str  # "APPROVE" | "REJECT" | "MODIFY"
    rationale: str
    suggested_args: Optional[dict[str, Any]]


async def review_with_llm(
    *,
    config: GuardianConfig,
    tool_name: str,
    args: dict[str, Any],
    risk: RiskAssessment,
    recent_context: str,
    provider_registry,
    workspace: str,
    _spawn_fn_for_test: Optional[Any] = None,
) -> GuardianVerdict:
    """Call the LLM to review the tool call. Returns verdict.

    Issue #4: when ``config.use_subagent`` is True, delegates the actual
    LLM call to a read-only ``GeneralPurposeSubagent`` (see
    ``review_with_subagent``). The direct-provider path is preserved as
    the default to keep existing behaviour intact.

    The ``_spawn_fn_for_test`` parameter is an internal hook so unit
    tests can inject a fake spawn callable without monkey-patching
    ``tera_pilot.agent.subagent_v2.spawn_subagent``. Production callers should
    not pass it.
    """
    if config.use_subagent:
        return await review_with_subagent(
            config=config,
            tool_name=tool_name,
            args=args,
            risk=risk,
            recent_context=recent_context,
            provider_registry=provider_registry,
            workspace=workspace,
            spawn_fn=_spawn_fn_for_test,
        )

    # Load system prompt from template file
    template_path = os.path.join(os.path.dirname(__file__), "templates", "guardian.md")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            system_prompt = f.read()
    except Exception as e:
        logger.warning("guardian: failed to load template %s: %s", template_path, e)
        system_prompt = (
            "You are a safety reviewer. Return JSON: "
            "{verdict, rationale, suggested_args}."
        )

    # Build user message
    user_data = {
        "tool": tool_name,
        "args": args,
        "risk_level": risk.level,
        "reasons": risk.reasons,
        "recent_context": recent_context,
    }
    user_prompt = json.dumps(user_data, ensure_ascii=False)

    # Get provider and model from config or registry
    provider_id = config.provider_id
    model = config.model

    # Resolve provider from registry
    if provider_id == "auto":
        provider_id = provider_registry.active_id or "ollama"
    if model == "auto":
        provider_obj = provider_registry.active if hasattr(provider_registry, "active") else None
        model = provider_obj.get_model() if provider_obj and hasattr(provider_obj, "get_model") else "llama3"

    provider = provider_registry.get(provider_id)
    if provider is None:
        # Fail CLOSED: a missing reviewer must not silently approve the
        # risky call under review.
        return GuardianVerdict(
            verdict="REJECT",
            rationale=f"Guardian: provider '{provider_id}' not found — defaulting to reject (fail closed)",
            suggested_args=None,
        )

    # Circuit breaker
    key = f"{provider_id}/{model}"
    breaker_registry = get_circuit_breaker_registry()
    breaker = breaker_registry.get(key)
    if not breaker.try_claim():
        logger.warning("guardian: circuit breaker open for %s", key)
        return GuardianVerdict(
            verdict="REJECT",
            rationale="Circuit breaker open — rate limited",
            suggested_args=None,
        )

    try:
        messages = [
            ProviderMessage(role="system", content=system_prompt),
            ProviderMessage(role="user", content=user_prompt),
        ]
        response: ProviderResponse = provider.generate(messages, model=model)
        raw = response.text or ""

        # Parse JSON from response
        verdict = _parse_verdict(raw)
        if verdict is None:
            # Fail CLOSED: an unparseable review is a failed review.
            logger.warning("guardian: failed to parse verdict from LLM — REJECTING (fail closed)")
            breaker.record(ok=True)
            return GuardianVerdict(
                verdict="REJECT",
                rationale="LLM response unparseable — defaulting to reject (fail closed)",
                suggested_args=None,
            )
        breaker.record(ok=True)
        return verdict

    except Exception as e:
        logger.exception("guardian: LLM call failed: %s", e)
        breaker.record(ok=False, rate_limited=_looks_like_rate_limit(e))
        # Fail CLOSED: an LLM error must not silently approve the risky call.
        return GuardianVerdict(
            verdict="REJECT",
            rationale=f"LLM error — defaulting to reject (fail closed): {e}",
            suggested_args=None,
        )


# --- Issue #4: Subagent reviewer ----------------------------------------


async def review_with_subagent(
    *,
    config: GuardianConfig,
    tool_name: str,
    args: dict[str, Any],
    risk: RiskAssessment,
    recent_context: str,
    provider_registry,
    workspace: str,
    spawn_fn: Optional[Any] = None,
    runtime: Optional[Any] = None,
) -> GuardianVerdict:
    """Delegate the Guardian LLM review to a read-only subagent.

    Issue #4: instead of calling the parent provider directly, we ask a
    read-only subagent (tool whitelist enforced at toolset construction
    time) to perform the review. The subagent's response is parsed with
    the same ``_parse_verdict`` helper used by the direct-provider path,
    so verdict semantics stay identical.

    Args:
        spawn_fn: optional callable used to spawn the subagent. If None,
            falls back to ``tera_pilot.agent.subagent_v2.spawn_subagent``.
            The callable must accept ``(runtime, subagent_type, prompt)``
            and return either a string or an object with ``.text`` /
            ``.content``. Tests inject an ``AsyncMock`` here.
        runtime: the parent runtime. Required when ``spawn_fn`` is None
            (so we can call the real ``spawn_subagent``). Ignored when
            ``spawn_fn`` is provided.
    """
    template_path = os.path.join(os.path.dirname(__file__), "templates", "guardian.md")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            system_prompt = f.read()
    except Exception as e:
        logger.warning("guardian: failed to load template %s: %s", template_path, e)
        system_prompt = (
            "You are a safety reviewer. Return JSON: "
            "{verdict, rationale, suggested_args}."
        )

    user_data = {
        "tool": tool_name,
        "args": args,
        "risk_level": risk.level,
        "reasons": risk.reasons,
        "recent_context": recent_context,
    }
    user_prompt = (
        f"{system_prompt}\n\n"
        f"--- REVIEW REQUEST ---\n{json.dumps(user_data, ensure_ascii=False)}"
    )

    # Resolve the spawn callable.
    if spawn_fn is None:
        try:
            from tera_pilot.agent.subagent_v2 import spawn_subagent as spawn_fn  # type: ignore
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("guardian: subagent module unavailable: %s", e)
            return GuardianVerdict(
                verdict="REJECT",
                rationale=f"Subagent reviewer unavailable — defaulting to reject (fail closed): {e}",
                suggested_args=None,
            )
        if runtime is None:
            return GuardianVerdict(
                verdict="REJECT",
                rationale="Subagent reviewer needs a runtime — defaulting to reject (fail closed)",
                suggested_args=None,
            )

    # Use the built-in `explore` subagent: it is read-only by toolset
    # construction (no write/exec tools advertised to the LLM). The
    # guardian system prompt is prepended to the user prompt so the
    # subagent's own prompt template doesn't drown it out.
    try:
        result = spawn_fn(runtime, "explore", user_prompt)
        if hasattr(result, "__await__"):
            result = await result
    except Exception as e:
        logger.warning("guardian: subagent spawn failed: %s", e)
        return GuardianVerdict(
            verdict="REJECT",
            rationale=f"Subagent spawn failed — defaulting to reject (fail closed): {e}",
            suggested_args=None,
        )

    raw = ""
    if isinstance(result, str):
        raw = result
    elif hasattr(result, "text"):
        raw = result.text or ""
    elif hasattr(result, "content"):
        raw = result.content or ""

    verdict = _parse_verdict(raw)
    if verdict is None:
        logger.warning(
            "guardian: subagent response unparseable — REJECTING (fail closed)"
        )
        return GuardianVerdict(
            verdict="REJECT",
            rationale="Subagent response unparseable — defaulting to reject (fail closed)",
            suggested_args=None,
        )
    return verdict


def _parse_verdict(raw: str) -> Optional[GuardianVerdict]:
    """Parse JSON verdict from LLM response. Handles fenced code blocks."""
    # Try to extract JSON from markdown fences
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        # Try bare JSON
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
    try:
        data = json.loads(raw)
        # All fields must be present
        if "verdict" not in data or "rationale" not in data or "suggested_args" not in data:
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
        return GuardianVerdict(verdict=verdict, rationale=rationale, suggested_args=suggested)
    except Exception:
        return None


def _looks_like_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in (
            "rate limit",
            "rate_limit",
            "ratelimit",
            "too many requests",
            "429",
            "quota exceeded",
            "throttl",
        )
    )


# --- Recent Context Builder ---------------------------------------------


def build_recent_context(memory, max_messages: int = 4, max_chars: int = 2000) -> str:
    """Build the projected-history string for Guardian (mirrors subagent_host.py)."""
    parts = []
    if getattr(memory, "compaction_summary", None):
        parts.append(f"[PARENT CONTEXT SUMMARY]\n{memory.compaction_summary}")
    messages = getattr(memory, "messages", [])
    for m in messages[-max_messages:]:
        role = getattr(m, "role", "unknown")
        content = getattr(m, "content", "")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... ({len(content)} total chars)"
        role_label = (
            "USER"
            if role == "user"
            else "ASSISTANT"
            if role == "assistant"
            else role.upper()
        )
        parts.append(f"[{role_label}]\n{content}")
    return "\n\n".join(parts)
