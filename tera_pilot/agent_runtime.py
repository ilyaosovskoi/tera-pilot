"""
Legacy shim for tera_pilot.agent_runtime.

The original 6217-line monolith has been refactored into the
`tera_pilot/agent_runtime/` package. This file re-exports the full
public API so existing imports keep working unchanged:

    from tera_pilot.agent_runtime import AgentRuntime   # still works
    from tera_pilot.agent_runtime import (
        AgentRuntime, AgentWorker, AgentEvent, Task, TaskType,
        ToolCall, ToolName, TaskResult, AgentStep,
        ConversationMessage, ContextMemory, ToolEngine,
        PromptBuilder, OutputParser,
        TOOL_SCHEMA, SYSTEM_PROMPT,
        GENERAL_SYSTEM_SUFFIX, HEAVY_CODE_SYSTEM_SUFFIX,
    )

See tera_pilot/agent_runtime/__init__.py for the package layout
and REFACTORING_NOTES.md for the migration map.
"""

from tera_pilot.agent_runtime import *  # noqa: F401,F403
from tera_pilot.agent_runtime import __all__  # noqa: F401
