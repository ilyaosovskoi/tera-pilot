"""
tera_pilot.agent_runtime — agent runtime package (refactored).

Drop-in replacement for the legacy monolithic
`tera_pilot/agent_runtime.py` file. The public API is re-exported so
existing imports keep working:

    from tera_pilot.agent_runtime import (
        AgentRuntime, AgentWorker, AgentEvent, Task, TaskType,
        ToolCall, ToolName, TaskResult, AgentStep,
        ConversationMessage, ContextMemory, ToolEngine,
        PromptBuilder, OutputParser,
        TOOL_SCHEMA, SYSTEM_PROMPT,
        GENERAL_SYSTEM_SUFFIX, HEAVY_CODE_SYSTEM_SUFFIX,
    )

Internal layout (see REFACTORING_NOTES.md):
- types.py            — enums + dataclasses
- _helpers.py         — _sanitize_command
- context_memory.py   — ContextMemory
- diff_utils.py       — diff helpers
- tool_engine/        — ToolEngine (the big dispatcher)
- prompts.py          — system prompt + PromptBuilder
- parser.py           — OutputParser
- runtime.py          — AgentRuntime (the agent loop)
- worker.py           — AgentWorker (QThread wrapper)
"""


from .types import (
    TaskType,
    ToolName,
    AgentEvent,
    Task,
    ToolCall,
    AgentStep,
    TaskResult,
    ConversationMessage,
)
from ._helpers import _sanitize_command, ALLOWED_COMMANDS
from .context_memory import ContextMemory, _estimate_tokens
from .diff_utils import (
    _split_multi_file_diff,
    _apply_unified_diff,
    _str_replace_hint,
    _compute_diff_text,
    _backup_file,
)
from .tool_engine import ToolEngine
from .prompts import (
    TOOL_SCHEMA,
    SYSTEM_PROMPT,
    GENERAL_SYSTEM_SUFFIX,
    HEAVY_CODE_SYSTEM_SUFFIX,
    PromptBuilder,
)
from .parser import OutputParser, _warn_unknown_tools
from .runtime import AgentRuntime

# v2.2.0: AgentWorker is now plain threading.Thread (Qt removed), so
# the import is always safe. We still wrap it in try/except so a
# broken worker.py doesn't take down the whole package import —
# AgentRuntime itself doesn't depend on the worker.
try:
    from .worker import AgentWorker
except ImportError:  # pragma: no cover - defensive
    AgentWorker = None  # type: ignore[assignment,misc]

__all__ = [
    # types
    "TaskType", "ToolName", "AgentEvent",
    "Task", "ToolCall", "AgentStep", "TaskResult",
    "ConversationMessage",
    # memory + helpers
    "ContextMemory", "_estimate_tokens",
    "_sanitize_command", "ALLOWED_COMMANDS",
    # diff utils
    "_split_multi_file_diff", "_apply_unified_diff",
    "_str_replace_hint", "_compute_diff_text", "_backup_file",
    # tool engine
    "ToolEngine",
    # prompts + parser
    "TOOL_SCHEMA", "SYSTEM_PROMPT",
    "GENERAL_SYSTEM_SUFFIX", "HEAVY_CODE_SYSTEM_SUFFIX",
    "PromptBuilder", "OutputParser", "_warn_unknown_tools",
    # runtime + worker
    "AgentRuntime", "AgentWorker",
]
