"""
Permission rules — user-editable allow/deny patterns plus a mode switch.

A rule looks like ``execute_command(git *)`` or ``read_file(*)`` and is
matched against ``"<tool> <summary>"`` with fnmatch (``*`` globs). The
first matching rule wins; deny beats allow on ties by file order, so
users put specific denies first.

Modes (stored next to the rules):
  default — prompt for every side-effecting action (unchanged behaviour).
  plan    — approve each action *kind* once per session, then auto-allow
            repeats of the same kind.
  auto    — heuristic: non-destructive actions auto-allow, destructive
            ones still prompt (classifier, not a barrier — the sandbox
            and command policy below it are unchanged).
  bypass  — auto-approve everything (explicit opt-in for trusted automation).

The file lives at ``~/.tera_pilot/permission-rules.json``::

    {"mode": "default", "rules": [
        {"pattern": "execute_command(git *)", "effect": "allow"},
        {"pattern": "execute_command(rm *)", "effect": "deny"}
    ]}

Malformed files are ignored (logged, fail-closed to prompting).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MODES = ("default", "plan", "auto", "bypass")

#: Actions the ``auto`` heuristic treats as destructive (still prompt).
_AUTO_DESTRUCTIVE_PREFIXES = (
    "execute_command", "delete_file", "rename_file", "apply_diff",
    "write_binary_file", "task_spawn", "task_stop", "worktree_add",
    "worktree_remove", "cron_add", "cron_remove", "call_mcp_tool",
    "repl_run",
)

_lock = threading.Lock()
_cache: Dict[str, Any] = {"mtime": None, "data": None, "path": None}


def rules_path() -> Path:
    return Path(os.path.expanduser("~/.tera_pilot")) / "permission-rules.json"


def _default_data() -> Dict[str, Any]:
    return {"mode": "default", "rules": []}


def load_data() -> Dict[str, Any]:
    """Load {mode, rules}, cached by mtime. Never raises."""
    path = rules_path()
    try:
        mtime = path.stat().st_mtime if path.exists() else None
    except OSError:
        mtime = None
    with _lock:
        if _cache["data"] is not None and _cache["mtime"] == mtime \
                and _cache["path"] == str(path):
            return _cache["data"]
    data = _default_data()
    if mtime is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if raw.get("mode") in MODES:
                    data["mode"] = raw["mode"]
                rules = raw.get("rules") or []
                if isinstance(rules, list):
                    data["rules"] = [r for r in rules if _valid_rule(r)]
        except Exception as exc:
            logger.warning("[permissions] ignoring malformed %s: %s", path, exc)
    with _lock:
        _cache.update(mtime=mtime, data=data, path=str(path))
    return data


def _valid_rule(rule: Any) -> bool:
    return (
        isinstance(rule, dict)
        and isinstance(rule.get("pattern"), str)
        and rule.get("effect") in ("allow", "deny")
    )


def save_data(mode: Optional[str] = None,
              rules: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Persist mode and/or rules. Returns the stored document."""
    data = load_data()
    if mode is not None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r} (expected one of {MODES})")
        data = {"mode": mode, "rules": data["rules"]}
    if rules is not None:
        for r in rules:
            if not _valid_rule(r):
                raise ValueError(f"bad rule {r!r}")
        data = {"mode": data["mode"], "rules": list(rules)}
    path = rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    with _lock:
        _cache.update(mtime=path.stat().st_mtime, data=data, path=str(path))
    return data


def get_mode() -> str:
    return str(load_data().get("mode", "default"))


def set_mode(mode: str) -> Dict[str, Any]:
    return save_data(mode=mode)


def list_rules() -> List[Dict[str, str]]:
    return list(load_data().get("rules") or [])


def add_rule(pattern: str, effect: str = "allow") -> Dict[str, Any]:
    effect = effect.lower()
    if effect not in ("allow", "deny"):
        raise ValueError("effect must be 'allow' or 'deny'")
    rules = list_rules()
    rules.append({"pattern": pattern.strip(), "effect": effect})
    return save_data(rules=rules)


def remove_rule(index: int) -> Dict[str, Any]:
    rules = list_rules()
    if not (0 <= index < len(rules)):
        raise IndexError(f"rule #{index} does not exist ({len(rules)} rules)")
    del rules[index]
    return save_data(rules=rules)


def match_rule(pattern: str, action: str, summary: str = "") -> bool:
    """fnmatch ``pattern`` against several ``"<action> <summary>"`` shapes.

    Rules are written ``tool(glob)`` — e.g. ``execute_command(git *)``.
    Summaries carry free-text prefixes (``Run: ...``), so besides the
    exact ``action(summary)`` / ``action summary`` shapes we also match
    the inner glob as a substring of the summary: ``execute_command(rm
    *)`` hits ``Run: rm -rf /tmp/x``. Exact shapes still work for
    precise rules; the substring fallback makes short globs behave.
    """
    shapes = (
        f"{action}({summary})",
        f"{action} {summary}".strip(),
        action,
    )
    if any(fnmatch.fnmatch(shape, pattern) for shape in shapes):
        return True
    inner = _pattern_inner(pattern)
    if inner is not None and _action_matches(pattern, action):
        return fnmatch.fnmatch(summary, f"*{inner}*")
    return False


def _pattern_inner(pattern: str) -> Optional[str]:
    """The glob inside ``name(...)``, or None when the pattern has no parens."""
    if "(" in pattern and pattern.endswith(")"):
        return pattern.split("(", 1)[1][:-1]
    return None


def _action_matches(pattern: str, action: str) -> bool:
    """The ``name`` part of ``name(glob)`` (or the whole pattern) vs the action."""
    name = pattern.split("(", 1)[0] if "(" in pattern else pattern
    return fnmatch.fnmatch(action, name.strip() or "*")


def check(action: str, summary: str = "") -> Optional[bool]:
    """First matching rule wins: True = allow, False = deny, None = no rule."""
    for rule in list_rules():
        try:
            if match_rule(str(rule.get("pattern", "")), action, summary):
                return rule.get("effect") == "allow"
        except Exception:
            continue
    return None


def auto_allows(action: str) -> bool:
    """Heuristic for ``auto`` mode: allow unless the action looks destructive."""
    base = (action or "").split("(")[0].strip()
    return not base.startswith(_AUTO_DESTRUCTIVE_PREFIXES)
