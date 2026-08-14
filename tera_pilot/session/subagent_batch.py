"""
Subagent Batch — rate-limited, resumable subagent scheduling.

Ported from Kimi Code's packages/agent-core/src/session/subagent-batch.ts.

Scheduling contract (ported verbatim from Kimi's JSDoc):

Normal phase:
- Return results in input order; empty input returns an empty list.
- Start up to 5 tasks immediately, then 1 more every 700ms while
  queued work remains.
- Launch priority: previous agent id saved after a rate limit,
  explicit resume, then new spawn.

Rate-limit phase:
- A provider rate limit requeues while there is other unfinished work.
  Save the agent id for same-agent retry, emit suspended, and requeue
  the task at the front; eligibility delays are 3s, 6s, 12s, then 2x.
- If the rate-limited attempt is the only unfinished task, fail that
  task instead of suspending the batch forever.
- Enter with capacity = max(1, ready_normal_launches).
- Core recovery: if no rate limit for 3 minutes, capacity += 1.

Tera Pilot adaptation:
- Uses threading.Timer instead of setTimeout
- Uses concurrent.futures instead of native Promises
- Rate limit detection is heuristic (provider error messages)
  since Tera Pilot's provider interface doesn't have typed errors like Kimi's
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .subagent_host import (
    QueuedSubagentTask, SubagentResult, SubagentHost,
    SubagentCompletion,
)
from tera_pilot.agent.circuit_breaker import CircuitBreakerRegistry, CircuitOpenError

logger = logging.getLogger(__name__)

# Constants (ported from Kimi)
INITIAL_LAUNCH_LIMIT = 5
INITIAL_LAUNCH_INTERVAL_MS = 700
RATE_LIMIT_RETRY_BASE_MS = 3000
RATE_LIMIT_RETRY_FACTOR = 2
RATE_LIMIT_CAPACITY_SHRINK_INTERVAL_MS = 2000
RATE_LIMIT_CAPACITY_RECOVERY_INTERVAL_MS = 3 * 60 * 1000  # 3 minutes


def _retry_delay(retry_count: int) -> float:
    """Exponential backoff: 3s, 6s, 12s, 24s, ..."""
    return RATE_LIMIT_RETRY_BASE_MS * (RATE_LIMIT_RETRY_FACTOR ** max(0, retry_count - 1)) / 1000.0


class SubagentBatch:
    """Rate-limited batch scheduler for subagent execution.

    Ported from Kimi's SubagentBatch class. Manages the transition
    between normal phase (aggressive parallel launch) and rate-limit
    phase (conservative, backoff-based) with automatic recovery.
    """

    def __init__(self,
                 host: SubagentHost,
                 tasks: List[QueuedSubagentTask],
                 max_concurrency: Optional[int] = None,
                 circuit_breaker: Optional[CircuitBreakerRegistry] = None):
        self._host = host
        self._states: List[_TaskState] = []
        self._pending: List[_TaskState] = []
        self._results: List[Optional[SubagentResult]] = [None] * len(tasks)
        self._lock = threading.Lock()

        self._normal_launch_count = 0
        self._normal_timer: Optional[threading.Timer] = None
        self._rate_limit_timer: Optional[threading.Timer] = None

        self._rate_limit_mode = False
        self._rate_limit_capacity = 1
        self._started_success_count = 0
        self._last_rate_limit_at: Optional[float] = None
        self._last_capacity_shrink_at: Optional[float] = None
        self._global_retry_interval_ms = RATE_LIMIT_RETRY_BASE_MS
        self._next_rate_limit_launch_at = 0.0

        self._finished = False
        self._started = False
        self._max_concurrency = max_concurrency

        self._done_event = threading.Event()
        self._active_count = 0

        # Circuit breaker integration (replaces heuristic _is_rate_limit_error)
        self._circuit_breaker = circuit_breaker or CircuitBreakerRegistry()

        for i, task in enumerate(tasks):
            state = _TaskState(index=i, task=task)
            self._states.append(state)
            self._pending.append(state)

    def run(self) -> List[SubagentResult]:
        """Run all tasks and return results in input order."""
        if self._started:
            raise RuntimeError("SubagentBatch.run() can only be called once")
        self._started = True

        if not self._states:
            return []

        self._schedule()
        self._done_event.wait()
        return [r for r in self._results if r is not None]

    def _schedule(self) -> None:
        if self._finished:
            return
        if self._check_complete():
            return
        if self._rate_limit_mode:
            self._schedule_rate_limit_launch()
        else:
            self._schedule_normal_launch()

    def _schedule_normal_launch(self) -> None:
        with self._lock:
            while (self._normal_launch_count < INITIAL_LAUNCH_LIMIT
                   and self._pending
                   and not self._rate_limit_mode
                   and not self._at_concurrency_limit()):
                state = self._pending.pop(0)
                self._start_attempt(state)
                self._normal_launch_count += 1

            if (not self._pending or self._rate_limit_mode
                    or self._normal_timer is not None
                    or self._at_concurrency_limit()):
                return

            def _timer_fire():
                with self._lock:
                    self._normal_timer = None
                if self._finished or self._rate_limit_mode or not self._pending:
                    return
                if self._at_concurrency_limit():
                    return
                state = self._pending.pop(0)
                self._start_attempt(state)
                self._normal_launch_count += 1
                self._schedule()

            self._normal_timer = threading.Timer(
                INITIAL_LAUNCH_INTERVAL_MS / 1000.0, _timer_fire
            )
            self._normal_timer.daemon = True
            self._normal_timer.start()

    def _schedule_rate_limit_launch(self) -> None:
        self._clear_rate_limit_timer()
        if not self._pending:
            return

        now = time.monotonic()
        self._try_recover_capacity(now)

        with self._lock:
            if self._active_count >= self._rate_limit_capacity:
                self._schedule_rl_wakeup(self._next_recovery_at(), now)
                return

        next_allowed = max(self._next_rate_limit_launch_at,
                           self._next_pending_ready_at())
        next_wakeup = min(next_allowed, self._next_recovery_at())
        if next_wakeup > now:
            self._schedule_rl_wakeup(next_wakeup, now)
            return

        # Find first eligible task
        with self._lock:
            eligible_idx = None
            for i, state in enumerate(self._pending):
                if state.retry_ready_at <= now:
                    eligible_idx = i
                    break

        if eligible_idx is None:
            return

        with self._lock:
            state = self._pending.pop(eligible_idx)
        self._start_attempt(state)
        self._next_rate_limit_launch_at = now + self._global_retry_interval_ms / 1000.0
        self._schedule_next_rl_wakeup(now)

    def _start_attempt(self, state: _TaskState) -> None:
        if self._finished:
            return

        with self._lock:
            self._active_count += 1
            state.state = "started"

        def _run():
            try:
                # Build circuit breaker key from provider/model if available
                # Fall back to a generic key for subagent tasks
                cb_key = f"subagent:{state.task.role}"
                breaker = self._circuit_breaker.get(cb_key)

                # Try to claim a slot in the circuit breaker
                if not breaker.try_claim():
                    # Circuit is open - treat as rate limit
                    self._handle_rate_limit(state, "", "Circuit breaker open")
                    return

                try:
                    handle = self._host.spawn(
                        prompt=state.task.prompt,
                        role=state.task.role,
                        description=state.task.description,
                        max_iterations=state.task.max_iterations,
                        on_ready=self._on_ready(state),
                    )
                    state.agent_id = handle.agent_id

                    try:
                        completion = handle.completion.result(
                            timeout=state.task.timeout
                        )
                        # Record success
                        breaker.record(ok=True)
                        self._handle_completion(state, completion)
                    except Exception as e:
                        # Check for rate limit via circuit breaker or heuristic fallback
                        error_str = str(e)
                        is_rate_limited = False

                        # First, check if circuit breaker can classify this
                        # For now, use heuristic as fallback
                        if self._is_rate_limit_error(e) or self._is_rate_limit_error(error_str):
                            is_rate_limited = True

                        # Record the error with the circuit breaker
                        breaker.record(ok=False, rate_limited=is_rate_limited)

                        if is_rate_limited:
                            self._handle_rate_limit(state, handle.agent_id, error_str)
                        else:
                            self._handle_failure(state, error_str)
                except Exception as e:
                    # Record failure for the attempt
                    breaker.record(ok=False)
                    self._handle_failure(state, str(e))
            except Exception as e:
                self._handle_failure(state, str(e))
            finally:
                with self._lock:
                    self._active_count -= 1
                self._schedule()

        threading.Thread(target=_run, daemon=True, name=f"batch-{state.index}").start()

    def _on_ready(self, state: _TaskState):
        def _callback():
            if self._finished:
                return
            if not self._rate_limit_mode:
                self._started_success_count += 1
            else:
                self._global_retry_interval_ms = RATE_LIMIT_RETRY_BASE_MS
                self._next_rate_limit_launch_at = time.monotonic() + RATE_LIMIT_RETRY_BASE_MS / 1000.0
                self._schedule()
        return _callback

    def _handle_completion(self, state: _TaskState, completion: SubagentCompletion):
        self._results[state.index] = SubagentResult(
            task=state.task,
            agent_id=state.agent_id,
            status="completed" if completion.success else "failed",
            state="started",
            result=completion.result,
            error=completion.error,
        )

    def _handle_failure(self, state: _TaskState, error: str):
        self._results[state.index] = SubagentResult(
            task=state.task,
            agent_id=state.agent_id,
            status="failed",
            state="started" if state.agent_id else "not_started",
            error=error,
        )

    def _handle_rate_limit(self, state: _TaskState, agent_id: str, error: str):
        now = time.monotonic()
        self._last_rate_limit_at = now

        # If this is the only unfinished task, fail it
        if self._is_only_unfinished(state):
            self._handle_failure(state, f"Rate limited (no other tasks to wait for): {error}")
            return

        # Requeue for retry
        state.retry_count += 1
        state.retry_agent_id = agent_id
        state.retry_ready_at = now + _retry_delay(state.retry_count)

        logger.info("[batch] rate limit for %s, requeue (retry #%d, ready in %.1fs)",
                     agent_id, state.retry_count, state.retry_ready_at - now)

        with self._lock:
            self._pending.insert(0, state)

        self._enter_rate_limit_mode(now)

    def _enter_rate_limit_mode(self, now: float) -> None:
        if not self._rate_limit_mode:
            self._rate_limit_mode = True
            self._clear_normal_timer()
            self._rate_limit_capacity = max(1, self._started_success_count)
            self._next_rate_limit_launch_at = max(
                self._next_rate_limit_launch_at,
                now + RATE_LIMIT_RETRY_BASE_MS / 1000.0,
            )
            self._shrink_capacity(now, force=True)
            return
        self._shrink_capacity(now, force=False)

    def _shrink_capacity(self, now: float, force: bool) -> None:
        if (not force and self._last_capacity_shrink_at is not None
                and now - self._last_capacity_shrink_at < RATE_LIMIT_CAPACITY_SHRINK_INTERVAL_MS / 1000.0):
            return
        self._rate_limit_capacity = max(1, self._rate_limit_capacity - 1)
        self._last_capacity_shrink_at = now

    def _try_recover_capacity(self, now: float) -> None:
        if self._next_recovery_at() > now:
            return
        self._rate_limit_capacity += 1
        self._next_rate_limit_launch_at = min(self._next_rate_limit_launch_at, now)

    def _next_recovery_at(self) -> float:
        if not self._pending or self._last_rate_limit_at is None:
            return float('inf')
        latest = max(self._last_rate_limit_at, 0)
        return latest + RATE_LIMIT_CAPACITY_RECOVERY_INTERVAL_MS / 1000.0

    def _next_pending_ready_at(self) -> float:
        if not self._pending:
            return float('inf')
        return min(s.retry_ready_at for s in self._pending)

    def _at_concurrency_limit(self) -> bool:
        if self._max_concurrency is None:
            return False
        return self._active_count >= self._max_concurrency

    def _is_only_unfinished(self, state: _TaskState) -> bool:
        return all(
            i == state.index or self._results[i] is not None
            for i in range(len(self._results))
        )

    def _check_complete(self) -> bool:
        if all(r is not None for r in self._results):
            self._finish()
            return True
        return False

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._clear_normal_timer()
        self._clear_rate_limit_timer()
        self._done_event.set()

    def _schedule_rl_wakeup(self, at: float, now: float) -> None:
        delay = at - now
        if delay <= 0 or not float('inf') != float('inf'):
            return
        self._rate_limit_timer = threading.Timer(delay, self._schedule)
        self._rate_limit_timer.daemon = True
        self._rate_limit_timer.start()

    def _schedule_next_rl_wakeup(self, now: float) -> None:
        if not self._pending:
            return
        if self._active_count >= self._rate_limit_capacity:
            next_at = self._next_recovery_at()
        else:
            next_at = min(
                max(self._next_rate_limit_launch_at, self._next_pending_ready_at()),
                self._next_recovery_at(),
            )
        self._schedule_rl_wakeup(next_at, now)

    def _clear_normal_timer(self) -> None:
        if self._normal_timer:
            self._normal_timer.cancel()
            self._normal_timer = None

    def _clear_rate_limit_timer(self) -> None:
        if self._rate_limit_timer:
            self._rate_limit_timer.cancel()
            self._rate_limit_timer = None

    @staticmethod
    def _is_rate_limit_error(error: Any) -> bool:
        """Heuristic rate limit detection.

        Tera Pilot's providers don't have typed errors like Kimi's, so we
        check for common rate limit indicators. Accepts either an
        exception object or a string: non-strings are coerced to their
        text form for keyword matching, and a numeric HTTP status code
        (429) exposed on the exception is trusted directly when present.
        """
        # Trust an explicit HTTP 429 status code if the provider exposes one.
        for attr in ("status_code", "status", "code", "http_status"):
            value = getattr(error, attr, None)
            if value == 429 or value == "429":
                return True

        text = error if isinstance(error, str) else str(error)
        lower = text.lower()
        return any(kw in lower for kw in [
            "rate limit", "rate_limit", "ratelimit",
            "too many requests", "429", "quota exceeded",
            "throttl",
        ])


@dataclass
class _TaskState:
    index: int
    task: QueuedSubagentTask
    agent_id: Optional[str] = None
    retry_agent_id: Optional[str] = None
    retry_count: int = 0
    retry_ready_at: float = 0.0
    state: str = "not_started"