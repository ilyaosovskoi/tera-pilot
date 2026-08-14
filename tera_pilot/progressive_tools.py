"""
Progressive Tool Disclosure — select_tools pattern.

Ported from Kimi Code's packages/agent-core/src/agent/context/dynamic-tools.ts.

Instead of sending ALL tool definitions to the LLM in every request
(which wastes tokens on tools the agent doesn't need for this
particular task), Tera Pilot now:

  1. Sends a compact "tool catalog" in the system prompt
     (name + one-line description for each tool)
  2. Provides a select_tools meta-tool that the agent can call
     to load full tool definitions on demand
  3. Tracks which tools have been loaded in the conversation history
  4. On compaction/resume, re-derives the loaded set from history

This dramatically reduces prompt size for simple tasks while still
giving the agent access to all tools when needed.

Tera Pilot-specific:
  - The catalog is injected by PromptBuilder.build()
  - The select_tools tool is only available when there are
    loadable (not-yet-loaded) tools
  - MCP tools are always loaded (they're dynamically discovered)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Tool Catalog ─────────────────────────────────────────────────────────

# Compact catalog: each tool gets name + one-line description.
# This is what the system prompt includes.
TOOL_CATALOG: Dict[str, str] = {
    # File operations
    "read_file": "Read a file from the workspace",
    "write_file": "Write or create a file (triggers diff review)",
    "str_replace": "Targeted string replacement in a file (preferred over write_file for edits)",
    "apply_diff": "Apply a unified diff patch to a file",
    "read_binary_file": "Read a binary file (returns base64)",
    "write_binary_file": "Write binary data to a file",
    "delete_file": "Delete a file or directory",
    "rename_file": "Rename a file or directory",
    "mkdir": "Create a directory",
    "file_info": "Get file metadata (size, modified date, type)",
    "undo_write": "Restore a file from the most recent backup",
    # Code execution
    "run_code": "Execute code in a sandboxed environment (python/js/bash)",
    "execute_command": "Execute a shell command (subject to whitelist)",
    # Search
    "search_project": "Search for text in project files",
    "grep": "Regex search across files (pattern, path, include filter)",
    "glob": "Find files matching a glob pattern",
    "list_files": "List files in a directory",
    "get_project_structure": "Get the project directory tree",
    # Git
    "git_status": "Show working tree status",
    "git_diff": "Show staged or unstaged diff",
    "git_stage": "Stage files for commit",
    "git_commit": "Commit staged changes",
    # Agent
    "spawn_subagent": "Spawn a child sub-agent for a sub-task",
    "spawn_multi_agents": "Spawn N sub-agents in parallel for independent sub-tasks",
    "watchdog_check": "Check if spawned sub-agents are stalled or repeating",
    "self_verify": "Verification pass at task close (re-read files, run tests, or spawn reviewer)",
    # Knowledge
    "get_skill": "Load the full text of a skill by ID",
    # Office (only in office section)
    "office_create": "Create a new Office document (.docx/.xlsx/.pptx)",
    "office_view": "View the structure of an Office document",
    "office_add_paragraph": "Add a paragraph to a Word document",
    "office_add_heading": "Add a heading to a Word document",
    "office_add_table": "Add a table to a Word/Excel document",
    "office_fill_table": "Fill a table with data",
    "office_add_sheet": "Add a sheet to an Excel workbook",
    "office_set_cell": "Set a cell value in Excel",
    "office_set_cell_format": "Format a cell in Excel",
    "office_add_chart": "Add a chart to Excel",
    "office_fill_sheet": "Fill a sheet with row data",
    "office_add_slide": "Add a slide to a PowerPoint",
    "office_add_text": "Add a text box to a PowerPoint slide",
    "office_add_shape": "Add a shape to a PowerPoint slide",
    "office_find_replace": "Find and replace text in an Office document",
    "office_save_as": "Save an Office document to a new path",
    # MCP (dynamically loaded — always included)
    "call_mcp_tool": "Call a tool from a connected MCP server",
}


def build_catalog_prompt() -> str:
    """Build the compact tool catalog for the system prompt.

    Returns a formatted string like:
      Available tools (call select_tools to load any):
        - read_file: Read a file from the workspace
        - write_file: Write or create a file
        ...
    """
    lines = ["Available tools (call select_tools to load full definitions before use):"]
    for name, desc in TOOL_CATALOG.items():
        lines.append(f"  - {name}: {desc}")
    return "\n".join(lines)


def build_select_tools_schema() -> Dict[str, Any]:
    """Build the JSON schema for the select_tools meta-tool."""
    return {
        "name": "select_tools",
        "description": (
            "Load full tool definitions by name. You MUST call this before "
            "using any tool for the first time in this conversation. "
            "Call with a list of tool names to load their full parameter schemas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tool_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of tool names to load",
                }
            },
            "required": ["tool_names"],
        },
    }


def build_search_tools_schema() -> Dict[str, Any]:
    """Build the JSON schema for the search_tools meta-tool."""
    return {
        "name": "search_tools",
        "description": (
            "Search the tool catalog by keyword. Returns matching tool names "
            "and their descriptions. Use select_tools to load full definitions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to search for in tool names and descriptions",
                }
            },
            "required": ["query"],
        },
    }


# ── Dynamic Tool State ───────────────────────────────────────────────────

# Matches <tools_added> ... </tools_added> and <tools_removed> ... </tools_removed>
_TOOLS_ADDED_RE = re.compile(r'<tools_added>\n?([\s\S]*?)\n?</tools_added>', re.MULTILINE)
_TOOLS_REMOVED_RE = re.compile(r'<tools_removed>\n?([\s\S]*?)\n?</tools_removed>', re.MULTILINE)


def fold_announced_tool_names(history_text: str) -> Set[str]:
    """Fold all loadable-tools announcements in history into the current set.

    Ported from Kimi's foldAnnouncedToolNames. Scans conversation
    history for <tools_added>/<tools_removed> blocks and computes the
    current set of loaded tools.

    This is self-healing: compaction, undo, and resume all work
    correctly because the announcements are in the history itself.
    """
    announced: Set[str] = set()

    # Process removals first, then additions (last wins)
    for match in _TOOLS_REMOVED_RE.finditer(history_text):
        body = match.group(1) or ""
        for line in body.split("\n"):
            name = line.strip()
            if name:
                announced.discard(name)

    for match in _TOOLS_ADDED_RE.finditer(history_text):
        body = match.group(1) or ""
        for line in body.split("\n"):
            name = line.strip()
            if name:
                announced.add(name)

    return announced


def render_loadable_tools_announcement(added: List[str],
                                        removed: List[str]) -> str:
    """Render a tools_added / tools_removed announcement for the history.

    Ported from Kimi's renderLoadableToolsAnnouncement.
    """
    sections = []
    if added:
        sections.append(f"<tools_added>\n" + "\n".join(added) + "\n</tools_added>")
    if removed:
        sections.append(f"<tools_removed>\n" + "\n".join(removed) + "\n</tools_removed>")
    sections.append(
        "Use the select_tools tool with exact names to load full tool "
        "definitions before calling them. Names listed as removed are "
        "no longer loadable. Fold all announcements in this conversation "
        "in order to get the current list."
    )
    return "\n\n".join(sections)


def get_loadable_tools(loaded: Set[str],
                       section: str = "general") -> List[str]:
    """Get tools that are NOT yet loaded (available for selection).

    Section filtering: some tools are only available in certain sections.
    """
    section_gates = {
        "office": {"office_create", "office_view", "office_add_paragraph",
                    "office_add_heading", "office_add_table", "office_fill_table",
                    "office_add_sheet", "office_set_cell", "office_set_cell_format",
                    "office_add_chart", "office_fill_sheet", "office_add_slide",
                    "office_add_text", "office_add_shape", "office_find_replace",
                    "office_save_as"},
        "heavy_code": {"spawn_subagent", "spawn_multi_agents", "watchdog_check"},
    }

    available = set(TOOL_CATALOG.keys())

    # Remove tools gated to other sections
    for sec, tools in section_gates.items():
        if sec != section:
            available -= tools

    # Remove already loaded
    available -= loaded

    # Remove always-loaded tools
    always_loaded = {"select_tools", "search_tools", "call_mcp_tool", "list_mcp_tools"}
    available -= always_loaded

    return sorted(available)


def _build_tool_definitions(runtime, tool_names: List[str]) -> str:
    """Build full tool definitions for the given tool names."""
    # Get the tool schema from the existing PromptBuilder
    try:
        from tera_pilot.agent_runtime import PromptBuilder
        pb = PromptBuilder(runtime)
        all_tools = pb.build_tool_list()
        tool_map = {t["name"]: t for t in all_tools}

        parts = []
        for name in tool_names:
            schema = tool_map.get(name)
            if schema:
                parts.append(f"### {name}\n{json.dumps(schema.get('input_schema', {}), indent=2)}")
            else:
                parts.append(f"### {name}\n[Schema not available for this tool]")
        return "\n\n".join(parts)
    except Exception as e:
        return f"[ERROR building definitions: {e}]"


def search_tools(query: str) -> str:
    """Search the tool catalog by keyword and return matching tools with descriptions."""
    if not query or not isinstance(query, str):
        return "[SEARCH_TOOLS ERROR] query must be a non-empty string"

    query_lower = query.lower().strip()
    if not query_lower:
        return "[SEARCH_TOOLS ERROR] query must be a non-empty string"

    matches = []
    for name, desc in TOOL_CATALOG.items():
        if query_lower in name.lower() or query_lower in desc.lower():
            matches.append((name, desc))

    if not matches:
        return f"[SEARCH_TOOLS] No tools found matching '{query}'"

    # Sort by name for consistent output
    matches.sort(key=lambda x: x[0])

    result_lines = [f"[SEARCH_TOOLS] Found {len(matches)} tool(s) matching '{query}':"]
    for name, desc in matches:
        result_lines.append(f"  - {name}: {desc}")

    return "\n".join(result_lines)