"""
Turn Loop — stateless single-turn execution.

Ported from Kimi Code's packages/agent-core/src/loop/run-turn.ts.

Instead of one monolithic _run_agent_loop, the agent now executes
stateless turns. Each turn:
  1. Builds messages (system + history + tool definitions)
  2. Calls the LLM (streaming)
  3. Parses tool_use calls from the response
  4. Executes tools (via ToolScheduler for parallel execution)
  5. Returns a TurnResult

The outer orchestrator (AgentRuntime) loops over turns, handling:
  - Auto-compaction when context is full
  - Watchdog checks between turns
  - Max-step enforcement
  - Cancellation via CancelToken

This separation makes the core loop testable and composable.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Turn Types ────────────────────────────────────────────────────────────

class TurnStopReason:
    """Why a turn ended."""
    TOOL_USE = "tool_use"          # model wants to call tools → loop continues
    END_TURN = "end_turn"          # model finished (no tool calls)
    MAX_TOKENS = "max_tokens"      # model hit output limit
    CANCELLED = "cancelled"        # user cancelled
    MAX_STEPS = "max_steps"        # exceeded step limit
    ERROR = "error"                # provider error


@dataclass
class TurnResult:
    """Result of a single turn execution."""
    stop_reason: str
    steps: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[str] = field(default_factory=list)
    assistant_text: str = ""
    error: Optional[str] = None


@dataclass
class ToolCallRequest:
    """A parsed tool_use from the LLM response."""
    id: str
    name: str
    args: Dict[str, Any]


# ── Turn Loop ─────────────────────────────────────────────────────────────

class TurnLoop:
    """Stateless turn executor.

    Each call to run_turn() is independent — all state lives in the
    caller (AgentRuntime). This mirrors Kimi's runTurn() function.
    """

    def __init__(self,
                 llm_call_fn: Callable,
                 tool_execute_fn: Callable,
                 parse_tool_calls_fn: Callable,
                 max_steps: int = 50):
        """
        Args:
            llm_call_fn: (messages, tools_schema) -> (text, tool_calls_raw, usage)
                Where tool_calls_raw is a list of dicts with 'id', 'name', 'args'.
            tool_execute_fn: (name, args, cancel_token) -> result_string
            parse_tool_calls_fn: (response_text) -> List[ToolCallRequest]
                Parses tool_use JSON from the model's response text.
            max_steps: maximum steps (LLM calls) per turn.
        """
        self._llm_call = llm_call_fn
        self._tool_execute = tool_execute_fn
        self._parse_tool_calls = parse_tool_calls_fn
        self._max_steps = max_steps

    def run_turn(self,
                 messages: List[Dict[str, Any]],
                 tools_schema: Optional[List[Dict[str, Any]]] = None,
                 cancel_token: Any = None,
                 on_step: Optional[Callable[[int, str, Any], None]] = None,
                 ) -> TurnResult:
        """Execute a single turn (may loop through multiple steps).

        Returns a TurnResult with the final state. The caller decides
        whether to start another turn based on stop_reason.

        Args:
            messages: the conversation messages to send to the LLM
            tools_schema: optional tool definitions for tool_use
            cancel_token: CancelToken for cooperative cancellation
            on_step: callback(step_number, event_type, data) for UI updates
        """
        from .tool_scheduler import ToolScheduler, CancelToken as CT, CancelledError

        if cancel_token is None:
            cancel_token = CT()

        result = TurnResult(stop_reason=TurnStopReason.END_TURN)
        step_messages = list(messages)  # copy — we append tool results

        for step in range(1, self._max_steps + 1):
            # Check cancellation at loop boundary (like Kimi's signal.throwIfAborted)
            try:
                cancel_token.check()
            except CancelledError:
                result.stop_reason = TurnStopReason.CANCELLED
                return result

            result.steps = step
            on_step and on_step(step, "llm_call", None)

            # Call the LLM
            try:
                text, raw_tool_calls, usage = self._llm_call(
                    step_messages, tools_schema
                )
            except CancelledError:
                result.stop_reason = TurnStopReason.CANCELLED
                return result
            except Exception as e:
                logger.error("[turn] LLM call failed: %s", e)
                result.stop_reason = TurnStopReason.ERROR
                result.error = str(e)
                return result

            result.total_input_tokens += usage.get("input_tokens", 0)
            result.total_output_tokens += usage.get("output_tokens", 0)
            result.assistant_text = text

            # Check for max tokens (finish_reason from provider or usage-based heuristic)
            # This must be checked BEFORE parsing tool calls
            finish_reason = usage.get("finish_reason", "").lower() if isinstance(usage, dict) else ""
            if finish_reason in ("length", "max_tokens"):
                result.stop_reason = TurnStopReason.MAX_TOKENS
                return result

            # Parse tool calls from the response
            tool_calls = self._parse_tool_calls(text)
            if not tool_calls:
                # No tool calls → turn is done
                result.stop_reason = TurnStopReason.END_TURN
                return result

            on_step and on_step(step, "tool_calls", len(tool_calls))

            # Execute tools via ToolScheduler (parallel with conflict detection)
            scheduler = ToolScheduler(
                execute_fn=self._tool_execute,
                cancel_token=cancel_token,
            )

            # Build the schedule
            schedule_input = [(tc.name, tc.args) for tc in tool_calls]
            scheduled = scheduler.schedule(schedule_input)

            # Collect results
            tool_results = []
            for i, sc in enumerate(scheduled):
                tc = tool_calls[i]
                if sc.error:
                    result_text = sc.error
                else:
                    result_text = sc.result or ""
                tool_results.append(result_text)
                result.tool_calls.append({
                    "id": tc.id, "name": tc.name,
                    "args": tc.args, "result": result_text,
                    "duration_ms": sc.duration_ms,
                })
                result.tool_results.append(result_text)
                on_step and on_step(step, "tool_result", {
                    "name": tc.name, "result": result_text[:200],
                })

            # Append assistant message + tool results to conversation
            step_messages.append({"role": "assistant", "content": text})
            for tc, tr in zip(tool_calls, tool_results):
                step_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tr,
                })

            # Tool calls were made → continue the loop
            result.stop_reason = TurnStopReason.TOOL_USE

        # Exceeded max steps
        result.stop_reason = TurnStopReason.MAX_STEPS
        return result