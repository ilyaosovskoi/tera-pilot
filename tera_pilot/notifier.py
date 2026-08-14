#!/usr/bin/env python3
"""
notifier.py — Send agent reports to messengers (Telegram, Discord, Slack).

Config: ~/.tera_pilot/notifiers.json
Integrates with the existing HookManager for automatic notifications on
agent-done, agent-error, and checkpoint events.

Zero external dependencies — uses only urllib from the standard library.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ── Event kinds ─────────────────────────────────────────────────

class EventKind(str, Enum):
    DONE = "done"
    ERROR = "error"
    CHECKPOINT = "checkpoint"
    TOOL_CALL = "tool_call"
    CUSTOM = "custom"


# ── Notification event ─────────────────────────────────────────

@dataclass
class NotificationEvent:
    """A single event that may be sent to one or more backends."""
    event: EventKind
    title: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# ── Backend base class ─────────────────────────────────────────

class NotifierBackend:
    """Base class for notification backends."""

    name: str = "base"

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.enabled: bool = config.get("enabled", False)
        self.events: List[str] = config.get("events", ["done", "error"])

    def send(self, event: NotificationEvent) -> bool:
        raise NotImplementedError

    def test(self) -> Dict[str, Any]:
        test_event = NotificationEvent(
            event=EventKind.CUSTOM,
            title="Tera Pilot Test Notification",
            message="If you see this, notifications are working correctly!",
        )
        try:
            ok = self.send(test_event)
            return {"ok": ok, "backend": self.name}
        except Exception as e:
            return {"ok": False, "backend": self.name, "error": str(e)}


# ── Telegram backend ───────────────────────────────────────────

class TelegramBackend(NotifierBackend):
    """Send notifications via the Telegram Bot API.

    Config keys:
        bot_token  — Bot token from @BotFather
        chat_id    — Target chat ID (user or group)
        parse_mode — "HTML" (default) or "MarkdownV2"
        events     — List of event kinds to forward
        enabled    — bool
    """

    name = "telegram"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.bot_token: str = config.get("bot_token", "")
        self.chat_id: str = config.get("chat_id", "")
        self.parse_mode: str = config.get("parse_mode", "HTML")

    def send(self, event: NotificationEvent) -> bool:
        if not self.enabled or not self.bot_token or not self.chat_id:
            return False
        if event.event.value not in self.events:
            return False

        text = self._format_message(event)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": self.parse_mode,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _format_message(self, event: NotificationEvent) -> str:
        emoji_map = {
            "done": "\u2705", "error": "\u274c", "checkpoint": "\U0001f4cc",
            "tool_call": "\U0001f527", "custom": "\U0001f916",
        }
        emoji = emoji_map.get(event.event.value, "\U0001f916")
        lines = [f"{emoji} <b>{event.title}</b>", ""]
        if event.message:
            lines.append(event.message)
        for key, val in event.data.items():
            lines.append(f"<b>{key}:</b> {val}")
        return "\n".join(lines)


# ── Discord backend ────────────────────────────────────────────

class DiscordBackend(NotifierBackend):
    """Send notifications via Discord webhook.

    Config keys:
        webhook_url — Discord webhook URL
        events      — List of event kinds to forward
        enabled     — bool
    """

    name = "discord"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.webhook_url: str = config.get("webhook_url", "")

    def send(self, event: NotificationEvent) -> bool:
        if not self.enabled or not self.webhook_url:
            return False
        if event.event.value not in self.events:
            return False

        payload = json.dumps({
            "content": self._format_message(event),
            "username": "Tera Pilot Agent",
        }).encode("utf-8")

        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 204
        except Exception:
            return False

    def _format_message(self, event: NotificationEvent) -> str:
        emoji_map = {
            "done": "\u2705", "error": "\u274c", "checkpoint": "\U0001f4cc",
            "tool_call": "\U0001f527", "custom": "\U0001f916",
        }
        emoji = emoji_map.get(event.event.value, "\U0001f916")
        lines = [f"{emoji} **{event.title}**", ""]
        if event.message:
            lines.append(event.message)
        for key, val in event.data.items():
            lines.append(f"**{key}:** {val}")
        return "\n".join(lines)


# ── Slack backend ──────────────────────────────────────────────

class SlackBackend(NotifierBackend):
    """Send notifications via Slack webhook.

    Config keys:
        webhook_url — Slack incoming webhook URL
        events      — List of event kinds to forward
        enabled     — bool
    """

    name = "slack"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.webhook_url: str = config.get("webhook_url", "")

    def send(self, event: NotificationEvent) -> bool:
        if not self.enabled or not self.webhook_url:
            return False
        if event.event.value not in self.events:
            return False

        emoji_map = {
            "done": "\u2705", "error": "\u274c", "checkpoint": "\U0001f4cc",
            "tool_call": "\U0001f527", "custom": "\U0001f916",
        }
        emoji = emoji_map.get(event.event.value, "\U0001f916")
        payload = json.dumps({
            "text": f"{emoji} {event.title}\n{event.message}",
            "username": "Tera Pilot Agent",
        }).encode("utf-8")

        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except Exception:
            return False


# ── Notifier singleton ─────────────────────────────────────────

_CONFIG_PATH = os.path.expanduser("~/.tera_pilot/notifiers.json")
_notifier_lock = threading.Lock()
_notifier_instance: Optional["Notifier"] = None


class Notifier:
    """Process-wide notification manager. Singleton via get_notifier().

    Manages multiple backends (Telegram, Discord, Slack). Each backend
    can be enabled/disabled independently and configured to listen for
    specific event kinds. Notifications are sent asynchronously in a
    background thread so they never block the agent loop.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._backends: Dict[str, NotifierBackend] = {}
        self._lock = threading.Lock()
        self._history: List[Dict[str, Any]] = []
        self._max_history: int = 200

        if config is None:
            config = self._load_config()
        self._init_backends(config)

    # ── Config persistence ──────────────────────────────────────

    @staticmethod
    def _load_config() -> Dict[str, Any]:
        try:
            with open(_CONFIG_PATH, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_config(self) -> None:
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        config: Dict[str, Any] = {}
        for name, backend in self._backends.items():
            config[name] = backend.config
        tmp = _CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp, _CONFIG_PATH)

    def _init_backends(self, config: Dict[str, Any]) -> None:
        backend_classes: Dict[str, type] = {
            "telegram": TelegramBackend,
            "discord": DiscordBackend,
            "slack": SlackBackend,
        }
        for name, cls in backend_classes.items():
            if name in config:
                self._backends[name] = cls(config[name])

    # ── Public API ──────────────────────────────────────────────

    def notify(self, event: NotificationEvent) -> Dict[str, Any]:
        """Send notification to all enabled backends that listen for this event.

        Returns a dict mapping backend name to {"sent": bool, ...}.
        """
        results: Dict[str, Any] = {}
        with self._lock:
            for name, backend in self._backends.items():
                if not backend.enabled:
                    results[name] = {"sent": False, "reason": "disabled"}
                    continue
                if event.event.value not in backend.events:
                    results[name] = {"sent": False, "reason": "event_filtered"}
                    continue
                try:
                    ok = backend.send(event)
                    results[name] = {"sent": ok}
                except Exception as e:
                    results[name] = {"sent": False, "error": str(e)}

        self._record(event, results)
        return results

    def notify_async(self, event: NotificationEvent) -> None:
        """Send notification in a background thread (non-blocking)."""
        t = threading.Thread(target=self.notify, args=(event,), daemon=True)
        t.start()

    def configure_backend(self, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Configure or update a backend. Persists to ~/.tera_pilot/notifiers.json."""
        backend_classes: Dict[str, type] = {
            "telegram": TelegramBackend,
            "discord": DiscordBackend,
            "slack": SlackBackend,
        }
        if name not in backend_classes:
            return {"ok": False, "error": f"Unknown backend: {name}. Use: {list(backend_classes.keys())}"}

        with self._lock:
            self._backends[name] = backend_classes[name](config)
            self._save_config()
        return {"ok": True, "backend": name}

    def set_backend_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        """Enable or disable a backend."""
        with self._lock:
            if name not in self._backends:
                return {"ok": False, "error": f"Backend not configured: {name}"}
            self._backends[name].enabled = enabled
            self._backends[name].config["enabled"] = enabled
            self._save_config()
        return {"ok": True, "backend": name, "enabled": enabled}

    def test_backend(self, name: str) -> Dict[str, Any]:
        """Send a test notification to a specific backend."""
        with self._lock:
            if name not in self._backends:
                return {"ok": False, "error": f"Backend not configured: {name}"}
            return self._backends[name].test()

    def test_all(self) -> Dict[str, Any]:
        """Send test notifications to all configured backends."""
        results: Dict[str, Any] = {}
        with self._lock:
            for name, backend in self._backends.items():
                results[name] = backend.test()
        return {"ok": True, "results": results}

    def list_backends(self) -> List[Dict[str, Any]]:
        """Return metadata for all configured backends."""
        with self._lock:
            result: List[Dict[str, Any]] = []
            for name, backend in self._backends.items():
                result.append({
                    "name": name,
                    "enabled": backend.enabled,
                    "events": list(backend.events),
                })
            return result

    def get_backend_config(self, name: str) -> Optional[Dict[str, Any]]:
        """Return config for a specific backend (secrets included)."""
        with self._lock:
            if name not in self._backends:
                return None
            return dict(self._backends[name].config)

    def remove_backend(self, name: str) -> Dict[str, Any]:
        """Remove a backend configuration."""
        with self._lock:
            if name not in self._backends:
                return {"ok": False, "error": f"Backend not configured: {name}"}
            del self._backends[name]
            self._save_config()
        return {"ok": True, "removed": name}

    def set_events(self, name: str, events: List[str]) -> Dict[str, Any]:
        """Set which event kinds trigger notifications for a backend."""
        with self._lock:
            if name not in self._backends:
                return {"ok": False, "error": f"Backend not configured: {name}"}
            self._backends[name].events = list(events)
            self._backends[name].config["events"] = list(events)
            self._save_config()
        return {"ok": True, "backend": name, "events": list(events)}

    def status(self) -> Dict[str, Any]:
        """Return overall notifier status."""
        with self._lock:
            backends = self.list_backends()
            enabled_count = sum(1 for b in backends if b["enabled"])
            return {
                "total_backends": len(backends),
                "enabled_backends": enabled_count,
                "backends": backends,
                "history_size": len(self._history),
            }

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return recent notification history."""
        with self._lock:
            return list(self._history[-limit:])

    def clear_history(self) -> Dict[str, Any]:
        """Clear notification history."""
        with self._lock:
            count = len(self._history)
            self._history.clear()
        return {"ok": True, "cleared": count}

    # ── Hook integration ────────────────────────────────────────

    def register_hooks(self) -> None:
        """Register notifier hooks with the process-wide HookManager.

        Called automatically when the daemon starts or when the user
        enables notifications via /notify hooks on.
        """
        try:
            from .hook_system import get_hook_manager, HookEvent
            mgr = get_hook_manager()
            mgr.register(
                hook_type="post_tool_use",
                callback=self._on_post_tool_use,
                name="notifier_post_tool",
                priority=999,
                description="Notifier: send notifications on agent events",
            )
        except Exception:
            pass

    def unregister_hooks(self) -> None:
        """Remove notifier hooks from the HookManager."""
        try:
            from .hook_system import get_hook_manager
            mgr = get_hook_manager()
            mgr.remove("notifier_post_tool")
        except Exception:
            pass

    def _on_post_tool_use(self, event: Any) -> None:
        """Hook callback — informational, does not send per-tool notifications.
        The actual notifications are sent by the daemon or bridge when the
        agent turn completes.
        """
        pass

    # ── Internal ────────────────────────────────────────────────

    def _record(self, event: NotificationEvent, results: Dict[str, Any]) -> None:
        with self._lock:
            self._history.append({
                "timestamp": event.timestamp,
                "event": event.event.value,
                "title": event.title,
                "results": results,
            })
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]


# ── Singleton access ───────────────────────────────────────────

def get_notifier() -> Notifier:
    """Return the process-wide Notifier singleton."""
    global _notifier_instance
    with _notifier_lock:
        if _notifier_instance is None:
            _notifier_instance = Notifier()
        return _notifier_instance


def reset_notifier() -> None:
    """Reset the singleton (for testing)."""
    global _notifier_instance
    with _notifier_lock:
        _notifier_instance = None
