"""
Tool Scheduler — parallel tool execution with resource-conflict detection.

Ported from Kimi Code's packages/agent-core/src/loop/tool-scheduler.ts.
Determines execution ordering for tool calls in a single model step:
  - Tasks with non-conflicting resource accesses may run concurrently.
  - Tasks with conflicting resource accesses (e.g. two writes to the
    same file) are queued until the conflict resolves.
  - Results are returned in provider (input) order.

Tera Pilot-specific additions:
  - Thread-based execution (vs Kimi's async/await).
  - Cooperative cancellation via CancelToken.
  - Path sandbox enforcement inherited from ToolEngine.
"""

from __future__ import annotations

import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Resource Access Types ────────────────────────────────────────────────

class FileAccessOp:
    READ = "read"
    WRITE = "write"
    READWRITE = "readwrite"
    SEARCH = "search"


@dataclass(frozen=True)
class ToolFileAccess:
    """Describes a file resource access by a tool call."""
    operation: str  # FileAccessOp value
    path: str
    recursive: bool = False

    @property
    def is_write(self) -> bool:
        return self.operation in (FileAccessOp.WRITE, FileAccessOp.READWRITE)


@dataclass(frozen=True)
class ToolAllAccess:
    """Global exclusive access — conflicts with everything."""
    kind: str = "all"


ToolResourceAccess = ToolFileAccess | ToolAllAccess


def accesses_conflict(left: List[ToolResourceAccess],
                      right: List[ToolResourceAccess]) -> bool:
    """True if any pair of resource accesses from left and right conflict."""
    return any(
        _resources_conflict(l, r)
        for l in left for r in right
    )


def _resources_conflict(left: ToolResourceAccess,
                        right: ToolResourceAccess) -> bool:
    if isinstance(left, ToolAllAccess) or isinstance(right, ToolAllAccess):
        return True
    # Both are ToolFileAccess
    if not left.is_write and not right.is_write:
        return False  # read+read and search+search never conflict
    return _file_paths_overlap(left, right)


def _file_paths_overlap(left: ToolFileAccess, right: ToolFileAccess) -> bool:
    lp = _normalize_path(left.path)
    rp = _normalize_path(right.path)
    if lp == rp:
        return True
    l_prefix = lp + "/" if not lp.endswith("/") else lp
    r_prefix = rp + "/" if not rp.endswith("/") else rp
    return (
        (left.recursive and rp.startswith(l_prefix)) or
        (right.recursive and lp.startswith(r_prefix))
    )


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/").replace("//", "/").lower().rstrip("/")


# ── Tool Access Registry ─────────────────────────────────────────────────
# Each built-in tool declares its resource accesses so the scheduler can
# detect conflicts. MCP tools default to ToolAllAccess (exclusive).

TOOL_ACCESSES: Dict[str, Callable[[Dict[str, Any]], List[ToolResourceAccess]]] = {}

def register_tool_accesses(name: str,
                           fn: Callable[[Dict[str, Any]], List[ToolResourceAccess]]):
    """Register a function that extracts resource accesses from tool args."""
    TOOL_ACCESSES[name] = fn

def get_tool_accesses(name: str, args: Dict[str, Any]) -> List[ToolResourceAccess]:
    """Get the resource accesses for a tool call. Defaults to [ToolAllAccess]."""
    fn = TOOL_ACCESSES.get(name)
    if fn is None:
        return [ToolAllAccess()]
    try:
        return fn(args)
    except Exception:
        return [ToolAllAccess()]


# Register built-in tool accesses
def _file_access(op: str, args: Dict[str, Any]) -> List[ToolResourceAccess]:
    path = args.get("path", args.get("directory", "."))
    return [ToolFileAccess(operation=op, path=path)]

def _read_access(args: Dict[str, Any]) -> List[ToolResourceAccess]:
    return _file_access(FileAccessOp.READ, args)

def _write_access(args: Dict[str, Any]) -> List[ToolResourceAccess]:
    return _file_access(FileAccessOp.WRITE, args)

def _readwrite_access(args: Dict[str, Any]) -> List[ToolResourceAccess]:
    return _file_access(FileAccessOp.READWRITE, args)

def _search_access(args: Dict[str, Any]) -> List[ToolResourceAccess]:
    path = args.get("path", args.get("directory", "."))
    return [ToolFileAccess(operation=FileAccessOp.SEARCH, path=path, recursive=True)]

def _no_access(args: Dict[str, Any]) -> List[ToolResourceAccess]:
    return []

# File read tools
for _name in ("read_file", "file_info", "read_binary_file", "git_status",
              "git_diff", "get_project_structure"):
    register_tool_accesses(_name, _read_access)

# File write tools
for _name in ("write_file", "apply_diff", "mkdir", "delete_file",
              "rename_file", "write_binary_file", "undo_write",
              "git_stage", "git_commit"):
    register_tool_accesses(_name, _write_access)

# Edit tools
for _name in ("str_replace",):
    register_tool_accesses(_name, _readwrite_access)

# Search tools
for _name in ("search_project", "grep", "glob", "list_files"):
    register_tool_accesses(_name, _search_access)

# Non-file tools
for _name in ("run_code", "get_skill", "self_verify", "watchdog_check",
              "spawn_subagent", "spawn_multi_agents"):
    register_tool_accesses(_name, _no_access)


# ── Cancel Token ──────────────────────────────────────────────────────────

class CancelToken:
    """Cooperative cancellation token, analogous to Kimi's AbortSignal.

    Unlike threading.Event (which is set-once), CancelToken carries a
    reason and supports chaining (child tokens linked to parent).
    """

    def __init__(self, parent: Optional[CancelToken] = None,
                 reason: str = "cancelled"):
        self._reason = reason
        self._cancelled = False
        self._lock = threading.Lock()
        self._parent = parent
        self._listeners: List[Callable[[str], None]] = []

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            if self._cancelled:
                return True
        if self._parent and self._parent.is_cancelled:
            return True
        return False

    @property
    def reason(self) -> Optional[str]:
        if self._cancelled:
            return self._reason
        if self._parent:
            return self._parent.reason
        return None

    def cancel(self, reason: Optional[str] = None) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason or self._reason
        for listener in self._listeners:
            try:
                listener(self._reason)
            except Exception:
                pass

    def on_cancel(self, listener: Callable[[str], None]) -> None:
        with self._lock:
            if self._cancelled:
                listener(self._reason)
                return
            self._listeners.append(listener)

    def check(self) -> None:
        """Raise CancelledError if cancelled. Analogous to signal.throwIfAborted()."""
        if self.is_cancelled:
            raise CancelledError(self.reason or "cancelled")


class CancelledError(Exception):
    """Raised by CancelToken.check() when the token is cancelled."""
    pass


# ── Scheduled Task ────────────────────────────────────────────────────────

@dataclass
class ScheduledToolCall:
    """A tool call scheduled for execution."""
    index: int
    name: str
    args: Dict[str, Any]
    accesses: List[ToolResourceAccess]
    result: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    done_event: threading.Event = field(default_factory=threading.Event)


# ── Tool Scheduler ────────────────────────────────────────────────────────

class ToolScheduler:
    """Stateful execution scheduler for tool calls in one model step.

    Ported from Kimi Code's ToolScheduler class. Owns only execution
    ordering — validation, hooks, and result finalization stay in the
    caller (ToolEngine / TurnLoop).
    """

    def __init__(self,
                 execute_fn: Callable[[str, Dict[str, Any], CancelToken], str],
                 cancel_token: Optional[CancelToken] = None,
                 max_parallel: int = 8):
        """
        Args:
            execute_fn: callable(name, args, cancel_token) -> result_string
            cancel_token: cooperative cancellation token
            max_parallel: maximum concurrent tool executions
        """
        self._execute = execute_fn
        self._cancel = cancel_token or CancelToken()
        self._max_parallel = max_parallel
        self._active: List[_RunningTask] = []
        self._queued: List[ScheduledToolCall] = []
        self._lock = threading.Lock()
        self._results: Dict[int, ScheduledToolCall] = {}

    def schedule(self, calls: List[Tuple[str, Dict[str, Any]]]) -> List[ScheduledToolCall]:
        """Schedule and execute a batch of tool calls.

        Returns results in input order. Non-conflicting calls run in
        parallel; conflicting calls wait for their conflicts to finish.

        Args:
            calls: list of (tool_name, args) tuples
        Returns:
            list of ScheduledToolCall in input order, with results filled
        """
        if not calls:
            return []

        # Build scheduled calls with resource accesses
        scheduled = []
        for i, (name, args) in enumerate(calls):
            accesses = get_tool_accesses(name, args)
            sc = ScheduledToolCall(
                index=i, name=name, args=args, accesses=accesses,
            )
            scheduled.append(sc)

        # Schedule each
        for sc in scheduled:
            with self._lock:
                # Check both conflict blocking AND max_parallel limit
                if self._is_blocked(sc) or len(self._active) >= self._max_parallel:
                    self._queued.append(sc)
                else:
                    self._start(sc)

        # Wait for all to complete
        for sc in scheduled:
            sc.done_event.wait()
            self._results[sc.index] = sc

        # Return in input order
        return [self._results[i] for i in range(len(scheduled))]

    def _is_blocked(self, task: ScheduledToolCall) -> bool:
        """Check if task conflicts with any active or already-queued task."""
        for running in self._active:
            if accesses_conflict(task.accesses, running.call.accesses):
                return True
        for queued in self._queued:
            if accesses_conflict(task.accesses, queued.accesses):
                return True
        return False

    def _start(self, task: ScheduledToolCall) -> None:
        """Start executing a tool call in a background thread."""
        running = _RunningTask(task, self._cancel)
        self._active.append(running)

        def _run():
            try:
                self._cancel.check()
                start = time.monotonic()
                result = self._execute(task.name, task.args, self._cancel)
                task.result = result
                task.duration_ms = (time.monotonic() - start) * 1000
            except CancelledError:
                task.error = "[CANCELLED]"
            except Exception as e:
                task.error = f"[TOOL ERROR] {e}"
            finally:
                task.done_event.set()
                self._finish(running)

        t = threading.Thread(target=_run, daemon=True, name=f"tool-{task.name}")
        t.start()

    def _finish(self, running: _RunningTask) -> None:
        """Remove from active and start any newly unblocked queued tasks."""
        with self._lock:
            try:
                self._active.remove(running)
            except ValueError:
                pass
            self._start_unblocked()

    def _start_unblocked(self) -> None:
        """Try to start queued tasks that are no longer blocked."""
        still_queued = []
        for task in self._queued:
            if len(self._active) >= self._max_parallel:
                still_queued.append(task)
                continue
            if self._is_blocked(task):
                still_queued.append(task)
                continue
            self._start(task)
        self._queued = still_queued

    def cancel_all(self, reason: str = "cancelled") -> None:
        """Cancel all running and queued tool calls."""
        self._cancel.cancel(reason)
        with self._lock:
            for task in self._queued:
                task.error = f"[CANCELLED] {reason}"
                task.done_event.set()
            self._queued.clear()


@dataclass
class _RunningTask:
    call: ScheduledToolCall
    cancel: CancelToken