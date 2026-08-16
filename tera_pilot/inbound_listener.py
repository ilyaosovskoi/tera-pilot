"""G21a — Inbound messenger listener.

Listens for incoming messages from configured messenger backends
(Telegram first; Discord/Slack stubbed) and submits them as tasks to
the existing ``tera_pilot/daemon.py`` :class:`TaskQueue`.

Design constraints (from the G21 prompt):
- **Mandatory allow-list**: only messages from explicitly configured
  sender/chat IDs are accepted — no wildcard "accept anyone who
  messages the bot" mode, ever. This is untrusted input turning
  directly into task instructions; treat it with the same seriousness
  as the untrusted-content handling already shipped for G18's
  ``web_fetch`` (content fetched from the internet is tagged as
  untrusted data, not a command).
- **Zero-infrastructure**: Telegram uses long-polling via
  ``getUpdates`` (no public webhook / port-forwarding needed) —
  matching the rest of the app's "just paste a token" philosophy.
- On an accepted message: call ``daemon.py``'s existing
  ``TaskQueue.submit(prompt, workspace=...)`` — don't build a second
  task-submission path.
- A reserved keyword (e.g. replying ``STOP``) cancels the currently
  running task via ``TaskQueue.cancel_task`` — a kill switch must
  exist from day one, not as a follow-up.
- Discord/Slack: use their existing bot event mechanisms if reasonably
  simple; if not, ship Telegram first and stub the others clearly
  rather than shipping something broken.

This module is the inbound counterpart to ``tera_pilot/notifier.py`` (which
handles OUTBOUND notifications). Together they close the loop for G21's
Hermes mode: an inbound message becomes a task, the task runs, the
outbound notifier sends progress/completion back to the same chat.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# Reserved kill-switch keyword. Replying this to the bot cancels the
# currently running task. Per G21 §21a: "a kill switch must exist from
# day one, not as a follow-up."
STOP_KEYWORD = "STOP"

# Telegram getUpdates long-poll timeout (seconds). 30s is the Telegram
# API maximum for long-polling — keeps the request open waiting for new
# messages, then returns. We loop and re-issue immediately.
TELEGRAM_LONGPOLL_TIMEOUT_S = 30

# How long to wait between failed getUpdates attempts (exponential
# backoff capped at this value).
TELEGRAM_BACKOFF_MAX_S = 60.0

# API base for Telegram Bot API.
TELEGRAM_API_BASE = "https://api.telegram.org"


@dataclass
class InboundMessage:
    """A single accepted inbound message.

    Produced by a backend (Telegram / Discord / Slack) and passed to
    the task-submission callback. The ``origin`` field tags the
    message source so the activity log can record "this remote message
    caused this agent action" (G21 §21b).
    """

    backend: str  # "telegram" | "discord" | "slack"
    chat_id: str  # the chat the message came from
    sender_id: str  # the user who sent the message
    sender_name: str  # display name if available, else id
    text: str  # the message body
    raw: Dict[str, Any] = field(default_factory=dict)  # raw backend payload

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "text": self.text,
            "raw": dict(self.raw),
        }


@dataclass
class InboundListenerConfig:
    """Configuration for the inbound listener.

    ``allowed_chat_ids`` is MANDATORY — an empty set means the listener
    refuses to start (no wildcard "accept anyone" mode, ever). Per
    G21 §21a.
    """

    backend: str = "telegram"  # "telegram" | "discord" | "slack"
    telegram_token: str = ""  # bot token from BotFather
    allowed_chat_ids: Set[str] = field(default_factory=set)
    workspace: str = ""  # workspace root for submitted tasks
    # Optional: only allow messages from these sender IDs (in addition
    # to the chat allow-list). Empty = allow any sender in an allowed chat.
    allowed_sender_ids: Set[str] = field(default_factory=set)
    # Reserved kill-switch keyword. Replying this cancels the running task.
    stop_keyword: str = STOP_KEYWORD

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "telegram_token": "***" if self.telegram_token else "",
            "allowed_chat_ids": sorted(self.allowed_chat_ids),
            "workspace": self.workspace,
            "allowed_sender_ids": sorted(self.allowed_sender_ids),
            "stop_keyword": self.stop_keyword,
        }

    def validate(self) -> List[str]:
        """Return a list of validation error messages (empty = valid)."""
        errors: List[str] = []
        if self.backend == "telegram" and not self.telegram_token:
            errors.append("telegram_token is required for backend=telegram")
        if not self.allowed_chat_ids:
            errors.append(
                "allowed_chat_ids is MANDATORY — no wildcard "
                "'accept anyone' mode is supported (per G21 §21a)"
            )
        if self.backend not in ("telegram", "discord", "slack"):
            errors.append(f"unknown backend {self.backend!r}")
        return errors


class InboundListenerError(Exception):
    """Raised on fatal inbound-listener errors (missing config, etc.)."""


class InboundListener:
    """Base class for inbound messenger listeners.

    Subclasses implement ``_poll_once()`` which returns a list of
    :class:`InboundMessage` (possibly empty). The base class handles
    the loop, the allow-list check, the kill-switch keyword, and the
    task-submission callback.
    """

    BACKEND_NAME = "base"

    def __init__(
        self,
        config: InboundListenerConfig,
        on_message: Callable[[InboundMessage], None],
        *,
        on_stop: Optional[Callable[[InboundMessage], None]] = None,
    ) -> None:
        errors = config.validate()
        if errors:
            raise InboundListenerError(
                "invalid inbound listener config: " + "; ".join(errors)
            )
        self._config = config
        self._on_message = on_message
        self._on_stop = on_stop or (lambda msg: None)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None
        self._messages_processed: int = 0
        self._messages_rejected: int = 0
        self._stops_processed: int = 0

    # ---------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------- #
    def start(self) -> None:
        """Start the listener in a background thread.

        Returns immediately. The thread runs :meth:`_run_loop` until
        :meth:`stop` is called.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[inbound/%s] already running", self.BACKEND_NAME)
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"inbound-{self.BACKEND_NAME}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[inbound/%s] started — allowed chats: %s",
            self.BACKEND_NAME,
            sorted(self._config.allowed_chat_ids),
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the listener to stop and wait up to ``timeout`` seconds."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("[inbound/%s] stopped", self.BACKEND_NAME)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> Dict[str, Any]:
        return {
            "backend": self.BACKEND_NAME,
            "running": self.is_running(),
            "allowed_chat_ids": sorted(self._config.allowed_chat_ids),
            "messages_processed": self._messages_processed,
            "messages_rejected": self._messages_rejected,
            "stops_processed": self._stops_processed,
            "last_error": self._last_error,
        }

    # ---------------------------------------------------------------- #
    # Subclass hook — must override
    # ---------------------------------------------------------------- #
    def _poll_once(self) -> List[InboundMessage]:
        """Poll the backend once and return a list of inbound messages.

        Subclasses MUST override. Should return quickly (a few seconds
        at most) so the stop event is checked regularly. For long-
        polling backends (Telegram), pass a short timeout to the
        backend's API and let the loop re-issue.
        """
        raise NotImplementedError

    # ---------------------------------------------------------------- #
    # Internal loop
    # ---------------------------------------------------------------- #
    def _run_loop(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                messages = self._poll_once()
                for msg in messages:
                    self._handle_message(msg)
                backoff = 1.0  # reset on success
                self._last_error = None
            except Exception as e:
                self._last_error = str(e)
                logger.warning(
                    "[inbound/%s] poll error: %s — backing off %.1fs",
                    self.BACKEND_NAME, e, backoff,
                )
                # Wait with the stop event so stop() interrupts the backoff.
                self._stop_event.wait(timeout=backoff)
                backoff = min(backoff * 2, TELEGRAM_BACKOFF_MAX_S)

    def _handle_message(self, msg: InboundMessage) -> None:
        """Apply the allow-list + kill-switch check, then dispatch."""
        # MANDATORY allow-list check. No wildcard mode, ever.
        if msg.chat_id not in self._config.allowed_chat_ids:
            self._messages_rejected += 1
            logger.info(
                "[inbound/%s] rejected message from non-allow-listed chat %s",
                self.BACKEND_NAME, msg.chat_id,
            )
            return
        # Optional sender allow-list (in addition to chat allow-list).
        if (
            self._config.allowed_sender_ids
            and msg.sender_id not in self._config.allowed_sender_ids
        ):
            self._messages_rejected += 1
            logger.info(
                "[inbound/%s] rejected message from non-allow-listed sender %s",
                self.BACKEND_NAME, msg.sender_id,
            )
            return
        # Kill-switch keyword check. Per G21 §21a: "A reserved keyword
        # (e.g. replying STOP) cancels the currently running task via
        # TaskQueue.cancel_task — a kill switch must exist from day one."
        if msg.text.strip().upper() == self._config.stop_keyword.upper():
            self._stops_processed += 1
            logger.info(
                "[inbound/%s] STOP received from %s — cancelling running task",
                self.BACKEND_NAME, msg.sender_id,
            )
            try:
                self._on_stop(msg)
            except Exception as e:
                logger.warning("[inbound/%s] stop handler failed: %s", self.BACKEND_NAME, e)
            return
        # Accepted — dispatch to the task-submission callback.
        self._messages_processed += 1
        logger.info(
            "[inbound/%s] accepted message from %s (chat %s): %r",
            self.BACKEND_NAME, msg.sender_id, msg.chat_id, msg.text[:80],
        )
        try:
            self._on_message(msg)
        except Exception as e:
            logger.warning("[inbound/%s] message handler failed: %s", self.BACKEND_NAME, e)


# ──────────────────────────────────────────────────────────────────────
# Telegram backend (long-polling via getUpdates)
# ──────────────────────────────────────────────────────────────────────
class TelegramInboundListener(InboundListener):
    """Telegram Bot API long-polling listener.

    Uses ``getUpdates`` with a 30s long-poll timeout. No webhook, no
    port-forwarding — matches the rest of the app's "just paste a
    token" philosophy. Per G21 §21a.
    """

    BACKEND_NAME = "telegram"

    def __init__(
        self,
        config: InboundListenerConfig,
        on_message: Callable[[InboundMessage], None],
        *,
        on_stop: Optional[Callable[[InboundMessage], None]] = None,
        # Test hook: inject a fake _http_post to avoid real network.
        _http_post: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(config, on_message, on_stop=on_stop)
        self._offset = 0  # Telegram update_id offset (0 = first call gets everything)
        self._http_post = _http_post or self._default_http_post

    def _poll_once(self) -> List[InboundMessage]:
        """Long-poll Telegram for new updates.

        Returns a list of :class:`InboundMessage` (possibly empty).
        Updates the internal ``_offset`` so subsequent calls don't
        re-receive the same updates.
        """
        # Stop-event-aware sleep: if the long-poll is interrupted by
        # stop(), we want to return immediately rather than waiting for
        # the full 30s.
        # We use a shorter long-poll timeout (10s) and loop internally
        # so stop() is checked every 10s instead of every 30s.
        params = {
            "timeout": 10,  # long-poll seconds
            "offset": self._offset,
            "limit": 50,
        }
        try:
            data = self._http_post("getUpdates", params)
        except Exception as e:
            self._last_error = str(e)
            raise

        if not isinstance(data, dict):
            return []
        updates = data.get("result", [])
        if not isinstance(updates, list):
            return []

        messages: List[InboundMessage] = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Advance offset past this update so Telegram marks it
                # as confirmed and doesn't redeliver it.
                self._offset = update_id + 1
            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                continue
            text = message.get("text") or ""
            if not text:
                # Non-text messages (stickers, photos, etc.) — skip.
                # Could be extended to handle captions in the future.
                continue
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            messages.append(
                InboundMessage(
                    backend="telegram",
                    chat_id=str(chat.get("id", "")),
                    sender_id=str(sender.get("id", "")),
                    sender_name=(
                        sender.get("username")
                        or sender.get("first_name", "")
                        or str(sender.get("id", ""))
                    ),
                    text=text,
                    raw=update,
                )
            )
        return messages

    def _default_http_post(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """POST to the Telegram Bot API. Uses urllib (no external dep).

        Returns the parsed JSON response. Raises ``urllib.error.URLError``
        on network failure or ``ValueError`` on non-JSON response.
        """
        token = self._config.telegram_token
        if not token:
            raise InboundListenerError("telegram_token is empty")
        url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
        body = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Per the no-telemetry constraint: the only network traffic is
        # to the explicit Telegram API the user configured. No analytics,
        # no crash reporting, no usage stats.
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            # Telegram returns 401 on bad token, 429 on rate limit.
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise urllib.error.URLError(
                f"Telegram API {method} returned HTTP {e.code}: {body}"
            ) from e


# ──────────────────────────────────────────────────────────────────────
# Discord / Slack stubs
# ──────────────────────────────────────────────────────────────────────
# Per G21 §21a: "Discord/Slack: use their existing bot event mechanisms
# if reasonably simple; if not, ship Telegram first and stub the others
# clearly rather than shipping something broken."
#
# Discord requires either websocket gateway connections (discord.py) or
# webhook setup with a publicly-routable URL — neither is "reasonably
# simple" in the zero-infrastructure sense Telegram is. Slack requires
# a webhook URL or the Slack Events API (also needs a public endpoint).
#
# We stub both with a clear NotImplementedError so a user trying to
# configure them gets a clear message instead of silent breakage.


class DiscordInboundListener(InboundListener):
    """Discord inbound listener — STUB.

    Discord's bot event mechanism requires a persistent websocket
    gateway connection (the ``discord.py`` library) OR a publicly-
    routable webhook URL. Neither fits the "just paste a token, zero
    infrastructure" philosophy of G21's Hermes mode the way Telegram's
    ``getUpdates`` long-poll does.

    This stub raises :class:`NotImplementedError` on construction so
    a user trying to configure ``backend=discord`` gets a clear error
    instead of silent breakage. Implementing Discord properly is a
    follow-up task (track separately from G19/20/21).
    """

    BACKEND_NAME = "discord"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "Discord inbound listener is not yet implemented. Discord "
            "requires either a websocket gateway connection (discord.py) "
            "or a publicly-routable webhook URL — neither fits the "
            "zero-infrastructure design of G21's Hermes mode. Use "
            "backend='telegram' instead, or track Discord support as a "
            "separate follow-up task."
        )

    def _poll_once(self) -> List[InboundMessage]:
        raise NotImplementedError


class SlackInboundListener(InboundListener):
    """Slack inbound listener — STUB.

    Slack's bot event mechanism requires either the Slack Events API
    (needs a publicly-routable URL to receive webhook deliveries) or
    the Slack Socket Mode (needs the ``slack-bolt`` library). Neither
    is "reasonably simple" in the zero-infrastructure sense.

    This stub raises :class:`NotImplementedError` on construction.
    """

    BACKEND_NAME = "slack"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "Slack inbound listener is not yet implemented. Slack "
            "requires either the Events API (public webhook URL) or "
            "Socket Mode (slack-bolt library). Use backend='telegram' "
            "instead, or track Slack support as a separate follow-up."
        )

    def _poll_once(self) -> List[InboundMessage]:
        raise NotImplementedError


# ──────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────
def make_inbound_listener(
    config: InboundListenerConfig,
    on_message: Callable[[InboundMessage], None],
    *,
    on_stop: Optional[Callable[[InboundMessage], None]] = None,
    **kwargs: Any,
) -> InboundListener:
    """Factory: build the right listener subclass for ``config.backend``.

    Returns a ready-to-start :class:`InboundListener`. The caller is
    responsible for calling ``.start()`` and (eventually) ``.stop()``.
    """
    if config.backend == "telegram":
        return TelegramInboundListener(config, on_message, on_stop=on_stop, **kwargs)
    elif config.backend == "discord":
        return DiscordInboundListener(config, on_message, on_stop=on_stop, **kwargs)
    elif config.backend == "slack":
        return SlackInboundListener(config, on_message, on_stop=on_stop, **kwargs)
    else:
        raise InboundListenerError(
            f"unknown backend {config.backend!r} — "
            "supported: telegram (discord/slack are stubs)"
        )


# ──────────────────────────────────────────────────────────────────────
# Daemon integration helper
# ──────────────────────────────────────────────────────────────────────
def make_daemon_callback(
    task_queue: Any,
    *,
    workspace: str = "",
    activity_log: Optional[Any] = None,
) -> Callable[[InboundMessage], None]:
    """Build an ``on_message`` callback that submits to a ``TaskQueue``.

    Per G21 §21a: "On an accepted message: call daemon.py's existing
    TaskQueue.submit(prompt, workspace=...) — don't build a second
    task-submission path."

    The callback also writes an ActivityLog entry tagged with the
    inbound origin so there is always a clear, signed record of "this
    remote message caused this agent action" (G21 §21b).
    """
    def _on_message(msg: InboundMessage) -> None:
        try:
            task_id = task_queue.submit(msg.text, workspace=workspace or msg.raw.get("workspace", ""))
            logger.info(
                "[inbound] submitted task %s from %s message (chat %s)",
                task_id, msg.backend, msg.chat_id,
            )
            # G21 §21b: "Every task that originated from an inbound
            # message is tagged as such in the activity log / G16 audit
            # trail, so there is always a clear, signed record of 'this
            # remote message caused this agent action.'"
            if activity_log is not None:
                try:
                    from tera_pilot.activity_log import CATEGORY_INFO, STATUS_OK
                    activity_log.record(
                        category=CATEGORY_INFO,
                        kind="inbound_task_submitted",
                        tool=f"inbound_{msg.backend}",
                        title=f"Inbound {msg.backend} message -> task {task_id}",
                        summary=f"chat={msg.chat_id} sender={msg.sender_name}",
                        status=STATUS_OK,
                        args={"task_id": task_id, "workspace": workspace},
                        meta={
                            "backend": msg.backend,
                            "chat_id": msg.chat_id,
                            "sender_id": msg.sender_id,
                            "sender_name": msg.sender_name,
                            "text_preview": msg.text[:200],
                        },
                    )
                except Exception as e:
                    logger.debug("[inbound] activity log write failed: %s", e)
        except Exception as e:
            logger.error("[inbound] failed to submit task from %s: %s", msg.backend, e)

    return _on_message


def make_daemon_stop_callback(
    task_queue: Any,
    *,
    activity_log: Optional[Any] = None,
) -> Callable[[InboundMessage], None]:
    """Build an ``on_stop`` callback that cancels the running task.

    Per G21 §21a: "A reserved keyword (e.g. replying STOP) cancels the
    currently running task via TaskQueue.cancel_task."
    """
    def _on_stop(msg: InboundMessage) -> None:
        try:
            # TaskQueue.list_tasks returns the current tasks; we
            # cancel the most recent RUNNING one. If the daemon exposes
            # a "cancel current" method, prefer that.
            #
            # v2.4.1-fix: the old code read ``t.get("status")`` /
            # ``t.get("task_id")`` — but TaskRecord.to_dict() emits
            # ``"state"`` / ``"id"``, so the running-task check never
            # matched and the kill switch silently cancelled NOTHING
            # (a G21 §21a "must exist from day one" security feature).
            cancelled_id = ""
            try:
                tasks = task_queue.list_tasks(limit=10)
                for t in tasks:
                    if isinstance(t, dict) and t.get("state") == "running":
                        cancelled_id = t.get("id", "")
                        task_queue.cancel_task(cancelled_id)
                        break
            except Exception:
                pass
            logger.info(
                "[inbound] STOP from %s — cancelled task %s",
                msg.backend, cancelled_id or "(none running)",
            )
            if activity_log is not None and cancelled_id:
                try:
                    from tera_pilot.activity_log import CATEGORY_INFO, STATUS_OK
                    activity_log.record(
                        category=CATEGORY_INFO,
                        kind="inbound_stop_received",
                        tool=f"inbound_{msg.backend}",
                        title=f"Inbound STOP cancelled task {cancelled_id}",
                        summary=f"chat={msg.chat_id} sender={msg.sender_name}",
                        status=STATUS_OK,
                        meta={
                            "backend": msg.backend,
                            "chat_id": msg.chat_id,
                            "sender_id": msg.sender_id,
                            "cancelled_task_id": cancelled_id,
                        },
                    )
                except Exception as e:
                    logger.debug("[inbound] activity log write failed: %s", e)
        except Exception as e:
            logger.error("[inbound] failed to cancel task from STOP: %s", e)

    return _on_stop
