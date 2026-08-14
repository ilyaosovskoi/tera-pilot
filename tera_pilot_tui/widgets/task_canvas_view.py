"""task_canvas_view.py — TUI widget rendering the live task graph.

G19a (Symbolic task canvas). Renders the same :class:`TaskCanvas` state
that gets injected into the agent prompt as a fragment, but as a live
tree in the TUI sidebar/status area.

Design constraints (from the G19 prompt):
- Reuse the visual language already established in the Loop 3 overhaul
  (terracotta #d77757 primary, muted #888888 for secondary, same
  ``Static`` widget base class as :class:`StatusBar` and
  :class:`ThinkingIndicator`).
- Don't introduce a clashing color scheme — pull colors from the same
  palette already in ``styles_dark.tcss`` / ``styles_light.tcss``.
- Bounded token cost / screen real estate: same ``MAX_VISIBLE_NODES``
  cap as the prompt fragment, with ``+N more`` summarisation.

The widget is read-only — mutation happens via :func:`get_task_canvas`
from anywhere in the process (typically the task-decomposition router
or the runtime itself). The widget just calls ``refresh_view()`` on
each agent event to pull the latest state.
"""

from __future__ import annotations

from typing import Any, Optional

from textual.widgets import Static

# Same palette as StatusBar / ThinkingIndicator — Loop 3 overhaul.
_TERRACOTTA = "#d77757"
_MUTED = "#888888"

# Status colors. Kept conservative to fit the existing palette — green
# for done, terracotta for running (matches the "active" brand color),
# yellow for pending (matches the heavy_code badge in StatusBar), red
# for failed (matches the "all" guardian badge in StatusBar).
_STATUS_COLORS = {
    "done": "green",
    "running": _TERRACOTTA,
    "pending": "yellow",
    "failed": "red",
}


class TaskCanvasView(Static):
    """Sidebar widget rendering the live :class:`TaskCanvas` state.

    Initially empty (displays a short placeholder). Call ``refresh_view()``
    after any canvas mutation to re-render. The runtime's event loop
    already calls this after each turn — see ``tera_pilot_tui/app.py``.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._last_text: str = ""
        # Initial placeholder so the widget is never visually empty
        # before the first canvas mutation lands.
        self.update(
            f"[{_TERRACOTTA}]task canvas[/{_TERRACOTTA}] "
            f"[{_MUTED}](empty)[/{_MUTED}]"
        )

    def refresh_view(self) -> None:
        """Pull the latest canvas state and re-render.

        Idempotent: if the canvas text hasn't changed since the last
        call, we skip the ``update()`` to avoid pointless DOM churn
        in Textual (which can cause flicker on slow terminals).
        """
        text = self._build_markup()
        if text == self._last_text:
            return
        self._last_text = text
        if not text:
            self.update(
                f"[{_TERRACOTTA}]task canvas[/{_TERRACOTTA}] "
                f"[{_MUTED}](empty)[/{_MUTED}]"
            )
        else:
            self.update(text)

    def _build_markup(self) -> str:
        """Render the canvas as Textual markup.

        Mirrors :meth:`TaskCanvas.to_compact_text` but with Textual
        color tags so statuses are visually distinguishable. Reuses
        the same node-ordering and ``+N more`` summarisation.
        """
        # Lazy import so the widget can be constructed even if the
        # canvas module hasn't been imported yet (defensive — shouldn't
        # happen in practice but keeps the widget self-contained).
        try:
            from tera_pilot.agent.task_canvas import get_task_canvas, MAX_VISIBLE_NODES
        except Exception:
            return ""

        canvas = get_task_canvas()
        nodes = canvas.nodes()
        if not nodes:
            return ""

        counts = {"done": 0, "running": 0, "pending": 0, "failed": 0}
        for n in nodes:
            counts[n.status] = counts.get(n.status, 0) + 1

        header = (
            f"[{_TERRACOTTA}]task canvas[/{_TERRACOTTA}] "
            f"[{_MUTED}]({len(nodes)}: "
            f"{counts['done']}d {counts['running']}r "
            f"{counts['pending']}p {counts['failed']}f)"
            f"[/{_MUTED}]"
        )

        # Same display order as the prompt fragment: running > failed >
        # pending > done, with insertion order as tiebreaker.
        priority = {"running": 0, "failed": 1, "pending": 2, "done": 3}
        ordered = sorted(
            enumerate(nodes), key=lambda p: (priority.get(p[1].status, 99), p[0])
        )
        visible = ordered[:MAX_VISIBLE_NODES]
        hidden = ordered[MAX_VISIBLE_NODES:]

        lines = [header]
        for _, n in visible:
            color = _STATUS_COLORS.get(n.status, _MUTED)
            # Truncate long labels the same way as the prompt fragment.
            label = n.label if len(n.label) <= 60 else n.label[:59] + "…"
            line = f"[{color}][{n.status}][/{color}] {label}"
            if n.model:
                line += f" [{_MUTED}]-> {n.model}[/{_MUTED}]"
            lines.append(line)
        if hidden:
            hidden_counts = {"done": 0, "running": 0, "pending": 0, "failed": 0}
            for _, n in hidden:
                hidden_counts[n.status] = hidden_counts.get(n.status, 0) + 1
            parts = []
            for s in ("done", "running", "pending", "failed"):
                if hidden_counts[s]:
                    parts.append(f"{hidden_counts[s]} {s[0]}")
            lines.append(f"[{_MUTED}]+{len(hidden)} more ({', '.join(parts)})[/{_MUTED}]")
        return "\n".join(lines)

    def on_unmount(self) -> None:
        """Clean up on widget removal — nothing to do beyond the base
        class, but defined explicitly so future resources (e.g. a poll
        timer) have an obvious place to land."""
        pass
