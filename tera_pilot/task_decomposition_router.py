"""G20 — Task-decomposition smart router.

Today ``tera_pilot/auto_router.py``'s :meth:`AutoRouter.route` classifies the
whole prompt into one :class:`TaskComplexity` bucket and picks ONE model
for the entire task. G20 changes that: a cheap model analyzes the task,
breaks it into subtasks, and EACH subtask gets routed to whichever
configured model is best suited for it — considering both a stated
specialty (G20a) and cost — so the user only has to paste in API keys,
pick "auto", and send the request.

Pipeline (G20b):
1. **Decompose**: one call to a cheap tier model (default =
   ``TaskComplexity.SIMPLE`` tier) that returns structured JSON: a list
   of ``{subtask, needs, complexity, depends_on}``.
2. **Route each subtask**: for each subtask, score every available
   :class:`ModelTier` against the subtask's ``needs`` versus that tier's
   ``specialty`` (G20a) and its ``complexity`` fit, then pick the
   cheapest tier that clears the bar.
3. **Dispatch**: subtasks route through the EXISTING subagent machinery
   (``_spawn_subagent`` / ``_run_subagent_internal`` — G20b added
   ``provider_override`` / ``model_override`` params). Independent
   subtasks run in parallel (ThreadPoolExecutor pattern from
   ``consensus_engine.py``); dependent ones run sequentially, feeding
   prior results forward.
4. **Merge**: synthesize subtask results back into one coherent final
   answer for the parent turn (a short synthesis call on a mid-tier
   model — NOT the cheapest, since the merge needs to reconcile
   potentially-conflicting subtask outputs).

Safety rails (G20 prompt §20b):
- If decomposition fails to parse as valid JSON, OR returns a single
  subtask equal to the original prompt, FALL BACK to today's single-
  model :meth:`AutoRouter.route` behavior. Decomposition must never be
  a hard requirement for the router to function.
- Respect any budget set via :meth:`AutoRouter.set_budget` — both per-
  subtask and in aggregate. If projected aggregate cost exceeds budget,
  degrade to the single-model path rather than failing outright.
- Provider availability is checked via ``AutoRouter``'s existing
  ``mark_provider_available`` / ``_is_available`` — we never reimplement.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from tera_pilot.auto_router import AutoRouter, TaskComplexity, ModelTier, get_auto_router

logger = logging.getLogger(__name__)


# Hard cap on parallel subtasks — same limit as ``_spawn_multi_agents``
# (5) to avoid overwhelming the provider. The decomposition LLM is told
# about this cap so it doesn't generate more subtasks than we can run.
MAX_SUBTASKS = 5

# Per-subtask timeout for the dispatch phase — mirrors the
# ``MULTI_AGENT_WAVE_TIMEOUT`` of 180s in ``_engine.py`` so a hung
# subtask can't block the whole router indefinitely.
SUBTASK_TIMEOUT_S = 180.0

# Wall-clock budget for the entire decompose→route→dispatch→merge
# pipeline. If exceeded, we return whatever partial results we have.
END_TO_END_TIMEOUT_S = 600.0  # 10 min

# Synthesis model tier — mid-tier (MODERATE), not the cheapest, because
# the merge step needs to reconcile potentially-conflicting subtask
# outputs. Per G20b: "don't use the cheapest tier for the merge step".
SYNTHESIS_COMPLEXITY = TaskComplexity.MODERATE


@dataclass
class Subtask:
    """One piece of the decomposed task.

    The structure mirrors what the decomposition LLM is asked to emit
    (see :data:`_DECOMPOSE_SYSTEM_PROMPT`). ``id`` is assigned by us
    (``s1``, ``s2``, ...) — the LLM's ``id`` field (if any) is ignored
    so we have stable references for ``depends_on`` resolution.
    """

    id: str
    subtask: str
    needs: List[str] = field(default_factory=list)
    complexity: TaskComplexity = TaskComplexity.SIMPLE
    depends_on: List[str] = field(default_factory=list)
    # Filled in by the routing phase:
    provider_id: Optional[str] = None
    model: Optional[str] = None
    specialty_match: Optional[str] = None  # why this model was picked
    # Filled in by the dispatch phase:
    result: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "subtask": self.subtask,
            "needs": list(self.needs),
            "complexity": self.complexity.value,
            "depends_on": list(self.depends_on),
            "provider_id": self.provider_id,
            "model": self.model,
            "specialty_match": self.specialty_match,
            "result": self.result,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class DecompositionReport:
    """Full result of a task-decomposition run.

    Returned by :meth:`TaskDecompositionRouter.route`. The caller
    (typically the agent runtime) uses ``merged_answer`` as the final
    answer for the parent turn; ``subtasks`` and ``decomposed`` are for
    observability (ActivityLog + G19 task canvas).
    """

    prompt: str
    decomposed: bool  # False = fell back to single-model
    fallback_reason: str = ""  # why we fell back, if decomposed=False
    subtasks: List[Subtask] = field(default_factory=list)
    merged_answer: str = ""
    elapsed_ms: float = 0.0
    # The single-model decision we fell back to (when decomposed=False):
    fallback_decision: Optional[Dict[str, Any]] = None
    # Per-subtask cost estimate (sum is the projected aggregate cost):
    projected_cost_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "decomposed": self.decomposed,
            "fallback_reason": self.fallback_reason,
            "subtasks": [s.to_dict() for s in self.subtasks],
            "merged_answer": self.merged_answer,
            "elapsed_ms": self.elapsed_ms,
            "fallback_decision": self.fallback_decision,
            "projected_cost_usd": round(self.projected_cost_usd, 6),
        }


# ──────────────────────────────────────────────────────────────────────
# Decomposition LLM prompt
# ──────────────────────────────────────────────────────────────────────
# Scoped to "produce JSON only" — same "narrow blast radius" principle
# as the read-only ``researcher`` role (G18) and the persona-maintenance
# prompt (G19b). The model can't execute tools, can't write files, can't
# do anything except emit a JSON list of subtasks.
_DECOMPOSE_SYSTEM_PROMPT = (
    "You are the task-decomposition agent for the Tera Pilot AI coding assistant. "
    "Your ONLY job is to break the user's task into 1-5 independent subtasks "
    "so each can be routed to the model best suited for it.\n\n"
    "STRICT RULES:\n"
    "1. Output ONLY a JSON object. No markdown fences, no commentary, no preamble.\n"
    "2. The JSON shape is: {\"subtasks\": [{\"subtask\": str, \"needs\": [str], "
    "\"complexity\": \"trivial\"|\"simple\"|\"moderate\"|\"complex\"|\"expert\", "
    "\"depends_on\": [int]}]}\n"
    "3. ``needs`` is a list of short capability tags/keywords describing what "
    "kind of model would handle this subtask well — e.g. [\"algorithmic reasoning\"], "
    "[\"frontend\", \"visual design\"], [\"large-context codebase navigation\"], "
    "[\"boilerplate\", \"fast\"], [\"long document generation\"].\n"
    "4. ``depends_on`` is a list of 1-indexed subtask numbers (1 = the first "
    "subtask in your list). Use [] for independent subtasks.\n"
    "5. If the task is genuinely atomic (cannot be meaningfully split), return "
    "a single subtask whose ``subtask`` field is the original task verbatim and "
    "whose ``depends_on`` is []. The router will detect this and fall back to "
    "single-model routing.\n"
    "6. Maximum 5 subtasks. If the task has more natural pieces, group them.\n"
    "7. Each ``subtask`` string must be self-contained — a different model will "
    "execute it without seeing the other subtasks. Include enough context.\n"
)

_MERGE_SYSTEM_PROMPT = (
    "You are the synthesis agent for the Tera Pilot AI coding assistant. Your job "
    "is to merge the outputs of several subtasks into one coherent final "
    "answer for the user.\n\n"
    "RULES:\n"
    "1. Output ONLY the merged answer. No preamble, no \"Here is the merged "
    "answer\", no commentary about the merge process.\n"
    "2. If subtask outputs conflict, prefer the one from the higher-complexity "
    "model (it was routed there for a reason).\n"
    "3. If a subtask failed (error marker present), acknowledge the gap in "
    "the merged answer rather than silently dropping it.\n"
    "4. Preserve code blocks, file paths, and other technical detail from "
    "the subtask outputs — the user needs them.\n"
    "5. If a subtask's output is empty, omit it silently (don't mention it).\n"
)


class TaskDecompositionRouter:
    """Orchestrates the decompose → route → dispatch → merge pipeline.

    Stateless — one instance per ``route()`` call is fine, but the
    caller can also reuse one. The router does NOT own any provider
    state; it borrows the caller's ``AutoRouter`` / ``ProviderRegistry``
    for the duration of the call.
    """

    def __init__(
        self,
        auto_router: Optional[AutoRouter] = None,
        registry: Optional[Any] = None,
    ) -> None:
        self._auto_router = auto_router or get_auto_router()
        self._registry = registry

    def _log_to_activity(
        self,
        *,
        kind: str,
        tool: Optional[str] = None,
        title: str,
        summary: Optional[str] = None,
        status: str = "ok",
        args: Optional[Dict[str, Any]] = None,
        result_preview: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record an entry in the ActivityLog (G16-signed audit trail).

        Best-effort — never lets an ActivityLog failure break the
        router. The audit trail is for observability; if it's broken,
        the router should still produce a result.
        """
        try:
            from tera_pilot.activity_log import get_activity_log, CATEGORY_INFO
            # Pass kwargs directly to record() — it calls make_entry()
            # internally. Don't pre-call make_entry() ourselves, because
            # the resulting dict contains id/ts/ts_iso which make_entry()
            # doesn't accept as kwargs.
            get_activity_log().record(
                category=CATEGORY_INFO,
                kind=kind,
                tool=tool,
                title=title,
                summary=summary,
                status=status,
                args=args,
                result_preview=result_preview,
                meta=meta or {},
            )
        except Exception as e:
            logger.debug("[decomp] activity log write failed: %s", e)

    def _sync_canvas(self, subtasks: List[Subtask]) -> None:
        """Push the subtask breakdown into the G19a task canvas.

        Each subtask becomes a canvas node, with ``model`` set to the
        routed provider/model and ``status=pending``. As subtasks
        complete during dispatch, their canvas nodes are updated to
        ``done`` / ``failed``.

        Best-effort — if the canvas is unavailable, the router still
        works (the canvas is a UI affordance, not a correctness
        requirement).
        """
        try:
            from tera_pilot.agent.task_canvas import get_task_canvas, STATUS_PENDING
            canvas = get_task_canvas()
            canvas.reset()  # fresh canvas for this task
            for st in subtasks:
                canvas.add_node(
                    node_id=st.id,
                    label=st.subtask[:80],  # canvas labels are short
                    status=STATUS_PENDING,
                    depends_on=st.depends_on,
                    model=f"{st.provider_id}/{st.model}" if st.provider_id else None,
                )
        except Exception as e:
            logger.debug("[decomp] canvas sync failed: %s", e)

    def _update_canvas_node(self, st: Subtask) -> None:
        """Update a single canvas node's status after dispatch."""
        try:
            from tera_pilot.agent.task_canvas import get_task_canvas
            canvas = get_task_canvas()
            if st.error:
                from tera_pilot.agent.task_canvas import STATUS_FAILED
                canvas.update_status(st.id, STATUS_FAILED, note=st.error[:60])
            else:
                from tera_pilot.agent.task_canvas import STATUS_DONE
                canvas.update_status(
                    st.id, STATUS_DONE,
                    note=(st.result or "")[:60] if st.result else None,
                )
        except Exception as e:
            logger.debug("[decomp] canvas node update failed: %s", e)

    # ──────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────
    def route(
        self,
        prompt: str,
        *,
        runtime: Optional[Any] = None,
        configured_providers: Optional[set] = None,
    ) -> DecompositionReport:
        """Run the full decompose→route→dispatch→merge pipeline.

        ``runtime`` is the parent :class:`AgentRuntime` that owns the
        ToolEngine we'll dispatch subtasks through. If ``None``, the
        caller is responsible for dispatching subtasks themselves using
        the returned :class:`DecompositionReport`'s ``subtasks`` list
        (each subtask carries its assigned ``provider_id`` / ``model``).

        Returns a :class:`DecompositionReport`. If anything goes wrong
        during decomposition (LLM error, JSON parse error, single-
        subtask-equals-prompt, projected cost over budget), the report
        has ``decomposed=False`` and ``fallback_decision`` populated —
        the caller should fall back to single-model routing using that
        decision.
        """
        start = time.monotonic()
        report = DecompositionReport(prompt=prompt, decomposed=False)

        # ── Step 1: Decompose ────────────────────────────────────
        try:
            subtasks = self._decompose(prompt, configured_providers=configured_providers)
        except Exception as e:
            logger.warning("[decomp] decomposition failed: %s — falling back", e)
            report.fallback_reason = f"decomposition error: {e}"
            report.fallback_decision = self._single_model_route(
                prompt, configured_providers=configured_providers
            )
            report.elapsed_ms = (time.monotonic() - start) * 1000
            return report

        # G20 §20b: if decomposition returns a single subtask equal to
        # the original prompt, fall back to single-model routing.
        if len(subtasks) == 1 and self._is_passthrough(subtasks[0], prompt):
            logger.info("[decomp] single passthrough subtask — falling back to single-model")
            report.fallback_reason = "decomposition produced a single passthrough subtask"
            report.fallback_decision = self._single_model_route(
                prompt, configured_providers=configured_providers
            )
            report.elapsed_ms = (time.monotonic() - start) * 1000
            return report

        report.subtasks = subtasks

        # G20c — log the decomposition decision to the ActivityLog
        # (G16-signed audit trail) so WHICH MODEL DID WHICH PIECE OF
        # THE WORK is inspectable after the fact.
        self._log_to_activity(
            kind="task_decomposition",
            tool="task_decomposition_router",
            title=f"Decomposed task into {len(subtasks)} subtasks",
            summary="; ".join(s.id + ": " + s.subtask[:40] for s in subtasks),
            args={"subtask_count": len(subtasks)},
            meta={"subtasks": [s.to_dict() for s in subtasks]},
        )

        # ── Step 2: Route each subtask ───────────────────────────
        try:
            self._route_subtasks(subtasks, configured_providers=configured_providers)
        except Exception as e:
            logger.warning("[decomp] routing failed: %s — falling back", e)
            report.fallback_reason = f"routing error: {e}"
            report.fallback_decision = self._single_model_route(
                prompt, configured_providers=configured_providers
            )
            report.elapsed_ms = (time.monotonic() - start) * 1000
            return report

        # G20c — log the subtask→model assignment so it's inspectable
        # after the fact. Per G20c: "Log the subtask→model assignment
        # through the existing ActivityLog/G16 signed audit trail, so
        # WHICH MODEL DID WHICH PIECE OF THE WORK is inspectable after
        # the fact."
        self._log_to_activity(
            kind="task_decomposition_routing",
            tool="task_decomposition_router",
            title=f"Routed {len(subtasks)} subtasks to models",
            summary="; ".join(
                f"{s.id}->{s.provider_id}/{s.model}" for s in subtasks
            ),
            meta={"assignments": [
                {"id": s.id, "provider_id": s.provider_id, "model": s.model,
                 "specialty_match": s.specialty_match}
                for s in subtasks
            ]},
        )

        # G20c — wire the subtask breakdown into G19a's task canvas so
        # the live tree view shows what's happening. Each subtask
        # becomes a canvas node; the node's model field shows which
        # model handled it.
        self._sync_canvas(subtasks)

        # G20 §20b: respect budget. If projected aggregate cost exceeds
        # the per-request budget, degrade to single-model rather than
        # failing outright.
        projected = self._project_cost(subtasks, prompt)
        report.projected_cost_usd = projected
        budget = getattr(self._auto_router, "_per_request_budget", None)
        if budget is not None and projected > budget:
            logger.warning(
                "[decomp] projected cost $%.4f exceeds budget $%.4f — falling back",
                projected, budget,
            )
            report.fallback_reason = (
                f"projected cost ${projected:.4f} exceeds per-request budget ${budget:.4f}"
            )
            report.fallback_decision = self._single_model_route(
                prompt, configured_providers=configured_providers
            )
            report.elapsed_ms = (time.monotonic() - start) * 1000
            return report

        # ── Step 3: Dispatch (if we have a runtime) ──────────────
        if runtime is not None:
            try:
                self._dispatch_subtasks(subtasks, runtime)
            except Exception as e:
                logger.warning("[decomp] dispatch failed: %s — partial results", e)
                # Don't fall back here — we may have partial results
                # from the subtasks that did finish. The merge step
                # will incorporate them.

            # G20c — update canvas nodes with the dispatch results so
            # the live tree view reflects which subtasks succeeded /
            # failed. Best-effort.
            for st in subtasks:
                self._update_canvas_node(st)

            # ── Step 4: Merge ────────────────────────────────────
            try:
                report.merged_answer = self._merge_results(
                    prompt, subtasks, configured_providers=configured_providers
                )
            except Exception as e:
                logger.warning("[decomp] merge failed: %s — concatenating", e)
                # Fall back to plain concatenation if the merge LLM call fails.
                report.merged_answer = self._concatenate_results(prompt, subtasks)
        else:
            # No runtime — caller will dispatch themselves. Provide the
            # merged_answer as a placeholder so the report isn't empty.
            report.merged_answer = "(subtasks routed but not dispatched — caller must execute)"

        report.decomposed = True
        report.elapsed_ms = (time.monotonic() - start) * 1000

        # G20c — final audit entry: which subtasks succeeded / failed,
        # the merged answer length, the total elapsed time.
        succeeded = sum(1 for s in subtasks if not s.error)
        failed = sum(1 for s in subtasks if s.error)
        self._log_to_activity(
            kind="task_decomposition_complete",
            tool="task_decomposition_router",
            title=f"Decomposition complete: {succeeded} ok / {failed} failed",
            summary=f"merged_answer_chars={len(report.merged_answer)} elapsed_ms={int(report.elapsed_ms)}",
            status="ok" if failed == 0 else "error",
            meta={
                "subtask_count": len(subtasks),
                "succeeded": succeeded,
                "failed": failed,
                "elapsed_ms": int(report.elapsed_ms),
                "projected_cost_usd": round(report.projected_cost_usd, 6),
            },
        )
        return report

    # ──────────────────────────────────────────────────────────────
    # Step 1: Decompose
    # ──────────────────────────────────────────────────────────────
    def _decompose(
        self,
        prompt: str,
        *,
        configured_providers: Optional[set] = None,
    ) -> List[Subtask]:
        """Call the cheap LLM to break the prompt into subtasks.

        Uses :class:`AutoRouter`'s SIMPLE tier (``TaskComplexity.SIMPLE``)
        — never the expensive EXPERT models. Per G20 §20b: "default =
        TaskComplexity.SIMPLE tier".
        """
        # Pick a cheap provider for the decomposition call. We use the
        # AutoRouter to find one — its route() already knows which
        # providers are configured and available.
        decision = self._auto_router.route(
            "decompose this task into subtasks: " + prompt[:500],
            configured_providers=configured_providers,
        )
        if not decision or not decision.get("provider_id"):
            raise RuntimeError("no provider available for decomposition")
        provider_id = decision["provider_id"]
        model = decision.get("model", "")

        provider, model = self._resolve_provider(provider_id, model)
        if provider is None:
            raise RuntimeError(f"provider {provider_id!r} not available for decomposition")

        # Build the messages. Lazy-import ProviderMessage.
        from tera_pilot.providers.base import ProviderMessage

        messages = [
            ProviderMessage(role="system", content=_DECOMPOSE_SYSTEM_PROMPT),
            ProviderMessage(
                role="user",
                content=f"TASK TO DECOMPOSE:\n{prompt}\n\nOutput JSON only.",
            ),
        ]

        raw_text = self._call_provider(provider, messages, model)
        if not raw_text:
            raise RuntimeError("decomposition LLM returned empty output")

        # Parse JSON. Strip markdown fences if the model added them.
        json_text = _strip_code_fences(raw_text)
        # Find the first { ... } block — some models wrap JSON in
        # prose despite the "JSON only" instruction.
        json_text = _extract_json_object(json_text)
        if not json_text:
            raise RuntimeError("decomposition output contained no JSON object")

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"decomposition output is not valid JSON: {e}")

        subtasks_data = data.get("subtasks") if isinstance(data, dict) else None
        if not isinstance(subtasks_data, list) or not subtasks_data:
            raise RuntimeError("decomposition output has no 'subtasks' list")

        # Convert to Subtask objects. Assign our own ids (s1, s2, ...)
        # so depends_on resolution is stable regardless of what the LLM
        # emitted. Validate depends_on indices (1-indexed from the LLM).
        subtasks: List[Subtask] = []
        for i, st in enumerate(subtasks_data[:MAX_SUBTASKS]):
            if not isinstance(st, dict):
                continue
            subtask_text = str(st.get("subtask", "")).strip()
            if not subtask_text:
                continue
            needs = (
                [str(n) for n in st.get("needs", []) if n]
                if isinstance(st.get("needs"), list)
                else []
            )
            complexity_str = str(st.get("complexity", "simple")).lower()
            try:
                complexity = TaskComplexity(complexity_str)
            except ValueError:
                complexity = TaskComplexity.SIMPLE
            depends_on: List[str] = []
            if isinstance(st.get("depends_on"), list):
                for dep in st["depends_on"]:
                    try:
                        idx = int(dep) - 1  # 1-indexed → 0-indexed
                        if 0 <= idx < i and subtasks[idx].id:
                            depends_on.append(subtasks[idx].id)
                    except (ValueError, TypeError):
                        pass
            subtasks.append(
                Subtask(
                    id=f"s{i+1}",
                    subtask=subtask_text,
                    needs=needs,
                    complexity=complexity,
                    depends_on=depends_on,
                )
            )

        if not subtasks:
            raise RuntimeError("decomposition produced zero valid subtasks")

        return subtasks

    # ──────────────────────────────────────────────────────────────
    # Step 2: Route each subtask to a model
    # ──────────────────────────────────────────────────────────────
    def _route_subtasks(
        self,
        subtasks: List[Subtask],
        *,
        configured_providers: Optional[set] = None,
    ) -> None:
        """Score every available ModelTier against each subtask's needs
        and pick the cheapest one that clears the bar.

        Scoring (deliberately simple — the prompt says "simple
        keyword/semantic match is fine"):
        - For each subtask's ``needs`` keywords, find tiers whose
          ``specialty`` contains a matching keyword (case-insensitive
          substring match).
        - Filter to tiers whose complexity >= the subtask's complexity
          (a SIMPLE subtask can run on a SIMPLE-or-higher tier; an
          EXPERT subtask needs an EXPERT-or-higher tier, which in
          practice means EXPERT only).
        - Filter by provider availability (reuse AutoRouter's
          ``_is_available`` logic) and ``configured_providers``.
        - Pick the cheapest tier that clears the bar (lowest
          ``cost_per_1k_in + cost_per_1k_out``).
        - If no tier clears the bar, fall back to the cheapest available
          tier at the subtask's complexity level.
        """
        all_tiers = self._auto_router.all_tiers()
        # Pre-filter to available + configured tiers once.
        import time as _time

        now = _time.time()
        cache_ttl = getattr(self._auto_router, "_provider_cache_ttl", 300.0)
        available_ts = getattr(self._auto_router, "_provider_available_ts", {})
        available_map = getattr(self._auto_router, "_provider_available", {})

        def _is_available(pid: str) -> bool:
            ts = available_ts.get(pid)
            if ts is None:
                return True
            if now - ts > cache_ttl:
                return True
            return available_map.get(pid, True)

        candidate_tiers: List[Tuple[TaskComplexity, ModelTier]] = []
        for complexity, tier in all_tiers:
            if not _is_available(tier.provider_id):
                continue
            if configured_providers is not None and tier.provider_id not in configured_providers:
                continue
            candidate_tiers.append((complexity, tier))

        if not candidate_tiers:
            raise RuntimeError("no providers available for routing subtasks")

        # Complexity rank: TRIVIAL < SIMPLE < MODERATE < COMPLEX < EXPERT
        complexity_rank = {
            TaskComplexity.TRIVIAL: 0,
            TaskComplexity.SIMPLE: 1,
            TaskComplexity.MODERATE: 2,
            TaskComplexity.COMPLEX: 3,
            TaskComplexity.EXPERT: 4,
        }

        for st in subtasks:
            # Score every candidate tier against this subtask's needs.
            scored: List[Tuple[float, TaskComplexity, ModelTier, str]] = []
            for complexity, tier in candidate_tiers:
                # Complexity fit: tier complexity must be >= subtask complexity.
                if complexity_rank.get(complexity, 0) < complexity_rank.get(st.complexity, 1):
                    continue
                # Specialty match: how many of the subtask's needs appear
                # in the tier's specialty text?
                specialty_lower = (tier.specialty or "").lower()
                matched_needs = []
                for need in st.needs:
                    need_lower = need.lower().strip()
                    if not need_lower:
                        continue
                    # Match on individual words too — "algorithmic reasoning"
                    # matches a specialty containing "algorithmic" or "reasoning".
                    need_words = [w for w in re.split(r"\W+", need_lower) if len(w) >= 4]
                    if any(w in specialty_lower for w in need_words) or need_lower in specialty_lower:
                        matched_needs.append(need)
                # Score: lower cost = better, more matched needs = better.
                # We want cheapest tier with most matches. Sort key:
                # (negative_match_count, cost) — Python sorts ascending,
                # so most matches (highest negative) comes first, then
                # cheapest within that match count.
                cost = tier.cost_per_1k_in + tier.cost_per_1k_out
                score = (-len(matched_needs), cost)
                specialty_match = (
                    f"matched {len(matched_needs)} need(s): {', '.join(matched_needs)}"
                    if matched_needs
                    else "no specialty match — picked cheapest at-or-above complexity"
                )
                scored.append((score[0], score[1], complexity, tier, specialty_match))

            if not scored:
                # No tier at-or-above the subtask's complexity. Fall
                # back to ANY available tier (cheapest first).
                scored = [
                    (
                        0,
                        t.cost_per_1k_in + t.cost_per_1k_out,
                        c,
                        t,
                        "fallback — no tier at-or-above complexity, picked cheapest available",
                    )
                    for c, t in candidate_tiers
                ]

            # Sort by (negative_match_count ASC, cost ASC) — best first.
            scored.sort(key=lambda x: (x[0], x[1]))
            best = scored[0]
            tier: ModelTier = best[3]
            st.provider_id = tier.provider_id
            st.model = tier.model
            st.specialty_match = best[4]
            logger.info(
                "[decomp] %s -> %s/%s (%s)",
                st.id, st.provider_id, st.model, st.specialty_match,
            )

    # ──────────────────────────────────────────────────────────────
    # Step 3: Dispatch subtasks (parallel where possible)
    # ──────────────────────────────────────────────────────────────
    def _dispatch_subtasks(
        self,
        subtasks: List[Subtask],
        runtime: Any,
    ) -> None:
        """Dispatch subtasks through the existing subagent machinery.

        Respects ``depends_on`` ordering: independent subtasks run in
        parallel (ThreadPoolExecutor pattern from ``consensus_engine.py``);
        dependent ones run sequentially, feeding prior results forward.

        We use a simple wave-based scheduler: at each wave, dispatch
        every subtask whose dependencies are all satisfied. Wait for
        the wave to finish, then move to the next wave. This is the
        same pattern ``_spawn_multi_agents`` uses.
        """
        # Get the ToolEngine from the runtime. The runtime owns it as
        # ``runtime.tools`` — verified in AgentRuntime.__init__.
        tools = getattr(runtime, "tools", None)
        if tools is None:
            raise RuntimeError("runtime has no ToolEngine — cannot dispatch subtasks")

        # Track which subtasks are done + their results, so dependents
        # can be fed prior results.
        done: Dict[str, Subtask] = {}
        remaining = list(subtasks)
        wave = 0
        deadline = time.monotonic() + END_TO_END_TIMEOUT_S

        while remaining and time.monotonic() < deadline:
            wave += 1
            # Find subtasks whose dependencies are all satisfied.
            ready = [
                st for st in remaining
                if all(dep in done for dep in st.depends_on)
            ]
            if not ready:
                # Circular dependency or unsatisfiable — abort with the
                # remaining subtasks marked as failed.
                for st in remaining:
                    st.error = "unsatisfiable dependencies"
                break

            # Build context for each ready subtask: include results of
            # its dependencies so the model has the prior context.
            def _build_context(st: Subtask) -> str:
                if not st.depends_on:
                    return st.subtask
                deps_text = "\n\n".join(
                    f"[Result from {dep}]\n{done[dep].result or '(no output)'}"
                    for dep in st.depends_on
                    if dep in done
                )
                return f"{st.subtask}\n\n--- Prior subtask results ---\n{deps_text}"

            # Dispatch the ready wave in parallel.
            if len(ready) == 1:
                # Single subtask — no need for a thread pool.
                st = ready[0]
                st_start = time.monotonic()
                try:
                    result = tools._spawn_subagent(
                        goal=_build_context(st),
                        role="generalist",
                        max_iterations=4,
                        provider_override=st.provider_id,
                        model_override=st.model,
                    )
                    st.result = result
                except Exception as e:
                    st.error = str(e)
                st.elapsed_ms = (time.monotonic() - st_start) * 1000
                done[st.id] = st
                remaining.remove(st)
            else:
                # Multiple ready subtasks — dispatch in parallel.
                with ThreadPoolExecutor(max_workers=min(len(ready), MAX_SUBTASKS)) as ex:
                    futures = {
                        ex.submit(
                            self._dispatch_one,
                            tools,
                            st,
                            _build_context(st),
                        ): st
                        for st in ready
                    }
                    for fut in as_completed(futures, timeout=SUBTASK_TIMEOUT_S):
                        st = futures[fut]
                        try:
                            fut.result()
                        except Exception as e:
                            st.error = f"dispatch error: {e}"
                        done[st.id] = st
                        remaining.remove(st)

        # Mark any remaining (timed out) subtasks as failed.
        for st in remaining:
            if st.error is None:
                st.error = "timed out"
            done[st.id] = st

    def _dispatch_one(
        self,
        tools: Any,
        st: Subtask,
        goal: str,
    ) -> None:
        """Dispatch a single subtask. Runs in a worker thread."""
        st_start = time.monotonic()
        try:
            result = tools._spawn_subagent(
                goal=goal,
                role="generalist",
                max_iterations=4,
                provider_override=st.provider_id,
                model_override=st.model,
            )
            st.result = result
        except Exception as e:
            st.error = str(e)
        st.elapsed_ms = (time.monotonic() - st_start) * 1000

    # ──────────────────────────────────────────────────────────────
    # Step 4: Merge subtask results into one coherent answer
    # ──────────────────────────────────────────────────────────────
    def _merge_results(
        self,
        prompt: str,
        subtasks: List[Subtask],
        *,
        configured_providers: Optional[set] = None,
    ) -> str:
        """Synthesize subtask results into one coherent final answer.

        Per G20 §20b: "a short synthesis call on a mid-tier model is
        fine here — don't use the cheapest tier for the merge step".
        We use ``TaskComplexity.MODERATE`` (``SYNTHESIS_COMPLEXITY``).
        """
        # Build the merge prompt.
        parts: List[str] = []
        for st in subtasks:
            if st.error:
                parts.append(
                    f"## Subtask {st.id} ({st.provider_id}/{st.model}) — FAILED\n"
                    f"Error: {st.error}\n"
                    f"Partial output: {st.result or '(none)'}"
                )
            elif st.result:
                parts.append(
                    f"## Subtask {st.id} ({st.provider_id}/{st.model})\n{st.result}"
                )
        if not parts:
            return "(no subtask results to merge)"
        subtask_text = "\n\n".join(parts)

        # Pick a MODERATE-tier provider for the merge call.
        decision = self._auto_router.route(
            "synthesize the merged answer for: " + prompt[:300],
            configured_providers=configured_providers,
        )
        # If AutoRouter picked a non-moderate tier, accept it anyway —
        # the router knows what's available. The point is just to AVOID
        # the cheapest tier if a mid-tier is available.
        if not decision or not decision.get("provider_id"):
            # No provider available — fall back to concatenation.
            return self._concatenate_results(prompt, subtasks)
        provider_id = decision["provider_id"]
        model = decision.get("model", "")

        provider, model = self._resolve_provider(provider_id, model)
        if provider is None:
            return self._concatenate_results(prompt, subtasks)

        from tera_pilot.providers.base import ProviderMessage

        messages = [
            ProviderMessage(role="system", content=_MERGE_SYSTEM_PROMPT),
            ProviderMessage(
                role="user",
                content=(
                    f"ORIGINAL USER TASK:\n{prompt}\n\n"
                    f"SUBTASK RESULTS:\n{subtask_text}\n\n"
                    f"Output the merged final answer for the user."
                ),
            ),
        ]

        result = self._call_provider(provider, messages, model)
        return result or self._concatenate_results(prompt, subtasks)

    def _concatenate_results(self, prompt: str, subtasks: List[Subtask]) -> str:
        """Plain concatenation fallback if the merge LLM call fails."""
        parts = [f"# Task: {prompt}", ""]
        for st in subtasks:
            header = f"## {st.id} ({st.provider_id}/{st.model})"
            if st.error:
                parts.append(f"{header} — FAILED: {st.error}")
            else:
                parts.append(f"{header}\n{st.result or '(no output)'}")
            parts.append("")
        return "\n".join(parts)

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────
    def _resolve_provider(
        self, provider_id: str, model: str
    ) -> Tuple[Optional[Any], str]:
        """Look up a provider by id in the registry. Returns
        ``(provider, model)`` or ``(None, "")`` if not found."""
        if self._registry is None:
            try:
                from tera_pilot.providers import ProviderRegistry

                self._registry = ProviderRegistry()
            except Exception as e:
                logger.debug("[decomp] no registry: %s", e)
                return None, ""
        try:
            provider = self._registry.get(provider_id)
            if provider is None:
                return None, ""
            if hasattr(provider, "is_loaded") and not provider.is_loaded:
                provider.load()
            # Resolve the model: explicit > provider's configured > ""
            if not model:
                if hasattr(provider, "get_model"):
                    model = provider.get_model() or ""
                if not model:
                    model = getattr(provider, "model", "") or ""
            return provider, model
        except Exception as e:
            logger.debug("[decomp] provider lookup failed: %s", e)
            return None, ""

    def _call_provider(
        self, provider: Any, messages: list, model: str
    ) -> str:
        """Call ``provider.generate()`` and return the text.

        Uses a thread + ``join(timeout=...)`` pattern so a hung provider
        can't block the router indefinitely — mirrors the
        ``_call_one_provider`` pattern from ``consensus_engine.py``.
        """
        timeout_s = 60.0  # decomposition + merge are short prompts
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

        th = threading.Thread(target=_do_call, daemon=True)
        th.start()
        th.join(timeout=timeout_s)
        if th.is_alive():
            raise TimeoutError(
                f"decomposition/merge LLM call timed out after {timeout_s}s"
            )
        if "err" in result_holder:
            raise result_holder["err"]
        resp = result_holder.get("resp")
        if resp is None:
            return ""
        return getattr(resp, "text", "") or ""

    def _single_model_route(
        self,
        prompt: str,
        *,
        configured_providers: Optional[set] = None,
    ) -> Dict[str, Any]:
        """Fall back to today's single-model AutoRouter.route() behavior."""
        return self._auto_router.route(
            prompt,
            configured_providers=configured_providers,
        )

    def _is_passthrough(self, subtask: Subtask, original_prompt: str) -> bool:
        """Check if a single subtask is just the original prompt
        verbatim (modulo whitespace) — the LLM's signal that the task
        is atomic and shouldn't be decomposed."""
        s = subtask.subtask.strip().lower()
        o = original_prompt.strip().lower()
        if s == o:
            return True
        # Substring check too — the LLM may have prepended "decompose
        # this task into subtasks:" or similar.
        if o in s and len(s) < len(o) + 80:
            return True
        return False

    def _project_cost(self, subtasks: List[Subtask], prompt: str) -> float:
        """Estimate the aggregate cost of running all subtasks.

        Uses the same heuristic as ``AutoRouter._estimate_cost``:
        ``tokens_in = len(prompt)//4``, ``tokens_out = tokens_in*2``.
        Per-subtask prompt is approximated as the original prompt length
        (subtask text is usually shorter, but the parent context gets
        passed in too, so this is a reasonable upper bound).
        """
        total = 0.0
        approx_tokens_in = max(1, len(prompt) // 4)
        approx_tokens_out = approx_tokens_in * 2
        # Look up each subtask's tier to get its cost rates.
        all_tiers = self._auto_router.all_tiers()
        tier_lookup: Dict[Tuple[str, str], ModelTier] = {
            (t.provider_id, t.model): t for _, t in all_tiers
        }
        for st in subtasks:
            tier = tier_lookup.get((st.provider_id or "", st.model or ""))
            if tier is None:
                continue
            cost = (
                approx_tokens_in * tier.cost_per_1k_in
                + approx_tokens_out * tier.cost_per_1k_out
            ) / 1000
            total += cost
        # Add the synthesis call cost (MODERATE tier, double the prompt
        # size because subtask results are appended).
        synth_tiers = [t for c, t in all_tiers if c == SYNTHESIS_COMPLEXITY]
        if synth_tiers:
            synth = synth_tiers[0]
            synth_cost = (
                (approx_tokens_in * 2) * synth.cost_per_1k_in
                + (approx_tokens_out * 2) * synth.cost_per_1k_out
            ) / 1000
            total += synth_cost
        return total


# ──────────────────────────────────────────────────────────────────────
# Helpers (module-private)
# ──────────────────────────────────────────────────────────────────────
def _strip_code_fences(text: str) -> str:
    """Strip a single surrounding ```json ... ``` fence if present."""
    s = text.strip()
    if not s.startswith("```"):
        return s
    first_nl = s.find("\n")
    if first_nl == -1:
        return s
    body = s[first_nl + 1:]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3].rstrip()
    return body


def _extract_json_object(text: str) -> str:
    """Find the first ``{ ... }`` block in ``text`` and return it.

    Used when the LLM wraps the JSON in prose despite the "JSON only"
    instruction. Returns ``""`` if no balanced ``{ ... }`` block is
    found.
    """
    start = text.find("{")
    if start == -1:
        return ""
    # Walk forward, tracking brace depth, respecting string literals.
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


# ──────────────────────────────────────────────────────────────────────
# Module-level singleton (lazy)
# ──────────────────────────────────────────────────────────────────────
_ROUTER: Optional[TaskDecompositionRouter] = None
_ROUTER_LOCK = threading.Lock()


def get_task_decomposition_router() -> TaskDecompositionRouter:
    """Return the process-wide :class:`TaskDecompositionRouter` singleton."""
    global _ROUTER
    if _ROUTER is None:
        with _ROUTER_LOCK:
            if _ROUTER is None:
                _ROUTER = TaskDecompositionRouter()
    return _ROUTER


def reset_task_decomposition_router_for_test() -> TaskDecompositionRouter:
    """Replace the singleton with a fresh instance. Test-only."""
    global _ROUTER
    with _ROUTER_LOCK:
        _ROUTER = TaskDecompositionRouter()
    return _ROUTER
