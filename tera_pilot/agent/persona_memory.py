"""G19b — Persona pyramid (cross-session, per-user profile).

A *per-user*, cross-project profile of how the person likes to work —
coding style preferences, tools they always want used, communication
preferences. Distilled hierarchically: raw conversation → atomic facts
→ scenario/scene grouping → a single condensed profile.

Contrast with G17 (``tera_pilot/learning_loop.py``) which is *per-repository*
"what went wrong" learnings. Persona lives at ``~/.tera_pilot/persona.md``
(global) so it travels with the user, not with the project.

Design constraints (from the G19 prompt):
- Hard size cap (~2000 chars). Overwrites are NOT allowed past the cap
  — the maintenance LLM call must prune stale/contradicted facts to
  stay under it.
- Update via a cheap, dedicated LLM call (reuse ``AutoRouter``'s
  trivial/simple tier). Must NOT be an expensive-model call.
- The maintenance call receives the current ``persona.md`` plus a short
  session digest and returns a **targeted edit** (not a full rewrite).
  The prompt must instruct the model to actively prune stale/
  contradicted facts and must scope it to editing this one file only
  (mirror the read-only ``researcher`` role pattern — same "narrow
  blast radius" principle, applied to a write-scoped maintenance call).
- Wire ``persona.md`` injection into the same prompt-building path as
  G17 learnings, through ``build_fragment()`` so it participates in
  the same compaction discipline and never becomes a second source of
  permanent bloat.

This module is the in-process API. The actual LLM maintenance call is
triggered by the runtime at session-end (see ``update_from_session()``)
and is deliberately best-effort: any failure leaves the existing
persona on disk untouched.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from tera_pilot.agent.context_fragments import build_fragment, stable_id

logger = logging.getLogger(__name__)

# Hard size cap, in characters. The prompt says "~2000 chars" — we use
# 2000 as the soft cap (the maintenance LLM is instructed to stay under)
# and 2200 as a hard ceiling (anything larger is truncated with a note
# so the file never silently grows past the budget).
SOFT_CAP_CHARS = 2000
HARD_CAP_CHARS = 2200

# Fragment type used when wrapping the persona for prompt injection.
# Mirrors the snake_case convention used by web_search / web_page /
# project_learnings / task_canvas.
FRAGMENT_TYPE = "persona"

# The maintenance prompt is intentionally scoped to "edit this one file
# only" — mirrors the read-only ``researcher`` role pattern from
# ``ROLE_TOOL_WHITELIST``, applied to a write-scoped maintenance call.
# The model receives the current persona + a session digest and returns
# a targeted edit, NOT a full rewrite.
_MAINTENANCE_SYSTEM_PROMPT = (
    "You are the persona-maintenance agent for the Tera Pilot AI coding assistant. "
    "Your ONLY job is to update the user's persona profile file (~/.tera_pilot/persona.md) "
    "based on a session digest.\n\n"
    "STRICT RULES:\n"
    "1. You may ONLY edit the persona file. Do not propose any other action.\n"
    "2. Output the FULL new persona.md content, nothing else (no markdown fences, "
    "no commentary, no preamble).\n"
    "3. Hard size cap: 2000 characters. If the existing file is already near the "
    "cap, you MUST prune stale or contradicted facts before adding new ones.\n"
    "4. Prefer concrete, actionable preferences over vague generalities "
    "(\"uses spaces, 4-wide\", not \"likes clean code\").\n"
    "5. If the session digest contradicts an existing fact, REPLACE the fact — "
    "do not append a contradiction.\n"
    "6. If the session digest adds nothing useful, return the existing file "
    "UNCHANGED (byte-for-byte).\n"
    "7. Never include secrets, API keys, tokens, or anything that looks like "
    "credentials. If the digest contains them, drop them silently.\n"
)

# Session digest prompt — short summary of what happened in the session.
_DIGEST_USER_PROMPT_TEMPLATE = (
    "CURRENT PERSONA (do not exceed {cap} chars in the output):\n"
    "-----\n"
    "{current}\n"
    "-----\n\n"
    "SESSION DIGEST (what the user did / asked for / accepted / rejected "
    "in the most recent session):\n"
    "-----\n"
    "{digest}\n"
    "-----\n\n"
    "Output the new persona.md content. Remember: cap is {cap} chars, "
    "drop stale/contradicted facts, output ONLY the file content."
)


@dataclass
class PersonaDigest:
    """Structured session digest fed to the maintenance LLM call.

    The runtime populates this at session-end from the ActivityLog /
    conversation history. Each field is optional — the maintenance call
    works with whatever is present.
    """

    summary: str = ""
    accepted_actions: list[str] = field(default_factory=list)
    rejected_actions: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    coding_preferences_observed: list[str] = field(default_factory=list)
    communication_preferences_observed: list[str] = field(default_factory=list)
    # Free-form notes (e.g. "user explicitly asked to use tabs not spaces")
    notes: list[str] = field(default_factory=list)

    def to_prompt_text(self) -> str:
        """Render the digest as a compact text block for the LLM."""
        parts: list[str] = []
        if self.summary:
            parts.append(f"Summary: {self.summary}")
        if self.accepted_actions:
            parts.append("Accepted: " + "; ".join(self.accepted_actions))
        if self.rejected_actions:
            parts.append("Rejected: " + "; ".join(self.rejected_actions))
        if self.tools_used:
            parts.append("Tools used: " + ", ".join(self.tools_used))
        if self.coding_preferences_observed:
            parts.append(
                "Coding preferences observed: "
                + "; ".join(self.coding_preferences_observed)
            )
        if self.communication_preferences_observed:
            parts.append(
                "Communication preferences observed: "
                + "; ".join(self.communication_preferences_observed)
            )
        if self.notes:
            parts.append("Notes: " + "; ".join(self.notes))
        return "\n".join(parts) if parts else "(empty digest)"


class PersonaMemory:
    """Per-user persona profile, stored at ``~/.tera_pilot/persona.md``.

    The file is plain Markdown so the user can edit it directly with any
    editor (and so the ``/persona`` TUI/CLI command can show it as-is).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        # Path is overridable for tests. Production callers should use
        # :func:`get_persona_memory` which lazy-inits the singleton with
        # the default path.
        self._path = path or _default_path()
        self._lock = threading.RLock()
        self._cached: Optional[str] = None  # None = not loaded yet
        self._cached_mtime: float = 0.0

    # ------------------------------------------------------------------ #
    # Path / persistence
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> Path:
        return self._path

    def _load_from_disk(self) -> str:
        """Read the persona file. Returns ``""`` if missing or unreadable.

        We intentionally swallow errors here — a missing or corrupt
        persona file must not break the agent loop. The caller (the
        prompt-builder) treats an empty persona as "skip injection".
        """
        try:
            if not self._path.exists():
                return ""
            content = self._path.read_text(encoding="utf-8")
            # Defensive: enforce hard cap even if the user hand-edited
            # past it. Truncating here is safer than letting a 50KB file
            # silently bloat every prompt.
            if len(content) > HARD_CAP_CHARS:
                content = content[:HARD_CAP_CHARS] + "\n[truncated by persona_memory]\n"
            return content
        except Exception as e:
            logger.debug("[persona] load failed (%s): %s", self._path, e)
            return ""

    def get(self, *, force_reload: bool = False) -> str:
        """Return the current persona content.

        Caches the result and re-reads only when the file mtime changes
        or ``force_reload=True``. This keeps the per-turn injection cost
        at one ``stat()`` call instead of a full file read.
        """
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime if self._path.exists() else 0.0
            except Exception:
                mtime = 0.0
            if force_reload or self._cached is None or mtime != self._cached_mtime:
                self._cached = self._load_from_disk()
                self._cached_mtime = mtime
            return self._cached or ""

    def set(self, content: str) -> None:
        """Write a new persona content to disk.

        Used by the ``/persona edit`` TUI command and by the maintenance
        LLM call. Enforces the hard cap (truncates with a note if
        exceeded) so the file never silently grows past the budget.
        Creates the parent directory if missing.
        """
        if not isinstance(content, str):
            raise TypeError("persona content must be a string")
        if len(content) > HARD_CAP_CHARS:
            content = content[:HARD_CAP_CHARS] + "\n[truncated by persona_memory]\n"
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # Atomic-ish write: write to .tmp then rename. Avoids
                # leaving a half-written file if the process is killed
                # mid-write (which would corrupt the persona for every
                # future session).
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, self._path)
                self._cached = content
                try:
                    self._cached_mtime = self._path.stat().st_mtime
                except Exception:
                    self._cached_mtime = time.time()
            except Exception as e:
                logger.warning("[persona] write failed (%s): %s", self._path, e)
                raise

    def reset(self) -> None:
        """Delete the persona file. Used by ``/persona reset``."""
        with self._lock:
            try:
                if self._path.exists():
                    self._path.unlink()
            except Exception as e:
                logger.warning("[persona] reset failed (%s): %s", self._path, e)
            self._cached = ""
            self._cached_mtime = 0.0

    # ------------------------------------------------------------------ #
    # Prompt injection
    # ------------------------------------------------------------------ #
    def to_fragment(self) -> Optional[str]:
        """Wrap the persona in a ``<context_fragment>`` block.

        Returns ``None`` when the persona is empty so the prompt-builder
        can skip injection cleanly without emitting an empty fragment
        (which would still cost tokens and pollute the compaction
        statistics).
        """
        content = self.get().strip()
        if not content:
            return None
        # Stable id means re-emitting each turn is idempotent — the
        # compactor keeps only the latest per-id, so the persona never
        # accumulates across turns even though we inject it every turn.
        fid = stable_id(FRAGMENT_TYPE, "current")
        return build_fragment(FRAGMENT_TYPE, fid, content)

    # ------------------------------------------------------------------ #
    # Maintenance LLM call
    # ------------------------------------------------------------------ #
    def update_from_session(
        self,
        digest: PersonaDigest,
        *,
        registry: Optional[Any] = None,
        auto_router: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Run the cheap maintenance LLM call to update the persona.

        Best-effort: any failure (no provider, LLM error, parse error,
        over-cap output) leaves the existing persona on disk untouched
        and returns ``{"ok": False, "error": str(e)}``.

        On success, returns ``{"ok": True, "before_chars": N, "after_chars": M,
        "provider_id": ..., "model": ...}``.

        Reuses ``AutoRouter``'s SIMPLE tier (``TaskComplexity.SIMPLE``)
        so this is never an expensive-model call. The caller may pass
        an explicit ``registry`` (e.g. for tests) — if omitted, the
        runtime's active provider is used.

        The maintenance prompt is scoped to "edit this one file only" —
        same "narrow blast radius" principle as the read-only
        ``researcher`` role, applied to a write-scoped maintenance call.
        """
        try:
            current = self.get()
            digest_text = digest.to_prompt_text()
            user_prompt = _DIGEST_USER_PROMPT_TEMPLATE.format(
                cap=SOFT_CAP_CHARS, current=current or "(empty)", digest=digest_text
            )

            provider, model, provider_id = self._pick_maintenance_provider(
                registry=registry, auto_router=auto_router
            )
            if provider is None:
                return {
                    "ok": False,
                    "error": "no provider available for persona maintenance",
                }

            # Build the messages. Lazy-import ProviderMessage to avoid
            # pulling the providers package at module import time.
            from tera_pilot.providers.base import ProviderMessage

            messages = [
                ProviderMessage(role="system", content=_MAINTENANCE_SYSTEM_PROMPT),
                ProviderMessage(role="user", content=user_prompt),
            ]

            # Use the cheap model with a tight max_tokens — the persona
            # is capped at 2000 chars, so 1500 tokens is plenty.
            raw_text = self._call_provider(provider, messages, model)
            if not raw_text:
                return {
                    "ok": False,
                    "error": "maintenance LLM returned empty output",
                    "provider_id": provider_id,
                    "model": model,
                }

            # Strip markdown fences if the model added them despite the
            # "no fences" instruction. Defensive — keeps the file clean.
            new_content = _strip_code_fences(raw_text)

            # If the model returned the input unchanged (byte-for-byte),
            # skip the write — saves disk I/O and an mtime bump that
            # would force every prompt-builder to re-read.
            if new_content.strip() == current.strip() and new_content.strip():
                return {
                    "ok": True,
                    "before_chars": len(current),
                    "after_chars": len(current),
                    "provider_id": provider_id,
                    "model": model,
                    "unchanged": True,
                }

            # Enforce soft cap. If the model exceeded it, truncate
            # rather than refusing — better to land an imperfect update
            # than to silently drop the user's session learnings.
            if len(new_content) > SOFT_CAP_CHARS:
                new_content = (
                    new_content[: SOFT_CAP_CHARS - 40]
                    + "\n[persona trimmed to fit cap]\n"
                )

            before_chars = len(current)
            self.set(new_content)
            after_chars = len(new_content)
            return {
                "ok": True,
                "before_chars": before_chars,
                "after_chars": after_chars,
                "provider_id": provider_id,
                "model": model,
                "unchanged": False,
            }
        except Exception as e:
            logger.warning("[persona] maintenance call failed: %s", e)
            return {"ok": False, "error": str(e)}

    def _pick_maintenance_provider(
        self,
        *,
        registry: Optional[Any] = None,
        auto_router: Optional[Any] = None,
    ) -> tuple[Optional[Any], str, str]:
        """Pick a cheap provider for the maintenance call.

        Preference order:
        1. The active provider from the registry (already configured by
           the user — guaranteed to work).
        2. An AutoRouter SIMPLE-tier pick (if auto_router is supplied).

        Returns ``(provider, model, provider_id)``. ``(None, "", "")`` if
        nothing is available.
        """
        # Try the active provider first — it's the one the user
        # explicitly configured, so it's guaranteed to have a working
        # API key.
        if registry is None:
            try:
                from tera_pilot.providers import ProviderRegistry

                registry = ProviderRegistry()
            except Exception as e:
                logger.debug("[persona] no registry: %s", e)
                registry = None
        if registry is not None:
            try:
                provider = registry.active
                if provider is not None:
                    # Some providers need explicit load() before generate()
                    if hasattr(provider, "is_loaded") and not provider.is_loaded:
                        provider.load()
                    model = ""
                    if hasattr(provider, "get_model"):
                        model = provider.get_model() or ""
                    if not model:
                        model = getattr(provider, "model", "") or ""
                    pid = getattr(provider, "provider_id", "") or getattr(
                        provider, "name", ""
                    )
                    return provider, model, pid
            except Exception as e:
                logger.debug("[persona] active provider unavailable: %s", e)

        # Fall back to AutoRouter SIMPLE tier. This is the "cheap tier"
        # the G19 prompt refers to — TaskComplexity.SIMPLE maps to
        # groq/deepseek/zai/etc., never to the expensive EXPERT models.
        if auto_router is None:
            try:
                from tera_pilot.auto_router import AutoRouter, TaskComplexity

                auto_router = AutoRouter()
                simple_tiers = auto_router._tiers.get(TaskComplexity.SIMPLE, [])
                for tier in simple_tiers:
                    auto_router.mark_provider_available(tier.provider_id, True)
            except Exception as e:
                logger.debug("[persona] no auto_router: %s", e)
                return None, "", ""
        try:
            from tera_pilot.auto_router import TaskComplexity

            decision = auto_router.route(
                "update the user persona profile based on a session digest",
                configured_providers=None,
            )
            if not decision or not decision.get("provider_id"):
                return None, "", ""
            provider_id = decision["provider_id"]
            model = decision.get("model", "")
            if registry is None:
                return None, "", ""
            provider = registry.get(provider_id)
            if provider is None:
                return None, "", ""
            if hasattr(provider, "is_loaded") and not provider.is_loaded:
                provider.load()
            return provider, model, provider_id
        except Exception as e:
            logger.debug("[persona] auto_router fallback failed: %s", e)
            return None, "", ""

    def _call_provider(
        self, provider: Any, messages: list, model: str
    ) -> str:
        """Call ``provider.generate()`` and return the text.

        Wrapped in try/except by the caller (``update_from_session``).
        Uses a thread + ``join(timeout=...)`` pattern so a hung provider
        can't block the session-end hook forever — mirrors the
        ``_call_one_provider`` pattern from ``consensus_engine.py``.
        """
        timeout_s = 30.0
        result_holder: Dict[str, Any] = {}

        def _do_call() -> None:
            try:
                if model:
                    resp = provider.generate(messages, model=model)
                else:
                    resp = provider.generate(messages)
                result_holder["resp"] = resp
            except Exception as e:
                result_holder["err"] = e

        import threading

        th = threading.Thread(target=_do_call, daemon=True)
        th.start()
        th.join(timeout=timeout_s)
        if th.is_alive():
            raise TimeoutError(
                f"persona maintenance LLM call timed out after {timeout_s}s"
            )
        if "err" in result_holder:
            raise result_holder["err"]
        resp = result_holder.get("resp")
        if resp is None:
            return ""
        # ProviderResponse.text — same attribute used by consensus_engine.
        return getattr(resp, "text", "") or ""

    # ------------------------------------------------------------------ #
    # Serialisation (for /persona show as JSON, /persona edit on disk)
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        """State snapshot for the TUI/GUI bridge."""
        content = self.get()
        return {
            "path": str(self._path),
            "content": content,
            "chars": len(content),
            "soft_cap": SOFT_CAP_CHARS,
            "hard_cap": HARD_CAP_CHARS,
            "over_soft_cap": len(content) > SOFT_CAP_CHARS,
        }


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _default_path() -> Path:
    """Return the default persona path: ``~/.tera_pilot/persona.md``.

    Does NOT create the directory — :meth:`PersonaMemory.set` does that
    on first write. We avoid touching the filesystem at module import
    time so importing this module has zero side effects.
    """
    return Path.home() / ".tera_pilot" / "persona.md"


def _strip_code_fences(text: str) -> str:
    """Strip a single surrounding ```markdown ... ``` fence if present.

    The maintenance prompt says "no markdown fences", but models
    sometimes add them anyway. We strip ONE outer fence (not inner
    ones — those might be legitimate content the user wants kept).
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Drop the opening fence line (which may have a language tag).
    first_nl = s.find("\n")
    if first_nl == -1:
        return s
    body = s[first_nl + 1 :]
    # Drop the closing fence if present.
    if body.rstrip().endswith("```"):
        body = body.rstrip()
        body = body[: -3].rstrip()
    return body


# ---------------------------------------------------------------------- #
# Module-level singleton (lazy) — mirrors get_activity_log() etc.
# ---------------------------------------------------------------------- #
_PERSONA: Optional[PersonaMemory] = None
_PERSONA_LOCK = threading.Lock()


def get_persona_memory() -> PersonaMemory:
    """Return the process-wide :class:`PersonaMemory` singleton."""
    global _PERSONA
    if _PERSONA is None:
        with _PERSONA_LOCK:
            if _PERSONA is None:
                _PERSONA = PersonaMemory()
    return _PERSONA


def reset_persona_memory_for_test(path: Optional[Path] = None) -> PersonaMemory:
    """Replace the singleton with a fresh instance pointing at ``path``.

    Test-only. Production callers should use :func:`get_persona_memory`.
    """
    global _PERSONA
    with _PERSONA_LOCK:
        _PERSONA = PersonaMemory(path=path) if path else PersonaMemory()
    return _PERSONA
