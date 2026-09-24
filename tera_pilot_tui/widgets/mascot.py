"""mascot.py — 8-bit pilot mascot for the TUI.

The README mascot (robot pilot: dark helmet, big amber visor with a
"T", antenna ball, ear pods, cockpit console) redrawn as terminal
pixel art. Rendered with half-block characters (▀▄), so the 24x17
logical pixels collapse to 24x9 terminal rows — the 16-bit look for
the welcome splash and the /mascot command.

Frames: 0 = visor open (T visible), 1 = blink (T stem off). The
/mascot command alternates frames per call, so the pilot blinks at
you. Palettes are per-theme; light values keep the amber readable on
white.
"""

from __future__ import annotations

import random
from typing import Dict, List

from rich.text import Text

# ── Pixel grid ─────────────────────────────────────────────────────
# '.' transparent · 'D' outline · 'K' hull · 'A' amber · 'W' shine.
_FRAME_OPEN: List[str] = [
    "...........WA...........",
    "...........AA...........",
    "...........KK...........",
    "......DDDDDDDDDDDD......",
    "....DDKKKKKKKKKKKKDD....",
    "...DKKKKKKKKKKKKKKKKD...",
    "..AAADKKKKKKKKKKKKDAAA..",
    "..ADADKKKAAAAAAKKKDADA..",
    "..ADADKKKAWAADAKKKDADA..",
    "..ADADKKKAWAADAKKKDADA..",
    "..ADADKKKAAAAAAKKKDADA..",
    "......DDDDDDDDDDDD......",
    "........DKKKKKKD........",
    ".........KKKKKK.........",
    "...AAAAAAAAAAAAAAAAAA...",
    "...DKKKKKKKKKKKKKKKKD...",
    "...DKKKKKKKKKKKKKAAD....",
]

# Blink: the T stem goes dark (rows 8-9 inside the visor).
_FRAME_BLINK: List[str] = list(_FRAME_OPEN)
_FRAME_BLINK[8] = "..ADADKKKAWDDDAKKKDADA.."
_FRAME_BLINK[9] = "..ADADKKKADDDDAKKKDADA.."

FRAMES: List[List[str]] = [_FRAME_OPEN, _FRAME_BLINK]

WIDTH = 24

_PALETTES: Dict[bool, Dict[str, str]] = {
    True: {   # dark
        "D": "#14151a",
        "K": "#4a5160",
        "A": "#f2b234",
        "W": "#ffffff",
    },
    False: {  # light
        "D": "#26262e",
        "K": "#9a9aa4",
        "A": "#c07f1a",
        "W": "#ffffff",
    },
}

# Pilot one-liners for /mascot (short, no emoji — TUI tone).
QUIPS: List[str] = [
    "Preflight complete. Where to?",
    "I read the whole repo so you don't have to.",
    "Plans first, writes second. Always.",
    "Ask me to find the riskiest file.",
    "Local-first. Your code stays home.",
    "I never push without asking.",
    "Small steps, green tests, signed evidence.",
    "Point me at the bug. I'll bring a plan.",
]


def render_mascot(dark: bool = True, frame: int = 0) -> Text:
    """Render the mascot as half-block pixel art (24 cols, 9 rows).

    Pure function — safe to unit-test without a running app.
    """
    grid = FRAMES[int(frame) % len(FRAMES)]
    pal = _PALETTES[bool(dark)]
    out = Text()
    rows = len(grid)
    for top_idx in range(0, rows, 2):
        top = grid[top_idx]
        bottom = grid[top_idx + 1] if top_idx + 1 < rows else "." * WIDTH
        for col in range(WIDTH):
            t = top[col] if col < len(top) else "."
            b = bottom[col] if col < len(bottom) else "."
            if t == b:
                if t == ".":
                    out.append(" ")
                else:
                    out.append(" ", style=f"on {pal[t]}")
            elif t == ".":
                out.append("▄", style=pal[b])
            elif b == ".":
                out.append("▀", style=pal[t])
            else:
                out.append("▄", style=f"{pal[b]} on {pal[t]}")
        if top_idx + 2 < rows:
            out.append("\n")
    return out


def random_quip() -> str:
    return random.choice(QUIPS)
