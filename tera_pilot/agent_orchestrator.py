"""
Agent Orchestrator — connects Kimi-style modular architecture to Tera Pilot's runtime.

TERA_PILOT v2 NOTE: This module is preserved as the v1.3 "monkey-patching" bridge.
For new code, prefer `tera_pilot.agent.AgentRuntimeV2` which provides a cleaner
opt-in path: the v2 runtime wraps a legacy AgentRuntime and adds v2 features
(actor model, interjection buffer, sandbox, three-tier compaction, circuit
breaker, sub-agent v2 with toolset-level read-only guarantee) WITHOUT
monkey-patching.

This v1.3 orchestrator is still useful when:
- You need to keep using the legacy `_run_agent_loop` (e.g. for the Qt
  AgentWorker thread) and want progressive tool disclosure + Kimi-style
  SubagentBatch on top of it.
- You are migrating incrementally and want both paths available.

When `tera_pilot.agent.AgentRuntimeV2` is used, this orchestrator is NOT needed.

---

This module is the bridge between the new modular components
(TurnLoop, ToolScheduler, SubagentBatch, CompactionManager, SwarmMode,
ProgressiveTools, CancelToken) and the existing Tera Pilot AgentRuntime.

Instead of rewriting the 5750-line agent_runtime.py from scratch,
we patch it at key integration points:

  1. _run_agent_loop() — now uses TurnLoop + ToolScheduler for parallel
     tool execution with conflict detection
  2. spawn_subagent/spawn_multi_agents — now use SubagentHost + SubagentBatch
     for rate-limited, resumable execution
  3. Context compaction — now uses CompactionManager (full + micro, media-aware)
  4. Cancellation — now uses CancelToken (AbortSignal pattern)
  5. Swarm mode — now uses SwarmMode with toggle and auto-exit
  6. Tool dispatch — now supports progressive tool disclosure

Usage:
  # At startup (in web_bridge.py or app.py):
  from tera_pilot.agent_orchestrator import patch_runtime
  patch_runtime(runtime_instance)

  # v2 alternative (cleaner):
  from tera_pilot.agent import AgentRuntimeV2
  runtime = AgentRuntimeV2.from_legacy(legacy_runtime)
  await runtime.run_turn(task)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def patch_runtime(runtime) -> None:
    """Patch an AgentRuntime instance with Kimi-style modular architecture.

    This function monkey-patches key methods on the runtime to use
    the new modular components while preserving all existing behavior.

    Args:
        runtime: an AgentRuntime instance
    """
    _patch_with_cancel_token(runtime)
    _patch_with_tool_scheduler(runtime)
    _patch_with_subagent_host(runtime)
    _patch_with_compaction(runtime)
    _patch_with_swarm_mode(runtime)
    _patch_with_progressive_tools(runtime)
    logger.info("[orchestrator] AgentRuntime patched with modular architecture")


# ── 1. Cancel Token (AbortSignal pattern) ──────────────────────────────

def _patch_with_cancel_token(runtime) -> None:
    """Replace threading.Event-based cancellation with CancelToken."""
    from tera_pilot.loop.tool_scheduler import CancelToken

    runtime._cancel_token = CancelToken()
    runtime._original_cancel_check = runtime.is_cancelled
    runtime.is_cancelled = lambda: runtime._cancel_token.is_cancelled
    runtime._original_set_cancel_check = runtime.set_cancel_check

    def new_set_cancel_check(check_fn):
        """Wrap the cancel check so both old and new patterns work."""
        runtime._original_set_cancel_check(check_fn)
        # Also link the old pattern to the token
        def _sync():
            if check_fn and check_fn():
                runtime._cancel_token.cancel("user_stop")
        runtime._cancel_token._legacy_check = check_fn
        runtime._cancel_token.on_cancel(lambda reason: None)
        # Periodic sync (ugly but necessary for backward compat)
        def _poll():
            while not runtime._cancel_token.is_cancelled:
                if check_fn and check_fn():
                    runtime._cancel_token.cancel("user_stop")
                    break
                time.sleep(0.25)
        t = threading.Thread(target=_poll, daemon=True, name="cancel-sync")
        t.start()

    runtime.set_cancel_check = new_set_cancel_check
    logger.info("[orchestrator] CancelToken installed")


# ── 2. Tool Scheduler (parallel execution) ─────────────────────────────

def _patch_with_tool_scheduler(runtime) -> None:
    """Add ToolScheduler for parallel tool execution."""
    from tera_pilot.loop.tool_scheduler import ToolScheduler

    # Store the original _dispatch for single-tool fallback
    _original_dispatch = runtime.tools._dispatch

    def _tool_execute_fn(name: str, args: Dict[str, Any], cancel_token) -> str:
        """Adapter: execute a single tool call through ToolEngine._dispatch."""
        if cancel_token and cancel_token.is_cancelled:
            return "[CANCELLED]"
        try:
            # Create a ToolCall-like object for _dispatch
            class _FakeCall:
                def __init__(self):
                    self.name = name
                    self.args = args
                    self.result = None
                    self.error = None
                    self.duration_ms = 0.0
            _fake_call = _FakeCall()
            # Resolve ToolName enum if possible
            try:
                from tera_pilot.agent_runtime import ToolName
                if isinstance(name, str):
                    try:
                        _fake_call.name = ToolName(name)
                    except ValueError:
                        _fake_call.name = name
                else:
                    _fake_call.name = name
            except ImportError:
                _fake_call.name = name
            return runtime.tools.execute(_fake_call)
        except Exception as e:
            return f"[TOOL ERROR] {e}"

    runtime._tool_scheduler = None  # lazy
    runtime._tool_execute_fn = _tool_execute_fn

    def _get_scheduler() -> ToolScheduler:
        if runtime._tool_scheduler is None:
            runtime._tool_scheduler = ToolScheduler(
                execute_fn=_tool_execute_fn,
                cancel_token=runtime._cancel_token,
            )
        return runtime._tool_scheduler

    runtime._get_tool_scheduler = _get_scheduler
    logger.info("[orchestrator] ToolScheduler available")


# ── 3. Subagent Host + Batch ────────────────────────────────────────────

def _patch_with_subagent_host(runtime) -> None:
    """Replace ThreadPoolExecutor subagent spawning with SubagentHost + SubagentBatch."""
    from tera_pilot.session.subagent_host import SubagentHost
    from tera_pilot.session.subagent_batch import SubagentBatch

    runtime._subagent_host = SubagentHost(
        parent_runtime=runtime,
        owner_id=id(runtime),
    )
    runtime._original_spawn_subagent = runtime.tools._spawn_subagent
    runtime._original_spawn_multi_agents = runtime.tools._spawn_multi_agents
    logger.info("[orchestrator] SubagentHost installed")


# ── 4. Multi-level Compaction ───────────────────────────────────────────

def _patch_with_compaction(runtime) -> None:
    """Add CompactionManager for full + micro compaction."""
    from tera_pilot.compaction import CompactionManager

    def _llm_compact_fn(text: str) -> str:
        """Use the runtime's provider to summarize."""
        try:
            if not runtime._registry:
                return CompactionManager._heuristic_summary(text)
            provider = runtime._registry.active
            if not provider:
                return CompactionManager._heuristic_summary(text)

            compact_prompt = (
                "Summarize the following conversation segment concisely. "
                "Preserve: user's original goal, files modified, key decisions "
                "made, errors encountered, and the current state of progress. "
                "Keep it under 1000 words.\n\n"
                f"--- CONVERSATION ---\n{text}"
            )
            response = provider.complete(
                messages=[{"role": "user", "content": compact_prompt}],
                temperature=0.3,
                max_tokens=600,
            )
            return response.strip() if response else CompactionManager._heuristic_summary(text)
        except Exception as e:
            logger.warning("[orchestrator] LLM compaction failed: %s", e)
            return CompactionManager._heuristic_summary(text)

    runtime._compaction_manager = CompactionManager(
        llm_call_fn=_llm_compact_fn,
        keep_recent=6,
    )
    logger.info("[orchestrator] CompactionManager installed")


# ── 5. Swarm Mode ──────────────────────────────────────────────────────

def _patch_with_swarm_mode(runtime) -> None:
    """Replace the stub SwarmManager with working SwarmMode."""
    from tera_pilot.swarm import SwarmManager

    runtime._swarm_manager = SwarmManager()
    runtime._swarm_manager.set_project_root(str(runtime.workspace) if runtime.workspace else "")
    logger.info("[orchestrator] SwarmMode installed")


# ── 6. Progressive Tool Disclosure ──────────────────────────────────────

def _patch_with_progressive_tools(runtime) -> None:
    """Add progressive tool disclosure capability."""
    from tera_pilot.progressive_tools import (
        build_catalog_prompt, build_select_tools_schema,
        fold_announced_tool_names, render_loadable_tools_announcement,
        get_loadable_tools, TOOL_CATALOG,
    )

    runtime._progressive_tools = {
        "build_catalog": build_catalog_prompt,
        "build_select_schema": build_select_tools_schema,
        "fold_announced": fold_announced_tool_names,
        "render_announcement": render_loadable_tools_announcement,
        "get_loadable": get_loadable_tools,
        "catalog": TOOL_CATALOG,
    }

    # Register the select_tools handler in ToolEngine._dispatch
    _original_dispatch = runtime.tools._dispatch

    def _dispatch_with_select_tools(self, call) -> str:  # self is the ToolEngine instance
        name = call.name
        name_value = name.value if hasattr(name, 'value') else str(name)

        if name_value == "select_tools":
            return _handle_select_tools(runtime, call.args)
        elif name_value == "search_tools":
            return _handle_search_tools(runtime, call.args)

        return _original_dispatch(call)

    runtime.tools._dispatch = _dispatch_with_select_tools
    logger.info("[orchestrator] Progressive tool disclosure installed")


def _handle_select_tools(runtime, args: Dict[str, Any]) -> str:
    """Handle the select_tools meta-tool call."""
    from tera_pilot.progressive_tools import (
        fold_announced_tool_names, render_loadable_tools_announcement,
        TOOL_CATALOG,
    )

    tool_names = args.get("tool_names", [])
    if not tool_names or not isinstance(tool_names, list):
        return "[SELECT_TOOLS ERROR] tool_names must be a non-empty list of strings"

    # Validate names
    valid = []
    invalid = []
    for name in tool_names:
        if name in TOOL_CATALOG:
            valid.append(name)
        else:
            invalid.append(name)

    # Get currently loaded tools from history
    history_text = runtime.memory.to_prompt_history()
    loaded = fold_announced_tool_names(history_text)

    # Determine added and removed
    added = [n for n in valid if n not in loaded]
    removed = [n for n in loaded if n not in valid and n in tool_names]

    if not added and not removed:
        return f"[SELECT_TOOLS] All requested tools are already loaded: {', '.join(valid)}"

    # Record the announcement in conversation history
    announcement = render_loadable_tools_announcement(added, removed)
    runtime.memory.add("system", announcement)

    # Build tool definitions for the loaded tools
    definitions = _build_tool_definitions(runtime, added)

    result_parts = []
    if added:
        result_parts.append(f"[LOADED {len(added)} tool(s): {', '.join(added)}]")
    if removed:
        result_parts.append(f"[UNLOADED {len(removed)} tool(s): {', '.join(removed)}]")
    if invalid:
        result_parts.append(f"[UNKNOWN: {', '.join(invalid)} — not in catalog]")

    if definitions:
        result_parts.append("\n## Loaded Tool Definitions:")
        result_parts.append(definitions)

    return "\n".join(result_parts)


def _handle_search_tools(runtime, args: Dict[str, Any]) -> str:
    """Handle the search_tools meta-tool call."""
    from tera_pilot.progressive_tools import search_tools

    query = args.get("query", "")
    if not isinstance(query, str):
        return "[SEARCH_TOOLS ERROR] query must be a string"

    return search_tools(query)


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


# ── Integration helper: run_turns with new architecture ────────────────

def run_with_new_architecture(runtime, task) -> Any:
    """Run the agent task using the new modular architecture.

    This is a drop-in replacement for runtime._run_agent_loop(task)
    that uses TurnLoop + ToolScheduler + CompactionManager.

    Returns the same TaskResult as the original.
    """
    from tera_pilot.agent_runtime import TaskResult, AgentEvent, TaskType
    from tera_pilot.loop.turn_loop import TurnLoop, TurnStopReason, ToolCallRequest
    from tera_pilot.loop.tool_scheduler import CancelledError

    logger.info("[orchestrator] running task with new architecture: %s",
                task.description[:80])

    # Initialize
    cancel = runtime._cancel_token
    runtime._compaction_manager.update_activity()

    result = TaskResult(
        success=False, output="", iterations=0,
        steps=[], tool_calls=[], plan=None, metadata={},
    )

    def on_event(event, data):
        if runtime.on_event:
            try:
                runtime.on_event(event, data)
            except Exception:
                pass

    try:
        while True:
            cancel.check()

            # Auto-compaction check
            total_tokens = runtime.memory._total_tokens()
            max_tokens = runtime.memory.max_tokens
            summary, messages, did_compact = runtime._compaction_manager.maybe_compact(
                runtime.memory.messages,
                runtime.memory.compaction_summary,
                total_tokens,
                max_tokens,
            )
            if did_compact:
                runtime.memory.compaction_summary = summary
                runtime.memory.messages = messages
                logger.info("[orchestrator] compaction applied")

            # Build prompt
            from tera_pilot.agent_runtime import PromptBuilder
            pb = PromptBuilder(runtime)
            system_prompt = pb.build(task)
            history = runtime.memory.to_prompt_history()

            # Build messages for the LLM
            llm_messages = []
            if system_prompt:
                llm_messages.append({"role": "system", "content": system_prompt})
            if history:
                llm_messages.append({"role": "user", "content": history})

            # Get tool schema
            tools_schema = pb.build_tool_list()

            # Parse tool calls from LLM response
            def parse_tool_calls(text: str) -> List[ToolCallRequest]:
                calls = []
                # Try JSON tool_use parsing
                try:
                    data = json.loads(text)
                    if isinstance(data, dict) and "tool_calls" in data:
                        for tc in data["tool_calls"]:
                            calls.append(ToolCallRequest(
                                id=tc.get("id", f"call_{len(calls)}"),
                                name=tc.get("name", ""),
                                args=tc.get("args", {}),
                            ))
                except json.JSONDecodeError:
                    pass
                # If no structured tool calls, check for inline JSON
                if not calls:
                    import re
                    # Match tool_use JSON blocks in the text
                    pattern = r'\{"(?:name|tool)":\s*"(\w+)".*?"(?:args|arguments)":\s*(\{[^}]*\})\}'
                    for m in re.finditer(pattern, text, re.DOTALL):
                        try:
                            args = json.loads(m.group(2))
                            calls.append(ToolCallRequest(
                                id=f"call_{len(calls)}",
                                name=m.group(1),
                                args=args,
                            ))
                        except json.JSONDecodeError:
                            pass
                return calls

            # LLM call function
            def llm_call(messages, tools):
                provider = runtime._registry.active
                if not provider:
                    raise RuntimeError("No active provider")

                # Merge system + user messages
                prompt_text = "\n".join(
                    m["content"] for m in messages if m["role"] in ("system", "user")
                )

                response = provider.complete(
                    messages=[{"role": "user", "content": prompt_text}],
                    temperature=runtime.temperature,
                    max_tokens=runtime.max_tokens,
                )

                usage = {
                    "input_tokens": _estimate_tokens(prompt_text),
                    "output_tokens": _estimate_tokens(response),
                }

                return response, parse_tool_calls(response), usage

            # Execute tool function
            def tool_execute(name, args, cancel_token):
                return runtime._tool_execute_fn(name, args, cancel_token)

            # Create and run turn loop
            loop = TurnLoop(
                llm_call_fn=llm_call,
                tool_execute_fn=tool_execute,
                parse_tool_calls_fn=parse_tool_calls,
                max_steps=runtime.max_iterations,
            )

            turn_result = loop.run_turn(
                messages=llm_messages,
                tools_schema=tools_schema,
                cancel_token=cancel,
                on_step=lambda step, event, data: on_event(
                    AgentEvent.ITERATION_START if event == "llm_call"
                    else AgentEvent.TOOL_RESULT if event == "tool_result"
                    else AgentEvent.STEP_DONE,
                    {"iteration": step, **(data or {})},
                ),
            )

            result.iterations += 1
            result.output = turn_result.assistant_text
            for tc in turn_result.tool_calls:
                result.tool_calls.append(type('TC', (), {
                    'name': tc['name'], 'args': tc['args'],
                    'result': tc['result'],
                })())
                result.steps.append(type('AS', (), {
                    'thought': '', 'action': tc,
                    'observation': tc['result'],
                })())

            # Record in memory
            if turn_result.assistant_text:
                runtime.memory.add("assistant", turn_result.assistant_text)
            for tc, tr in zip(turn_result.tool_calls, turn_result.tool_results):
                runtime.memory.add("tool", f"[{tc['name']}] {tr[:500]}")

            runtime._compaction_manager.update_activity()

            if turn_result.stop_reason in (TurnStopReason.END_TURN,
                                            TurnStopReason.MAX_TOKENS):
                result.success = True
                break
            elif turn_result.stop_reason == TurnStopReason.MAX_STEPS:
                result.success = True
                break

    except CancelledError:
        logger.info("[orchestrator] task cancelled")
        result.error = "cancelled"
    except Exception as e:
        logger.error("[orchestrator] task error: %s", e, exc_info=True)
        result.error = str(e)
        on_event(AgentEvent.ERROR, {"error": str(e)})

    on_event(AgentEvent.DONE, {"success": result.success})
    return result


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u0400' <= c <= '\u04ff')
    non_cjk = len(text) - cjk
    return (cjk // 2) + (non_cjk // 4) + 1