"""
Swarm Manager — manages a roster of parallel agent sessions (the "Boris" pattern).

Each agent in the swarm works in its own git checkout directory to avoid
file conflicts, following the Boris methodology where each Claude session
works in a separate checkout.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import json
import os
import shutil
import logging

logger = logging.getLogger(__name__)


@dataclass
class SwarmAgent:
    id: str
    name: str  # e.g. "Agent #1", "Agent #2"
    goal: str
    role: str  # architect, implementer, reviewer, tester, generalist
    status: str  # idle, planning, working, waiting, done, error
    checkout_path: Optional[str]  # separate git checkout path
    created_at: str
    updated_at: str
    iterations: int = 0
    tool_calls_count: int = 0
    tokens_used: int = 0
    result: Optional[str] = None
    error: Optional[str] = None


class SwarmManager:
    """Manages a roster of parallel agent sessions (the 'Swarm' pattern).

    Each agent in the swarm works in its own git checkout directory to avoid
    file conflicts, following the Boris methodology where each Claude session
    works in a separate checkout.
    """

    def __init__(self):
        self._agents: Dict[str, SwarmAgent] = {}
        self._lock = threading.Lock()
        self._project_root: Optional[str] = None

    def set_project_root(self, root: str):
        self._project_root = root

    def spawn(self, name: str, goal: str, role: str = "generalist") -> SwarmAgent:
        """Spawn a new agent in the swarm with a separate git checkout."""
        agent_id = uuid.uuid4().hex[:8]
        checkout = self._create_checkout(agent_id)

        agent = SwarmAgent(
            id=agent_id,
            name=name,
            goal=goal,
            role=role,
            status="idle",
            checkout_path=checkout,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        with self._lock:
            self._agents[agent_id] = agent
        logger.info("[swarm] spawned agent %s (%s) role=%s checkout=%s",
                     agent_id, name, role, checkout)
        return agent

    def _create_checkout(self, agent_id: str) -> Optional[str]:
        """Create a separate git checkout for an agent to avoid conflicts.

        Uses 'git checkout --' approach: copies the working tree to a
        sibling directory so agents can edit independently.
        """
        if not self._project_root:
            return None
        checkout_dir = os.path.join(
            os.path.dirname(self._project_root),
            f".tera-pilot-swarm-{agent_id}"
        )
        if os.path.exists(checkout_dir):
            shutil.rmtree(checkout_dir)
        shutil.copytree(
            self._project_root, checkout_dir,
            ignore=shutil.ignore_patterns(
                '.git', '__pycache__', '*.pyc', '.tera-pilot-swarm-*'
            ),
        )
        return checkout_dir

    def update_status(self, agent_id: str, status: str, **kwargs):
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent:
                agent.status = status
                agent.updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                for k, v in kwargs.items():
                    if hasattr(agent, k):
                        setattr(agent, k, v)

    def remove(self, agent_id: str):
        """Remove an agent and clean up its checkout directory."""
        with self._lock:
            agent = self._agents.pop(agent_id, None)
        if agent and agent.checkout_path:
            try:
                shutil.rmtree(agent.checkout_path, ignore_errors=True)
            except Exception:
                pass
        logger.info("[swarm] removed agent %s", agent_id)

    def list_agents(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": a.id, "name": a.name, "goal": a.goal,
                    "role": a.role, "status": a.status,
                    "checkout_path": a.checkout_path,
                    "created_at": a.created_at, "updated_at": a.updated_at,
                    "iterations": a.iterations, "tool_calls_count": a.tool_calls_count,
                    "tokens_used": a.tokens_used, "result": a.result,
                    "error": a.error,
                }
                for a in self._agents.values()
            ]

    def get_agent(self, agent_id: str) -> Optional[SwarmAgent]:
        with self._lock:
            return self._agents.get(agent_id)

    def cleanup_all(self):
        """Remove all agents and their checkout directories."""
        with self._lock:
            for agent_id in list(self._agents.keys()):
                self.remove(agent_id)
        logger.info("[swarm] cleaned up all agents")