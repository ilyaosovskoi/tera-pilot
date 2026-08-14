"""Sub-agent v2 — built-in `explore` / `plan` / `general-purpose` with toolset-level read-only guarantee.

Ported from Grok Build's `BUILTIN_SUBAGENTS` design:
- The `explore` subagent literally has NO `bash` / `write_file` / `str_replace`
  tools in its toolset — read-only is enforced at the toolset schema level,
  not in the runtime dispatch.
- The `plan` subagent is also read-only (no `bash`/`search_replace`).
- The `general-purpose` subagent has all tools.

This is structurally cleaner than Tera Pilot v1's role-based whitelist (which was
enforced at dispatch time — fragile, easy to bypass).

Definitions can be extended via `.tera_pilot/agents/*.md` (Markdown + YAML frontmatter),
following Grok Build's discovery convention.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sub-agent definitions.
# ---------------------------------------------------------------------------


@dataclass
class SubagentDefinition:
    """A sub-agent definition — name, description, allowed tool names, prompt."""

    name: str
    description: str
    tools: List[str]  # Tool name whitelist — enforced at toolset schema level.
    prompt_template: str
    default_model: Optional[str] = None
    isolation_mode: Optional[str] = None  # None | "worktree"


# Built-in sub-agents. Ported from Grok Build's `BUILTIN_SUBAGENTS` in
# `crates/common/xai-tool-types/src/task.rs:685-878`.
#
# The tool lists below are enforced at the toolset schema level: only these
# tools are advertised to the LLM, so the model literally cannot call tools
# that aren't in the list. This is "read-only by toolset construction".

_READ_ONLY_TOOLS = [
    "read_file",
    "read_binary_file",
    "search_project",
    "grep",
    "glob",
    "list_files",
    "get_project_structure",
    "file_info",
    "git_status",
    "git_diff",
    "list_mcp_tools",
    "get_skill",
    "select_tools",
]

_ALL_TOOLS = _READ_ONLY_TOOLS + [
    "write_file",
    "str_replace",
    "apply_diff",
    "write_binary_file",
    "delete_file",
    "rename_file",
    "mkdir",
    "run_code",
    "execute_command",
    "git_stage",
    "git_commit",
    "call_mcp_tool",
    "spawn_subagent",
    "watchdog_check",
    "self_verify",
    "undo_write",
]


EXPLORE_SUBAGENT = SubagentDefinition(
    name="explore",
    description=(
        "A read-only code exploration subagent. Use this to map out unfamiliar "
        "code, find all references, or understand architecture before making "
        "changes. Cannot modify files or execute commands."
    ),
    tools=_READ_ONLY_TOOLS,
    prompt_template=(
        "You are an `explore` subagent. === READ-ONLY MODE ===\n"
        "You have NO file editing tools, NO bash, NO network-mutating tools.\n"
        "Your job: read code, search for symbols, and report findings concisely.\n"
        "Always cite file paths and line numbers.\n"
        "Do NOT propose changes — just describe the current state."
    ),
)

PLAN_SUBAGENT = SubagentDefinition(
    name="plan",
    description=(
        "A read-only software architect subagent. Explores the codebase and "
        "designs implementation plans. Use this before any non-trivial change."
    ),
    tools=_READ_ONLY_TOOLS,
    prompt_template=(
        "You are a `plan` subagent — a read-only software architect.\n"
        "Your job: explore the codebase and design an implementation plan.\n\n"
        "Output structure:\n"
        "1. Goal (1-2 sentences)\n"
        "2. Critical Files for Implementation (list with paths)\n"
        "3. Step-by-step plan (numbered)\n"
        "4. Risks and edge cases\n"
        "5. Test plan\n\n"
        "Do NOT write code. Do NOT modify files."
    ),
)

GENERAL_PURPOSE_SUBAGENT = SubagentDefinition(
    name="general-purpose",
    description=(
        "A general-purpose subagent with all tools. Use for tasks that need "
        "to both explore and modify."
    ),
    tools=_ALL_TOOLS,
    prompt_template=(
        "You are a `general-purpose` subagent.\n"
        "Complete the assigned task directly. Do what was asked; nothing more, "
        "nothing less.\n"
        "When you are done, report concisely what you did."
    ),
)

BUILTIN_SUBAGENTS: List[SubagentDefinition] = [
    EXPLORE_SUBAGENT,
    PLAN_SUBAGENT,
    GENERAL_PURPOSE_SUBAGENT,
]


# ---------------------------------------------------------------------------
# Built-in lookup.
# ---------------------------------------------------------------------------


def get_builtin(name: str) -> Optional[SubagentDefinition]:
    for s in BUILTIN_SUBAGENTS:
        if s.name == name:
            return s
    return None


# ---------------------------------------------------------------------------
# User-defined sub-agents (.tera_pilot/agents/*.md)
# ---------------------------------------------------------------------------


def discover_user_subagents(workspace_root: Optional[str] = None) -> List[SubagentDefinition]:
    """Discover user-defined sub-agents in `.tera_pilot/agents/*.md`.

    File format:
        ---
        name: explore-with-deps
        description: Explore a Python module and its dependencies
        tools: read_file, grep, glob, list_files
        model: claude-sonnet-5
        isolation: worktree
        ---
        You are an `explore-with-deps` subagent...
    """
    out: List[SubagentDefinition] = []
    search_paths = []
    if workspace_root:
        search_paths.append(Path(workspace_root) / ".tera_pilot" / "agents")
        search_paths.append(Path(workspace_root) / ".claude" / "agents")
    search_paths.append(Path.home() / ".tera_pilot" / "agents")
    search_paths.append(Path.home() / ".claude" / "agents")

    seen_names = set()
    for p in search_paths:
        if not p.is_dir():
            continue
        for md in sorted(p.glob("*.md")):
            try:
                d = _parse_agent_md(md.read_text(encoding="utf-8"))
                if d and d.name not in seen_names:
                    out.append(d)
                    seen_names.add(d.name)
            except Exception as e:
                logger.warning("failed to parse %s: %s", md, e)
    return out


def _parse_agent_md(text: str) -> Optional[SubagentDefinition]:
    """Parse a Markdown + YAML frontmatter sub-agent definition."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    frontmatter = text[3:end]
    body = text[end + 4 :].strip()
    fields: Dict[str, str] = {}
    for line in frontmatter.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fields[k.strip()] = v.strip()
    name = fields.get("name")
    if not name:
        return None
    tools_raw = fields.get("tools", "")
    tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
    return SubagentDefinition(
        name=name,
        description=fields.get("description", ""),
        tools=tools or _ALL_TOOLS,
        prompt_template=body or fields.get("description", ""),
        default_model=fields.get("model"),
        isolation_mode=fields.get("isolation"),
    )


# ---------------------------------------------------------------------------
# Spawn — delegates to legacy AgentRuntime for now (v2 wiring in progress).
# ---------------------------------------------------------------------------


def spawn_subagent(
    runtime,
    subagent_type: str,
    prompt: str,
    *,
    run_in_background: bool = False,
    resume_from: Optional[str] = None,
    isolation: Optional[str] = None,
) -> Any:
    """Spawn a sub-agent of the given type.

    Args:
        runtime: an AgentRuntime instance (v1 legacy or v2).
        subagent_type: one of the built-in names or a user-defined name.
        prompt: the task prompt.
        run_in_background: if True, return immediately with a handle.
        resume_from: optional subagent id to resume.
        isolation: optional isolation mode ("worktree").

    Returns:
        SubagentHandle (future-like) — see `tera_pilot.session.subagent_host.SubagentHandle`.

    Raises:
        ValueError: if `subagent_type` is unknown.
    """
    # Look up the definition.
    sub = get_builtin(subagent_type)
    if sub is None:
        user_subs = discover_user_subagents(getattr(runtime, "workspace_root", None))
        for u in user_subs:
            if u.name == subagent_type:
                sub = u
                break
    if sub is None:
        raise ValueError(f"unknown subagent_type: {subagent_type!r}")

    # The legacy `_spawn_subagent` accepts a `role` and uses role-based whitelist.
    # We translate: pass `role=subagent_type` and also set the explicit tool whitelist.
    if hasattr(runtime, "tools"):
        runtime.tools.set_role_whitelist(subagent_type, sub.tools)
    return runtime.tools._spawn_subagent(
        task=prompt,
        role=subagent_type,
        subagent_prompt=sub.prompt_template,
        model=sub.default_model,
        run_in_background=run_in_background,
    )


# Backwards-compat aliases for the public API.
ExploreSubagent = EXPLORE_SUBAGENT
PlanSubagent = PLAN_SUBAGENT
GeneralPurposeSubagent = GENERAL_PURPOSE_SUBAGENT
