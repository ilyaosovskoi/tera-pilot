"""
AgentWorker — threading.Thread wrapper around AgentRuntime.run_stream().

v2.2.0: the legacy QThread-based AgentWorker has been rewritten on
top of plain :mod:`threading`. The Qt GUI has been removed; the
new HTTP API in :mod:`tera_pilot.api_server` runs the agent in a daemon
thread and streams results to the browser via Server-Sent Events.

This class is kept for backward compatibility — any code that still
imports ``AgentWorker`` (e.g. legacy tests, scripts, the TUI bridge
optional path) keeps working. New code should prefer driving the
agent directly through ``AgentRuntime._run_agent_loop`` (as
:mod:`tera_pilot.api_server` does) and threading it with
``threading.Thread``.

Public API (unchanged):

    worker = AgentWorker(agent_runtime, task)
    worker.step_update.connect(callback)   # callback(event_type, data_json)
    worker.result_ready.connect(callback)  # callback(task_result)
    worker.error.connect(callback)         # callback(error_str)
    worker.start()                         # background thread
    worker.cancel()                        # cooperative cancel

The signal/slot API is replaced by simple callback registration —
the ``connect()`` method just appends to a list of listeners.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from .types import AgentEvent, Task
from .runtime import AgentRuntime

logger = logging.getLogger(__name__)


class _Signal:
    """Tiny callback-list signal — drop-in for ``PySide6.QtCore.Signal``.

    Only the ``connect`` + ``emit`` API is implemented. Thread-safe
    via a lock so emit() from a worker thread doesn't race with
    connect() from the main thread.
    """

    def __init__(self, *arg_types: Any) -> None:
        self._listeners: List[Callable[..., None]] = []
        self._lock = threading.Lock()

    def connect(self, fn: Callable[..., None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def disconnect(self, fn: Optional[Callable[..., None]] = None) -> None:
        with self._lock:
            if fn is None:
                self._listeners.clear()
            else:
                try:
                    self._listeners.remove(fn)
                except ValueError:
                    pass

    def emit(self, *args: Any) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(*args)
            except Exception:
                logger.exception("[AgentWorker] signal listener raised")


class AgentWorker(threading.Thread):
    """Runs agent tasks in a background thread — does NOT block the caller.

    Drop-in replacement for the legacy QThread-based AgentWorker.
    Same constructor signature; ``start()``, ``cancel()`` and the
    three signals (``result_ready``, ``step_update``, ``error``)
    behave identically.

    v2.2.4-fix: signals are now instance-level (not class-level) so
    that each AgentWorker gets its own signal set. Previously the
    class-level _Signal objects were shared across ALL instances,
    causing cross-worker event leakage (BUGS_REPORT W-1).
    """

    def __init__(
        self,
        agent_runtime: AgentRuntime,
        task: Task,
        parent: Any = None,        # ignored — kept for API compat
        **gen_kwargs: Any,
    ) -> None:
        # NOTE: parent is intentionally swallowed. In the legacy QThread
        # version it was forwarded to QThread for Qt ownership semantics.
        # Plain threading.Thread has no parent concept.
        super().__init__(daemon=True, name="tera-pilot-agent-worker")
        self.agent = agent_runtime
        self.task = task
        self.gen_kwargs = gen_kwargs
        self._cancelled = threading.Event()

        # Instance-level signals — each worker has its own set.
        self.result_ready = _Signal(object)    # TaskResult
        self.step_update = _Signal(str, str)   # event_type, data_json
        self.progress = _Signal(int, str)      # percent, message
        self.error = _Signal(str)

    # ── Public API ───────────────────────────────────────────────
    def cancel(self) -> None:
        self._cancelled.set()

    def _is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _on_event(self, event: AgentEvent, data: Dict[str, Any]) -> None:
        if self._cancelled.is_set():
            return
        try:
            self.step_update.emit(event.value, json.dumps(data, default=str))
        except Exception:
            logger.exception("[AgentWorker] step_update emit failed")

    # ── Thread body ──────────────────────────────────────────────
    def run(self) -> None:
        original_callback = None
        try:
            original_callback = self.agent.on_event
            self.agent.on_event = self._on_event
            # Give the agent loop a way to see `cancel()` — without this,
            # Stop only silenced UI events while the loop kept running
            # writes/commands/deletes in the background.
            self.agent.set_cancel_check(self._is_cancelled)

            result = self.agent._run_agent_loop(self.task, **self.gen_kwargs)

            self.agent.on_event = original_callback
            self.result_ready.emit(result)

        except Exception as e:
            # Restore on_event on the exception path so the agent doesn't
            # permanently point at this worker's stale _on_event closure.
            if original_callback is not None:
                self.agent.on_event = original_callback
            logger.error("[AgentWorker] failed: %s", e, exc_info=True)
            try:
                self.error.emit(str(e))
            except Exception:
                logger.exception("[AgentWorker] error emit failed")
        finally:
            try:
                self.agent.set_cancel_check(None)
            except Exception:
                pass


__all__ = ["AgentWorker", "_Signal"]
