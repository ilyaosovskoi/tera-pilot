"""Tera Pilot v2 Agent package — refactored, modular, with optional Rust acceleration.

This is the new home for the agent runtime. The legacy `tera_pilot/agent_runtime.py`
remains as a back-compat shim; new code should import from `tera_pilot.agent`.

Submodules:
- `tera_pilot.agent.runtime` — v2 runtime entry point (delegates to legacy by default
  for safety, opt-in to new path via `AgentRuntimeV2`).
- `tera_pilot.agent.actor` — asyncio-based ChatStateActor (port of Grok's
  ChatStateActor pattern, single-task-ownership of state).
- `tera_pilot.agent.subagent_v2` — sub-agents with toolset-level read-only guarantee.
- `tera_pilot.agent.interjection` — mid-turn user interjection buffer.
- `tera_pilot.agent.sandbox` — process sandbox (wraps Rust `tera_pilot_native.sandbox`).
- `tera_pilot.agent.circuit_breaker` — provider call circuit breaker.
- `tera_pilot.agent.compaction_v2` — three-tier compaction.
- `tera_pilot.agent.acp_server` — Agent Client Protocol endpoint.
- `tera_pilot.agent.encrypted_prompt` — encrypted prompt templates for enterprise.
- `tera_pilot.agent.native` — loader for the Rust extension module, with graceful
  pure-Python fallback.

Public API:
    from tera_pilot.agent import AgentRuntimeV2, CancelToken, InterjectionBuffer

Version: 2.3.3
"""

from .native import (
    NATIVE_AVAILABLE,
    native_version,
    get_native_module,
)
from .runtime import (
    AgentRuntimeV2,
    RunResult,
    RunStopReason,
)
from .actor import ChatStateActor, ChatStateCommand
from .interjection import InterjectionBuffer, InterjectionEntry
from .sandbox import (
    apply_sandbox,
    current_sandbox_profile,
    SandboxProfile,
)
from .circuit_breaker import (
    CircuitBreakerRegistry,
    CircuitBreaker,
    RetryDisposition,
)
from .compaction_v2 import CompactionEngine, CompactionPolicy, ConversationItem
from .subagent_v2 import (
    SubagentDefinition,
    BUILTIN_SUBAGENTS,
    spawn_subagent,
    ExploreSubagent,
    PlanSubagent,
    GeneralPurposeSubagent,
)
from .encrypted_prompt import EncryptedPromptStore, EncryptedPromptError
from .acp_server import ACPServer

__version__ = "2.3.3"

def get_circuit_breaker_registry():
    """Return a new CircuitBreakerRegistry instance."""
    return CircuitBreakerRegistry()


__all__ = [
    "__version__",
    # Native
    "NATIVE_AVAILABLE",
    "native_version",
    "get_native_module",
    # Runtime
    "AgentRuntimeV2",
    "RunResult",
    "RunStopReason",
    # Actor
    "ChatStateActor",
    "ChatStateCommand",
    # Interjection
    "InterjectionBuffer",
    "InterjectionEntry",
    # Sandbox
    "apply_sandbox",
    "current_sandbox_profile",
    "SandboxProfile",
    # Circuit breaker
    "CircuitBreakerRegistry",
    "CircuitBreaker",
    "RetryDisposition",
    "get_circuit_breaker_registry",
    # Compaction
    "CompactionEngine",
    "CompactionPolicy",
    "ConversationItem",
    # Sub-agents
    "SubagentDefinition",
    "BUILTIN_SUBAGENTS",
    "spawn_subagent",
    "ExploreSubagent",
    "PlanSubagent",
    "GeneralPurposeSubagent",
    # Encrypted prompts
    "EncryptedPromptStore",
    "EncryptedPromptError",
    # ACP
    "ACPServer",
]
