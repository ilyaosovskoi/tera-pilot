"""
Turn suggestions — heuristic follow-ups and rotating tips.

No LLM calls: after a turn finishes, the TUI inspects which tools ran
and proposes 1-3 likely next steps (tests after edits, commit after
green tests, review after a refactor). Separately, a rotating
tip-of-the-turn surfaces underused features; the rotation index lives
in ``~/.tera_pilot/tips.json`` so tips don't repeat until exhausted.
Both are best-effort and silent on any error.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, List

logger = logging.getLogger(__name__)

TIPS: List[str] = [
    "Tip: /review checks your staged changes without writing anything.",
    "Tip: /permissions allow \"execute_command(pytest *)\" skips repeat prompts for tests.",
    "Tip: enter a planning mode with the agent's enter_plan_mode before big refactors.",
    "Tip: /remember stores a fact the agent will reuse every run.",
    "Tip: task_spawn runs long commands in the background — /tasks shows them live.",
    "Tip: /compact summarises old context when the conversation gets long.",
    "Tip: worktree_add isolates a risky refactor from your main checkout.",
    "Tip: /share-signed exports the run as tamper-evident evidence.",
    "Tip: /output-style can load a custom voice from ~/.tera_pilot/styles/.",
    "Tip: lsp_references finds every usage of a symbol, precisely.",
    "Tip: /sandbox on fails closed when no OS backend exists.",
    "Tip: /schedule shows cron entries the agent stored for the daemon.",
]


def _tips_path() -> Path:
    return Path(os.path.expanduser("~/.tera_pilot")) / "tips.json"


def rotating_tip() -> str:
    """Next tip in rotation. Never raises."""
    try:
        idx = 0
        path = _tips_path()
        if path.exists():
            try:
                idx = int(json.loads(path.read_text(encoding="utf-8")).get("index", 0))
            except (ValueError, TypeError):
                idx = 0
        tip = TIPS[idx % len(TIPS)]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"index": (idx + 1) % len(TIPS)}), encoding="utf-8")
        except OSError:
            pass
        return tip
    except Exception as exc:
        logger.debug("[suggest] tip failed: %s", exc)
        return TIPS[0]


def _tool_names(result: Any) -> List[str]:
    names = []
    try:
        for call in (getattr(result, "tool_calls", None) or []):
            name = getattr(getattr(call, "name", None), "value", None) or str(
                getattr(call, "name", ""))
            if name:
                names.append(name)
    except Exception:
        pass
    return names


def follow_ups(result: Any, max_items: int = 3) -> List[str]:
    """Heuristic next steps from the tools the turn used."""
    tools = set(_tool_names(result))
    success = bool(getattr(result, "success", True))
    out: List[str] = []
    wrote = bool(tools & {"write_file", "str_replace", "apply_diff", "office_create"})
    ran_tests = bool(tools & {"execute_command", "run_code", "task_spawn"})

    if not success:
        out.append("The turn hit an error — describe it and ask the agent to fix it.")
        return out[:max_items]
    if wrote and not ran_tests:
        out.append("Files changed but no tests ran — ask: “run the relevant tests”.")
    if wrote and ran_tests and "git_commit" not in tools:
        out.append("Green change, not committed — /commit stages one logical change.")
    if "git_commit" in tools:
        out.append("Committed — /review double-checks, /commit-push-pr ships it.")
    if tools & {"git_diff", "git_status"} and not wrote:
        out.append("You inspected the diff — /review gives a second opinion.")
    if "ask_user" in tools:
        out.append("Open question logged — answer it so the next turn proceeds.")
    if not out and tools:
        out.append("Turn done — /compact keeps context lean for the next task.")
    return out[:max_items]


def turn_footer(result: Any, turn_count: int, tip_every: int = 5) -> str:
    """One chat-ready block: follow-ups + periodic tip ("" when nothing)."""
    parts = []
    for item in follow_ups(result):
        parts.append(f"→ {item}")
    if turn_count > 0 and turn_count % tip_every == 0:
        parts.append(rotating_tip())
    if not parts:
        return ""
    return "\n".join(parts)
