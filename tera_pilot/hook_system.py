#!/usr/bin/env python3
"""
G9 — Hook System at Tool Level.

Provides PreToolUse / PostToolUse / UserPromptSubmit hooks that let users
register callbacks to intercept tool calls and user prompts for audit,
enforcement policies, auto-formatters, security scanners, etc.

Design:
  - HookManager is a process-wide singleton that holds named hook lists.
  - Each hook is a callable registered under one of three event types:
      * "pre_tool_use"   — called BEFORE a tool executes.  Receives (tool_name, args)
                           and returns a HookResult.  If the result blocks, the tool
                           call is rejected with the result's message.
      * "post_tool_use"  — called AFTER a tool executes.   Receives (tool_name, args, result)
                           and returns a HookResult (informational only — the tool has
                           already run, but the hook can log or transform the result).
      * "user_prompt_submit" — called BEFORE the prompt is sent to the LLM.
                           Receives (prompt,) and returns a HookResult.  If the result
                           blocks, the prompt is rejected.
  - Hook callbacks are synchronous (the agent loop is on a background thread).
  - Hooks are executed in priority order (lower = first).  If any pre hook blocks,
    remaining hooks are skipped.
  - Config persistence via ~/.tera_pilot/hooks.json (enabled/disabled per hook, not the
    callback code itself — that lives in user Python modules under ~/.tera_pilot/hooks/).
  - User hook modules: any .py file under ~/.tera_pilot/hooks/ is auto-loaded on startup.
    Each module may define register_hooks(manager) which is called with the HookManager.

Thread safety:
  - HookManager uses a threading.RLock for all mutations.
  - Reads (dispatch) are lock-free for performance — the hook list is replaced
    atomically via a snapshot pattern.

Integration points:
  - ToolEngine.execute() calls HookManager.dispatch_pre_tool_use() before _dispatch().
  - ToolEngine.execute() calls HookManager.dispatch_post_tool_use() after _dispatch().
  - AgentRuntime.run_prompt() calls HookManager.dispatch_user_prompt_submit() before
    the LLM call.
  - TeraPilotBridge (TUI + GUI) exposes register_hook / list_hooks / remove_hook / test_hook.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

HOOK_TYPES = ("pre_tool_use", "post_tool_use", "user_prompt_submit")


def _tera_pilot_home() -> Path:
    p = Path.home() / ".tera_pilot"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _hooks_config_path() -> Path:
    return _tera_pilot_home() / "hooks.json"


def _hooks_dir() -> Path:
    d = _tera_pilot_home() / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Data classes ─────────────────────────────────────────────────────────

class HookAction(str, Enum):
    """What the hook recommends after inspecting the event."""
    ALLOW = "allow"       # continue execution
    BLOCK = "block"       # reject the tool call / prompt
    MODIFY = "modify"     # change args (pre_tool_use only) or prompt


@dataclass
class HookResult:
    """Returned by a hook callback after processing an event.

    Attributes
    ----------
    action : HookAction
        ALLOW  — let the tool/prompt proceed.
        BLOCK  — reject with ``message``.
        MODIFY — replace args/prompt with ``modified_args`` / ``modified_prompt``.
    message : str
        Human-readable explanation (shown in audit log and UI).
    modified_args : dict | None
        If action is MODIFY and this is a pre_tool_use hook, the replacement args.
    modified_prompt : str | None
        If action is MODIFY and this is a user_prompt_submit hook, the replacement prompt.
    """
    action: HookAction = HookAction.ALLOW
    message: str = ""
    modified_args: Optional[Dict[str, Any]] = None
    modified_prompt: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"action": self.action.value, "message": self.message}
        if self.modified_args is not None:
            d["modified_args"] = self.modified_args
        if self.modified_prompt is not None:
            d["modified_prompt"] = self.modified_prompt
        return d


@dataclass
class HookEntry:
    """A registered hook (callback + metadata).

    Attributes
    ----------
    id : str
        Unique identifier (auto-generated if not provided).
    name : str
        Human-readable name.
    hook_type : str
        One of HOOK_TYPES.
    callback : Callable
        The actual hook function.
    priority : int
        Lower = runs first.  Default 100.
    enabled : bool
        Whether the hook is active.
    description : str
        Short description for the UI.
    source : str
        Where the hook was registered from (e.g. "user_module:my_hook.py", "api").
    """
    id: str
    name: str
    hook_type: str
    callback: Callable
    priority: int = 100
    enabled: bool = True
    description: str = ""
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "hook_type": self.hook_type,
            "priority": self.priority,
            "enabled": self.enabled,
            "description": self.description,
            "source": self.source,
        }


@dataclass
class HookEvent:
    """The event object passed to hook callbacks.

    Provides read-only context about the event and a mutable ``data`` dict
    for cross-hook communication within the same dispatch.
    """
    event_type: str           # "pre_tool_use" | "post_tool_use" | "user_prompt_submit"
    tool_name: str = ""       # empty for user_prompt_submit
    args: Dict[str, Any] = field(default_factory=dict)
    result: str = ""          # empty for pre_tool_use / user_prompt_submit
    prompt: str = ""          # empty for tool hooks
    data: Dict[str, Any] = field(default_factory=dict)  # shared across hooks in one dispatch
    timestamp: float = field(default_factory=time.time)


# ── HookManager ──────────────────────────────────────────────────────────

class HookManager:
    """Central registry and dispatcher for tool-level hooks.

    Thread-safe: all mutations go through a lock; dispatch reads a snapshot.
    """

    def __init__(self):
        self._hooks: Dict[str, List[HookEntry]] = {t: [] for t in HOOK_TYPES}
        self._lock = threading.RLock()
        self._config_loaded = False
        # Snapshot for lock-free reads — replaced atomically on each mutation.
        self._snapshot: Dict[str, List[HookEntry]] = {t: [] for t in HOOK_TYPES}
        self._build_snapshot()

    # ── Registration ────────────────────────────────────────────────

    def register(
        self,
        hook_type: str,
        callback: Callable,
        name: str = "",
        priority: int = 100,
        enabled: bool = True,
        description: str = "",
        source: str = "",
        hook_id: str = "",
    ) -> HookEntry:
        """Register a hook callback.

        Parameters
        ----------
        hook_type : str
            One of "pre_tool_use", "post_tool_use", "user_prompt_submit".
        callback : Callable
            The hook function.  Signature varies by hook_type:
              - pre_tool_use:    callback(event: HookEvent) -> HookResult
              - post_tool_use:   callback(event: HookEvent) -> HookResult
              - user_prompt_submit: callback(event: HookEvent) -> HookResult
        name : str
            Human-readable name (defaults to callback.__name__).
        priority : int
            Lower = runs first.
        enabled : bool
            Whether the hook is active initially.
        description : str
            Short description for UI.
        source : str
            Where the hook came from.
        hook_id : str
            Explicit id (auto-generated if empty).

        Returns
        -------
        HookEntry
            The registered hook entry (with id assigned).
        """
        if hook_type not in HOOK_TYPES:
            raise ValueError(f"Invalid hook_type {hook_type!r}; must be one of {HOOK_TYPES}")
        if not callable(callback):
            raise TypeError("callback must be callable")

        entry = HookEntry(
            id=hook_id or f"hk_{uuid.uuid4().hex[:8]}",
            name=name or getattr(callback, "__name__", "anonymous"),
            hook_type=hook_type,
            callback=callback,
            priority=priority,
            enabled=enabled,
            description=description,
            source=source,
        )
        with self._lock:
            self._hooks[hook_type].append(entry)
            self._hooks[hook_type].sort(key=lambda e: e.priority)
            self._build_snapshot()
        logger.info("[hooks] registered %s hook %r (id=%s, priority=%d)",
                     hook_type, entry.name, entry.id, entry.priority)
        return entry

    def remove(self, hook_id: str) -> bool:
        """Remove a hook by its id.  Returns True if found and removed."""
        with self._lock:
            for hook_type in HOOK_TYPES:
                before = len(self._hooks[hook_type])
                self._hooks[hook_type] = [h for h in self._hooks[hook_type] if h.id != hook_id]
                if len(self._hooks[hook_type]) < before:
                    self._build_snapshot()
                    logger.info("[hooks] removed hook %r", hook_id)
                    return True
        return False

    def set_enabled(self, hook_id: str, enabled: bool) -> bool:
        """Enable or disable a hook by id.  Returns True if found."""
        with self._lock:
            for hook_type in HOOK_TYPES:
                for h in self._hooks[hook_type]:
                    if h.id == hook_id:
                        h.enabled = enabled
                        self._build_snapshot()
                        return True
        return False

    def list_hooks(self, hook_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return hook metadata dicts, optionally filtered by hook_type."""
        result = []
        snap = self._snapshot
        types = [hook_type] if hook_type else HOOK_TYPES
        for t in types:
            if t not in snap:
                continue
            for h in snap[t]:
                result.append(h.to_dict())
        return result

    def get_hook(self, hook_id: str) -> Optional[Dict[str, Any]]:
        """Return metadata for a single hook, or None."""
        snap = self._snapshot
        for t in HOOK_TYPES:
            for h in snap[t]:
                if h.id == hook_id:
                    return h.to_dict()
        return None

    # ── Dispatch ────────────────────────────────────────────────────

    def dispatch_pre_tool_use(
        self, tool_name: str, args: Dict[str, Any],
    ) -> HookResult:
        """Dispatch pre_tool_use hooks.  Returns the aggregate result.

        If any hook returns BLOCK, the tool call is rejected.
        If any hook returns MODIFY, the args are replaced.
        Hooks are processed in priority order; the first BLOCK wins.
        MODIFY results are accumulated (last MODIFY wins per key).
        """
        snap = self._snapshot.get("pre_tool_use", [])
        event = HookEvent(
            event_type="pre_tool_use",
            tool_name=tool_name,
            args=dict(args),
        )
        combined = HookResult(action=HookAction.ALLOW)
        modified_args = dict(args)

        for entry in snap:
            if not entry.enabled:
                continue
            try:
                result = entry.callback(event)
                if not isinstance(result, HookResult):
                    continue
                if result.action == HookAction.BLOCK:
                    logger.info("[hooks] pre_tool_use hook %r BLOCKED %s: %s",
                                entry.name, tool_name, result.message)
                    return result
                if result.action == HookAction.MODIFY and result.modified_args is not None:
                    modified_args.update(result.modified_args)
                    combined.action = HookAction.MODIFY
                    combined.modified_args = modified_args
                    combined.message = result.message or f"Modified by {entry.name}"
            except Exception as e:
                logger.warning("[hooks] pre_tool_use hook %r raised: %s", entry.name, e)

        if combined.action == HookAction.MODIFY:
            combined.modified_args = modified_args
        return combined

    def dispatch_post_tool_use(
        self, tool_name: str, args: Dict[str, Any], result_text: str,
    ) -> HookResult:
        """Dispatch post_tool_use hooks.  Returns the aggregate result.

        Post hooks are informational — they cannot block the tool call
        (it already ran).  They can log, record, or transform the result
        for downstream consumers via the event.data dict.
        """
        snap = self._snapshot.get("post_tool_use", [])
        event = HookEvent(
            event_type="post_tool_use",
            tool_name=tool_name,
            args=dict(args),
            result=result_text,
        )
        combined = HookResult(action=HookAction.ALLOW)

        for entry in snap:
            if not entry.enabled:
                continue
            try:
                result = entry.callback(event)
                if not isinstance(result, HookResult):
                    continue
                # Post hooks can signal BLOCK for audit/alert purposes,
                # but the tool has already run.  The result is informational.
                if result.action == HookAction.BLOCK:
                    logger.warning("[hooks] post_tool_use hook %r flagged %s: %s",
                                   entry.name, tool_name, result.message)
            except Exception as e:
                logger.warning("[hooks] post_tool_use hook %r raised: %s", entry.name, e)

        return combined

    def dispatch_user_prompt_submit(self, prompt: str) -> HookResult:
        """Dispatch user_prompt_submit hooks.  Returns the aggregate result.

        If any hook returns BLOCK, the prompt is rejected.
        If any hook returns MODIFY, the prompt is replaced.
        """
        snap = self._snapshot.get("user_prompt_submit", [])
        event = HookEvent(
            event_type="user_prompt_submit",
            prompt=prompt,
        )
        combined = HookResult(action=HookAction.ALLOW)
        current_prompt = prompt

        for entry in snap:
            if not entry.enabled:
                continue
            try:
                result = entry.callback(event)
                if not isinstance(result, HookResult):
                    continue
                if result.action == HookAction.BLOCK:
                    logger.info("[hooks] user_prompt_submit hook %r BLOCKED: %s",
                                entry.name, result.message)
                    return result
                if result.action == HookAction.MODIFY and result.modified_prompt is not None:
                    current_prompt = result.modified_prompt
                    combined.action = HookAction.MODIFY
                    combined.modified_prompt = current_prompt
                    combined.message = result.message or f"Modified by {entry.name}"
                    event.prompt = current_prompt
            except Exception as e:
                logger.warning("[hooks] user_prompt_submit hook %r raised: %s", entry.name, e)

        if combined.action == HookAction.MODIFY:
            combined.modified_prompt = current_prompt
        return combined

    # ── Test hook (dry-run) ─────────────────────────────────────────

    def test_hook(
        self, hook_id: str, event_type: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run a single hook in dry-run mode with a synthetic event.

        Returns {ok, result} where result is the HookResult dict.
        """
        # Find the hook entry
        entry = None
        with self._lock:
            for t in HOOK_TYPES:
                for h in self._hooks[t]:
                    if h.id == hook_id:
                        entry = h
                        break
                if entry:
                    break
        if entry is None:
            return {"ok": False, "error": f"Hook {hook_id!r} not found"}
        if event_type not in HOOK_TYPES:
            return {"ok": False, "error": f"Invalid event_type {event_type!r}"}

        event = HookEvent(event_type=event_type, **kwargs)
        try:
            result = entry.callback(event)
            if isinstance(result, HookResult):
                return {"ok": True, "result": result.to_dict()}
            return {"ok": True, "result": {"action": "allow", "message": "hook returned non-HookResult"}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Snapshot management ─────────────────────────────────────────

    def _build_snapshot(self) -> None:
        """Replace the read-only snapshot with a shallow copy of the current hooks."""
        self._snapshot = {t: list(hooks) for t, hooks in self._hooks.items()}

    # ── Config persistence ──────────────────────────────────────────

    def load_config(self) -> None:
        """Load hook enabled/disabled state from ~/.tera_pilot/hooks.json.

        The config file does NOT store callback code — only metadata
        (id, name, enabled, priority).  Callback code comes from user
        modules under ~/.tera_pilot/hooks/ or from the API.
        """
        config_path = _hooks_config_path()
        if not config_path.exists():
            return
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
        except Exception as e:
            logger.warning("[hooks] failed to load config: %s", e)
            return

        # Apply enabled/disabled state
        overrides = config.get("overrides", {})
        with self._lock:
            for hook_type in HOOK_TYPES:
                for h in self._hooks[hook_type]:
                    if h.id in overrides:
                        ov = overrides[h.id]
                        if "enabled" in ov:
                            h.enabled = ov["enabled"]
                        if "priority" in ov:
                            h.priority = ov["priority"]
                self._hooks[hook_type].sort(key=lambda e: e.priority)
            self._build_snapshot()
        self._config_loaded = True

    def save_config(self) -> None:
        """Save hook enabled/disabled state to ~/.tera_pilot/hooks.json."""
        overrides: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for hook_type in HOOK_TYPES:
                for h in self._hooks[hook_type]:
                    overrides[h.id] = {
                        "name": h.name,
                        "hook_type": h.hook_type,
                        "enabled": h.enabled,
                        "priority": h.priority,
                        "source": h.source,
                    }
        config_path = _hooks_config_path()
        try:
            with open(config_path, "w") as f:
                json.dump({"overrides": overrides, "version": 1}, f, indent=2)
        except Exception as e:
            logger.warning("[hooks] failed to save config: %s", e)

    # ── User module auto-loading ────────────────────────────────────

    def load_user_modules(self) -> int:
        """Auto-load hook modules from ~/.tera_pilot/hooks/*.py.

        Each module may define:
          - register_hooks(manager: HookManager) — called with the HookManager.

        Returns the number of modules loaded.
        """
        hooks_dir = _hooks_dir()
        if not hooks_dir.exists():
            return 0

        loaded = 0
        for py_file in sorted(hooks_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"tera_pilot_user_hook_{py_file.stem}", str(py_file),
                )
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "register_hooks"):
                    mod.register_hooks(self)
                    loaded += 1
                    logger.info("[hooks] loaded user module %s", py_file.name)
            except Exception as e:
                logger.warning("[hooks] failed to load user module %s: %s", py_file.name, e)
        return loaded

    # ── Stats ───────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Return summary statistics."""
        snap = self._snapshot
        counts = {}
        for t in HOOK_TYPES:
            hooks = snap.get(t, [])
            counts[t] = {
                "total": len(hooks),
                "enabled": sum(1 for h in hooks if h.enabled),
                "disabled": sum(1 for h in hooks if not h.enabled),
            }
        return {"hooks": counts}


# ── Process-wide singleton ──────────────────────────────────────────────

_HOOK_MANAGER: Optional[HookManager] = None


def get_hook_manager() -> HookManager:
    """Return the process-wide HookManager singleton."""
    global _HOOK_MANAGER
    if _HOOK_MANAGER is None:
        _HOOK_MANAGER = HookManager()
        _HOOK_MANAGER.load_config()
        _HOOK_MANAGER.load_user_modules()
    return _HOOK_MANAGER


def reset_hook_manager() -> None:
    """Reset the singleton (for testing)."""
    global _HOOK_MANAGER
    _HOOK_MANAGER = None
