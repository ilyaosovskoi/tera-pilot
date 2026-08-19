"""Repetition-dominated response detection (ported from Nous Research's
hermes-agent, MIT license — https://github.com/NousResearch/hermes-agent).

A model in a degenerate repetition loop spends its ENTIRE output budget
echoing one fragment. Without a content-sanity check, such a response
can be stitched into the final answer (or, worse, streamed to the user
as a wall of repeated text). These helpers detect repetition-dominated
responses BEFORE they are accepted as a final answer so the agent loop
can refuse to finalize on them and nudge the model back on track.

The detection is deliberately conservative: only LONG verbatim repeats
(60+ chars) whose occurrences cover a majority of the response trip the
guard, so ordinary prose (a sentence repeated for emphasis, code with
similar-looking lines) is never blocked.
"""

from __future__ import annotations

import math

# A response must be at least this long before the repetition check runs
# at all. Short responses can trivially contain repeated tokens and are
# legitimately final.
MIN_FRAGMENT_LENGTH = 400

# Length of the exact-repeat window. A verbatim repeat of this many
# chars is far beyond ordinary phrasing reuse.
_REPEAT_WINDOW = 60

# A window that repeats at least this many times is a repetition signal,
# even for short fragments.
_MIN_REPEAT_COUNT = 5

# A response is "repetition-dominated" when repeated windows account for
# at least this fraction of its characters.
_DOMINANCE_RATIO = 0.5


def is_repetition_dominated(text: str) -> bool:
    """True when ``text`` is dominated by verbatim repeated fragments.

    A response is "repetition-dominated" when a single 60+ char
    substring appears often enough that its occurrences cover at least
    half of the fragment. That shape is the signature of a model
    repetition loop, and finalizing on it would surface garbage.

    Returns False for non-string / empty / short inputs (fail-open:
    never blocks a response the guard cannot confidently judge).
    """
    if not isinstance(text, str):
        return False
    n = len(text)
    if n < MIN_FRAGMENT_LENGTH:
        return False

    # Fast path: one normalized line duplicated often enough to cover
    # half the fragment (the most common echo shape — a repeated
    # paragraph or sentence on its own line). Cheap, no big allocations.
    if _line_repetition_dominated(text, n):
        return True

    # General path: fixed-size exact-repeat windows, sliding one char at
    # a time. Catches repetition loops that do not align to line
    # boundaries.
    window = _REPEAT_WINDOW
    # A window must appear this many times for its occurrences to cover
    # >= DOMINANCE_RATIO of the response (and at least _MIN_REPEAT_COUNT).
    needed = max(_MIN_REPEAT_COUNT, math.ceil(n * _DOMINANCE_RATIO / window))
    counts: dict = {}
    for i in range(n - window + 1):
        key = text[i:i + window]
        c = counts.get(key, 0) + 1
        if c >= needed:
            return True
        counts[key] = c
    return False


def _line_repetition_dominated(text: str, n: int) -> bool:
    """True when a single normalized line covers half the response via repeats."""
    counts: dict = {}
    for line in text.splitlines():
        norm = line.strip()
        if not norm:
            continue
        counts[norm] = counts.get(norm, 0) + 1
    for line, c in counts.items():
        if c >= _MIN_REPEAT_COUNT and c * len(line) >= n * _DOMINANCE_RATIO:
            return True
    return False
