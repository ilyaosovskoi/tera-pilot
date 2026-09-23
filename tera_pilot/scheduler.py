"""
Schedule runner — executes ``schedule.json`` entries whose cron time is due.

``cron_add`` (agent tool) only *stores* entries. This module is the other
half: a background thread for the daemon that wakes every ~30s, matches
entries against the current minute and submits due task texts to the
``TaskQueue``. Each entry fires at most once per minute (tracked in
``schedule-state.json``); disabled entries are skipped. Standard 5-field
cron (minute hour day-of-month month day-of-week) with ``*``, ``*/step``,
ranges, steps on ranges and comma lists.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def schedule_path() -> Path:
    return Path(os.path.expanduser("~/.tera_pilot")) / "schedule.json"


def state_path() -> Path:
    return Path(os.path.expanduser("~/.tera_pilot")) / "schedule-state.json"


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """Match one cron field. Never raises (bad syntax → False)."""
    try:
        for part in field.split(","):
            part = part.strip()
            if not part:
                continue
            step = 1
            if "/" in part:
                part, step_s = part.split("/", 1)
                step = int(step_s)
                if step < 1:
                    return False
            if part in ("*", ""):
                if (value - lo) % step == 0:
                    return True
                continue
            if "-" in part:
                a_s, b_s = part.split("-", 1)
                a, b = int(a_s), int(b_s)
                if a <= value <= b and (value - a) % step == 0:
                    return True
                continue
            if int(part) == value and step == 1:
                return True
        return False
    except (ValueError, TypeError):
        return False


def entry_due(schedule: str, now: Optional[datetime] = None) -> bool:
    """True when a 5-field cron schedule matches ``now`` (default: now)."""
    now = now or datetime.now()
    parts = schedule.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    checks = (
        (minute, now.minute, 0, 59),
        (hour, now.hour, 0, 23),
        (dom, now.day, 1, 31),
        (month, now.month, 1, 12),
        # cron DOW: 0 and 7 both mean Sunday.
        (dow, now.isoweekday() % 7, 0, 7),
    )
    return all(_field_matches(f, v, lo, hi) for f, v, lo, hi in checks)


def load_entries() -> List[Dict[str, Any]]:
    try:
        raw = json.loads(schedule_path().read_text(encoding="utf-8"))
        return [e for e in raw if isinstance(e, dict) and e.get("enabled")]
    except (OSError, ValueError):
        return []


def load_state() -> Dict[str, str]:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: Dict[str, str]) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("[schedule] state save failed: %s", exc)


class ScheduleRunner:
    """Background thread: submit due entries to a task queue."""

    def __init__(self, submit: Callable[[str, str], Any],
                 workspace: str = "", interval: float = 30.0) -> None:
        self._submit = submit
        self._workspace = workspace
        self._interval = max(10.0, interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="tera-scheduler")
        self._thread.start()
        logger.info("[schedule] runner started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as exc:
                logger.debug("[schedule] tick failed: %s", exc)

    def tick(self, now: Optional[datetime] = None) -> List[str]:
        """Submit every due entry once per minute. Returns fired entry ids."""
        now = now or datetime.now()
        bucket = now.strftime("%Y-%m-%d %H:%M")
        state = load_state()
        fired = []
        for entry in load_entries():
            entry_id = str(entry.get("id", ""))
            schedule = str(entry.get("schedule", ""))
            task = str(entry.get("task", ""))
            if not entry_id or not task or not entry_due(schedule, now):
                continue
            if state.get(entry_id) == bucket:
                continue
            try:
                self._submit(task, self._workspace)
            except Exception as exc:
                logger.warning("[schedule] submit failed for #%s: %s", entry_id, exc)
                continue
            state[entry_id] = bucket
            fired.append(entry_id)
            logger.info("[schedule] fired #%s: %s", entry_id, task[:100])
        if fired:
            save_state(state)
        return fired
