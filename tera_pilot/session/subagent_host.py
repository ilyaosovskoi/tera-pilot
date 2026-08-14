"""
Subagent Host — manages child agent lifecycles.

Ported from Kimi Code's packages/agent-core/src/session/subagent-host.ts.

Key concepts ported from Kimi:
  - SubagentHandle: a promise-like object with agentId + completion future
  - Projected history: child gets a summary of parent's conversation
    (not the full history — saves tokens and prevents context overflow)
  - cancelAll(): propagates cancellation to all active children
  - on_ready callback: signals when the child has made its first LLM request

Tera Pilot-specific:
  - Role-based tool whitelists (architect/reviewer/tester/implementer/generalist)
  - Workspace sandbox inherited from parent
  - Quota tracking shared with parent
  - Event forwarding with parent_label for UI nesting
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SubagentHandle:
    """Promise-like handle for a running subagent.

    Ported from Kimi's SubagentHandle type. The parent agent awaits
    `completion` to get the result. The `agent_id` is available
    immediately for logging/cancellation.

    `cancel_token` holds the child's cooperative CancelToken so that
    `cancel_all()` can stop an already-running child (Future.cancel()
    alone is a no-op once the task has started). The `cancelled` bool is
    a convenience marker for external API consumers.
    """
    agent_id: str
    profile_name: str  # role: architect, implementer, reviewer, etc.
    description: str
    completion: Future  # will resolve to SubagentCompletion
    started_at: float = field(default_factory=time.time)
    cancelled: bool = False
    cancel_token: Any = None


@dataclass
class SubagentCompletion:
    """Result of a completed subagent run."""
    result: str
    success: bool
    iterations: int = 0
    tokens_used: int = 0
    error: Optional[str] = None


@dataclass
class QueuedSubagentTask:
    """A task queued for batch execution."""
    index: int
    prompt: str
    description: str
    role: str
    max_iterations: int = 4
    swarm_index: Optional[int] = None
    swarm_item: Optional[str] = None
    run_in_background: bool = False
    timeout: float = 7200.0  # 2 hours default


@dataclass
class SubagentResult:
    """Result of a single subagent in a batch."""
    task: QueuedSubagentTask
    agent_id: Optional[str] = None
    status: str = "pending"  # pending, completed, failed, aborted, timed_out
    state: str = "not_started"  # not_started, started
    result: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    error: Optional[str] = None


class SubagentHost:
    """Manages child agent lifecycles for a parent AgentRuntime.

    Ported from Kimi's SessionSubagentHost. Key differences from the
    old Tera Pilot spawn_subagent:
      - Handles are first-class objects with cancellation support
      - Active children are tracked and can be cancelled as a group
      - runQueued() delegates to SubagentBatch for rate-limited execution
      - Event forwarding is built-in (not ad-hoc)
    """

    def __init__(self,
                 parent_runtime: Any = None,
                 owner_id: Optional[str] = None):
        self._parent = parent_runtime
        self._owner_id = owner_id or uuid.uuid4().hex[:8]
        self._active_children: Dict[str, SubagentHandle] = {}
        self._lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="subagent"
            )
        return self._executor

    def spawn(self,
              prompt: str,
              role: str = "generalist",
              description: str = "",
              max_iterations: int = 4,
              on_ready: Optional[Callable[[], None]] = None,
              ) -> SubagentHandle:
        """Spawn a child subagent. Returns a handle immediately.

        The child runs in a background thread. Await `handle.completion`
        to get the SubagentCompletion.
        """
        from ..loop.tool_scheduler import CancelToken

        agent_id = uuid.uuid4().hex[:8]
        cancel = CancelToken()

        # Create a future for the completion
        completion: Future = Future()

        handle = SubagentHandle(
            agent_id=agent_id,
            profile_name=role,
            description=description or prompt[:80],
            completion=completion,
            cancel_token=cancel,
        )

        with self._lock:
            self._active_children[agent_id] = handle

        # Build the child runtime (same as old _run_subagent_internal)
        def _run():
            try:
                cancel.check()
                child_result = self._run_child(
                    agent_id=agent_id,
                    prompt=prompt,
                    role=role,
                    max_iterations=max_iterations,
                    cancel=cancel,
                    on_ready=on_ready,
                )
                if not completion.set_result(child_result):
                    pass  # already cancelled
            except Exception as e:
                if not completion.set_result(
                    SubagentCompletion(
                        result="", success=False, error=str(e)
                    )
                ):
                    pass
            finally:
                with self._lock:
                    self._active_children.pop(agent_id, None)

        self._get_executor().submit(_run)

        # Link parent cancellation to child via CancelToken
        if self._parent and hasattr(self._parent, '_cancel_token'):
            parent_token = self._parent._cancel_token
            def _on_parent_cancel(reason):
                cancel.cancel(reason)
            parent_token.on_cancel(_on_parent_cancel)

        return handle

    def _run_child(self,
                   agent_id: str,
                   prompt: str,
                   role: str,
                   max_iterations: int,
                   cancel: Any,
                   on_ready: Optional[Callable[[], None]] = None,
                   ) -> SubagentCompletion:
        """Run a child AgentRuntime to completion."""
        from ..agent_runtime import AgentRuntime, Task, TaskType

        if not self._parent or not self._parent._registry:
            return SubagentCompletion(
                result=f"[{agent_id}] no provider registry", success=False,
                error="no_registry",
            )

        # Role → system prompt + tool whitelist
        role_prompts = {
            "architect": (
                "You are a sub-agent focused on PLANNING and DESIGN. "
                "Read files, analyze structure, propose a plan. Do NOT write "
                "or modify any files — return your plan as the final answer."
            ),
            "implementer": (
                "You are a sub-agent focused on IMPLEMENTATION. Make the "
                "requested changes precisely. Prefer str_replace over "
                "write_file. Verify your changes by re-reading the file."
            ),
            "reviewer": (
                "You are a sub-agent focused on CODE REVIEW. Read the "
                "specified files, identify bugs / style issues / risks. "
                "Do NOT modify files — return your review as the final answer."
            ),
            "tester": (
                "You are a sub-agent focused on TESTING. Generate test cases "
                "for the specified code. You may write test files but do NOT "
                "modify production code."
            ),
            "generalist": (
                "You are a sub-agent. Complete the assigned sub-task. "
                "Read files as needed, return your findings/changes as "
                "the final answer."
            ),
        }
        system_suffix = role_prompts.get(role, role_prompts["generalist"])

        # Create child with projected history (summary from parent, not full)
        child_persist = tempfile.NamedTemporaryFile(
            prefix=f"tera_pilot_sub_{agent_id}_",
            suffix=".json", delete=False,
        )
        child_persist.close()

        # Build projected history for the child
        projected_history = self._build_projected_history()

        child = AgentRuntime(
            registry=self._parent._registry,
            workspace=str(self._parent.workspace),
            max_iterations=max(1, min(max_iterations, 10)),
            enable_planning=False,
            on_event=None,
            memory_persist_path=child_persist.name,
            token_tracker=getattr(self._parent, "_token_tracker", None),
            section=getattr(self._parent, "section", "general"),
        )

        # Inherit parent settings
        # cancel.is_cancelled is a property; wrap in a lambda so the child
        # re-reads the live token state each iteration (passing it directly
        # would bind a False snapshot and disable cancellation).
        child.set_cancel_check(lambda: cancel.is_cancelled)
        child.set_quota_tracker(getattr(self._parent, "_quota_tracker", None))
        child.tools.autonomy = self._parent.tools.autonomy
        child.tools.diff_review_enabled = self._parent.tools.diff_review_enabled
        child.tools.set_role_whitelist(role)
        if role == "implementer":
            child.tools.autonomy = "never_ask"

        # Inject projected history as context
        if projected_history:
            child.memory.add("user", projected_history)

        # Forward events with parent_label
        parent_on_event = self._parent.on_event
        if parent_on_event:
            label = f"sub-{agent_id[:6]}"
            def _forward(event, data):
                data = dict(data)
                data["parent_label"] = label
                data["subagent"] = True
                try:
                    parent_on_event(event, data)
                except Exception:
                    pass
            child.on_event = _forward

        # Signal on_ready after first LLM call
        if on_ready:
            _ready_fired = [False]
            _orig = child.on_event
            def _ready_tap(event, data):
                if not _ready_fired[0] and event.value == "iteration_start":
                    _ready_fired[0] = True
                    try:
                        on_ready()
                    except Exception:
                        pass
                if _orig:
                    try:
                        _orig(event, data)
                    except Exception:
                        pass
            child.on_event = _ready_tap

        # Build and run the task
        task = Task(
            type=TaskType.AGENTIC,
            description=(
                f"{system_suffix}\n\n"
                f"## Sub-task (assigned by parent agent)\n{prompt}\n\n"
                f"Return your final answer concisely."
            ),
            language="python",
        )

        try:
            result = child._run_agent_loop(task)
            return SubagentCompletion(
                result=result.output,
                success=result.success,
                iterations=result.iterations,
                error=result.error,
            )
        finally:
            import os
            try:
                os.unlink(child_persist.name)
            except OSError:
                pass

    def _build_projected_history(self) -> str:
        """Build a projected (summarized) history for child agents.

        Instead of sending the full parent conversation (which can be
        50K+ tokens), we send:
          1. The compaction summary (if any)
          2. The last few user messages
          3. Key tool results (file writes)

        This mirrors Kimi's projectedHistory pattern.
        """
        if not self._parent:
            return ""

        memory = self._parent.memory
        parts = []

        # Include compaction summary
        if memory.compaction_summary:
            parts.append(f"[PARENT CONTEXT SUMMARY]\n{memory.compaction_summary}")

        # Include last 4 messages (2 user + 2 assistant)
        recent = memory.messages[-4:]
        if recent:
            for m in recent:
                role_label = {"user": "USER", "assistant": "PARENT"}.get(
                    m.role, m.role.upper()
                )
                # Truncate long messages
                content = m.content
                if len(content) > 2000:
                    content = content[:2000] + f"\n... ({len(m.content)} total chars)"
                parts.append(f"[{role_label}]\n{content}")

        return "\n\n".join(parts)

    def cancel_all(self, reason: str = "cancelled") -> None:
        """Cancel all active children.

        `Future.cancel()` only prevents *not-yet-started* tasks from running,
        so for already-running children we signal their cooperative
        CancelToken (stored on the handle). The child's agent loop checks the
        token and unwinds. `handle.cancelled` is set for external observers.
        """
        with self._lock:
            handles = list(self._active_children.values())
        for handle in handles:
            handle.cancelled = True
            token = getattr(handle, "cancel_token", None)
            if token is not None:
                try:
                    token.cancel(reason)
                except Exception:
                    pass
            if not handle.completion.done():
                handle.completion.cancel()

    def run_queued(self,
                   tasks: List[QueuedSubagentTask],
                   ) -> List[SubagentResult]:
        """Run a batch of subagent tasks with rate limiting.

        Delegates to SubagentBatch for the sophisticated scheduling
        (port of Kimi's SubagentBatch).
        """
        from .subagent_batch import SubagentBatch
        batch = SubagentBatch(self, tasks)
        return batch.run()

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_children)

    def shutdown(self) -> None:
        self.cancel_all("shutdown")
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)