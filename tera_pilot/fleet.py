"""fleet.py — run several Tera Pilot agent profiles at once (a "fleet").

Each fleet agent is a headless AgentRuntime running its own profile (e.g.
``code``, ``video``, ``reviewer``, ``fable5``, or a custom one) against
its own workspace. A single supervisor process spawns one worker thread
per agent; every agent continuously writes a compact status file and an
activity log, so ONE "main" terminal can watch a live summary of what
every agent is doing — no need to open a window per agent.

Layout under ~/.tera_pilot/fleet/<fleet_id>/:
    fleet.json        — fleet metadata (created_at, agent list)
    agents/<id>.json  — live status of one agent (atomic writes)
    tasks/<id>.jsonl  — task mailbox for one agent (appended by `fleet task`)
    stop              — presence ⇒ workers exit after the current task

CLI (see tera_pilot/cli.py):
    tera-pilot fleet start --agent video:/path/to/videos --agent code:~/code
    tera-pilot fleet task video "make a 30s teaser from clips/"
    tera-pilot fleet watch            # the "main terminal"
    tera-pilot fleet stop

Security model (headless — no interactive approvals available):
  - controlled → autonomy=always_ask, confirmations FAIL CLOSED. Side-
    effecting tools (write/edit/execute) are blocked and logged as such;
    the agent is effectively read-only until a human runs it interactively.
  - balanced   → autonomy=new_files_only + headless auto-approve. New
    files are created freely; other side effects run headless.
  - free       → autonomy=never_ask. Maximum freedom, nothing gated.
This is the fleet interpretation of the profile's security level — more
control or more freedom, as the user picked per agent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

FLEET_ROOT = "fleet"
DEFAULT_FLEET_ID = "default"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# state values written to agents/<id>.json
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"

# Profile security → (autonomy, headless_auto_approve)
HEADLESS_MAP = {
    "controlled": ("always_ask", False),
    "balanced": ("new_files_only", True),
    "free": ("never_ask", True),
}

RECENT_ACTIVITY_LIMIT = 25


# ── paths ───────────────────────────────────────────────────────────────

def _fleet_root() -> Path:
    from tera_pilot.utils import get_tera_pilot_dir

    return get_tera_pilot_dir() / FLEET_ROOT


def fleet_dir(fleet_id: str = DEFAULT_FLEET_ID) -> Path:
    return _fleet_root() / fleet_id


def agents_dir(fleet_id: str = DEFAULT_FLEET_ID) -> Path:
    return fleet_dir(fleet_id) / "agents"


def tasks_dir(fleet_id: str = DEFAULT_FLEET_ID) -> Path:
    return fleet_dir(fleet_id) / "tasks"


def status_path(fleet_id: str, agent_id: str) -> Path:
    return agents_dir(fleet_id) / f"{agent_id}.json"


def mailbox_path(fleet_id: str, agent_id: str) -> Path:
    return tasks_dir(fleet_id) / f"{agent_id}.jsonl"


def stop_path(fleet_id: str = DEFAULT_FLEET_ID) -> Path:
    return fleet_dir(fleet_id) / "stop"


def valid_agent_id(agent_id: str) -> bool:
    return bool(_ID_RE.match(agent_id or ""))


# ── status file helpers ─────────────────────────────────────────────────

_status_lock = threading.Lock()


def _write_status(path: Path, status: Dict[str, Any]) -> None:
    with _status_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, ensure_ascii=False)
            f.flush()
        os.replace(tmp, path)


def read_status(fleet_id: str, agent_id: str) -> Dict[str, Any]:
    path = status_path(fleet_id, agent_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _log_activity(status: Dict[str, Any], kind: str, text: str) -> None:
    entry = {"t": _now(), "kind": kind, "text": text}
    recent = status.setdefault("recent_activity", [])
    recent.append(entry)
    del recent[:-RECENT_ACTIVITY_LIMIT]
    status["last_activity"] = text
    status["updated_at"] = _now()


# ── task mailbox ────────────────────────────────────────────────────────

def submit_task(fleet_id: str, agent_id: str, prompt: str) -> Dict[str, Any]:
    """Queue a task for an agent (works from any terminal while the fleet
    is running). Returns {ok, task_id, agent, mailbox}."""
    if not valid_agent_id(agent_id):
        return {"ok": False, "error": f"invalid agent id: {agent_id!r}"}
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "empty prompt"}
    path = mailbox_path(fleet_id, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    task_id = uuid.uuid4().hex[:12]
    line = json.dumps(
        {"task_id": task_id, "prompt": prompt, "created_at": time.time()},
        ensure_ascii=False,
    )
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        return {"ok": True, "task_id": task_id, "agent": agent_id, "mailbox": str(path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _take_task(fleet_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
    """Pop the first unclaimed line from the agent's mailbox. Lines are
    claimed by rewriting the file without the taken line (atomic rename)."""
    path = mailbox_path(fleet_id, agent_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        if not lines:
            return None
        first = json.loads(lines[0])
        if len(lines) > 1:
            tmp = path.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines[1:]) + ("\n" if len(lines) > 1 else ""))
            os.replace(tmp, path)
        else:
            try:
                path.unlink()
            except OSError:
                pass
        return first
    except Exception:
        return None


def list_fleets() -> List[Dict[str, Any]]:
    root = _fleet_root()
    out: List[Dict[str, Any]] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta: Dict[str, Any] = {"fleet_id": d.name, "agents": [], "running": False}
        meta_path = d / "fleet.json"
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                fleet_meta = json.load(f)
            if isinstance(fleet_meta, dict):
                meta["created_at"] = fleet_meta.get("created_at")
                meta["planned_agents"] = fleet_meta.get("agents", [])
        except Exception:
            pass
        stopped = (d / "stop").exists()
        meta["running"] = not stopped
        agents_dir_ = d / "agents"
        if agents_dir_.is_dir():
            for sp in sorted(agents_dir_.glob("*.json")):
                try:
                    with open(sp, "r", encoding="utf-8") as f:
                        meta["agents"].append(json.load(f))
                except Exception:
                    continue
        out.append(meta)
    return out


# ── the headless worker ─────────────────────────────────────────────────

@dataclass
class FleetAgentSpec:
    """One agent in a fleet."""
    profile: str                # agent profile id (code/video/reviewer/fable5/custom)
    workspace: str              # absolute workspace path
    agent_id: str = ""          # defaults to the profile id
    section: str = "general"
    max_iterations: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.agent_id:
            self.agent_id = self.profile
        self.workspace = str(Path(self.workspace).expanduser())


def _build_registry():
    """Provider registry configured from ~/.tera_pilot/config.json —
    mirror of the daemon's _build_registry (keys/models/api_base)."""
    from tera_pilot.providers import get_registry, ProviderConfig
    from tera_pilot.utils import load_config

    registry = get_registry()
    try:
        if not registry.list_providers():
            registry.register_default()
    except Exception:
        registry.register_default()
    cfg = load_config() or {}
    for pid, pcfg in (cfg.get("providers") or {}).items():
        try:
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
        except Exception:
            continue
    active = cfg.get("active_provider") or registry.active_id or "ollama"
    try:
        registry.set_active(active)
    except Exception:
        try:
            registry.set_active("ollama")
        except Exception:
            pass
    return registry


class FleetWorker:
    """Headless agent loop for one fleet agent.

    Polls the mailbox, runs each task through AgentRuntime (with the
    profile's persona + security mapping applied), and keeps the status
    file fresh. ``runner`` is injectable for tests.
    """

    def __init__(
        self,
        fleet_id: str,
        spec: FleetAgentSpec,
        *,
        runner: Optional[Callable[[str, str], Dict[str, Any]]] = None,
        poll_interval: float = 0.5,
    ) -> None:
        self.fleet_id = fleet_id
        self.spec = spec
        self._runner = runner
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status: Dict[str, Any] = {
            "fleet_id": fleet_id,
            "agent_id": spec.agent_id,
            "profile": spec.profile,
            "workspace": spec.workspace,
            "state": STATE_IDLE,
            "current_task": None,
            "tasks_done": 0,
            "tasks_failed": 0,
            "last_activity": "",
            "started_at": _now(),
            "updated_at": _now(),
            "recent_activity": [],
        }

    # ------------------------------------------------------------- control
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name=f"fleet-{self.spec.agent_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Block until the worker thread exits (bounded by timeout)."""
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --------------------------------------------------------------- loop
    def _loop(self) -> None:
        _write_status(status_path(self.fleet_id, self.spec.agent_id), self.status)
        while not self._stop_event.is_set():
            if stop_path(self.fleet_id).exists():
                break
            task = _take_task(self.fleet_id, self.spec.agent_id)
            if task is None:
                if self.status["state"] == STATE_RUNNING:
                    self.status["state"] = STATE_IDLE
                    _write_status(status_path(self.fleet_id, self.spec.agent_id), self.status)
                self._stop_event.wait(self._poll_interval)
                continue
            self._run_task(task)
        self.status["state"] = STATE_STOPPED
        _log_activity(self.status, "stop", "worker stopped")
        _write_status(status_path(self.fleet_id, self.spec.agent_id), self.status)

    def _run_task(self, task: Dict[str, Any]) -> None:
        prompt = str(task.get("prompt", ""))
        task_id = str(task.get("task_id", "?"))
        self.status["state"] = STATE_RUNNING
        self.status["current_task"] = prompt
        _log_activity(self.status, "task", f"started task {task_id}: {prompt[:120]}")
        _write_status(status_path(self.fleet_id, self.spec.agent_id), self.status)
        try:
            if self._runner is not None:
                result = self._runner(prompt, task_id)
            else:
                result = self._run_agent(prompt)
            ok = bool(result.get("success", result.get("ok", False)))
            summary = str(result.get("output", "") or "")
            if ok:
                self.status["tasks_done"] += 1
                tail = " ".join(summary.split())[:160] or "done"
                _log_activity(self.status, "done", f"task {task_id} completed: {tail}")
            else:
                self.status["tasks_failed"] += 1
                err = str(result.get("error", "") or "")[:160]
                _log_activity(self.status, "error", f"task {task_id} failed: {err or 'unknown error'}")
        except Exception as e:
            self.status["tasks_failed"] += 1
            _log_activity(self.status, "error", f"task {task_id} crashed: {str(e)[:160]}")
        finally:
            self.status["state"] = STATE_IDLE
            self.status["current_task"] = None
            _write_status(status_path(self.fleet_id, self.spec.agent_id), self.status)

    # ----------------------------------------------------------- agent run
    def _run_agent(self, prompt: str) -> Dict[str, Any]:
        """Build (once) and run the AgentRuntime for this profile."""
        if getattr(self, "_agent", None) is None:
            self._agent = self._build_agent()
        agent = self._agent
        try:
            result = agent.run(prompt)
            return {
                "success": bool(getattr(result, "success", False)),
                "output": str(getattr(result, "output", "") or ""),
                "iterations": getattr(result, "iterations", 0),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _build_agent(self):
        from tera_pilot.agent_runtime import AgentRuntime

        profile = None
        try:
            from tera_pilot.agent_profiles import get_agent_profile_manager

            profile = get_agent_profile_manager().get_profile(self.spec.profile)
        except Exception:
            pass

        agent = AgentRuntime(
            registry=_build_registry(),
            workspace=self.spec.workspace,
            max_iterations=max(1, int(self.spec.max_iterations or 8)),
            section=self.spec.section,
            on_event=self._on_event,
        )
        security = "controlled"
        if profile is not None:
            try:
                from tera_pilot.agent_profiles import apply_profile_to_runtime

                apply_profile_to_runtime(agent, profile)
                security = str(profile.get("security", "controlled"))
            except Exception:
                pass
        else:
            agent.set_autonomy("always_ask")
        # Headless override: free → never_ask (apply_profile_to_runtime maps
        # free → new_files_only; headless freedom means never even gating).
        autonomy, auto_approve = HEADLESS_MAP.get(
            security, ("always_ask", False)
        )
        agent.set_autonomy(autonomy)
        agent.tools.headless_confirm = "allow" if auto_approve else "fail_closed"
        return agent

    def _on_event(self, event: Any, data: Dict[str, Any]) -> None:
        """Translate runtime events into short one-line summaries."""
        kind = getattr(event, "value", str(event))
        text = ""
        if kind == "tool_called":
            tool = data.get("tool") or data.get("name") or ""
            text = f"ran {tool}"
        elif kind == "tool_result":
            status = data.get("status") or data.get("ok")
            text = f"tool result: {status}"
        elif kind == "thought":
            text = "thinking"
        elif kind == "iteration_end":
            text = f"iteration {data.get('iteration', '?')} done"
        elif kind == "error":
            text = f"error: {str(data.get('error', ''))[:120]}"
        elif kind == "done":
            text = "finished"
        if text:
            _log_activity(self.status, kind, text)
            _write_status(status_path(self.fleet_id, self.spec.agent_id), self.status)


# ── supervisor ──────────────────────────────────────────────────────────

class Fleet:
    """Supervisor: spawns one worker per agent, tracks status, handles stop."""

    def __init__(self, fleet_id: str = DEFAULT_FLEET_ID) -> None:
        self.fleet_id = fleet_id
        self.workers: List[FleetWorker] = []
        self._threads: List[threading.Thread] = []
        self._started = False

    def add_agent(self, spec: FleetAgentSpec) -> Dict[str, Any]:
        if not valid_agent_id(spec.agent_id):
            return {"ok": False, "error": f"invalid agent id: {spec.agent_id!r}"}
        if any(w.spec.agent_id == spec.agent_id for w in self.workers):
            return {"ok": False, "error": f"duplicate agent id: {spec.agent_id}"}
        if not Path(spec.workspace).is_dir():
            return {"ok": False, "error": f"workspace not a directory: {spec.workspace}"}
        self.workers.append(FleetWorker(self.fleet_id, spec))
        return {"ok": True}

    def start(self) -> Dict[str, Any]:
        if self._started:
            return {"ok": False, "error": "fleet already started"}
        if not self.workers:
            return {"ok": False, "error": "no agents in fleet"}
        # reset any stale stop flag
        try:
            stop_path(self.fleet_id).unlink()
        except OSError:
            pass
        fleet_dir(self.fleet_id).mkdir(parents=True, exist_ok=True)
        meta = {
            "fleet_id": self.fleet_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "agents": [
                {
                    "agent_id": w.spec.agent_id,
                    "profile": w.spec.profile,
                    "workspace": w.spec.workspace,
                }
                for w in self.workers
            ],
        }
        tmp = fleet_dir(self.fleet_id) / "fleet.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        os.replace(tmp, fleet_dir(self.fleet_id) / "fleet.json")
        for w in self.workers:
            w.start()
        self._started = True
        return {"ok": True, "fleet_id": self.fleet_id, "agents": len(self.workers)}

    def stop(self) -> Dict[str, Any]:
        """Signal all workers to stop after their current task."""
        for w in self.workers:
            w.stop()
        try:
            stop_path(self.fleet_id).write_text("stopped\n", encoding="utf-8")
        except OSError:
            pass
        return {"ok": True, "fleet_id": self.fleet_id}

    def wait(self, timeout: Optional[float] = None) -> None:
        """Block until all workers finish (or timeout)."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            alive = [w for w in self.workers if w.running]
            if not alive:
                return
            if deadline is not None and time.time() > deadline:
                return
            time.sleep(0.2)


def start_fleet_cli(agents: List[Dict[str, Any]], fleet_id: str = DEFAULT_FLEET_ID) -> int:
    """Start a fleet in the foreground. ``agents`` is a list of
    {profile, workspace, agent_id?, section?, max_iterations?} dicts.
    Returns the process exit code. Ctrl+C stops the fleet."""
    fleet = Fleet(fleet_id)
    for a in agents:
        r = fleet.add_agent(
            FleetAgentSpec(
                profile=a["profile"],
                workspace=a["workspace"],
                agent_id=a.get("agent_id", ""),
                section=a.get("section", "general"),
                max_iterations=a.get("max_iterations"),
            )
        )
        if not r.get("ok"):
            print(f"✗ {a['profile']}: {r.get('error')}")
            return 1
    r = fleet.start()
    if not r.get("ok"):
        print(f"✗ {r.get('error')}")
        return 1
    print(
        f"Fleet [bold]{fleet_id}[/bold] started with {len(fleet.workers)} agents.\n"
        f"  Queue tasks:  tera-pilot fleet task <agent> \"<prompt>\"\n"
        f"  Watch:        tera-pilot fleet watch [--fleet {fleet_id}]\n"
        f"  Stop:         Ctrl+C (or: tera-pilot fleet stop)\n"
    )
    try:
        fleet.wait()
    except KeyboardInterrupt:
        print("\nStopping fleet…")
    finally:
        fleet.stop()
        fleet.wait(timeout=5)
    return 0


# ── main-terminal watch (rendered by `tera-pilot fleet watch`) ─────────

def collect_fleet_status(fleet_id: str = DEFAULT_FLEET_ID) -> List[Dict[str, Any]]:
    """Read the status files of every agent in a fleet (sorted by id)."""
    d = agents_dir(fleet_id)
    out: List[Dict[str, Any]] = []
    if not d.is_dir():
        return out
    for sp in sorted(d.glob("*.json")):
        try:
            with open(sp, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                out.append(data)
        except Exception:
            continue
    return out


def render_fleet_table(statuses: List[Dict[str, Any]], fleet_id: str = DEFAULT_FLEET_ID) -> str:
    """Rich-rendered summary table for the main-terminal watch."""
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    table = Table(expand=True, box=None, padding=(0, 1))
    table.add_column("Agent", style="bold cyan", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Tasks", justify="right", no_wrap=True)
    table.add_column("Last activity", overflow="ellipsis")
    for s in statuses:
        state = s.get("state", "?")
        state_color = {
            "running": "yellow",
            "done": "green",
            "error": "red",
            "stopped": "dim",
        }.get(state, "white")
        done = s.get("tasks_done", 0)
        failed = s.get("tasks_failed", 0)
        tasks = f"[green]{done}[/green]/[red]{failed}[/red]" if failed else str(done)
        last = s.get("last_activity") or "(idle)"
        table.add_row(
            f"{s.get('agent_id', '?')} [dim]({s.get('profile', '')})[/dim]",
            f"[{state_color}]{state}[/{state_color}]",
            tasks,
            last,
        )
    header = f"[bold]Tera Pilot fleet[/bold] [dim]— {fleet_id}[/dim]"
    for s in statuses:
        if s.get("state") == "running" and s.get("current_task"):
            header += f"\n[dim]  {s.get('agent_id')} → {(s.get('current_task') or '')[:100]}[/dim]"
    return Panel(Group(table, Text(header)), border_style="cyan", padding=(0, 1))
