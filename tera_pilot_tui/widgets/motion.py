"""motion.py — shared entrance-animation helpers (v2.3.1).

Textual 8's app-level CSS does not support `animation` / `transition` /
`@keyframes`, so all entrance motion is driven from Python through the
`animate()` API. These helpers keep the motion language consistent
across the TUI: a fast fade with a decelerating (out_cubic) easing —
quiet and premium.

Every helper is fail-safe: animation is purely cosmetic, and if anything
goes wrong the widget is restored to its fully-visible state so the UI
never ends up with an invisible modal.

v2.3.4-fix (two separate crashes, both fatal in Textual 8):
1. `widget.animate("opacity", ...)` animated the widget's *attribute*
   ``opacity``, which is a read-only property in Textual 8 — the first
   animator tick raised ``AttributeError: property 'opacity' has no
   setter``. Textual's ``_handle_exception`` treats any timer exception
   as fatal and EXITS the whole app (return code 1), so opening the
   Ctrl+P palette, the model selector, an approval/verification dialog
   or typing "/" crashed the TUI a moment after the widget appeared.
   Styles must be animated through ``widget.styles.animate(...)``
   instead, which targets the Styles object (``opacity`` there has a
   real setter).
2. The "slight rise" used ``styles.animate("offset", Offset(0, 0))``,
   but the *current* offset style is a ``ScalarOffset``, which has no
   ``blend()`` — the animator asserted ``start_value must be float``
   and crashed the app the same way. Offset animation is therefore
   dropped; the entrance is a pure opacity fade, which is safe and
   visually clean.
"""

from __future__ import annotations

from textual.widget import Widget

_DURATION = 0.18
_EASING = "out_cubic"


def _fade_to(widget: Widget, value: float, duration: float, easing: str) -> bool:
    """Animate the widget's opacity style. Returns True on success.

    The animator runs asynchronously, so a failure at call time can't be
    detected by the caller afterwards — on any error we snap straight to
    the fully-visible state (opacity 1) instead of leaving an invisible
    modal behind.
    """
    try:
        widget.styles.animate("opacity", value, duration=duration, easing=easing)
        return True
    except Exception:
        return False


def entrance(widget: Widget, duration: float = _DURATION) -> None:
    """Fade-in entrance for a container (modal bodies etc.)."""
    try:
        widget.styles.opacity = 0.0
    except Exception:
        # Can't even set the starting style — leave the widget visible.
        return
    if not _fade_to(widget, 1.0, duration=duration, easing=_EASING):
        # Animation failed — snap back to the fully-visible state so the
        # UI never ends up with an invisible modal.
        try:
            widget.styles.opacity = 1.0
        except Exception:
            pass


def fade_in(widget: Widget, duration: float = _DURATION) -> None:
    """Plain opacity fade-in (for suggestion bars, panels)."""
    entrance(widget, duration=duration)
