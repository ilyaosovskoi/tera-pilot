"""thinking.py — animated thinking indicator with whimsical verbs.

Displays an animated spinner cycle with a randomly-chosen whimsical
verb next to it. The spinner uses terracotta + shimmer colors
per the Loop 3 design spec. Never displays "Thinking…" or
"Loading…" — personality is mandatory.

v2.1.0 (Loop 3): TUI Visual Overhaul — signature feature.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Optional

from textual.widgets import Static


# ── Spinner frames (6 frames, 120ms cycle) ──────────────────────────
_SPINNER_FRAMES = [
    "·", "✢", "✳", "✶", "✻", "✽",
]

# ── Whimsical verbs (50+) ────────────────────────────────────────────
# Never use "Thinking…" / "Loading…" — personality is mandatory.
_WHIMSICAL_VERBS = [
    "Percolating…",
    "Cogitating…",
    "Shenaniganing…",
    "Moonwalking…",
    "Ruminating…",
    "Simmering…",
    "Marinating…",
    "Faffing…",
    "Pottering…",
    "Noodeling…",
    "Pondering…",
    "Musing…",
    "Deliberating…",
    "Mulling…",
    "Chewing…",
    "Digesting…",
    "Fermenting…",
    "Brewing…",
    "Steeping…",
    "Aging…",
    "Incubating…",
    "Gestating…",
    "Hatching…",
    "Crystallizing…",
    "Coalescing…",
    "Synthesizing…",
    "Weaving…",
    "Knitting…",
    "Stitching…",
    "Embroidering…",
    "Orchestrating…",
    "Choreographing…",
    "Conducting…",
    "Directing…",
    "Curating…",
    "Puzzling…",
    "Tinkering…",
    "Dabbling…",
    "Meandering…",
    "Wandering…",
    "Frolicking…",
    "Gallivanting…",
    "Skedaddling…",
    "Lollygagging…",
    "Dilly-dallying…",
    "Hobnobbing…",
    "Whittling…",
    "Juggling…",
    "Balancing…",
    "Untangling…",
]

# ── Terracotta + shimmer colors ──────────────────────────────────────
_TERRACOTTA = "#d77757"
_SHIMMER = "#eb9f7f"


class ThinkingIndicator(Static):
    """Animated thinking indicator with whimsical verbs.

    Shows an animated spinner cycle (6 frames, 120ms) with a
    randomly-chosen whimsical verb in terracotta + shimmer colors.
    The verb changes each time the indicator is started.

    Usage:
        indicator = ThinkingIndicator()
        indicator.start()  # begins animation
        indicator.stop()   # stops animation, clears content
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._spinner_frame: int = 0
        self._anim_task: Optional[asyncio.Task] = None
        self._verb: str = random.choice(_WHIMSICAL_VERBS)
        self._shimmer: bool = False

    def start(self) -> None:
        """Start the thinking animation. Picks a new random verb."""
        self._verb = random.choice(_WHIMSICAL_VERBS)
        self._spinner_frame = 0
        self._shimmer = False
        self._start_animation()

    def stop(self) -> None:
        """Stop the thinking animation and clear the content."""
        self._stop_animation()
        self.update("")

    def _start_animation(self) -> None:
        self._stop_animation()
        try:
            self._anim_task = asyncio.create_task(self._anim_loop())
        except RuntimeError:
            pass  # no event loop yet

    def _stop_animation(self) -> None:
        if self._anim_task and not self._anim_task.done():
            self._anim_task.cancel()
        self._anim_task = None

    async def _anim_loop(self) -> None:
        """Animate the spinner — 120ms per frame."""
        try:
            while True:
                frame = _SPINNER_FRAMES[self._spinner_frame % len(_SPINNER_FRAMES)]
                color = _SHIMMER if self._shimmer else _TERRACOTTA
                self.update(f"[{color}]{frame} {self._verb}[/{color}]")
                self._spinner_frame = (self._spinner_frame + 1) % len(_SPINNER_FRAMES)
                # Toggle shimmer every 4 frames (~480ms)
                if self._spinner_frame % 4 == 0:
                    self._shimmer = not self._shimmer
                await asyncio.sleep(0.12)
        except asyncio.CancelledError:
            pass

    def on_unmount(self) -> None:
        """Clean up animation on widget removal."""
        self._stop_animation()
