"""
Consensus Engine — run the same task on 2–3 providers in parallel and
produce a structured diff/comparison between the approaches (G15).

Builds on the existing M1 (``second_opinion.py``) and ``AutoRouter``
infrastructure — it does NOT reimplement provider orchestration. Instead
it reuses:

- ``ProviderRegistry`` to look up provider instances.
- The same ``ProviderMessage`` shape every other provider call uses.
- The same ``~/.tera_pilot/config.json`` persistence convention as M1.

The consensus engine is intentionally *narrow* in scope compared to a
full ensemble agent:

1. It runs the SAME prompt on 2–3 providers in parallel (threads —
   provider.generate is blocking).
2. It collects each provider's textual answer + structured metadata.
3. It computes a structured diff/comparison (not just raw text side by
   side): files touched, code blocks, surface risk profile, length.
4. It explains WHY the approaches diverge where it can — e.g.
   "Provider A produced 3 files, Provider B produced 1 larger file"
   is reported as an explicit divergence point with a likely reason.
5. It fails safe: if one provider errors out, the comparison still
   shows the providers that succeeded and explicitly flags the failed
   one in the output (no whole-comparison abort).

Configurable via ``~/.tera_pilot/config.json`` under the ``consensus`` key,
mirroring the persistence pattern of M1's ``second_opinion`` block.

Slash command surface (TUI + GUI): ``/consensus`` mirrors the existing
``/second_opinion`` and ``/verify`` pattern — see ``tera_pilot_tui/app.py``
and ``tera_pilot/web_bridge/bridge.py``.

Zero-telemetry: nothing here phones home. The providers are called
directly by the user's existing provider registry, exactly like every
other LLM call in Tera Pilot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Config persistence (mirrors second_opinion.py) ─────────────────────

_CONFIG_KEY = "consensus"


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
        logger.warning("[consensus] failed to save config: %s", e)


@dataclass(frozen=True)
class ConsensusConfig:
    """User-facing configuration for the consensus engine.

    - ``providers``: ordered list of provider ids to fan out to. If
      empty, ``run_consensus`` picks a default triplet based on the
      active provider (cross-family, mirroring second_opinion's logic).
    - ``min_agreement``: 0.0–1.0 threshold below which the comparison
      is flagged as "low agreement" in the summary. 1.0 = all answers
      must be byte-identical, 0.0 = never flag. Default 0.4 — useful
      signal without being noisy.
    - ``timeout_s``: per-provider wall-clock budget. Default 60s.
    - ``max_chars_per_response``: cap each provider's textual answer
      before diffing. Keeps the comparison output bounded.
    """
    providers: Tuple[str, ...] = ()
    min_agreement: float = 0.4
    timeout_s: float = 60.0
    max_chars_per_response: int = 8000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "providers": list(self.providers),
            "min_agreement": self.min_agreement,
            "timeout_s": self.timeout_s,
            "max_chars_per_response": self.max_chars_per_response,
        }


def get_consensus_config() -> ConsensusConfig:
    """Load the consensus config from ``~/.tera_pilot/config.json``."""
    cfg = _load_config()
    block = cfg.get(_CONFIG_KEY, {}) or {}
    providers_raw = block.get("providers", []) or []
    # Coerce to tuple of strings, deduped, preserving order.
    seen: set = set()
    providers: List[str] = []
    for p in providers_raw:
        pid = str(p).strip()
        if pid and pid not in seen:
            seen.add(pid)
            providers.append(pid)
    return ConsensusConfig(
        providers=tuple(providers),
        min_agreement=float(block.get("min_agreement", 0.4)),
        timeout_s=float(block.get("timeout_s", 60.0)),
        max_chars_per_response=int(block.get("max_chars_per_response", 8000)),
    )


def set_consensus_config(new_cfg: ConsensusConfig) -> None:
    """Persist the consensus config."""
    cfg = _load_config()
    cfg[_CONFIG_KEY] = new_cfg.to_dict()
    _save_config(cfg)


# ── Default provider triplet picker ────────────────────────────────────
# Mirrors second_opinion.py's _CROSS_FAMILY_DEFAULTS but picks THREE
# providers from different families so the comparison is meaningful.
# We always include the active provider so the user can compare "what
# I'm using now" vs two alternatives.

_DEFAULT_TRIPLETS: Dict[str, Tuple[str, str, str]] = {
    # active provider → (active, second, third) — three different families
    "ollama":       ("ollama", "groq", "openai"),
    "lmstudio":     ("lmstudio", "groq", "openai"),
    "nvidia_nim":   ("nvidia_nim", "groq", "openai"),
    "openai":       ("openai", "anthropic", "groq"),
    "anthropic":    ("anthropic", "openai", "groq"),
    "openrouter":   ("openrouter", "groq", "openai"),
    "groq":         ("groq", "openai", "anthropic"),
    "deepseek":     ("deepseek", "openai", "groq"),
    "zai":          ("zai", "openai", "anthropic"),
    "gemini":       ("gemini", "openai", "anthropic"),
    "mistral":      ("mistral", "openai", "groq"),
    "together":     ("together", "groq", "openai"),
    "fireworks":    ("fireworks", "groq", "openai"),
    "xai":          ("xai", "openai", "anthropic"),
    "cerebras":     ("cerebras", "openai", "groq"),
    "sambanova":    ("sambanova", "openai", "groq"),
}


def resolve_default_providers(active_provider_id: str) -> Tuple[str, str, str]:
    """Pick a default triplet for the comparison.

    Always includes the active provider so the user can compare their
    current choice against two alternatives from different families.
    Falls back to (active, groq, openai) for unknown active providers.
    """
    if not active_provider_id:
        return ("ollama", "groq", "openai")
    triplet = _DEFAULT_TRIPLETS.get(active_provider_id)
    if triplet:
        return triplet
    return (active_provider_id, "groq", "openai")


# ── Result dataclasses ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderResponse:
    """One provider's response in a consensus run.

    ``error`` is None on success. When set, ``text`` is "" and the
    comparison layer treats this provider as failed-but-present (the
    fail-safe rule: don't discard the whole comparison).
    """
    provider_id: str
    model: str
    text: str
    elapsed_ms: float
    error: Optional[str] = None
    # Extracted features — populated by _extract_features()
    files_touched: Tuple[str, ...] = ()
    code_blocks: int = 0
    code_chars: int = 0
    text_chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "text": self.text,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
            "files_touched": list(self.files_touched),
            "code_blocks": self.code_blocks,
            "code_chars": self.code_chars,
            "text_chars": self.text_chars,
        }


@dataclass(frozen=True)
class DivergencePoint:
    """One structured difference between provider responses.

    - ``dimension``: what aspect differs (files, code_volume, length,
      approach, risk).
    - ``description``: human-readable one-liner.
    - ``likely_reason``: best-effort explanation of WHY they diverge.
    """
    dimension: str
    description: str
    likely_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension,
            "description": self.description,
            "likely_reason": self.likely_reason,
        }


@dataclass(frozen=True)
class ConsensusReport:
    """Full structured comparison of a multi-provider run."""
    prompt: str
    responses: Tuple[ProviderResponse, ...]
    divergences: Tuple[DivergencePoint, ...]
    agreement_score: float  # 0.0–1.0, 1.0 = identical
    summary: str
    elapsed_ms: float

    @property
    def succeeded(self) -> Tuple[ProviderResponse, ...]:
        return tuple(r for r in self.responses if r.error is None)

    @property
    def failed(self) -> Tuple[ProviderResponse, ...]:
        return tuple(r for r in self.responses if r.error is not None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "responses": [r.to_dict() for r in self.responses],
            "divergences": [d.to_dict() for d in self.divergences],
            "agreement_score": round(self.agreement_score, 3),
            "summary": self.summary,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "succeeded_count": len(self.succeeded),
            "failed_count": len(self.failed),
        }


# ── Feature extraction ─────────────────────────────────────────────────
# These are the "structured diff" primitives — we pull out the aspects
# that actually matter for comparing coding-agent outputs, rather than
# doing a naive line diff on the full text (which is noisy and rarely
# surfaces the real differences).

# File paths in fenced code blocks: ```lang path/to/file.py
_FILE_HEADER_RE = re.compile(
    r"^```[a-zA-Z0-9]*\s+([^\s`]+/[^\s`]+|[^\s`]+\.(?:py|js|ts|tsx|jsx|"
    r"rs|go|java|rb|php|c|cpp|h|hpp|md|txt|json|yaml|yml|toml|sh|bash))\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Any fenced code block (for counting + measuring)
_CODE_BLOCK_RE = re.compile(r"^```[a-zA-Z0-9]*\s*$", re.MULTILINE)


def _extract_features(text: str) -> Dict[str, Any]:
    """Pull structured features out of a provider's raw text response.

    Returns dict with: files_touched (tuple of str), code_blocks (int),
    code_chars (int), text_chars (int).
    """
    if not text:
        return {
            "files_touched": (),
            "code_blocks": 0,
            "code_chars": 0,
            "text_chars": 0,
        }
    # Find file headers
    files: List[str] = []
    seen: set = set()
    for m in _FILE_HEADER_RE.finditer(text):
        path = m.group(1).strip()
        if path and path not in seen:
            seen.add(path)
            files.append(path)

    # Find code blocks — count them and measure total chars inside.
    # We walk the regex matches and pair opening/closing fences.
    lines = text.split("\n")
    in_block = False
    block_starts: List[int] = []
    block_ends: List[int] = []
    code_chars = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") and len(stripped) <= 30:
            if not in_block:
                in_block = True
                block_starts.append(i)
            else:
                in_block = False
                block_ends.append(i)
        elif in_block:
            code_chars += len(line) + 1  # +1 for the newline
    # If we have an odd count (unclosed block), drop the last start.
    if len(block_starts) > len(block_ends):
        block_starts = block_starts[:len(block_ends)]
    code_blocks = len(block_starts)

    return {
        "files_touched": tuple(files),
        "code_blocks": code_blocks,
        "code_chars": code_chars,
        "text_chars": len(text),
    }


# ── Per-provider call (threaded) ───────────────────────────────────────


def _call_one_provider(
    provider_id: str,
    prompt: str,
    system_prompt: str,
    registry: Any,
    timeout_s: float,
    max_chars: int,
) -> ProviderResponse:
    """Call a single provider. Returns a ProviderResponse (never raises —
    errors are captured in the ``error`` field so the comparison layer
    can include failed providers in the report).
    """
    t0 = time.time()
    try:
        from tera_pilot.providers import ProviderMessage
    except Exception as e:
        return ProviderResponse(
            provider_id=provider_id, model="",
            text="", elapsed_ms=(time.time() - t0) * 1000,
            error=f"ProviderMessage import failed: {e}",
        )

    # Look up the provider instance.
    try:
        provider = registry.get(provider_id)
        if not provider.is_loaded:
            provider.load()
    except Exception as e:
        return ProviderResponse(
            provider_id=provider_id, model="",
            text="", elapsed_ms=(time.time() - t0) * 1000,
            error=f"provider '{provider_id}' unavailable: {e}",
        )

    # Determine the model — ask the provider for its default.
    model = ""
    try:
        if hasattr(provider, "get_model"):
            model = provider.get_model() or ""
        elif hasattr(provider, "model"):
            model = str(getattr(provider, "model", "") or "")
    except Exception:
        pass

    messages = [
        ProviderMessage(role="system", content=system_prompt),
        ProviderMessage(role="user", content=prompt),
    ]

    # The actual generate call — wrapped in a thread + timeout because
    # provider.generate is blocking and we don't want one slow provider
    # to hold up the whole comparison.
    result_holder: Dict[str, Any] = {}

    def _do_call() -> None:
        try:
            resp = provider.generate(messages, model=model) if model else provider.generate(messages)
            result_holder["resp"] = resp
        except Exception as e:
            result_holder["err"] = e

    th = threading.Thread(target=_do_call, daemon=True)
    th.start()
    th.join(timeout=timeout_s)
    if th.is_alive():
        return ProviderResponse(
            provider_id=provider_id, model=model,
            text="", elapsed_ms=(time.time() - t0) * 1000,
            error=f"timeout after {timeout_s}s",
        )
    if "err" in result_holder:
        return ProviderResponse(
            provider_id=provider_id, model=model,
            text="", elapsed_ms=(time.time() - t0) * 1000,
            error=str(result_holder["err"]),
        )

    resp = result_holder.get("resp")
    raw_text = ""
    if resp is not None:
        raw_text = getattr(resp, "text", "") or ""
    # Cap the response length so the comparison output stays bounded.
    if len(raw_text) > max_chars:
        raw_text = raw_text[:max_chars] + f"\n... [truncated, {len(raw_text)} total chars]"

    feats = _extract_features(raw_text)
    return ProviderResponse(
        provider_id=provider_id,
        model=model,
        text=raw_text,
        elapsed_ms=(time.time() - t0) * 1000,
        error=None,
        files_touched=feats["files_touched"],
        code_blocks=feats["code_blocks"],
        code_chars=feats["code_chars"],
        text_chars=feats["text_chars"],
    )


# ── Divergence analysis ────────────────────────────────────────────────


def _agreement_score(responses: List[ProviderResponse]) -> float:
    """Compute a 0.0–1.0 agreement score across succeeded responses.

    Uses a normalised Jaccard similarity over the set of alphanumeric
    tokens (length >= 4) in each response. 1.0 means every response
    uses the same vocabulary, 0.0 means no overlap at all.

    This is a deliberately crude measure — the goal is to flag "these
    answers are wildly different" without claiming to be a semantic
    similarity engine. The divergences list is what the user actually
    reads; the score is just a quick triage signal.
    """
    succeeded = [r for r in responses if r.error is None and r.text]
    if len(succeeded) < 2:
        return 1.0 if succeeded else 0.0
    token_sets: List[set] = []
    for r in succeeded:
        toks = set(re.findall(r"\b[A-Za-z0-9_]{4,}\b", r.text.lower()))
        token_sets.append(toks)
    # Pairwise average Jaccard.
    total = 0.0
    count = 0
    for i in range(len(token_sets)):
        for j in range(i + 1, len(token_sets)):
            a, b = token_sets[i], token_sets[j]
            union = len(a | b)
            if union == 0:
                continue
            total += len(a & b) / union
            count += 1
    return total / count if count > 0 else 0.0


def _analyze_divergences(responses: List[ProviderResponse]) -> List[DivergencePoint]:
    """Find structured differences between the succeeded responses."""
    succeeded = [r for r in responses if r.error is None]
    if len(succeeded) < 2:
        return []
    out: List[DivergencePoint] = []

    # 1. Files touched — different file sets are a strong signal of
    #    different approaches.
    file_sets = {r.provider_id: set(r.files_touched) for r in succeeded}
    all_files = set().union(*file_sets.values()) if file_sets else set()
    if all_files:
        only_in: Dict[str, set] = {}
        for pid, files in file_sets.items():
            others = all_files - files
            if others:
                only_in[pid] = files - set().union(*(s for p, s in file_sets.items() if p != pid))
        # Build a description of file-set divergence.
        differing_files = []
        for pid, files in file_sets.items():
            unique = only_in.get(pid, set())
            if unique:
                differing_files.append(f"{pid}: {sorted(unique)[:3]}")
        if differing_files:
            out.append(DivergencePoint(
                dimension="files_touched",
                description="; ".join(differing_files[:3]),
                likely_reason=(
                    "Providers disagree on which files to modify — this "
                    "usually reflects different mental models of where "
                    "the change belongs (new file vs. extend existing)."
                ),
            ))

    # 2. Code volume — large differences in code_chars suggest different
    #    verbosity / refactoring vs. minimal-patch strategies.
    code_volumes = {r.provider_id: r.code_chars for r in succeeded if r.code_chars > 0}
    if len(code_volumes) >= 2:
        max_v = max(code_volumes.values())
        min_v = min(code_volumes.values())
        if min_v > 0 and max_v / min_v >= 2.0:
            max_pid = next(p for p, v in code_volumes.items() if v == max_v)
            min_pid = next(p for p, v in code_volumes.items() if v == min_v)
            out.append(DivergencePoint(
                dimension="code_volume",
                description=(
                    f"{max_pid} produced {max_v} chars of code vs "
                    f"{min_pid}'s {min_v} chars ({max_v // max(1, min_v)}x)."
                ),
                likely_reason=(
                    f"{max_pid} likely rewrote the file/section while "
                    f"{min_pid} applied a minimal patch. Pick the minimal "
                    f"one if the change is small and surgical; pick the "
                    f"larger one if the surrounding code was messy."
                ),
            ))

    # 3. Response length (text_chars) — long text + short code often
    #    means more explanation, which can be either helpful padding or
    #    a sign the provider is hedging.
    text_lens = {r.provider_id: r.text_chars for r in succeeded}
    if len(text_lens) >= 2:
        max_t = max(text_lens.values())
        min_t = min(text_lens.values())
        if min_t > 0 and max_t / min_t >= 2.5:
            max_pid = next(p for p, v in text_lens.items() if v == max_t)
            min_pid = next(p for p, v in text_lens.items() if v == min_t)
            out.append(DivergencePoint(
                dimension="explanation_length",
                description=(
                    f"{max_pid} wrote {max_t} chars of total response vs "
                    f"{min_pid}'s {min_t} chars."
                ),
                likely_reason=(
                    f"{max_pid} is likely providing more context/rationale; "
                    f"{min_pid} is more terse. Neither is inherently better — "
                    f"choose based on whether you need the explanation."
                ),
            ))

    # 4. Files count divergence (number of separate files modified).
    file_counts = {r.provider_id: len(r.files_touched) for r in succeeded}
    if len(file_counts) >= 2:
        max_c = max(file_counts.values())
        min_c = min(file_counts.values())
        if max_c - min_c >= 2:
            max_pid = next(p for p, v in file_counts.items() if v == max_c)
            min_pid = next(p for p, v in file_counts.items() if v == min_c)
            out.append(DivergencePoint(
                dimension="file_count",
                description=(
                    f"{max_pid} touched {max_c} files vs {min_pid}'s {min_c}."
                ),
                likely_reason=(
                    f"{max_pid} is splitting the change across modules "
                    f"(broader refactor); {min_pid} is keeping it local. "
                    f"Prefer the broader split if the project conventions "
                    f"favour separation of concerns."
                ),
            ))

    return out


def _build_summary(
    responses: List[ProviderResponse],
    divergences: List[DivergencePoint],
    agreement: float,
    min_agreement: float,
) -> str:
    """Build the human-readable one-paragraph summary."""
    succeeded = [r for r in responses if r.error is None]
    failed = [r for r in responses if r.error is not None]
    parts: List[str] = []
    parts.append(
        f"Consensus run: {len(succeeded)} succeeded, {len(failed)} failed "
        f"out of {len(responses)} provider(s)."
    )
    if agreement >= 0.8:
        parts.append(f"High agreement ({agreement:.0%}) — providers converged on similar approaches.")
    elif agreement >= min_agreement:
        parts.append(f"Moderate agreement ({agreement:.0%}) — same direction, some divergence in detail.")
    else:
        parts.append(
            f"Low agreement ({agreement:.0%}) — providers took substantially "
            f"different approaches. Review the divergences below before picking one."
        )
    if divergences:
        parts.append(f"{len(divergences)} structured divergence(s) detected.")
    if failed:
        failed_ids = ", ".join(r.provider_id for r in failed)
        parts.append(f"Failed providers: {failed_ids}.")
    return " ".join(parts)


# ── Public entry point ─────────────────────────────────────────────────

_DEFAULT_SYSTEM_PROMPT = (
    "You are a coding agent. Complete the user's task end-to-end. "
    "Use fenced code blocks with the file path in the header (e.g. "
    "```python path/to/file.py) when you produce code, so the result "
    "can be parsed. Be concise but complete."
)


def run_consensus(
    *,
    prompt: str,
    registry: Any,
    active_provider_id: str = "",
    config: Optional[ConsensusConfig] = None,
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
    _provider_for_test: Optional[Any] = None,
) -> ConsensusReport:
    """Run the same prompt on 2–3 providers in parallel and return a
    structured comparison. BLOCKING (but threaded internally).

    Args:
        prompt: the task/prompt to send to every provider.
        registry: a ``ProviderRegistry`` with the requested providers
            registered.
        active_provider_id: the provider the user is currently using.
            Used to pick a default triplet if ``config.providers`` is
            empty, and to make sure the active provider is always one
            of the three.
        config: optional ConsensusConfig override. If None, loaded
            from ``~/.tera_pilot/config.json``.
        system_prompt: optional override for the system prompt sent to
            every provider.
        _provider_for_test: injected provider instance used INSTEAD of
            looking up via registry. Tests use this to avoid real HTTP.

    Returns:
        ``ConsensusReport``. Never raises — provider errors are
        captured per-response so the comparison still returns.
    """
    t0 = time.time()
    cfg = config or get_consensus_config()

    # Determine the provider list.
    if cfg.providers:
        providers = list(cfg.providers)
    else:
        providers = list(resolve_default_providers(active_provider_id))
    # De-dup while preserving order.
    seen: set = set()
    unique_providers: List[str] = []
    for p in providers:
        if p and p not in seen:
            seen.add(p)
            unique_providers.append(p)
    if not unique_providers:
        unique_providers = list(resolve_default_providers(active_provider_id))

    # Fan out in parallel.
    if _provider_for_test is not None:
        # Test path — bypass registry, call _call_one_provider with a
        # fake registry whose .get() returns the injected provider.
        class _FakeRegistry:
            def get(self, pid: str):
                return _provider_for_test
        registry = _FakeRegistry()

    responses: List[ProviderResponse] = []
    # Cap concurrency at len(providers) (small N — usually 2 or 3).
    with ThreadPoolExecutor(max_workers=max(2, len(unique_providers))) as ex:
        futures = {
            ex.submit(
                _call_one_provider,
                pid, prompt, system_prompt, registry,
                cfg.timeout_s, cfg.max_chars_per_response,
            ): pid
            for pid in unique_providers
        }
        for fut in as_completed(futures):
            pid = futures[fut]
            try:
                resp = fut.result()
            except Exception as e:
                resp = ProviderResponse(
                    provider_id=pid, model="",
                    text="", elapsed_ms=0.0,
                    error=f"unexpected: {e}",
                )
            responses.append(resp)
    # Sort responses by the original provider order so the output is
    # deterministic (ThreadPoolExecutor completion order is not).
    order = {pid: i for i, pid in enumerate(unique_providers)}
    responses.sort(key=lambda r: order.get(r.provider_id, 999))

    agreement = _agreement_score(responses)
    divergences = _analyze_divergences(responses)
    summary = _build_summary(responses, divergences, agreement, cfg.min_agreement)

    return ConsensusReport(
        prompt=prompt,
        responses=tuple(responses),
        divergences=tuple(divergences),
        agreement_score=agreement,
        summary=summary,
        elapsed_ms=(time.time() - t0) * 1000,
    )


def render_report_text(report: ConsensusReport) -> str:
    """Render a ConsensusReport as a human-readable multi-line string.

    Used by the TUI/GUI slash command handlers to print the result
    without forcing them to walk the dataclass themselves.
    """
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("CONSENSUS REPORT")
    lines.append("=" * 60)
    lines.append(f"Prompt: {report.prompt[:200]}{'…' if len(report.prompt) > 200 else ''}")
    lines.append(f"Elapsed: {report.elapsed_ms:.0f}ms")
    lines.append(f"Agreement: {report.agreement_score:.0%}")
    lines.append("")
    lines.append(f"## Responses ({len(report.responses)} total)")
    for r in report.responses:
        status = "OK" if r.error is None else f"FAILED: {r.error}"
        lines.append(
            f"  [{r.provider_id}] model={r.model or '?'} "
            f"elapsed={r.elapsed_ms:.0f}ms "
            f"code_blocks={r.code_blocks} code_chars={r.code_chars} "
            f"files={len(r.files_touched)} → {status}"
        )
    lines.append("")
    if report.divergences:
        lines.append(f"## Divergences ({len(report.divergences)})")
        for d in report.divergences:
            lines.append(f"  [{d.dimension}] {d.description}")
            lines.append(f"    why: {d.likely_reason}")
    else:
        lines.append("## Divergences")
        lines.append("  (none detected — providers converged)")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"  {report.summary}")
    lines.append("")
    # Include the actual responses (truncated) so the user can compare.
    lines.append("## Full responses")
    for r in report.responses:
        lines.append(f"\n--- [{r.provider_id}] ({r.model or '?'}) ---")
        if r.error is not None:
            lines.append(f"[ERROR] {r.error}")
        else:
            text = r.text
            if len(text) > 2000:
                text = text[:2000] + f"\n... [{len(r.text)} total chars, truncated]"
            lines.append(text)
    return "\n".join(lines)
