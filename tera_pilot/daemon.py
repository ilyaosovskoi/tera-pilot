#!/usr/bin/env python3
"""
daemon.py — Tera Pilot remote agent daemon.

Run the agent on a remote server for long-running tasks, with an HTTP API
for task management and Server-Sent Events (SSE) for real-time streaming.

Usage:
    # Start the daemon server (long-running):
    tera-pilot-daemon --port 8765 --notify telegram

    # Remote task mode — "set a task, walk away": accept tasks via Telegram
    # (message -> task -> runs -> result is sent back to the same chat).
    tera-pilot-daemon serve --inbound telegram --notify telegram

    # Run a single task with notification when done:
    tera-pilot-daemon task "Refactor the auth module" --workspace /projects/myapp --notify telegram

    # Run a task from a file:
    tera-pilot-daemon task-file prompt.txt --workspace /projects/myapp

Architecture:
    tera-pilot-daemon
    ├── HTTP API (REST + SSE)
    │   ├── POST /task              — submit a new task
    │   ├── GET  /task/:id          — get task status
    │   ├── GET  /tasks             — list all tasks
    │   ├── POST /task/:id/cancel   — cancel a running task
    │   ├── GET  /stream/:id        — SSE stream for a task
    │   └── GET  /health            — health check
    ├── TaskQueue (threading)
    │   └── Worker threads run AgentRuntime headless
    ├── Notifier (optional)
    │   └── Sends reports to Telegram/Discord/Slack on task completion
    └── Inbound listener (optional, remote task mode)
        └── Accepts tasks from Telegram; reply STOP cancels the run

Zero external dependencies — uses only the Python standard library.
Authentication via Bearer token stored in ~/.tera_pilot/daemon.json.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse, parse_qs


def _build_registry():
    """Build a ProviderRegistry configured from ~/.tera_pilot/config.json.

    v2.3.4-fix (real-run): the daemon previously called
    ``AgentRuntime(workspace=...)`` with no ``registry`` argument at all —
    but ``AgentRuntime.__init__`` requires one, so every daemon task
    (HTTP + `tera-pilot-daemon task`) crashed with TypeError before the
    agent even started. Mirror the api_server wiring: apply each
    provider's config from config.json (api_key / model / api_base),
    then set the active provider.
    """
    from tera_pilot.providers import get_registry, ProviderConfig
    from tera_pilot.utils import load_config

    registry = get_registry()
    cfg = load_config() or {}
    for pid, pcfg in (cfg.get("providers") or {}).items():
        try:
            # v2.3.5-fix: thread optional provider extras (e.g.
            # reasoning_effort for reasoning models).
            extra = {}
            if pcfg.get("reasoning_effort"):
                extra["reasoning_effort"] = pcfg["reasoning_effort"]
            registry.configure(
                pid,
                ProviderConfig(
                    provider_id=pid,
                    model=pcfg.get("model", ""),
                    api_key=pcfg.get("api_key") or None,
                    api_base=pcfg.get("api_base") or None,
                    temperature=float(pcfg.get("temperature", 0.2)),
                    max_tokens=int(pcfg.get("max_tokens", 4096)),
                    extra=extra,
                ),
            )
        except Exception as e:
            print(f"[daemon] failed to configure provider {pid}: {e}")
    active = cfg.get("active_provider")
    if active:
        try:
            registry.set_active(active)
        except Exception as e:
            print(f"[daemon] failed to set active provider {active}: {e}")
    return registry


# ── Task states ────────────────────────────────────────────────

class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── Task record ────────────────────────────────────────────────

@dataclass
class TaskRecord:
    """A single task tracked by the daemon."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    prompt: str = ""
    workspace: str = ""
    state: TaskState = TaskState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[str] = None
    error: Optional[str] = None
    token_count: int = 0
    cost_usd: float = 0.0
    tools_used: int = 0
    files_changed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "workspace": self.workspace,
            "state": self.state.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
            "token_count": self.token_count,
            "cost_usd": self.cost_usd,
            "tools_used": self.tools_used,
            "files_changed": self.files_changed,
        }

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.completed_at or time.time()
        return round(end - self.started_at, 1)


# ── SSE subscriber ─────────────────────────────────────────────

class SSESubscriber:
    """Manages a list of callbacks for Server-Sent Events on a task."""

    def __init__(self) -> None:
        self._callbacks: List[Callable[[str, Dict[str, Any]], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def unsubscribe(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        with self._lock:
            self._callbacks = [cb for cb in self._callbacks if cb is not callback]

    def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        with self._lock:
            for cb in list(self._callbacks):
                try:
                    cb(event_type, data)
                except Exception:
                    pass


# ── Task queue ─────────────────────────────────────────────────

class TaskQueue:
    """Manages background task execution with thread-based workers."""

    def __init__(self, max_workers: int = 2, allow_headless_confirm: bool = False) -> None:
        # v2.3.4-security (P0.4): headless confirmations fail CLOSED by
        # default. ``allow_headless_confirm`` is the explicit opt-in set
        # by the daemon's --no-confirm flag — without it, side-effecting
        # agent actions are blocked (never silently run).
        self.allow_headless_confirm = allow_headless_confirm
        self._tasks: Dict[str, TaskRecord] = {}
        self._subscribers: Dict[str, SSESubscriber] = {}
        self._lock = threading.Lock()
        self._max_workers = max_workers
        self._active_count = 0
        self._active_lock = threading.Lock()
        self._queue: queue.Queue[Optional[str]] = queue.Queue()

    def submit(self, prompt: str, workspace: str = "") -> TaskRecord:
        """Submit a new task and return its record."""
        task = TaskRecord(prompt=prompt, workspace=workspace)
        with self._lock:
            self._tasks[task.id] = task
            self._subscribers[task.id] = SSESubscriber()
        self._queue.put(task.id)
        return task

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda t: t.created_at,
                reverse=True,
            )
            return [t.to_dict() for t in tasks[:limit]]

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return {"ok": False, "error": "task not found"}
            if task.state not in (TaskState.PENDING, TaskState.RUNNING):
                return {"ok": False, "error": f"task is {task.state.value}"}
            task.state = TaskState.CANCELLED
            task.completed_at = time.time()
        self._emit(task_id, "cancelled", {"task_id": task_id})
        return {"ok": True, "task_id": task_id}

    def subscribe(self, task_id: str, callback: Callable[[str, Dict[str, Any]], None]) -> bool:
        with self._lock:
            sub = self._subscribers.get(task_id)
            if sub is None:
                return False
            sub.subscribe(callback)
            return True

    def unsubscribe(self, task_id: str, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        with self._lock:
            sub = self._subscribers.get(task_id)
            if sub is not None:
                sub.unsubscribe(callback)

    def _emit(self, task_id: str, event_type: str, data: Dict[str, Any]) -> None:
        with self._lock:
            sub = self._subscribers.get(task_id)
        if sub is not None:
            sub.emit(event_type, data)

    def start_workers(self) -> None:
        """Start background worker threads."""
        for _ in range(self._max_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()

    def _worker_loop(self) -> None:
        """Worker thread: pick tasks from the queue and execute them."""
        while True:
            task_id = self._queue.get()
            if task_id is None:
                break

            with self._lock:
                task = self._tasks.get(task_id)
                if task is None or task.state == TaskState.CANCELLED:
                    continue

            with self._active_lock:
                self._active_count += 1

            try:
                self._execute_task(task)
            except Exception as e:
                with self._lock:
                    task.state = TaskState.FAILED
                    task.error = str(e)
                    task.completed_at = time.time()
                self._emit(task_id, "error", {"task_id": task_id, "error": str(e)})
                self._notify_if_configured(task)
            finally:
                with self._active_lock:
                    self._active_count -= 1

    def _execute_task(self, task: TaskRecord) -> None:
        """Execute a single task using AgentRuntime (headless)."""
        with self._lock:
            task.state = TaskState.RUNNING
            task.started_at = time.time()

        self._emit(task.id, "started", {"task_id": task.id, "prompt": task.prompt})

        # Import AgentRuntime lazily — the daemon may be started before
        # the full Tera Pilot package is installed.
        try:
            from .agent_runtime import AgentRuntime
        except ImportError:
            # Fallback: try the legacy path
            from .agent_runtime.runtime import AgentRuntime

        workspace = task.workspace or os.getcwd()

        # Create a headless AgentRuntime (same as CLI/TUI path).
        # v2.3.4-fix: pass a config-backed registry (see _build_registry).
        agent = AgentRuntime(registry=_build_registry(), workspace=workspace)
        # v2.3.4-security (P0.4): headless confirmations fail closed unless
        # the daemon was started with --no-confirm (explicit opt-in).
        if self.allow_headless_confirm:
            agent.tools.headless_confirm = "allow"

        # Event callback — streams to SSE subscribers
        def on_event(kind: str, data: Dict[str, Any]) -> None:
            self._emit(task.id, kind, data)

        agent.on_event = on_event

        # Check for cancellation
        def cancel_check() -> bool:
            with self._lock:
                t = self._tasks.get(task.id)
                return t is not None and t.state == TaskState.CANCELLED

        agent.set_cancel_check(cancel_check)

        # Run the agent
        result = agent.run(task.prompt)

        # Update task record
        with self._lock:
            if task.state == TaskState.CANCELLED:
                return

            task.state = TaskState.COMPLETED
            task.completed_at = time.time()
            task.result = getattr(result, "output", "") or ""
            task.error = getattr(result, "error", None)

            # Extract token/cost info if available
            try:
                stats = agent.get_token_stats()
                task.token_count = stats.get("total_tokens", 0)
                task.cost_usd = stats.get("total_cost_usd", 0.0)
            except Exception:
                pass

            try:
                task.tools_used = getattr(agent, "_tool_call_count", 0)
            except Exception:
                pass

        self._emit(task.id, "completed", task.to_dict())
        self._notify_if_configured(task)

    def _notify_if_configured(self, task: TaskRecord) -> None:
        """Send a notification if the notifier is configured."""
        try:
            from .notifier import get_notifier, EventKind, NotificationEvent
            notifier = get_notifier()
            if notifier.status()["enabled_backends"] == 0:
                return

            event_kind = EventKind.DONE if task.state == TaskState.COMPLETED else EventKind.ERROR
            duration = task.duration_s
            duration_str = f"{int(duration // 60)}m {int(duration % 60)}s" if duration else "?"

            message = (
                f"Task: {task.prompt[:100]}\n"
                f"Status: {task.state.value}\n"
                f"Duration: {duration_str}\n"
                f"Tokens: {task.token_count:,}"
            )
            if task.error:
                message += f"\nError: {task.error[:200]}"

            data: Dict[str, Any] = {"task_id": task.id}
            if task.token_count:
                data["tokens"] = f"{task.token_count:,}"
            if task.cost_usd:
                data["cost"] = f"${task.cost_usd:.4f}"

            notifier.notify_async(NotificationEvent(
                event=event_kind,
                title=f"Tera Pilot: Task {task.state.value}",
                message=message,
                data=data,
            ))
        except Exception:
            pass

    def shutdown(self) -> None:
        """Signal all workers to stop."""
        for _ in range(self._max_workers):
            self._queue.put(None)


# ── HTTP API handler ───────────────────────────────────────────

class DaemonHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Tera Pilot daemon API."""

    # Set by the server on startup
    task_queue: TaskQueue
    auth_token: Optional[str] = None

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default stderr logging
        pass

    def _check_auth(self) -> bool:
        """Check Bearer token authentication.

        v2.3.4-security: use ``secrets.compare_digest`` (constant-time)
        instead of plain ``==`` so the token comparison has no timing
        side-channel — mirrors api_server.py's _check_auth.
        """
        if self.auth_token is None:
            return True
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return secrets.compare_digest(auth_header[7:], self.auth_token)
        return False

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # v2.3.4-security: cap request bodies so a malicious local caller
    # can't exhaust memory with a huge Content-Length claim.
    MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB is plenty for task/JSON payloads

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > self.MAX_BODY_BYTES:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ── GET routes ──────────────────────────────────────────────

    def do_GET(self) -> None:
        if not self._check_auth():
            self._send_json({"error": "unauthorized"}, 401)
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json({"status": "ok", "uptime": time.time()})
        elif path == "/tasks":
            # Guard the limit param: a non-integer ?limit=abc must not
            # 500 the request thread (ValueError from int()).
            try:
                limit = int(query.get("limit", ["50"])[0])
            except (ValueError, TypeError):
                limit = 50
            limit = max(1, min(limit, 500))
            self._send_json({"tasks": self.task_queue.list_tasks(limit=limit)})
        elif path.startswith("/task/") and "/stream" not in path:
            task_id = path.split("/task/")[1]
            task = self.task_queue.get_task(task_id)
            if task is None:
                self._send_json({"error": "task not found"}, 404)
            else:
                self._send_json(task.to_dict())
        elif path.startswith("/stream/"):
            task_id = path.split("/stream/")[1]
            self._handle_sse(task_id)
        else:
            self._send_json({"error": "not found"}, 404)

    # ── POST routes ─────────────────────────────────────────────

    def do_POST(self) -> None:
        if not self._check_auth():
            self._send_json({"error": "unauthorized"}, 401)
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/task":
            body = self._read_body()
            prompt = body.get("prompt", "")
            workspace = body.get("workspace", "")
            if not prompt:
                self._send_json({"error": "prompt is required"}, 400)
                return
            task = self.task_queue.submit(prompt, workspace)
            self._send_json(task.to_dict(), 201)
        elif path.startswith("/task/") and path.endswith("/cancel"):
            task_id = path.split("/task/")[1].replace("/cancel", "")
            result = self.task_queue.cancel_task(task_id)
            if result.get("ok"):
                self._send_json(result)
            else:
                self._send_json(result, 400)
        else:
            self._send_json({"error": "not found"}, 404)

    # ── SSE streaming ───────────────────────────────────────────

    def _handle_sse(self, task_id: str) -> None:
        """Server-Sent Events stream for a running task."""
        task = self.task_queue.get_task(task_id)
        if task is None:
            self._send_json({"error": "task not found"}, 404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        # Send initial state
        self._sse_send("state", task.to_dict())

        # If the task is already in a terminal state (completed / failed /
        # cancelled), there will never be another event — close the stream
        # now instead of keepalive-looping until the client disconnects.
        #
        # v2.3.4-fix: also mark the connection for close. The response
        # above advertises ``Connection: keep-alive``, so returning alone
        # left the socket open: an SSE client polling a finished task
        # would block in read() forever (until ITS timeout) waiting for a
        # stream end that never came. Setting close_connection makes
        # BaseHTTPRequestHandler close the socket right after this
        # response, so the client sees EOF immediately.
        if task.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            self.close_connection = True
            return

        # Subscribe to updates
        event_queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(maxsize=100)

        def on_event(event_type: str, data: Dict[str, Any]) -> None:
            try:
                event_queue.put_nowait({"type": event_type, "data": data})
            except queue.Full:
                pass

        self.task_queue.subscribe(task_id, on_event)

        # v2.3.9-fix (SSE race): the task may have reached a terminal
        # state between the initial check above and the subscribe call.
        # In that window the terminal "completed"/"failed"/"cancelled"
        # event was already emitted to a subscriber list that did not
        # include us yet — so it is lost, and without this re-check the
        # stream would keepalive-loop forever (leaked connection, client
        # blocked in read() until its own timeout). Re-check after
        # subscribing and close if the task is already terminal.
        task = self.task_queue.get_task(task_id)
        if task is not None and task.state in (
            TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED,
        ):
            self.close_connection = True
            return

        try:
            # Stream events until the task is done
            while True:
                try:
                    event = event_queue.get(timeout=30)
                    if event is None:
                        break
                    self._sse_send(event["type"], event["data"])
                    # Check if task is in a terminal state
                    data = event.get("data", {})
                    if isinstance(data, dict):
                        state = data.get("state", "")
                        if state in ("completed", "failed", "cancelled"):
                            break
                except queue.Empty:
                    # Send keepalive
                    self._sse_send("keepalive", {"ts": time.time()})
                    # v2.3.9-fix: also exit when the task reached a
                    # terminal state without us ever seeing the terminal
                    # event (e.g. the bounded event queue dropped it under
                    # load). Re-checking here closes the stream instead of
                    # keepalive-looping on a finished task.
                    task = self.task_queue.get_task(task_id)
                    if task is not None and task.state in (
                        TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED,
                    ):
                        self.close_connection = True
                        break
        except Exception:
            pass
        finally:
            self.task_queue.unsubscribe(task_id, on_event)

    def _sse_send(self, event_type: str, data: Any) -> None:
        """Send a single SSE event."""
        try:
            payload = json.dumps(data)
            message = f"event: {event_type}\ndata: {payload}\n\n"
            self.wfile.write(message.encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass


# ── Remote task mode (inbound listener) config ─────────────────

# Remote task mode: accept tasks from a messenger (Telegram first). Config
# lives in ~/.tera_pilot/inbound.json, mirroring notifiers.json:
#
#   {
#     "backend": "telegram",
#     "telegram_token": "123456:ABC...",
#     "allowed_chat_ids": ["123456789"],
#     "workspace": "/path/to/projects"
#   }
#
# ``allowed_chat_ids`` is MANDATORY — the listener refuses to start
# without it (no wildcard "accept anyone" mode, ever). See
# tera_pilot/inbound_listener.py for the full contract.
def _inbound_config_path() -> str:
    """Path to the remote-task-mode inbound config (resolved per call so
    tests can override HOME)."""
    return os.path.expanduser("~/.tera_pilot/inbound.json")


def load_inbound_config() -> Dict[str, Any]:
    """Load the remote-task-mode inbound listener config."""
    try:
        with open(_inbound_config_path(), "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_inbound_config(config: Dict[str, Any]) -> None:
    """Persist the remote-task-mode inbound listener config."""
    path = _inbound_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)


# ── Daemon config ──────────────────────────────────────────────

_DAEMON_CONFIG_PATH = os.path.expanduser("~/.tera_pilot/daemon.json")


def load_daemon_config() -> Dict[str, Any]:
    """Load daemon configuration from ~/.tera_pilot/daemon.json."""
    try:
        with open(_DAEMON_CONFIG_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_daemon_config(config: Dict[str, Any]) -> None:
    """Save daemon configuration."""
    os.makedirs(os.path.dirname(_DAEMON_CONFIG_PATH), exist_ok=True)
    tmp = _DAEMON_CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, _DAEMON_CONFIG_PATH)


def generate_token() -> str:
    """Generate a random API token."""
    return f"tera-pilot-{uuid.uuid4().hex}"


# ── Daemon server ──────────────────────────────────────────────

class TeraPilotDaemon:
    """The Tera Pilot daemon server — manages the task queue and HTTP API."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        auth_token: Optional[str] = None,
        max_workers: int = 2,
        notify: Optional[str] = None,
        allow_headless_confirm: bool = False,
        inbound: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.auth_token = auth_token
        # v2.3.4-security (P0.4): --no-confirm opts into headless
        # auto-approve; default stays fail-closed.
        self.task_queue = TaskQueue(max_workers=max_workers, allow_headless_confirm=allow_headless_confirm)
        self._notify = notify
        # Remote task mode: accept tasks from a messenger backend (telegram).
        self._inbound = inbound
        self._inbound_listener: Any = None
        self._server: Optional[ThreadingHTTPServer] = None

    def start(self) -> None:
        """Start the daemon server (blocking)."""
        # Set up auth token
        if self.auth_token is None:
            config = load_daemon_config()
            self.auth_token = config.get("auth_token")
            if self.auth_token is None:
                self.auth_token = generate_token()
                config["auth_token"] = self.auth_token
                save_daemon_config(config)
                print(f"Generated new API token: {self.auth_token}")
                print(f"Saved to {_DAEMON_CONFIG_PATH}")
            else:
                print(f"Using existing API token from {_DAEMON_CONFIG_PATH}")

        # Enable notifier if requested
        if self._notify:
            self._enable_notifier(self._notify)

        # Register notifier hooks
        try:
            from .notifier import get_notifier
            get_notifier().register_hooks()
        except Exception:
            pass

        # Remote task mode: accept tasks from a messenger if requested
        if self._inbound:
            self._enable_inbound(self._inbound)

        # Configure handler
        DaemonHandler.task_queue = self.task_queue
        DaemonHandler.auth_token = self.auth_token

        # Start workers
        self.task_queue.start_workers()

        # Start HTTP server. MUST be threaded: each SSE stream
        # (/stream/:id) blocks its request handler until the task
        # finishes, so a single-stream server would stall every other
        # endpoint (task submit, cancel, health) for the whole run.
        # This mirrors api_server.py / web_server.py which both use
        # ThreadingHTTPServer.
        self._server = ThreadingHTTPServer((self.host, self.port), DaemonHandler)
        print(f"Tera Pilot daemon running on http://{self.host}:{self.port}")
        print(f"API token: {self.auth_token}")
        print(f"Workers: {self.task_queue._max_workers}")
        print(f"Notifications: {self._notify or 'off'}")
        print()
        print("Endpoints:")
        print(f"  POST http://{self.host}:{self.port}/task")
        print(f"  GET  http://{self.host}:{self.port}/task/:id")
        print(f"  GET  http://{self.host}:{self.port}/tasks")
        print(f"  POST http://{self.host}:{self.port}/task/:id/cancel")
        print(f"  GET  http://{self.host}:{self.port}/stream/:id")
        print(f"  GET  http://{self.host}:{self.port}/health")
        print()
        print("Press Ctrl+C to stop.")

        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")
            self.task_queue.shutdown()
            self._server.shutdown()
            if self._inbound_listener is not None:
                try:
                    self._inbound_listener.stop()
                except Exception:
                    pass

    def _enable_inbound(self, backend_name: str) -> None:
        """Start the remote-task-mode inbound listener (tasks via messenger).

        Reads ~/.tera_pilot/inbound.json and wires the listener to the
        task queue: an accepted message becomes a task, a reply of STOP
        cancels the running task. The daemon keeps serving HTTP; the
        listener runs in its own background thread.
        """
        try:
            from .inbound_listener import (
                InboundListenerConfig,
                make_inbound_listener,
                make_daemon_callback,
                make_daemon_stop_callback,
            )
            cfg = load_inbound_config()
            if not cfg:
                print("[inbound] no config at ~/.tera_pilot/inbound.json — create one, e.g.:")
                print("  {\"backend\": \"telegram\", \"telegram_token\": \"...\", \"allowed_chat_ids\": [\"123456\"]}")
                print("[inbound] remote task mode disabled.")
                return

            config = InboundListenerConfig(
                backend=cfg.get("backend", backend_name),
                telegram_token=cfg.get("telegram_token", ""),
                allowed_chat_ids=set(cfg.get("allowed_chat_ids", []) or []),
                allowed_sender_ids=set(cfg.get("allowed_sender_ids", []) or []),
                workspace=cfg.get("workspace", ""),
                stop_keyword=cfg.get("stop_keyword", "STOP"),
            )
            listener = make_inbound_listener(
                config,
                make_daemon_callback(self.task_queue, workspace=config.workspace),
                on_stop=make_daemon_stop_callback(self.task_queue),
            )
            listener.start()
            self._inbound_listener = listener
            print(f"[inbound] remote task mode enabled: {config.backend} (chats: {sorted(config.allowed_chat_ids)})")
        except Exception as e:
            print(f"[inbound] failed to enable remote task mode: {e}")

    def _enable_notifier(self, backend_name: str) -> None:
        """Enable a notification backend for the daemon."""
        try:
            from .notifier import get_notifier
            notifier = get_notifier()
            backends = notifier.list_backends()
            configured = [b["name"] for b in backends]
            if backend_name in configured:
                notifier.set_backend_enabled(backend_name, True)
                print(f"Notifications enabled: {backend_name}")
            else:
                print(f"Backend '{backend_name}' not configured.")
                print("Configure it first: ~/.tera_pilot/notifiers.json")
                print("Available backends: telegram, discord, slack")
        except Exception as e:
            print(f"Failed to enable notifier: {e}")


# ── One-shot task runner ───────────────────────────────────────

def run_single_task(
    prompt: str,
    workspace: str = "",
    notify: Optional[str] = None,
    allow_headless_confirm: bool = False,
) -> None:
    """Run a single task with optional notification when done.

    This is a convenience function for the `tera-pilot-daemon task` CLI command.
    It does NOT start the HTTP server — just runs the agent and optionally
    sends a notification.

    v2.3.4-security (P0.4): headless confirmations fail closed unless
    ``allow_headless_confirm`` (--no-confirm) is set explicitly.
    """
    task = TaskRecord(prompt=prompt, workspace=workspace)
    task.state = TaskState.RUNNING
    task.started_at = time.time()

    print(f"Running task: {prompt[:80]}...")
    print(f"Workspace: {workspace or os.getcwd()}")

    try:
        from .agent_runtime import AgentRuntime
    except ImportError:
        from .agent_runtime.runtime import AgentRuntime

    # v2.3.4-fix: pass a config-backed registry (see _build_registry).
    agent = AgentRuntime(registry=_build_registry(), workspace=workspace or os.getcwd())
    if allow_headless_confirm:
        agent.tools.headless_confirm = "allow"
    result = agent.run(prompt)

    task.state = TaskState.COMPLETED
    task.completed_at = time.time()
    task.result = getattr(result, "output", "") or ""
    task.error = getattr(result, "error", None)

    try:
        stats = agent.get_token_stats()
        task.token_count = stats.get("total_tokens", 0)
        task.cost_usd = stats.get("total_cost_usd", 0.0)
    except Exception:
        pass

    # Print result
    duration = task.duration_s
    duration_str = f"{int(duration // 60)}m {int(duration % 60)}s" if duration else "?"
    print(f"\nTask completed in {duration_str}")
    if task.result:
        print(f"Result: {task.result[:500]}")
    if task.error:
        print(f"Error: {task.error}")

    # Send notification
    if notify:
        try:
            from .notifier import get_notifier, EventKind, NotificationEvent
            notifier = get_notifier()
            message = (
                f"Task: {prompt[:100]}\n"
                f"Status: {task.state.value}\n"
                f"Duration: {duration_str}\n"
                f"Tokens: {task.token_count:,}"
            )
            if task.error:
                message += f"\nError: {task.error[:200]}"

            notifier.notify_async(NotificationEvent(
                event=EventKind.DONE if task.state == TaskState.COMPLETED else EventKind.ERROR,
                title=f"Tera Pilot: Task {task.state.value}",
                message=message,
                data={"task_id": task.id, "tokens": f"{task.token_count:,}"},
            ))
        except Exception as e:
            print(f"Notification failed: {e}")


# ── CLI entry point ────────────────────────────────────────────

def main() -> None:
    """CLI entry point for tera-pilot-daemon."""
    parser = argparse.ArgumentParser(
        prog="tera-pilot-daemon",
        description="Tera Pilot remote agent daemon — run tasks on a server with notifications",
    )
    sub = parser.add_subparsers(dest="command")

    # Daemon mode
    # v2.3.4-security (P0.4): explicit opt-in for headless auto-approve.
    # Without --no-confirm, headless confirmations fail CLOSED (side-
    # effecting actions are blocked, never silently run).
    _NO_CONFIRM_HELP = (
        "Allow side-effecting agent actions (commands, writes, git) to run "
        "WITHOUT confirmation in headless mode. Default: fail-closed — "
        "these actions are blocked when no UI is present."
    )
    _INBOUND_HELP = (
        "Remote task mode: accept tasks from a messenger backend (telegram). "
        "Config: ~/.tera_pilot/inbound.json (telegram_token + allowed_chat_ids). "
        "Reply STOP to cancel the running task."
    )
    daemon_parser = sub.add_parser("serve", help="Start the daemon server")
    daemon_parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    daemon_parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    daemon_parser.add_argument("--token", default=None, help="API auth token (auto-generated if not set)")
    daemon_parser.add_argument("--workers", type=int, default=2, help="Max concurrent workers (default: 2)")
    daemon_parser.add_argument("--notify", default=None, help="Enable notifications (telegram/discord/slack)")
    daemon_parser.add_argument("--inbound", default=None, help=_INBOUND_HELP)
    daemon_parser.add_argument("--no-confirm", action="store_true", dest="no_confirm", help=_NO_CONFIRM_HELP)

    # Single task mode
    task_parser = sub.add_parser("task", help="Run a single task with notification")
    task_parser.add_argument("prompt", help="Task prompt")
    task_parser.add_argument("--workspace", default="", help="Workspace path")
    task_parser.add_argument("--notify", default=None, help="Enable notifications (telegram/discord/slack)")
    task_parser.add_argument("--no-confirm", action="store_true", dest="no_confirm", help=_NO_CONFIRM_HELP)

    # Task from file
    file_parser = sub.add_parser("task-file", help="Run a task from a file")
    file_parser.add_argument("file", help="File containing the task prompt")
    file_parser.add_argument("--workspace", default="", help="Workspace path")
    file_parser.add_argument("--notify", default=None, help="Enable notifications (telegram/discord/slack)")
    file_parser.add_argument("--no-confirm", action="store_true", dest="no_confirm", help=_NO_CONFIRM_HELP)

    # Default: serve mode (no subcommand)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--notify", default=None)
    parser.add_argument("--inbound", default=None, help=_INBOUND_HELP)
    parser.add_argument("--no-confirm", action="store_true", dest="no_confirm", help=_NO_CONFIRM_HELP)

    args = parser.parse_args()
    no_confirm = bool(getattr(args, "no_confirm", False))
    inbound = getattr(args, "inbound", None)

    if args.command == "task":
        run_single_task(args.prompt, workspace=args.workspace, notify=args.notify,
                        allow_headless_confirm=no_confirm)
    elif args.command == "task-file":
        with open(args.file, "r") as f:
            prompt = f.read().strip()
        run_single_task(prompt, workspace=args.workspace, notify=args.notify,
                        allow_headless_confirm=no_confirm)
    elif args.command == "serve":
        daemon = TeraPilotDaemon(
            host=args.host,
            port=args.port,
            auth_token=args.token,
            max_workers=args.workers,
            notify=args.notify,
            allow_headless_confirm=no_confirm,
            inbound=inbound,
        )
        daemon.start()
    else:
        # Default: serve mode
        daemon = TeraPilotDaemon(
            host=args.host,
            port=args.port,
            auth_token=args.token,
            max_workers=args.workers,
            notify=args.notify,
            allow_headless_confirm=no_confirm,
            inbound=inbound,
        )
        daemon.start()


if __name__ == "__main__":
    main()
