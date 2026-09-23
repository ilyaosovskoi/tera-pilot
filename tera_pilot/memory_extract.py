"""
Project memory — persistent facts across sessions, plus heuristic extraction.

Two scopes, plain Markdown (hand-editable, no database):
  project  ``<project>/.tera_pilot/MEMORY.md``
  global   ``~/.tera_pilot/MEMORY.md``

``extract_facts()`` scans a conversation transcript for preference-like
statements ("remember that …", "always …", "never …", "prefer …",
"my <x> is …") and returns candidates with a rough confidence. Nothing
is written automatically: ``/remember`` (or the agent via the same
helpers) stores a fact after the user confirms. ``facts_for_prompt()``
renders the top facts for system-prompt injection, bounded in size so
memory guidance can never dominate the prompt.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_FACTS_IN_PROMPT = 8
MAX_FACT_CHARS = 200
MAX_PROMPT_CHARS = 1500

_PATTERNS = [
    # (regex, confidence)
    (re.compile(r"\bremember that\b(.{8,200})", re.IGNORECASE), 0.9),
    (re.compile(r"\bplease remember\b(.{8,200})", re.IGNORECASE), 0.9),
    (re.compile(r"\b(?:always|never)\b(.{8,200})", re.IGNORECASE), 0.8),
    (re.compile(r"\bprefer\b(.{8,200})", re.IGNORECASE), 0.7),
    (re.compile(r"\bmy (?:project|repo|codebase|team|org|company)\b.{0,20}\b(is|uses|runs on|requires)\b(.{4,160})",
                re.IGNORECASE), 0.6),
    (re.compile(r"\bwe use\b(.{8,160})", re.IGNORECASE), 0.6),
]


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" .;:\t")
    return text[:MAX_FACT_CHARS]


def extract_facts(transcript: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Heuristic fact candidates from a transcript. Never raises."""
    found: List[Dict[str, Any]] = []
    seen = set()
    try:
        for pattern, confidence in _PATTERNS:
            for match in pattern.finditer(transcript or ""):
                fact = _clean(match.group(match.lastindex or 0))
                if len(fact) < 8 or fact.lower() in seen:
                    continue
                seen.add(fact.lower())
                found.append({"fact": fact, "confidence": confidence})
                if len(found) >= limit:
                    return sorted(found, key=lambda f: -f["confidence"])
    except Exception as exc:
        logger.debug("[memory] extract failed: %s", exc)
    return sorted(found, key=lambda f: -f["confidence"])


def project_memory_path(project_root: Optional[str] = None) -> Path:
    base = Path(project_root).expanduser() if project_root \
        else Path.cwd()
    return base / ".tera_pilot" / "MEMORY.md"


def global_memory_path() -> Path:
    return Path(os.path.expanduser("~/.tera_pilot")) / "MEMORY.md"


def read_facts(path: Path, limit: int = 50) -> List[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    facts = [ln[2:].strip() for ln in lines if ln.startswith("- ")]
    return facts[-limit:]


def remember_fact(fact: str, project_root: Optional[str] = None,
                  scope: str = "project") -> Dict[str, Any]:
    """Append a fact (deduplicated, case-insensitive)."""
    fact = _clean(fact)
    if len(fact) < 3:
        return {"ok": False, "error": "fact is too short"}
    path = project_memory_path(project_root) if scope == "project" \
        else global_memory_path()
    existing = read_facts(path, limit=500)
    if fact.lower() in {f.lower() for f in existing}:
        return {"ok": True, "path": str(path), "duplicate": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Project memory (Tera Pilot)\n\n" if scope == "project" \
        else "# Global memory (Tera Pilot)\n\n"
    prefix = "" if path.exists() else header
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{prefix}- {fact}\n")
    return {"ok": True, "path": str(path), "duplicate": False}


def forget_fact(index: int, project_root: Optional[str] = None,
                scope: str = "project") -> Dict[str, Any]:
    path = project_memory_path(project_root) if scope == "project" \
        else global_memory_path()
    facts = read_facts(path, limit=1000)
    if not (0 <= index < len(facts)):
        return {"ok": False, "error": f"no fact #{index}"}
    del facts[index]
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Project memory (Tera Pilot)\n\n" if scope == "project" \
        else "# Global memory (Tera Pilot)\n\n"
    path.write_text(header + "".join(f"- {f}\n" for f in facts),
                    encoding="utf-8")
    return {"ok": True, "path": str(path)}


def facts_for_prompt(project_root: Optional[str] = None) -> str:
    """Render global + project facts for prompt injection (bounded)."""
    facts = read_facts(global_memory_path())[-MAX_FACTS_IN_PROMPT:]
    facts += [f for f in read_facts(project_memory_path(project_root))
              if f not in facts][-MAX_FACTS_IN_PROMPT:]
    facts = facts[:MAX_FACTS_IN_PROMPT]
    if not facts:
        return ""
    out = "## Remembered facts\n" + "".join(f"- {f}\n" for f in facts)
    return out[:MAX_PROMPT_CHARS]
