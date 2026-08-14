"""Collaboration modes — Issue #7.

The existing :class:`tera_pilot.swarm_manager.SwarmManager` treats every
spawned agent as a peer that races to complete its own goal. Real
multi-agent workflows are richer than that: they need *coordination
patterns* (reviewer + implementer, pair-programmer + observer, ...).

This module ships four collaboration modes that compose on top of
``SwarmManager``:

1. **Reviewer** — one agent implements, another agent reviews its
   output and either APPROVE / REJECT / MODIFY. The implementer is
   blocked until the reviewer returns a verdict.
2. **Codegen** — a planner agent decomposes the task, then N parallel
   implementer agents each pick a sub-task. Results are aggregated.
3. **Pair** — two agents alternate turns on the same task. Agent A
   takes the first turn, Agent B reviews A's output and continues
   from there, A reviews B's output and so on.
4. **Observer** — one agent does the work, one or more observer
   agents watch the activity log and may emit warnings/suggestions
   (read-only) without blocking the worker.

Modes are pluggable: each mode is a class implementing the
``CollaborationMode`` protocol. The :class:`CollaborationOrchestrator`
runs the chosen mode against a :class:`SwarmManager`.

The modes are *orchestration strategies*, not new runtime primitives.
They spawn / observe agents via the existing SwarmManager API and
serialize their final result as a single string so callers don't
have to learn a new return type.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


# ── Enums / dataclasses ───────────────────────────────────────────────────


class CollaborationMode(str, Enum):
    REVIEWER = "reviewer"
    CODEGEN = "codegen"
    PAIR = "pair"
    OBSERVER = "observer"


@dataclass
class CollaborationResult:
    """The outcome of running a collaboration mode.

    - ``output``: the final user-facing answer (string).
    - ``artifacts``: per-agent outputs (for debugging / inspection).
    - ``mode``: which mode produced this result.
    - ``metadata``: free-form dict (e.g. reviewer verdict, observer warnings).
    """

    mode: CollaborationMode
    output: str
    artifacts: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Mode protocol ─────────────────────────────────────────────────────────


class _ModeImpl(Protocol):
    """Internal protocol implemented by each mode."""

    def run(
        self,
        orchestrator: "CollaborationOrchestrator",
        task: str,
    ) -> CollaborationResult: ...


# ── Orchestrator ──────────────────────────────────────────────────────────


class CollaborationOrchestrator:
    """Drives a collaboration mode against a :class:`SwarmManager`.

    The orchestrator owns:

    - a reference to the ``SwarmManager`` (for spawning agents),
    - a callable that runs a single agent to completion
      (``run_agent_fn``) — this decouples the orchestrator from the
      concrete runtime, so unit tests can pass a mock.
    """

    def __init__(
        self,
        swarm_manager,
        run_agent_fn: Callable[[Any, str], str],
    ):
        """
        Args:
            swarm_manager: a :class:`tera_pilot.swarm_manager.SwarmManager`
                instance. ``spawn()`` returns a :class:`SwarmAgent`.
            run_agent_fn: callable that takes (agent, task) and
                returns the agent's output as a string. The orchestrator
                does NOT call the runtime directly — this indirection
                makes the modes unit-testable.
        """
        self._swarm = swarm_manager
        self._run_agent = run_agent_fn

    @property
    def swarm(self):
        return self._swarm

    def run_agent(self, agent, task: str) -> str:
        """Run a single agent to completion. Wraps the injected callable."""
        return self._run_agent(agent, task)

    def run(
        self,
        mode: CollaborationMode,
        task: str,
    ) -> CollaborationResult:
        """Run ``mode`` against ``task`` and return the merged result."""
        impl = _MODE_IMPLS.get(mode)
        if impl is None:
            raise ValueError(f"unknown collaboration mode: {mode!r}")
        return impl.run(self, task)


# ── Mode: reviewer ────────────────────────────────────────────────────────


@dataclass
class ReviewerMode:
    """One implementer + one reviewer.

    The implementer produces a draft. The reviewer returns a verdict
    (APPROVE / REJECT / MODIFY). On MODIFY, the implementer gets the
    reviewer's feedback and produces a new draft (up to
    ``max_iterations`` rounds). On REJECT, the result is empty and
    ``metadata['rejected'] = True``.
    """

    max_iterations: int = 3

    def run(self, orch: CollaborationOrchestrator, task: str) -> CollaborationResult:
        implementer = orch.swarm.spawn(
            name="implementer", goal=task, role="implementer"
        )
        reviewer = orch.swarm.spawn(
            name="reviewer", goal="review the implementer's output",
            role="reviewer",
        )

        artifacts: Dict[str, str] = {}
        current_draft = ""
        verdict = "PENDING"

        for it in range(self.max_iterations):
            current_draft = orch.run_agent(
                implementer,
                task if it == 0 else f"{task}\n\nReviewer feedback: {current_draft}\n\nPlease revise.",
            )
            artifacts[f"implementer_round_{it}"] = current_draft

            review_prompt = (
                f"You are the reviewer. Review the following output:\n\n"
                f"{current_draft}\n\n"
                f"Return JSON: {{\"verdict\": \"APPROVE\"|\"REJECT\"|\"MODIFY\", "
                f"\"feedback\": \"...\"}}"
            )
            review_raw = orch.run_agent(reviewer, review_prompt)
            artifacts[f"reviewer_round_{it}"] = review_raw

            verdict, feedback = _parse_review_verdict(review_raw)
            if verdict == "APPROVE":
                return CollaborationResult(
                    mode=CollaborationMode.REVIEWER,
                    output=current_draft,
                    artifacts=artifacts,
                    metadata={"verdict": "APPROVE", "iterations": it + 1},
                )
            if verdict == "REJECT":
                return CollaborationResult(
                    mode=CollaborationMode.REVIEWER,
                    output="",
                    artifacts=artifacts,
                    metadata={
                        "verdict": "REJECT",
                        "iterations": it + 1,
                        "rejected": True,
                        "feedback": feedback,
                    },
                )
            # MODIFY: feed back into next implementer round.
            current_draft = feedback

        # Exhausted retries — return last draft with verdict=EXHAUSTED.
        return CollaborationResult(
            mode=CollaborationMode.REVIEWER,
            output=current_draft,
            artifacts=artifacts,
            metadata={"verdict": "EXHAUSTED", "iterations": self.max_iterations},
        )


# ── Mode: codegen ─────────────────────────────────────────────────────────


@dataclass
class CodegenMode:
    """Planner + N parallel implementers.

    The planner decomposes the task into ``planner_count`` sub-tasks
    (returns a list of strings). Each implementer picks one sub-task
    and runs to completion. The orchestrator concatenates the results
    in order.

    For simplicity this implementation runs the implementers
    sequentially (real parallelism requires a runtime with thread
    pools — the orchestrator is intentionally runtime-agnostic).
    """

    planner_count: int = 3

    def run(self, orch: CollaborationOrchestrator, task: str) -> CollaborationResult:
        planner = orch.swarm.spawn(
            name="planner", goal="decompose task into sub-tasks",
            role="planner",
        )
        plan_raw = orch.run_agent(
            planner,
            f"Decompose the following task into at most {self.planner_count} "
            f"sub-tasks. Return one sub-task per line, no numbering.\n\n"
            f"Task: {task}",
        )
        sub_tasks = [ln.strip() for ln in plan_raw.splitlines() if ln.strip()]
        if not sub_tasks:
            sub_tasks = [task]  # fall back to single-task

        artifacts: Dict[str, str] = {"plan": plan_raw}
        outputs: List[str] = []
        for i, sub in enumerate(sub_tasks):
            impl = orch.swarm.spawn(
                name=f"impl-{i}", goal=sub, role="implementer"
            )
            out = orch.run_agent(impl, sub)
            artifacts[f"impl_{i}"] = out
            outputs.append(out)

        merged = "\n\n---\n\n".join(outputs)
        return CollaborationResult(
            mode=CollaborationMode.CODEGEN,
            output=merged,
            artifacts=artifacts,
            metadata={
                "sub_task_count": len(sub_tasks),
                "sub_tasks": sub_tasks,
            },
        )


# ── Mode: pair ────────────────────────────────────────────────────────────


@dataclass
class PairMode:
    """Two agents alternate turns on the same task.

    Agent A takes the first turn. Agent B reviews A's output and
    continues from there. A reviews B's output and continues. And so
    on, up to ``rounds`` total turns.
    """

    rounds: int = 4

    def run(self, orch: CollaborationOrchestrator, task: str) -> CollaborationResult:
        agents = [
            orch.swarm.spawn(name="pair-A", goal=task, role="pair-programmer"),
            orch.swarm.spawn(name="pair-B", goal=task, role="pair-programmer"),
        ]
        artifacts: Dict[str, str] = {}
        current = ""
        for r in range(self.rounds):
            agent = agents[r % 2]
            prompt = task if r == 0 else (
                f"Previous turn output:\n{current}\n\n"
                f"Continue the work, building on / correcting the above."
            )
            current = orch.run_agent(agent, prompt)
            artifacts[f"round_{r}_agent_{agent.name}"] = current
        return CollaborationResult(
            mode=CollaborationMode.PAIR,
            output=current,
            artifacts=artifacts,
            metadata={"rounds": self.rounds},
        )


# ── Mode: observer ────────────────────────────────────────────────────────


@dataclass
class ObserverMode:
    """One worker + N observers.

    The worker does the task. Observers (read-only) watch the worker's
    output and may emit warnings. The warnings are collected into
    ``metadata['warnings']`` but do NOT block the worker.
    """

    observer_count: int = 1

    def run(self, orch: CollaborationOrchestrator, task: str) -> CollaborationResult:
        worker = orch.swarm.spawn(
            name="worker", goal=task, role="generalist"
        )
        # Worker produces output first.
        output = orch.run_agent(worker, task)
        artifacts: Dict[str, str] = {"worker": output}

        warnings: List[str] = []
        for i in range(self.observer_count):
            obs = orch.swarm.spawn(
                name=f"observer-{i}",
                goal="observe the worker's output and warn about issues",
                role="observer",
            )
            obs_prompt = (
                "You are a read-only observer. Review the following output "
                "and emit ONE warning if you spot a problem. If everything "
                "looks fine, reply with the literal text 'OK'.\n\n"
                f"Output:\n{output}"
            )
            obs_out = orch.run_agent(obs, obs_prompt)
            artifacts[f"observer_{i}"] = obs_out
            if obs_out.strip().upper() != "OK":
                warnings.append(obs_out.strip())

        return CollaborationResult(
            mode=CollaborationMode.OBSERVER,
            output=output,
            artifacts=artifacts,
            metadata={
                "observer_count": self.observer_count,
                "warnings": warnings,
            },
        )


# ── Helpers ───────────────────────────────────────────────────────────────


def _parse_review_verdict(raw: str) -> tuple[str, str]:
    """Parse a reviewer's verdict from raw text.

    Looks for ``"verdict": "..."`` and ``"feedback": "..."`` (JSON-ish
    or just a quoted string). Defaults to ``(MODIFY, raw)`` so a
    malformed reviewer response still nudges the implementer.
    """
    import json
    import re

    # Try JSON first.
    try:
        # Find a JSON object in the response.
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            v = str(data.get("verdict", "")).upper()
            if v in ("APPROVE", "REJECT", "MODIFY"):
                return v, str(data.get("feedback", ""))
    except Exception:
        pass

    # Fall back to keyword matching.
    upper = raw.upper()
    if "APPROVE" in upper:
        return "APPROVE", raw
    if "REJECT" in upper:
        return "REJECT", raw
    return "MODIFY", raw


# ── Mode registry ─────────────────────────────────────────────────────────


_MODE_IMPLS: Dict[CollaborationMode, _ModeImpl] = {
    CollaborationMode.REVIEWER: ReviewerMode(),
    CollaborationMode.CODEGEN: CodegenMode(),
    CollaborationMode.PAIR: PairMode(),
    CollaborationMode.OBSERVER: ObserverMode(),
}


def get_mode(mode: CollaborationMode) -> _ModeImpl:
    """Look up the implementation for ``mode``."""
    impl = _MODE_IMPLS.get(mode)
    if impl is None:
        raise ValueError(f"unknown collaboration mode: {mode!r}")
    return impl
