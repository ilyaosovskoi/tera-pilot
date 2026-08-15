"""motion.py — shared entrance-animation helpers (v2.3.1).

Textual 8's app-level CSS does not support `animation` / `transition` /
`@keyframes`, so all entrance motion is driven from Python through the
`animate()` API. These helpers keep the motion language consistent
across the TUI: a fast fade + slight rise with a decelerating
(out_cubic) easing — quiet and premium.

Every helper is fail-safe: animation is purely cosmetic, and if anything
goes wrong the widget is restored to its fully-visible state so the UI
never ends up with an invisible modal.
"""

from __future__ import annotations

from textual.geometry import Offset
from textual.widget import Widget

_DURATION = 0.18
_EASING = "out_cubic"


def entrance(widget: Widget, duration: float = _DURATION) -> None:
    """Fade + slight rise entrance for a container (modal bodies etc.)."""
    try:
        widget.styles.opacity = 0.0
        widget.styles.offset = Offset(0, 2)
    except Exception:
        # Can't even set the starting style — leave the widget visible.
        return
    try:
        widget.animate("opacity", 1.0, duration=duration, easing=_EASING)
        widget.animate("offset", Offset(0, 0), duration=duration, easing=_EASING)
    except Exception:
        # Animation failed — snap back to the fully-visible state.
        try:
            widget.styles.opacity = 1.0
            widget.styles.offset = Offset(0, 0)
        except Exception:
            pass


def fade_in(widget: Widget, duration: float = _DURATION) -> None:
    """Plain opacity fade-in (for suggestion bars, panels)."""
    try:
        widget.styles.opacity = 0.0
    except Exception:
        return
    try:
        widget.animate("opacity", 1.0, duration=duration, easing=_EASING)
    except Exception:
        try:
            widget.styles.opacity = 1.0
        except Exception:
            pass
