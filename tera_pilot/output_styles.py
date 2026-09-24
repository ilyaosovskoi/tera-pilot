"""
Output styles — file-based prompt suffixes controlling answer shape.

A style is a short suffix appended to the system prompt (after any
verbosity suffix). Built-ins (``normal``/``brief``/``detailed``/``fast``)
preserve the historical verbosity behaviour byte-for-byte; custom styles
are Markdown files so users can craft and share their own voice:

  ~/.tera_pilot/styles/<name>.md          (user-global)
  <project>/.tera_pilot/styles/<name>.md  (project, wins on clash)

File format — optional frontmatter, then the suffix body::

    ---
    name: report
    description: Structured status reports.
    ---
    ## Output style: report
    Answer with sections: Summary, Changes, Verification, Next steps.

Without frontmatter the filename (stem) is the name and the first
``# `` heading is the description. The active style name lives in
config.json as ``agent_output_style`` (``normal`` = empty suffix).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Built-in styles. Bodies are the historical verbosity suffixes —
#: ``normal`` stays empty so default prompts are byte-identical.
BUILTIN_STYLES: Dict[str, Dict[str, str]] = {
    "normal": {
        "name": "Normal",
        "description": "Default balanced output.",
        "suffix": "",
    },
    "brief": {
        "name": "Brief",
        "description": "Terse output: one-line status per action.",
        "suffix": (
            "\n\n## Output style: brief\n"
            "Be terse. One-line status per action, no preamble, no summaries "
            "longer than 3 lines. Still emit tool calls and final_answer normally."
        ),
    },
    "detailed": {
        "name": "Detailed",
        "description": "Explain reasoning before consequential calls.",
        "suffix": (
            "\n\n## Output style: detailed\n"
            "Explain your reasoning briefly before each consequential tool call "
            "(1-2 sentences), and close with a short summary of what changed "
            "and what to verify next."
        ),
    },
    "fast": {
        "name": "Fast",
        "description": "Minimal chatter, minimal tool calls.",
        "suffix": (
            "\n\n## Output style: fast\n"
            "Minimize chatter and minimize tool calls: batch independent reads, "
            "prefer grep/glob over opening files, skip re-verification reads "
            "you already have in context."
        ),
    },
}


@dataclass
class OutputStyle:
    id: str
    name: str
    description: str
    suffix: str = ""
    source: str = "builtin"
    path: str = ""
    builtin: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name,
                "description": self.description, "source": self.source,
                "builtin": self.builtin}


def _parse_style_file(path: Path) -> Optional[OutputStyle]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta: Dict[str, str] = {}
    body = content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            front = content[3:end]
            body = content[end + 3:].lstrip("\n")
            for line in front.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip().lower()] = value.strip()
    name = meta.get("name", "")
    description = meta.get("description", "")
    if not name:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                name = stripped[2:].strip()[:80]
                break
    stem = path.stem
    return OutputStyle(
        id=stem.lower().replace(" ", "-"),
        name=name or stem.replace("-", " ").replace("_", " ").title(),
        description=description or "",
        suffix=body.strip()[:2000],
        source="file",
        path=str(path),
    )


def _iter_style_dir(style_dir: Path) -> List[OutputStyle]:
    out = []
    if not style_dir.is_dir():
        return out
    for filepath in sorted(style_dir.glob("*.md")):
        try:
            style = _parse_style_file(filepath)
        except Exception:
            continue
        if style is not None and style.suffix:
            out.append(style)
    return out


def load_all_styles(project_root: Optional[str] = None) -> List[OutputStyle]:
    """Built-ins first, then user-global, then project (override by id)."""
    by_id: Dict[str, OutputStyle] = {}
    for style_id, spec in BUILTIN_STYLES.items():
        by_id[style_id] = OutputStyle(
            id=style_id, name=spec["name"], description=spec["description"],
            suffix=spec["suffix"], source="builtin", builtin=True)
    user_dir = Path(os.path.expanduser("~/.tera_pilot")) / "styles"
    for style in _iter_style_dir(user_dir):
        style.source = "global"
        by_id[style.id] = style
    if project_root:
        proj_dir = Path(project_root).expanduser() / ".tera_pilot" / "styles"
        for style in _iter_style_dir(proj_dir):
            style.source = "project"
            by_id[style.id] = style
    return list(by_id.values())


def get_style_suffix(style_id: str,
                     project_root: Optional[str] = None) -> str:
    """Prompt suffix for a style id ("" when unknown). Never raises."""
    try:
        wanted = (style_id or "normal").strip().lower()
        for style in load_all_styles(project_root):
            if style.id == wanted:
                return style.suffix
    except Exception as exc:
        logger.debug("[styles] resolve failed: %s", exc)
    return ""


def active_style_id() -> str:
    try:
        from tera_pilot.utils import load_config
        style = str((load_config() or {}).get("agent_output_style", "normal"))
        return style.strip().lower() or "normal"
    except Exception:
        return "normal"


def set_active_style(style_id: str,
                     project_root: Optional[str] = None) -> Dict[str, Any]:
    style_id = (style_id or "").strip().lower()
    known = {s.id for s in load_all_styles(project_root)}
    if style_id not in known:
        return {"ok": False,
                "error": f"unknown style {style_id!r} (known: {sorted(known)})"}
    try:
        from tera_pilot.utils import load_config, save_config
        cfg = load_config() or {}
        cfg["agent_output_style"] = style_id
        save_config(cfg)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "style": style_id}
