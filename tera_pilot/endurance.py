"""
Endurance limits — «how long may a single run keep working?»

Problem
-------
A Tera Pilot run is bounded by an *iteration budget*. Since v2.3.6 the
soft cap is a **soft** cap: while the agent keeps executing tools
successfully, the budget auto-extends up to a hard ceiling derived as
``3 × soft`` (at least 40, at most 200). That ceiling is hardcoded, and
there is no bound on **wall-clock time** — a slow provider plus a long
task can keep one turn alive for a very long time with no way for the
user to say «work longer, but stop after N minutes».

This module makes the endurance policy explicit, configurable and
inspectable:

- ``hard_iterations``   — explicit hard ceiling (0 = derive from soft).
- ``extend_factor``     — multiplier used when deriving the ceiling.
- ``hard_floor`` / ``hard_ceiling`` — bounds for the derived ceiling.
- ``extend_margin``     — how many iterations may pass without a
  successful tool call before the run stops extending (loops never
  extend; a genuinely productive agent usually has a successful tool
  call every iteration, so the default 2 is generous already).
- ``max_wall_seconds``  — wall-clock budget for one run (0 = unlimited).
  Checked between iterations, so the current LLM call always finishes;
  the run then stops with an explicit «wall-clock budget» error instead
  of silently disappearing into a stuck provider.

Precedence (highest first): constructor argument → environment variable
→ ``~/.tera_pilot/config.json`` → defaults.

Zero telemetry: this is a local policy object; it never phones home.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────

#: Multiplier for the derived hard ceiling (soft_cap × factor).
_DEFAULT_EXTEND_FACTOR: int = 3
#: Lower bound of the derived ceiling — a task that needs 12 iterations
#: should not be cut off just because the user set a small soft cap.
_DEFAULT_HARD_FLOOR: int = 40
#: Upper bound of the derived ceiling. Raising this via config is the
#: main «work longer» lever for very large refactors.
_DEFAULT_HARD_CEILING: int = 200
#: How many iterations without a successful tool call still count as
#: «making progress» for the purpose of auto-extension.
_DEFAULT_EXTEND_MARGIN: int = 2
#: Wall-clock budget for a single run in seconds (0 = unlimited).
_DEFAULT_MAX_WALL_SECONDS: float = 0.0

#: Config key inside ~/.tera_pilot/config.json (nested, like `token_budget`).
_CONFIG_KEY = "endurance"

#: Environment overrides — handy for CI, where editing config.json is awkward.
_ENV_HARD_ITERATIONS = "TERA_PILOT_HARD_MAX_ITERATIONS"
_ENV_MAX_WALL_SECONDS = "TERA_PILOT_RUN_MAX_SECONDS"
_ENV_EXTEND_FACTOR = "TERA_PILOT_ITERATION_EXTEND_FACTOR"
_ENV_EXTEND_MARGIN = "TERA_PILOT_ITERATION_EXTEND_MARGIN"
_ENV_HARD_CEILING = "TERA_PILOT_HARD_ITERATION_CEILING"

_lock = threading.RLock()


def _config_path() -> Path:
    return Path.home() / ".tera_pilot" / "config.json"


def _load_full_config() -> Dict[str, Any]:
    try:
        if _config_path().exists():
            with open(_config_path(), "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[endurance] failed to read config: %s", e)
    return {}


def _save_full_config(cfg: Dict[str, Any]) -> None:
    try:
        _config_path().parent.mkdir(parents=True, exist_ok=True)
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[endurance] failed to save config: %s", e)


def _int_env(name: str) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("[endurance] ignoring non-numeric %s=%r", name, raw)
        return None


def _float_env(name: str) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("[endurance] ignoring non-numeric %s=%r", name, raw)
        return None


@dataclass
class EnduranceLimits:
    """How long one agent run may keep working.

    The soft iteration cap stays a per-run/per-request value (the API
    server and ``/budget iterations N`` already manipulate it); this
    object only describes what happens *around* it — the derived hard
    ceiling, the extension policy, and the wall-clock budget.
    """

    #: Explicit hard iteration ceiling. 0 = derive from the soft cap.
    hard_iterations: int = 0
    #: Multiplier for the derived ceiling.
    extend_factor: int = _DEFAULT_EXTEND_FACTOR
    #: Lower bound of the derived ceiling.
    hard_floor: int = _DEFAULT_HARD_FLOOR
    #: Upper bound of the derived ceiling.
    hard_ceiling: int = _DEFAULT_HARD_CEILING
    #: Iterations without successful tool work that still allow extending.
    extend_margin: int = _DEFAULT_EXTEND_MARGIN
    #: Wall-clock budget for one run, in seconds. 0 = unlimited.
    max_wall_seconds: float = _DEFAULT_MAX_WALL_SECONDS

    # ── Derived values ───────────────────────────────────────────────

    def hard_for(self, soft: int) -> int:
        """Hard ceiling for a run whose soft cap is ``soft``.

        An explicit ``hard_iterations`` always wins (but is never
        *below* the soft cap — a hard limit smaller than the soft limit
        would make the soft cap meaningless). Otherwise the ceiling is
        ``soft × extend_factor`` clamped into ``[hard_floor, hard_ceiling]``.
        """
        soft = max(0, int(soft or 0))
        if self.hard_iterations > 0:
            return max(soft, int(self.hard_iterations))
        derived = soft * max(1, int(self.extend_factor))
        return int(min(max(self.hard_ceiling, self.hard_floor),
                       max(derived, self.hard_floor)))

    def wall_clock_enabled(self) -> bool:
        return float(self.max_wall_seconds or 0.0) > 0.0

    # ── Serialization ────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "EnduranceLimits":
        d = d or {}
        return cls(
            hard_iterations=max(0, int(d.get("hard_iterations", 0) or 0)),
            extend_factor=max(1, int(d.get("extend_factor", _DEFAULT_EXTEND_FACTOR) or 1)),
            hard_floor=max(1, int(d.get("hard_floor", _DEFAULT_HARD_FLOOR) or 1)),
            hard_ceiling=max(1, int(d.get("hard_ceiling", _DEFAULT_HARD_CEILING) or 1)),
            extend_margin=max(0, int(d.get("extend_margin", _DEFAULT_EXTEND_MARGIN) or 0)),
            max_wall_seconds=max(0.0, float(d.get("max_wall_seconds", _DEFAULT_MAX_WALL_SECONDS) or 0.0)),
        )

    def normalized(self) -> "EnduranceLimits":
        """Return a copy with internally consistent bounds."""
        ceiling = max(self.hard_ceiling, self.hard_floor)
        return EnduranceLimits(
            hard_iterations=max(0, int(self.hard_iterations)),
            extend_factor=max(1, int(self.extend_factor)),
            hard_floor=max(1, int(self.hard_floor)),
            hard_ceiling=ceiling,
            extend_margin=max(0, int(self.extend_margin)),
            max_wall_seconds=max(0.0, float(self.max_wall_seconds or 0.0)),
        )

    # ── Human-readable ───────────────────────────────────────────────

    def describe(self, soft: int = 8) -> List[str]:
        """Lines describing the effective policy for a given soft cap."""
        hard = self.hard_for(soft)
        wall = (
            f"{self.max_wall_seconds:.0f}s per run"
            if self.wall_clock_enabled()
            else "unlimited (no wall-clock cap)"
        )
        source = "explicit" if self.hard_iterations > 0 else "derived"
        return [
            f"soft cap:         {soft} iterations (per run / per request)",
            f"hard ceiling:     {hard} iterations ({source})",
            f"extension:        +1 per iteration while a tool call succeeded "
            f"within the last {self.extend_margin} iteration(s)",
            f"wall clock:       {wall}",
            f"config file:      {_config_path()}",
        ]


# ── Public API ───────────────────────────────────────────────────────


def get_endurance_limits() -> EnduranceLimits:
    """Load endurance limits: config file, then environment overrides.

    Never raises — a corrupt config falls back to defaults, because an
    unreadable preference file must not stop the agent from working.
    """
    with _lock:
        cfg = _load_full_config()
        raw = cfg.get(_CONFIG_KEY, {}) or {}
        limits = EnduranceLimits.from_dict(raw)

        env_hard = _int_env(_ENV_HARD_ITERATIONS)
        if env_hard is not None:
            limits.hard_iterations = max(0, env_hard)
        env_ceiling = _int_env(_ENV_HARD_CEILING)
        if env_ceiling is not None:
            limits.hard_ceiling = max(1, env_ceiling)
        env_factor = _int_env(_ENV_EXTEND_FACTOR)
        if env_factor is not None:
            limits.extend_factor = max(1, env_factor)
        env_margin = _int_env(_ENV_EXTEND_MARGIN)
        if env_margin is not None:
            limits.extend_margin = max(0, env_margin)
        env_wall = _float_env(_ENV_MAX_WALL_SECONDS)
        if env_wall is not None:
            limits.max_wall_seconds = max(0.0, env_wall)

        return limits.normalized()


def set_endurance_limits(
    *,
    hard_iterations: Optional[int] = None,
    extend_factor: Optional[int] = None,
    hard_floor: Optional[int] = None,
    hard_ceiling: Optional[int] = None,
    extend_margin: Optional[int] = None,
    max_wall_seconds: Optional[float] = None,
) -> EnduranceLimits:
    """Update selected fields and persist them to config.json.

    Only non-None fields change (same contract as ``set_token_budget``).
    Values are clamped so a typo cannot produce a nonsensical policy —
    e.g. ``hard_iterations=-5`` becomes 0 («derive»), ``extend_margin``
    cannot go below 0, and ``max_wall_seconds`` cannot go negative.
    """
    with _lock:
        cur = get_endurance_limits()
        if hard_iterations is not None:
            cur.hard_iterations = max(0, int(hard_iterations))
        if extend_factor is not None:
            cur.extend_factor = max(1, int(extend_factor))
        if hard_floor is not None:
            cur.hard_floor = max(1, int(hard_floor))
        if hard_ceiling is not None:
            cur.hard_ceiling = max(1, int(hard_ceiling))
        if extend_margin is not None:
            cur.extend_margin = max(0, int(extend_margin))
        if max_wall_seconds is not None:
            cur.max_wall_seconds = max(0.0, float(max_wall_seconds))
        cur = cur.normalized()

        cfg = _load_full_config()
        cfg[_CONFIG_KEY] = cur.to_dict()
        _save_full_config(cfg)
        return cur


def reset_endurance_limits() -> EnduranceLimits:
    """Restore the stock endurance policy."""
    with _lock:
        cfg = _load_full_config()
        cfg.pop(_CONFIG_KEY, None)
        _save_full_config(cfg)
        return get_endurance_limits()


def endurance_summary(soft: int = 8) -> Dict[str, Any]:
    """Machine-readable status for the TUI/GUI (``/endurance``)."""
    limits = get_endurance_limits()
    return {
        "limits": limits.to_dict(),
        "soft_iterations": int(soft),
        "hard_iterations": limits.hard_for(soft),
        "wall_clock_enabled": limits.wall_clock_enabled(),
        "config_path": str(_config_path()),
    }
