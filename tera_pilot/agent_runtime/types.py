"""
Type definitions for the Tera Pilot agent runtime.

Contains:
- TaskType, ToolName, AgentEvent enums
- Task, ToolCall, AgentStep, TaskResult, ConversationMessage dataclasses

These are the "vocabulary" types shared across the agent runtime
package. They have no internal dependencies and are safe to import
from any other module.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskType(Enum):
    WRITE = "write"
    EDIT = "edit"
    REFACTOR = "refactor"
    TEST = "test"
    ANALYZE = "analyze"
    DEBUG = "debug"
    PLAN = "plan"
    CHAT = "chat"
    AGENTIC = "agentic"


class ToolName(Enum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    RUN_CODE = "run_code"
    SEARCH_PROJECT = "search_project"
    LIST_FILES = "list_files"
    APPLY_DIFF = "apply_diff"
    EXECUTE_COMMAND = "execute_command"
    GET_PROJECT_STRUCTURE = "get_project_structure"
    DELETE_FILE = "delete_file"
    RENAME_FILE = "rename_file"
    MKDIR = "mkdir"
    READ_BINARY_FILE = "read_binary_file"
    WRITE_BINARY_FILE = "write_binary_file"
    FILE_INFO = "file_info"
    UNDO_WRITE = "undo_write"
    # v1.0.5: targeted string replacement — preferred over full
    # write_file for edits (per качество_кода_llm.md §3.1). Forces the
    # model to localise the change instead of rewriting the whole file,
    # and gives a deterministic verification: either old_str is found
    # (patch applies cleanly) or it is not (model hallucinated context).
    STR_REPLACE = "str_replace"
    # v1.0.11: git tools — direct project access like Claude Code.
    # The agent can check git status, see diffs, stage files, and
    # commit. This makes Tera Pilot closer to an autonomous dev assistant
    # than a chat bot — the user says "commit my changes" and the
    # agent does it directly via the git_status / git_diff / git_commit
    # tools, without asking the user to run commands manually.
    GIT_STATUS = "git_status"
    GIT_DIFF = "git_diff"
    GIT_STAGE = "git_stage"
    GIT_COMMIT = "git_commit"
    # v1.0.11: skill tools — the agent can request the full body of a
    # skill by id. The skill catalog (id + name + description) is
    # injected into the system prompt so the agent knows what's
    # available. When it decides a skill fits the task, it calls
    # get_skill to pull the full instructions into context.
    GET_SKILL = "get_skill"
    # v1.1.0: MCP — call an external MCP server tool (filesystem,
    # github, browser, etc.). Available in ALL sections (general,
    # heavy_code, office) — the catalog is injected into the system
    # prompt dynamically by MCPManager.catalog_prompt().
    CALL_MCP_TOOL = "call_mcp_tool"
    # v1.1.0: Multi-agent — spawn a sub-agent for a sub-task. The
    # sub-agent runs in its own AgentRuntime instance with a narrower
    # scope (read-only by default) and returns its final answer as the
    # observation. Available in Heavy Code section.
    SPAWN_SUBAGENT = "spawn_subagent"
    # v1.1.0: Multi-agent parallel — spawn N sub-agents in parallel
    # for independent sub-tasks (e.g. "refactor these 3 files in
    # parallel"). Returns each sub-agent's result. Available in Heavy
    # Code section.
    SPAWN_MULTI_AGENTS = "spawn_multi_agents"
    # v1.2.0: Office Worker tools — gated to the `office` section. The
    # actual implementation lives in tera_pilot/office_worker.py (built from
    # scratch on top of python-docx / openpyxl / python-pptx; no code
    # copied from any external Office CLI).
    OFFICE_CREATE = "office_create"
    OFFICE_VIEW = "office_view"
    OFFICE_ADD_PARAGRAPH = "office_add_paragraph"
    OFFICE_ADD_HEADING = "office_add_heading"
    OFFICE_ADD_TABLE = "office_add_table"
    OFFICE_FILL_TABLE = "office_fill_table"
    OFFICE_ADD_SHEET = "office_add_sheet"
    OFFICE_SET_CELL = "office_set_cell"
    OFFICE_SET_CELL_FORMAT = "office_set_cell_format"
    OFFICE_ADD_CHART = "office_add_chart"
    OFFICE_FILL_SHEET = "office_fill_sheet"
    OFFICE_ADD_SLIDE = "office_add_slide"
    OFFICE_ADD_TEXT = "office_add_text"
    OFFICE_ADD_SHAPE = "office_add_shape"
    OFFICE_FIND_REPLACE = "office_find_replace"
    OFFICE_SAVE_AS = "office_save_as"
    # v1.2.0: Self-verify — a meta-tool that triggers an explicit
    # verification pass at task close (General section). It re-reads
    # the files the agent touched and checks the stated goal against
    # the actual state. Inspired by architect-loop's "fresh-context
    # verifier subagent" pattern, but kept lightweight (single LLM
    # call, not a full subagent) so it works in all sections.
    SELF_VERIFY = "self_verify"
    # v1.2.1-fix (review §4.2): explicit watchdog probe tool. Lets the
    # agent ask "are any of my spawned sub-agents stuck?" between waves
    # instead of relying on the runtime to inject the result silently.
    # Returns the same typed evidence string as _watchdog_check.
    WATCHDOG_CHECK = "watchdog_check"
    # v1.2.1-fix (review §4.4): agentic-search tools — model-driven
    # grep/glob over the workspace, complementing the heuristic file
    # auto-attach in ContextManager. Inspired by Claude Code's
    # "agentic search instead of pre-loading" pattern.
    GREP = "grep"
    GLOB = "glob"
    # v2.0.0: Progressive tool disclosure — search tool catalog
    SEARCH_TOOLS = "search_tools"
    # v2.1.0: web search/fetch — see G18. Routes through MCPManager
    # (web_search is a thin wrapper over an MCP search server) or
    # falls back to a free no-API-key backend. web_fetch is a direct
    # HTTP GET + HTML-to-text extraction. Available in ALL sections
    # (general, heavy_code, office) — same visibility rule as
    # call_mcp_tool. The 'researcher' role whitelist (see
    # ToolEngine.ROLE_TOOL_WHITELIST) is read-only by construction
    # and explicitly includes web_search/web_fetch.
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"


class AgentEvent(Enum):
    PLAN_CREATED = "plan_created"
    STEP_STARTED = "step_started"
    STEP_DONE = "step_done"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    THOUGHT = "thought"
    TOKEN_DELTA = "token_delta"
    ERROR = "error"
    DONE = "done"
    GUARDIAN_REVIEW = "guardian_review"


@dataclass
class Task:
    type: TaskType
    description: str
    context: Optional[str] = None
    file_path: Optional[str] = None
    language: str = "python"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    name: ToolName
    args: Dict[str, Any]
    result: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class AgentStep:
    thought: str
    action: Optional[ToolCall] = None
    observation: str = ""
    is_final: bool = False


@dataclass
class TaskResult:
    success: bool
    output: str
    error: Optional[str] = None
    iterations: int = 0
    steps: List[AgentStep] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    plan: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # v1.1.3-fix (bug 1.12): removed the unused code_blocks / primary_code
    # properties. They were defined but never read by _run_agent_loop or
    # any caller — dead code that just cluttered the class. If a future
    # UI wants to highlight code blocks in the result, it can parse them
    # from ``output`` directly.


@dataclass
class ConversationMessage:
    role: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "content": self.content, "metadata": self.metadata}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationMessage":
        return cls(
            role=data["role"],
            content=data["content"],
            metadata=data.get("metadata", {}),
        )


# ── Context Memory (with Persistence) ──────────────────────────────────

