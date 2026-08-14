"""
Swarm Mode — working swarm toggle with auto-exit.

Ported from Kimi Code's packages/agent-core/src/agent/swarm/index.ts.

SwarmMode manages a persistent toggle that enables batch subagent
execution. In Kimi, this is a simple class tracking the trigger type.

Key differences from the old Tera Pilot SwarmManager:
  - Actually works (old one was a stub registry)
  - Auto-exit for one-shot tasks (trigger='task' or 'tool')
  - System prompt injection when entering/exiting swarm mode
  - Event emission for UI status updates

SwarmModeTrigger types (ported from Kimi):
  - manual: persistent toggle (/swarm on) — stays on until /swarm off
  - task: one-shot /swarm prompt — exits after the task completes
  - tool: AgentSwarm tool entry — exits after the tool completes
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class SwarmTrigger(Enum):
    MANUAL = "manual"
    TASK = "task"
    TOOL = "tool"


# System prompt additions (ported from Kimi's enter-reminder.md / exit-reminder.md)
SWARM_MODE_ENTER_REMINDER = """\
<swarm_mode>
You are now in SWARM MODE. Multiple sub-agents can work in parallel.
When given a complex task, break it into independent sub-tasks and
use spawn_multi_agents to execute them concurrently. Each sub-agent
works in its own context with role-based tool access.
Monitor sub-agent progress with watchdog_check between waves.
This mode will automatically deactivate after the current task.
</swarm_mode>"""

SWARM_MODE_EXIT_REMINDER = """\
Swarm mode has been deactivated. You are now operating as a single agent.
Multi-agent capabilities are no longer automatically available."""


class SwarmMode:
    """Working swarm mode with toggle and auto-exit.

    Ported from Kimi's SwarmMode class.
    """

    def __init__(self, on_status_change: Optional[Callable[[], None]] = None):
        self._active: Optional[SwarmTrigger] = None
        self._on_status_change = on_status_change

    @property
    def is_active(self) -> bool:
        return self._active is not None

    @property
    def should_auto_exit(self) -> bool:
        """True for task/tool triggers — they auto-exit after completion."""
        return self._active in (SwarmTrigger.TASK, SwarmTrigger.TOOL)

    @property
    def trigger(self) -> Optional[SwarmTrigger]:
        return self._active

    def enter(self, trigger: SwarmTrigger) -> str:
        """Enter swarm mode. Returns system prompt reminder to inject."""
        if self._active is not None:
            return ""  # already active

        self._active = trigger
        logger.info("[swarm] entered swarm mode (trigger=%s)", trigger.value)
        self._emit_status()

        if trigger != SwarmTrigger.TOOL:
            return SWARM_MODE_ENTER_REMINDER
        return ""

    def restore_enter(self, trigger: SwarmTrigger) -> None:
        """Restore swarm mode (e.g. after session resume)."""
        self._active = trigger

    def exit(self) -> str:
        """Exit swarm mode. Returns system prompt reminder to inject."""
        if self._active is None:
            return ""

        trigger = self._active
        self._active = None
        logger.info("[swarm] exited swarm mode (was trigger=%s)", trigger.value)
        self._emit_status()

        if trigger == SwarmTrigger.TOOL:
            return ""

        return SWARM_MODE_EXIT_REMINDER

    def toggle(self) -> str:
        """Toggle swarm mode on/off. Returns any system prompt reminder."""
        if self._active is not None:
            return self.exit()
        return self.enter(SwarmTrigger.MANUAL)

    def _emit_status(self) -> None:
        if self._on_status_change:
            try:
                self._on_status_change()
            except Exception:
                pass


class SwarmManager:
    """Enhanced SwarmManager that actually works.

    Combines the old Tera Pilot SwarmManager (roster + git checkouts) with
    the new SwarmMode (toggle + auto-exit). This is the public API.
    """

    def __init__(self):
        self._mode = SwarmMode(on_status_change=None)
        self._agents: Dict[str, dict] = {}
        self._lock = None  # lazy init
        self._project_root: Optional[str] = None

    def set_project_root(self, root: str) -> None:
        self._project_root = root

    @property
    def mode(self) -> SwarmMode:
        return self._mode

    @property
    def is_active(self) -> bool:
        return self._mode.is_active

    def enter_swarm(self, trigger: str = "manual") -> str:
        """Enter swarm mode. Returns system prompt reminder."""
        t = SwarmTrigger(trigger) if isinstance(trigger, str) else trigger
        return self._mode.enter(t)

    def exit_swarm(self) -> str:
        """Exit swarm mode. Returns system prompt reminder."""
        return self._mode.exit()

    def toggle_swarm(self) -> str:
        """Toggle swarm mode. Returns system prompt reminder."""
        return self._mode.toggle()

    def should_auto_exit(self) -> bool:
        return self._mode.should_auto_exit

    def register_agent(self, agent_id: str, name: str, goal: str,
                       role: str = "generalist", status: str = "idle") -> dict:
        """Register a swarm agent (for UI display)."""
        import time
        agent = {
            "id": agent_id,
            "name": name,
            "goal": goal,
            "role": role,
            "status": status,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._agents[agent_id] = agent
        return agent

    def update_agent_status(self, agent_id: str, status: str, **kwargs) -> None:
        """Update a registered agent's status."""
        import time
        agent = self._agents.get(agent_id)
        if agent:
            agent["status"] = status
            agent["updated_at"] = time.time()
            agent.update(kwargs)

    def remove_agent(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def list_agents(self) -> List[dict]:
        return list(self._agents.values())

    def get_agent(self, agent_id: str) -> Optional[dict]:
        return self._agents.get(agent_id)

    def cleanup_all(self) -> None:
        self._agents.clear()
        if self._mode.is_active:
            self._mode.exit()