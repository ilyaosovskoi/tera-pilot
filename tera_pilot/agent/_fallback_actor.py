"""Pure-Python fallback for the actor module — CancelToken etc."""

from __future__ import annotations

import logging
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)


class CancelToken:
    """AbortSignal-pattern cancel token. Chained parent->child via threading.Event.

    Pure-Python fallback; the native Rust version uses atomic bools and
    tokio::spawn for parent->child propagation. In fallback mode propagation
    is via a daemon thread, which is slightly less timely but functionally
    equivalent.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: Optional[str] = None
        self._lock = threading.Lock()
        self._listeners: List[threading.Event] = []

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "") -> None:
        with self._lock:
            if self._event.is_set():
                return  # already cancelled; first reason wins
            self._reason = reason
        self._event.set()
        # Notify listeners.
        for ev in self._listeners:
            ev.set()

    def on_cancel(self) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            # Register the listener BEFORE checking the state: a cancel
            # landing in the window between the two used to be lost
            # (TOCTOU race), leaving the waiter blocked forever.
            self._listeners.append(ev)
            if self._event.is_set():
                ev.set()
        return ev

    def child(self) -> "CancelToken":
        child = CancelToken()

        def _propagate():
            parent_ev = self.on_cancel()
            parent_ev.wait()
            child.cancel("parent cancelled")

        t = threading.Thread(target=_propagate, name="cancel-token-propagate", daemon=True)
        t.start()
        return child
