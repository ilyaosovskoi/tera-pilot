"""
Remote approvals — resolve agent confirmation prompts from a messenger.

When the daemon runs headless with an inbound backend (Telegram), there
is no local UI to answer approval modals, so side-effecting actions
fail closed. The broker bridges that gap without weakening the default:

* a confirmation request becomes a pending approval ``#N`` and a message
  (``Approve? reply ALLOW <N> / DENY <N>``) is sent to the allow-listed
  chats;
* an inbound ``ALLOW <N>`` / ``DENY <N>`` from an allow-listed chat
  resolves it; anything else is ignored (and still becomes a task);
* unanswered approvals time out (default 300s) and resolve to DENY.

The inbound layer enforces the chat allow-list before the broker ever
sees a message; the broker additionally never auto-approves.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_APPROVAL_RE = re.compile(r"^\s*(ALLOW|DENY)\s+(\d+)\s*$", re.IGNORECASE)


def send_telegram_message(bot_token: str, chat_id: str, text: str,
                          timeout: float = 15.0) -> bool:
    """Best-effort Bot API sendMessage. Never raises."""
    try:
        payload = json.dumps({"chat_id": chat_id, "text": text[:3500]}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.debug("[approval] telegram send failed: %s", exc)
        return False


class RemoteApprovalBroker:
    """Pending approvals keyed by small integer ids."""

    def __init__(self, sender: Optional[Callable[[str], None]] = None,
                 timeout: float = 300.0) -> None:
        self._sender = sender
        self._timeout = max(30.0, timeout)
        self._lock = threading.Lock()
        self._counter = 0
        self._pending: Dict[str, Dict[str, Any]] = {}

    def set_sender(self, sender: Callable[[str], None]) -> None:
        self._sender = sender

    def request_approval(self, task_id: str, action: str, summary: str) -> Optional[bool]:
        """Register + announce a request; block until resolved/timeout."""
        with self._lock:
            self._counter += 1
            approval_id = str(self._counter)
            event = threading.Event()
            self._pending[approval_id] = {
                "id": approval_id, "task_id": task_id, "action": action,
                "summary": summary[:300], "created": time.time(),
                "event": event, "decision": None,
            }
        text = (f"Approval #{approval_id} (task {task_id}):\n"
                f"{action}: {summary[:300]}\n"
                f"Reply ALLOW {approval_id} or DENY {approval_id}")
        if self._sender is not None:
            try:
                self._sender(text)
            except Exception as exc:
                logger.debug("[approval] announce failed: %s", exc)
        resolved = event.wait(timeout=self._timeout)
        with self._lock:
            entry = self._pending.pop(approval_id, None)
        if not resolved or entry is None:
            return None
        return entry["decision"]

    def resolve(self, text: str) -> Optional[str]:
        """Apply an ALLOW/DENY command. Returns an ack or None (not a command)."""
        match = _APPROVAL_RE.match(text or "")
        if not match:
            return None
        verdict, approval_id = match.group(1).upper(), match.group(2)
        with self._lock:
            entry = self._pending.get(approval_id)
            if entry is None:
                return f"No pending approval #{approval_id}."
            if entry["decision"] is not None:
                return f"Approval #{approval_id} already decided."
            entry["decision"] = verdict == "ALLOW"
            entry["event"].set()
        logger.info("[approval] #%s %s", approval_id, verdict)
        return f"Approval #{approval_id}: {verdict}."

    def pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{k: v for k, v in entry.items() if k != "event"}
                    for entry in sorted(self._pending.values(),
                                        key=lambda e: int(e["id"]))]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
