"""G19a — Symbolic task canvas.

A compact, structured summary of the current task as a small graph of steps.
Lives near the top of the agent prompt so the model always knows where it is
in the plan, without having to re-read scrollback.

Design constraints (from the G19 prompt):
- Reuse the existing tombstone/compaction discipline from
  ``agent/context_fragments.py`` — emit the canvas as a single fragment via
  ``build_fragment()`` so it participates in the same compaction as
  everything else. Stable id via ``stable_id()`` so re-emission is idempotent
  (the compactor keeps only the latest).
- Render a small ASCII/tree representation: a few hundred tokens max regardless
  of task size. Cap node count shown, summarise the rest as "+N more done".
- Full detail for any node stays reachable via its ``id`` through the existing
  fragment system — but the canvas does NOT inline that detail by default.

This module is intentionally side-effect-free (no I/O, no LLM calls).
Mutation happens through the small public API (``add_node``, ``update_status``,
``reset``); the prompt-builder in ``agent_runtime/runtime.py`` calls
``to_compact_text()`` once per turn and ``to_fragment()`` to wrap it.

Thread-safety: an internal RLock guards every public mutator/reader because
the canvas is shared between the prompt-building thread and any sub-agent
threads that may update node statuses from ``task_decomposition_router.py``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# Re-use the existing fragment helpers so the canvas participates in the
# same tombstone-compaction discipline as every other tool output.
from tera_pilot.agent.context_fragments import build_fragment, stable_id


# Public status constants — kept as plain strings so they serialise cleanly
# to JSON / activity-log meta without a custom encoder.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
ALL_STATUSES: Tuple[str, ...] = (STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED)

# Hard cap on the number of nodes shown in the compact rendering. The
# remainder are summarised as "+N more done" / "+N more pending" so the
# token cost stays bounded regardless of how large the task graph grows.
# The prompt says "a few hundred tokens max regardless of task size" —
# 12 visible nodes at ~20-30 tokens each fits that budget comfortably.
MAX_VISIBLE_NODES = 12

# Hard cap on the per-node label length in the compact rendering. Long
# labels get truncated with an ellipsis; the full label is still in the
# node's ``label`` field (and reachable via the node id).
MAX_LABEL_CHARS = 80

# Fragment type used when wrapping the canvas. Mirrors the snake_case
# convention used by web_search / web_page / project_learnings.
FRAGMENT_TYPE = "task_canvas"


class TaskCanvasError(Exception):
    """Raised on invalid canvas operations (duplicate id, unknown parent, ...)."""


@dataclass
class CanvasNode:
    """A single step in the task graph.

    Stored by id in :class:`TaskCanvas`. ``depends_on`` references parent
    node ids (must already exist when the node is added).
    """

    id: str
    label: str
    status: str = STATUS_PENDING
    depends_on: List[str] = field(default_factory=list)
    # Optional model assignment, populated by G20's task-decomposition
    # router so the canvas can show which model handled which subtask.
    model: Optional[str] = None
    # Optional free-text note (e.g. subtask result digest). Kept short —
    # the canvas is a summary, not a log. Full detail stays reachable
    # via the node ``id`` through the existing fragment system.
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "model": self.model,
            "note": self.note,
        }


class TaskCanvas:
    """In-memory task graph.

    Singleton-per-process is NOT enforced — callers may instantiate as many
    as they like (useful for tests). The prompt-builder in
    ``agent_runtime/runtime.py`` will use a module-level singleton via
    :func:`get_task_canvas` so all sub-agent threads see the same canvas.
    """

    def __init__(self) -> None:
        self._nodes: Dict[str, CanvasNode] = {}
        # Insertion order is preserved (Python 3.7+ dict) so ``node_ids()``
        # returns a deterministic list — important for test assertions and
        # for the compact renderer's "first N nodes" slice.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Mutators
    # ------------------------------------------------------------------ #
    def add_node(
        self,
        node_id: str,
        label: str,
        *,
        status: str = STATUS_PENDING,
        depends_on: Optional[Iterable[str]] = None,
        model: Optional[str] = None,
        note: Optional[str] = None,
    ) -> CanvasNode:
        """Add a new node.

        Raises :class:`TaskCanvasError` if the id is already taken or a
        ``depends_on`` parent does not exist. This is the only way to add
        a node — there is no public ``CanvasNode`` constructor entry point
        so callers can't bypass validation.
        """
        if not node_id or not isinstance(node_id, str):
            raise TaskCanvasError("node_id must be a non-empty string")
        if not label or not isinstance(label, str):
            raise TaskCanvasError("label must be a non-empty string")
        if status not in ALL_STATUSES:
            raise TaskCanvasError(
                f"status must be one of {ALL_STATUSES!r}, got {status!r}"
            )
        deps = list(depends_on or [])
        with self._lock:
            if node_id in self._nodes:
                raise TaskCanvasError(f"node {node_id!r} already exists")
            for dep in deps:
                if dep not in self._nodes:
                    raise TaskCanvasError(
                        f"unknown depends_on parent {dep!r} for node {node_id!r}"
                    )
            node = CanvasNode(
                id=node_id,
                label=label,
                status=status,
                depends_on=deps,
                model=model,
                note=note,
            )
            self._nodes[node_id] = node
            return node

    def update_status(
        self,
        node_id: str,
        status: str,
        *,
        note: Optional[str] = None,
        model: Optional[str] = None,
    ) -> CanvasNode:
        """Update a node's status (and optionally its note/model).

        Raises :class:`TaskCanvasError` if the node does not exist or the
        status is invalid. ``note`` and ``model`` are only overwritten
        when explicitly passed (not None) — this matches the
        "targeted edit, not full rewrite" discipline used elsewhere in
        the codebase.
        """
        if status not in ALL_STATUSES:
            raise TaskCanvasError(
                f"status must be one of {ALL_STATUSES!r}, got {status!r}"
            )
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                raise TaskCanvasError(f"unknown node {node_id!r}")
            node.status = status
            if note is not None:
                node.note = note
            if model is not None:
                node.model = model
            return node

    def reset(self) -> None:
        """Drop every node. Used at the start of a new top-level task so
        the canvas from the previous turn doesn't leak into the new one."""
        with self._lock:
            self._nodes.clear()

    # ------------------------------------------------------------------ #
    # Readers
    # ------------------------------------------------------------------ #
    def node_ids(self) -> List[str]:
        """Return all node ids in insertion order.

        Used for drill-down: the caller can pass any of these ids to the
        existing fragment system to retrieve the full detail for that
        node (the canvas itself only keeps a short label + status).
        """
        with self._lock:
            return list(self._nodes.keys())

    def get(self, node_id: str) -> Optional[CanvasNode]:
        with self._lock:
            return self._nodes.get(node_id)

    def nodes(self) -> List[CanvasNode]:
        """Snapshot of all nodes (insertion order)."""
        with self._lock:
            return list(self._nodes.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._nodes)

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def to_compact_text(self, *, max_visible: int = MAX_VISIBLE_NODES) -> str:
        """Render the canvas as a small ASCII tree.

        Format (stable, parsed by tests):
        ::
            task_canvas (N nodes: X done, Y running, Z pending, W failed)
            [done]      step1  -> model:gpt-4o
            [running]   step2  (depends: step1)
            [pending]   step3  (depends: step2)
            +N more (M done, K pending)

        Token budget: ~20-30 tokens per visible node + ~30 tokens for the
        header + summary line. With ``max_visible=12`` that's ~400 tokens
        worst case — comfortably within "a few hundred tokens max".

        Empty canvas returns ``""`` (caller should skip injection).
        """
        with self._lock:
            nodes = list(self._nodes.values())
        if not nodes:
            return ""

        counts = _count_by_status(nodes)
        header = (
            f"task_canvas ({len(nodes)} nodes: "
            f"{counts[STATUS_DONE]} done, "
            f"{counts[STATUS_RUNNING]} running, "
            f"{counts[STATUS_PENDING]} pending, "
            f"{counts[STATUS_FAILED]} failed)"
        )

        # Order: running first (the hot path), then failed (needs attention),
        # then pending, then done. Within each bucket, insertion order.
        ordered = _order_for_display(nodes)

        visible = ordered[:max_visible]
        hidden = ordered[max_visible:]

        lines: List[str] = [header]
        for n in visible:
            lines.append(_render_node_line(n))
        if hidden:
            hidden_counts = _count_by_status(hidden)
            parts: List[str] = []
            if hidden_counts[STATUS_DONE]:
                parts.append(f"{hidden_counts[STATUS_DONE]} done")
            if hidden_counts[STATUS_RUNNING]:
                parts.append(f"{hidden_counts[STATUS_RUNNING]} running")
            if hidden_counts[STATUS_PENDING]:
                parts.append(f"{hidden_counts[STATUS_PENDING]} pending")
            if hidden_counts[STATUS_FAILED]:
                parts.append(f"{hidden_counts[STATUS_FAILED]} failed")
            lines.append(f"+{len(hidden)} more ({', '.join(parts)})")
        return "\n".join(lines)

    def to_fragment(self) -> Optional[str]:
        """Wrap :meth:`to_compact_text` in a ``<context_fragment>`` block.

        Returns ``None`` when the canvas is empty so the prompt-builder
        can skip injection cleanly without emitting an empty fragment
        (which would still cost tokens and pollute the compaction
        statistics).
        """
        text = self.to_compact_text()
        if not text:
            return None
        # Stable id means re-emitting each turn is idempotent — the
        # compactor keeps only the latest per-id, so the canvas never
        # accumulates across turns even though we inject it every turn.
        fid = stable_id(FRAGMENT_TYPE, "current")
        return build_fragment(FRAGMENT_TYPE, fid, text)

    def to_dict(self) -> Dict[str, object]:
        """Full structured state (for the web bridge / GUI / drill-down)."""
        with self._lock:
            return {
                "nodes": [n.to_dict() for n in self._nodes.values()],
                "counts": _count_by_status(list(self._nodes.values())),
                "total": len(self._nodes),
            }


# ---------------------------------------------------------------------- #
# Helpers (module-private)
# ---------------------------------------------------------------------- #
def _count_by_status(nodes: List[CanvasNode]) -> Dict[str, int]:
    counts = {s: 0 for s in ALL_STATUSES}
    for n in nodes:
        counts[n.status] = counts.get(n.status, 0) + 1
    return counts


# Display order: running > failed > pending > done. Within a bucket,
# preserve insertion order so the rendering is deterministic.
_DISPLAY_PRIORITY = {
    STATUS_RUNNING: 0,
    STATUS_FAILED: 1,
    STATUS_PENDING: 2,
    STATUS_DONE: 3,
}


def _order_for_display(nodes: List[CanvasNode]) -> List[CanvasNode]:
    # ``sorted`` is stable, so ties keep insertion order. Index into the
    # original list to recover insertion order without needing an explicit
    # counter on CanvasNode.
    indexed = list(enumerate(nodes))
    indexed.sort(key=lambda pair: (_DISPLAY_PRIORITY.get(pair[1].status, 99), pair[0]))
    return [n for _, n in indexed]


def _render_node_line(n: CanvasNode) -> str:
    label = n.label if len(n.label) <= MAX_LABEL_CHARS else n.label[: MAX_LABEL_CHARS - 1] + "…"
    status_tag = f"[{n.status}]"
    # Pad status tag to a fixed width so labels line up visually.
    status_tag = status_tag.ljust(len("[pending]"))
    parts: List[str] = [status_tag, label]
    if n.model:
        parts.append(f"-> model:{n.model}")
    if n.depends_on:
        parts.append(f"(depends: {', '.join(n.depends_on)})")
    if n.note:
        # Notes are kept short — the canvas is a summary, not a log.
        note = n.note if len(n.note) <= 60 else n.note[:59] + "…"
        parts.append(f"// {note}")
    return "  ".join(parts)


# ---------------------------------------------------------------------- #
# Module-level singleton (lazy)
# ---------------------------------------------------------------------- #
# Same pattern as ``activity_log.get_activity_log()`` and
# ``cost_router.get_cost_router()``: lazy-init under a Lock so the first
# caller pays the init cost, every subsequent caller gets the same instance.
# Tests can call ``reset_task_canvas_for_test()`` to get a fresh one.
_CANVAS: Optional[TaskCanvas] = None
_CANVAS_LOCK = threading.Lock()


def get_task_canvas() -> TaskCanvas:
    """Return the process-wide :class:`TaskCanvas` singleton."""
    global _CANVAS
    if _CANVAS is None:
        with _CANVAS_LOCK:
            if _CANVAS is None:
                _CANVAS = TaskCanvas()
    return _CANVAS


def reset_task_canvas_for_test() -> TaskCanvas:
    """Replace the singleton with a fresh empty canvas. Test-only."""
    global _CANVAS
    with _CANVAS_LOCK:
        _CANVAS = TaskCanvas()
    return _CANVAS
