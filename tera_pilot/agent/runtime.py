"""Agent runtime v2 — entry point that ties together actor, interjection, sandbox,
compaction, circuit breaker, and sub-agents.

The v2 runtime is **opt-in**. The legacy `AgentRuntime` in `tera_pilot.agent.legacy`
remains the default; users can opt-in via:

    from tera_pilot.agent import AgentRuntimeV2
    runtime = AgentRuntimeV2.from_legacy(legacy_runtime)
    await runtime.run_turn("hello")

Or via the CLI flag `--runtime v2` (TODO — not wired in this commit).

Design notes:
- The v2 runtime does NOT replace the legacy runtime's tool engine, providers,
  or system prompt builder. It wraps a legacy runtime and adds:
    - asyncio ChatStateActor for state ownership
    - InterjectionBuffer for mid-turn user messages
    - Optional sandbox application at startup
    - Three-tier compaction via the v2 engine
    - Circuit breaker around provider calls
    - Sub-agent v2 with toolset-level read-only guarantee
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable, List, Optional

from .actor import CancelToken, ChatStateActor
from .circuit_breaker import CircuitBreakerRegistry, CircuitOpenError
from .compaction_v2 import CompactionEngine, CompactionPolicy, ConversationItem
from .interjection import InterjectionBuffer
from .sandbox import apply_sandbox, current_sandbox_profile, SandboxProfile
from .subagent_v2 import spawn_subagent
from ..loop.turn_loop import TurnLoop
from ..loop.tool_scheduler import ToolScheduler

logger = logging.getLogger(__name__)


class RunStopReason(enum.Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    MAX_STEPS = "max_steps"
    CANCELLED = "cancelled"
    ERROR = "error"
    INTERJECTION = "interjection"  # user injected a message mid-turn


@dataclass
class RunResult:
    success: bool
    stop_reason: RunStopReason
    final_output: Optional[str] = None
    error: Optional[str] = None
    iterations: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class AgentRuntimeV2:
    """The v2 runtime — wraps a legacy runtime and adds new capabilities.

    The wrapped legacy runtime provides:
    - `_generate_with_explicit_system(system, user_prompt) -> (raw, tok_in, tok_out)`
    - `tools: ToolEngine` (with `execute(tool_call)`)
    - `memory: ContextMemory` (legacy state)
    - `provider` and `auto_router`
    - `tools.cancel(reason)` for cooperative cancellation

    This wrapper adds:
    - asyncio ChatStateActor (parallel to legacy memory; can be primary
      in v2 mode)
    - Interjection buffer
    - Compaction v2 (intra/inter/code)
    - Circuit breaker around _generate
    """

    def __init__(self, legacy_runtime):
        self._legacy = legacy_runtime
        self._cancel_token = CancelToken()
        self._interjection_buffer = InterjectionBuffer()
        self._chat_actor: Optional[ChatStateActor] = None
        self._compaction_engine: Optional[CompactionEngine] = None
        self._compaction_policy: CompactionPolicy = CompactionPolicy.inter()
        self._breaker_registry = CircuitBreakerRegistry()
        self._sandbox_applied = False

    # ------------------------------------------------------------------
    # Constructors.
    # ------------------------------------------------------------------

    @classmethod
    def from_legacy(cls, legacy_runtime) -> "AgentRuntimeV2":
        """Wrap a legacy AgentRuntime."""
        return cls(legacy_runtime)

    # ------------------------------------------------------------------
    # Configuration.
    # ------------------------------------------------------------------

    def with_compaction(self, policy: CompactionPolicy, engine: CompactionEngine) -> "AgentRuntimeV2":
        self._compaction_policy = policy
        self._compaction_engine = engine
        return self

    def with_sandbox(
        self,
        profile: str = SandboxProfile.WORKSPACE,
        workspace_root: Optional[str] = None,
        extra_readwrite_paths: Optional[List[str]] = None,
        allowed_egress: Optional[List[str]] = None,
    ) -> "AgentRuntimeV2":
        """Apply the sandbox. **Irreversible**."""
        if self._sandbox_applied:
            logger.warning("sandbox already applied; ignoring")
            return self
        apply_sandbox(
            profile=profile,
            workspace_root=workspace_root,
            extra_readwrite_paths=extra_readwrite_paths,
            allowed_egress=allowed_egress,
        )
        self._sandbox_applied = True
        return self

    def push_interjection(self, text: str, attachment: Optional[str] = None) -> int:
        """Queue a mid-turn user interjection. Returns the assigned id."""
        return self._interjection_buffer.push(text, attachment)

    def cancel(self, reason: str = "user") -> None:
        """Cooperatively cancel the current turn."""
        self._cancel_token.cancel(reason)
        if hasattr(self._legacy, "tools"):
            self._legacy.tools.cancel(reason)

    # ------------------------------------------------------------------
    # Main entry point: run a single turn.
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        user_prompt: str,
        *,
        max_iterations: int = 8,
        on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> RunResult:
        """Run a single turn of the agent loop.

        Implements a ReAct loop with:
        - Auto-compaction (intra) when > 80% of context window.
        - Interjection draining at safe points (between tool calls).
        - Circuit breaker around the provider call.
        - Cooperative cancellation (CancelToken).
        """
        if self._chat_actor is None:
            self._chat_actor = ChatStateActor(
                compaction_engine=self._compaction_engine,
                cancel_token=self._cancel_token,
            )
            await self._chat_actor.start()
        await self._chat_actor.push_user_message(user_prompt)

        iterations = 0
        total_in = 0
        total_out = 0

        try:
            while iterations < max_iterations:
                if self._cancel_token.is_cancelled():
                    return RunResult(
                        success=False,
                        stop_reason=RunStopReason.CANCELLED,
                        iterations=iterations,
                        error=self._cancel_token.reason or "cancelled",
                        tokens_in=total_in,
                        tokens_out=total_out,
                    )

                # 1. Check for interjections.
                interjection = await self._chat_actor.drain_interjections()
                if interjection:
                    if on_event:
                        await on_event({"type": "interjection", "text": interjection})
                    # Inject as a new user message and continue.
                    await self._chat_actor.push_user_message(interjection)

                iterations += 1
                if on_event:
                    await on_event({"type": "iteration_start", "iteration": iterations, "max": max_iterations})

                # 2. Build prompt from chat state.
                items = await self._chat_actor.get_items()
                # Check if compaction is needed.
                ctx_window = getattr(self._legacy.provider, "get_context_window", lambda: 8000)()
                if self._compaction_engine is not None:
                    needed = await self._chat_actor.check_auto_compact_needed(ctx_window)
                    if needed:
                        items = await self._compact(items)

                # 3. Call the provider via the legacy runtime (with circuit breaker).
                system_prompt = self._build_system_prompt()
                user_block = self._build_user_block(items)
                try:
                    raw, tok_in, tok_out = await self._call_with_breaker(
                        system_prompt, user_block
                    )
                except CircuitOpenError as e:
                    return RunResult(
                        success=False,
                        stop_reason=RunStopReason.ERROR,
                        iterations=iterations,
                        error=str(e),
                        tokens_in=total_in,
                        tokens_out=total_out,
                    )
                total_in += tok_in
                total_out += tok_out
                await self._chat_actor.push_assistant_message(raw)

                # 4. Parse tool calls.
                from tera_pilot.agent_runtime import OutputParser  # legacy
                is_final = OutputParser.is_final(raw)

                if is_final:
                    if on_event:
                        await on_event({"type": "final", "output": raw})
                    return RunResult(
                        success=True,
                        stop_reason=RunStopReason.END_TURN,
                        final_output=raw,
                        iterations=iterations,
                        tokens_in=total_in,
                        tokens_out=total_out,
                    )

                # Parse multiple tool calls using ToolScheduler-compatible format
                # For now, parse single tool call and wrap in list for compatibility
                tool_call = OutputParser.parse_tool_call(raw)
                if tool_call is None:
                    # No tool call and not final — retry with reminder (like legacy).
                    continue

                # Parse into list format for ToolScheduler
                from .loop.turn_loop import ToolCallRequest
                from .types import ToolName
                # MCP tools (mcp__*, list_mcp_tools) are stored as raw
                # strings in ToolCall.name, not as ToolName enum values.
                # Calling .value on a str raises AttributeError.
                tool_name_str = (
                    tool_call.name.value
                    if isinstance(tool_call.name, ToolName)
                    else str(tool_call.name)
                )
                tool_calls = [ToolCallRequest(
                    id=f"call_{iterations}",
                    name=tool_name_str,
                    args=tool_call.args,
                )]

                # 5. Execute tools via ToolScheduler (parallel with conflict detection).
                if self._cancel_token.is_cancelled():
                    return RunResult(
                        success=False,
                        stop_reason=RunStopReason.CANCELLED,
                        iterations=iterations,
                        tokens_in=total_in,
                        tokens_out=total_out,
                    )

                # Create scheduler and execute.
                # NOTE: ToolScheduler invokes execute_fn as
                # execute_fn(name, args, cancel_token) and requires a
                # scheduler-native CancelToken (it calls .check()). We must
                # NOT hand it ToolEngine.execute (which takes a single
                # ToolCall) nor the actor CancelToken (no .check()).
                from ..loop.tool_scheduler import CancelToken as SchedulerCancelToken
                sched_token = SchedulerCancelToken()
                if self._cancel_token.is_cancelled():
                    sched_token.cancel("parent cancelled")
                scheduler = ToolScheduler(
                    execute_fn=self._execute_tool,
                    cancel_token=sched_token,
                    max_parallel=8,
                )
                scheduled = scheduler.schedule([(tc.name, tc.args) for tc in tool_calls])

                # Collect results
                for sc in scheduled:
                    observation = sc.result if not sc.error else str(sc.error)
                    await self._chat_actor.push_tool_result(
                        sc.name, str(observation)
                    )
                    if on_event:
                        await on_event({
                            "type": "tool_call",
                            "tool": sc.name,
                            "result_preview": str(observation)[:300],
                        })

            # Out of iterations.
            return RunResult(
                success=True,
                stop_reason=RunStopReason.MAX_STEPS,
                iterations=iterations,
                tokens_in=total_in,
                tokens_out=total_out,
            )
        finally:
            pass  # actor stays alive across turns

    def _execute_tool(self, name: str, args: Any, cancel_token: Any = None) -> str:
        """Adapter bridging ToolScheduler's execute_fn(name, args, cancel_token)
        contract to the legacy ToolEngine.execute(ToolCall).

        ToolScheduler calls this with three positional args; ToolEngine.execute
        takes a single ToolCall. Without this adapter every tool call raised
        TypeError and was silently swallowed into "[TOOL ERROR]".
        """
        if cancel_token is not None:
            flag = getattr(cancel_token, "is_cancelled", None)
            cancelled = flag() if callable(flag) else bool(flag)
            if cancelled:
                return "[CANCELLED]"

        from ..agent_runtime import ToolCall, ToolName
        try:
            resolved = ToolName(name) if isinstance(name, str) else name
        except ValueError:
            resolved = name
        call = ToolCall(name=resolved, args=args)
        return self._legacy.tools.execute(call)

    async def _call_with_breaker(self, system: str, user: str):
        """Wrap the legacy _generate_with_explicit_system call with a circuit breaker."""
        provider_id = getattr(self._legacy.provider, "provider_id", "unknown")
        model_name = getattr(self._legacy.provider, "model", "unknown")
        key = f"{provider_id}/{model_name}"
        breaker = self._breaker_registry.get(key)
        if not breaker.try_claim():
            raise CircuitOpenError(f"breaker open for {key}")
        try:
            # The legacy runtime is synchronous; run in a thread.
            result = await asyncio.to_thread(
                self._legacy._generate_with_explicit_system, system, user
            )
            raw, tok_in, tok_out = result
            breaker.record(ok=True)
            return raw, tok_in, tok_out
        except Exception as e:
            rate_limited = _looks_like_rate_limit(e)
            breaker.record(ok=False, rate_limited=rate_limited)
            raise

    def _build_system_prompt(self) -> str:
        # Delegate to the legacy PromptBuilder.
        from tera_pilot.agent_runtime import PromptBuilder
        pb = PromptBuilder(self._legacy)
        return pb.build_system_prompt()

    def _build_user_block(self, items: List[ConversationItem]) -> str:
        parts = []
        for it in items:
            parts.append(f"[{it.role.upper()}]\n{it.content}")
        return "\n\n".join(parts)

    async def _compact(self, items: List[ConversationItem]) -> List[ConversationItem]:
        if self._compaction_engine is None:
            return items
        try:
            if self._compaction_policy.strategy == "intra":
                _, new_items = self._compaction_engine.intra_compact(
                    items, keep_recent=self._compaction_policy.keep_recent
                )
            elif self._compaction_policy.strategy == "inter":
                _, new_items = self._compaction_engine.inter_compact(
                    items,
                    chunk_size=self._compaction_policy.chunk_size,
                    keep_recent=self._compaction_policy.keep_recent,
                )
            elif self._compaction_policy.strategy == "code":
                _, new_items = self._compaction_engine.code_compact(items)
            else:
                return items
            await self._chat_actor.replace_conversation(new_items)
            return new_items
        except Exception as e:
            logger.warning("compaction failed: %s", e)
            return items

    # ------------------------------------------------------------------
    # Spawn sub-agents via the new v2 API.
    # ------------------------------------------------------------------

    def spawn(self, subagent_type: str, prompt: str, **kwargs):
        """Spawn a sub-agent of the given type."""
        return spawn_subagent(self._legacy, subagent_type, prompt, **kwargs)


def _looks_like_rate_limit(exc: Exception) -> bool:
    """Heuristic: does this exception look like a rate-limit error?"""
    msg = str(exc).lower()
    return any(kw in msg for kw in ("rate limit", "rate_limit", "ratelimit", "too many requests", "429", "quota exceeded", "throttl"))
