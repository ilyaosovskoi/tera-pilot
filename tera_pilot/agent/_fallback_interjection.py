"""Pure-Python fallback for the interjection buffer."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import List, Optional

LARGE_PROMPT_THRESHOLD = 25_000


@dataclass
class InterjectionEntry:
    id: int
    received_at_unix_millis: int
    raw_text: str
    truncated: bool
    attachment: Optional[str] = None

    def render(self) -> str:
        body = self.raw_text
        if self.truncated:
            body = f"{body}\n\n[truncated — original was {len(self.raw_text)} chars]"
        return (
            "The user sent a message while you were working:\n"
            f"<user_query>\n{body}\n</user_query>"
        )


class InterjectionBuffer:
    """Thread-safe FIFO interjection buffer."""

    def __init__(self) -> None:
        self._queue: deque = deque()
        self._lock = threading.Lock()
        self._next_id = 1

    def push(self, text: str, attachment: Optional[str] = None) -> int:
        with self._lock:
            iid = self._next_id
            self._next_id += 1
            self._queue.append(
                {
                    "id": iid,
                    "received_at_unix_millis": int(time.time() * 1000),
                    "text": text,
                    "attachment": attachment,
                }
            )
            return iid

    def push_with_attachment(self, text: str, attachment: Optional[str]) -> int:
        return self.push(text, attachment)

    def drain(self) -> List[InterjectionEntry]:
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
        out: List[InterjectionEntry] = []
        for it in items:
            raw, truncated = _truncate_utf8_safe(it["text"], LARGE_PROMPT_THRESHOLD)
            out.append(
                InterjectionEntry(
                    id=it["id"],
                    received_at_unix_millis=it["received_at_unix_millis"],
                    raw_text=raw,
                    truncated=truncated,
                    attachment=it["attachment"],
                )
            )
        return out

    def drain_formatted(self) -> Optional[str]:
        """Drain all pending interjections and return them as a single combined
        formatted message, or None if empty.

        This joins multiple entries with double newlines to form a single
        synthetic user message, matching the Rust implementation.
        """
        entries = self.drain()
        if not entries:
            return None
        return "\n\n".join(e.render() for e in entries).strip()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)


def _truncate_utf8_safe(s: str, max_chars: int):
    if len(s) <= max_chars:
        return s, False
    # Walk char-by-char to find a UTF-8-safe boundary.
    count = 0
    end = 0
    for i, _ in enumerate(s):
        if count >= max_chars:
            end = i
            break
        count += 1
    else:
        end = len(s)
    return s[:end], True


def render_entry(text: str, truncated: bool) -> str:
    return InterjectionEntry(
        id=0, received_at_unix_millis=0, raw_text=text, truncated=truncated
    ).render()
