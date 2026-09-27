"""
Fuzzy command matching (LOCAL PROTOTYPE — not committed).

Weighted subsequence matcher for the slash-command menus (inline
suggestions + Ctrl+P palette). Same idea as fuzzy file/command
finders everywhere: the name counts most, word parts less,
descriptions least; prefix and consecutive runs score higher.

No third-party dependency — ~80 lines, deterministic, <0.1ms for the
~100-command catalog, so it runs on every keystroke.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

_WORD_SEP = re.compile(r"[-_/]+")


def subsequence_positions(query: str, text: str) -> Optional[List[int]]:
    """Greedy subsequence match positions of query in text (case-insensitive).

    Both sides are lowered internally, so callers may pass raw input."""
    positions: List[int] = []
    cursor = 0
    lowered = text.lower()
    for char in query.lower():
        found = lowered.find(char, cursor)
        if found < 0:
            return None
        positions.append(found)
        cursor = found + 1
    return positions


def _field_score(query: str, field: str) -> Tuple[float, List[int]]:
    """(coverage, positions) for one field. Coverage rewards tight,
    prefix-anchored matches over sparse ones in long strings."""
    positions = subsequence_positions(query, field)
    if positions is None:
        return 0.0, []
    adjacent = sum(1 for a, b in zip(positions, positions[1:]) if b == a + 1)
    prefix = 1.0 if positions[0] == 0 else 0.0
    coverage = len(query) / max(len(field), 1)
    score = coverage * (1.0 + prefix + 0.15 * adjacent)
    return score, positions


def match_score(query: str, name: str, description: str = "") -> Tuple[float, List[int]]:
    """Weighted score + label highlight positions.

    Weights: full name ×3, dash-separated word parts ×2, description
    ×0.5. Returns (0.0, []) below the strictness threshold so weak
    description-only grazes never surface.
    """
    query = (query or "").lower().strip().lstrip("/")
    if not query:
        return 1.0, []
    name_lowered = name.lower()
    best = 0.0
    best_positions: List[int] = []
    name_score, name_pos = _field_score(query, name_lowered)
    if name_pos:
        best, best_positions = 3.0 * name_score, name_pos
    for part in [p for p in _WORD_SEP.split(name_lowered) if p]:
        part_score, _ = _field_score(query, part)
        if 2.0 * part_score > best:
            best = 2.0 * part_score
            best_positions = subsequence_positions(query, name_lowered) or []
    desc_score, _ = _field_score(query, (description or "").lower())
    if 0.5 * desc_score > best:
        best = 0.5 * desc_score
        best_positions = subsequence_positions(query, name_lowered) or []
    if best < 0.35:
        return 0.0, []
    return best, best_positions


def match_commands(query: str, items: List[Any]) -> List[Tuple[Any, float, List[int]]]:
    """Rank generic items with .id/.label/.description (or dicts).

    Returns [(item, score, label_positions)] sorted best-first; ties
    break toward shorter labels, then alphabetically. Empty query
    returns everything unscored in original order.
    """
    if not (query or "").strip().lstrip("/"):
        return [(item, 1.0, []) for item in items]

    def _field(item: Any, key: str) -> str:
        if isinstance(item, dict):
            value = item.get(key, "")
            if not value and key == "description":
                value = item.get("desc", "")
            return str(value or "")
        return str(getattr(item, key, "") or "")

    ranked = []
    for order, item in enumerate(items):
        name = _field(item, "id") or _field(item, "label")
        score, positions = match_score(query, name, _field(item, "description"))
        if score > 0:
            ranked.append((item, score, positions, order))
    ranked.sort(key=lambda r: (-r[1], len(_field(r[0], "label")), _field(r[0], "id"), r[3]))
    return [(item, score, positions) for item, score, positions, _ in ranked]


def highlight_text(label: str, positions: List[int], plain_style: str = "",
                   mark_style: str = "bold") -> Any:
    """Rich Text with matched positions bolded. Falls back to plain str."""
    if not positions:
        return label
    try:
        from rich.text import Text
        out = Text()
        pos_set = set(positions)
        for index, char in enumerate(label):
            if index in pos_set:
                out.append(char, style=mark_style or None)
            elif plain_style:
                out.append(char, style=plain_style)
            else:
                out.append(char)
        return out
    except Exception:
        return label
