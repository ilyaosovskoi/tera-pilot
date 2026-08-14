"""Interjection — mid-turn user interjection buffer.

Ported from Grok Build's `xai-interjection-core` design:
- User can queue a message while a turn is in flight.
- Messages are buffered (FIFO) and drained at safe points.
- Each drained entry is framed as a synthetic user message.
- Truncation is UTF-8-safe at the boundary.
- Multiple entries are joined into a single formatted message when drained
  via `drain_formatted()`; use `drain()` to get individual entries.
- "The model decides how to weigh it against in-flight work."

The agent loop calls `drain_formatted()` at safe points (between tool calls,
after tool execution). If it returns a non-None value, the loop injects it as
a synthetic user message and continues the loop.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .native import get_interjection, NATIVE_AVAILABLE

logger = logging.getLogger(__name__)


class InterjectionEntry:
    """A drained interjection entry. Use `render()` to get the synthetic message body."""

    def __init__(
        self,
        id: int,
        received_at_unix_millis: int,
        raw_text: str,
        truncated: bool,
        attachment: Optional[str] = None,
    ):
        self.id = id
        self.received_at_unix_millis = received_at_unix_millis
        self.raw_text = raw_text
        self.truncated = truncated
        self.attachment = attachment

    def render(self) -> str:
        body = self.raw_text
        if self.truncated:
            body = f"{body}\n\n[truncated — showing {len(self.raw_text)} chars]"
        return (
            "The user sent a message while you were working:\n"
            f"<user_query>\n{body}\n</user_query>"
        )

    def __repr__(self) -> str:
        return (
            f"InterjectionEntry(id={self.id}, truncated={self.truncated}, "
            f"text={self.raw_text!r:.50})"
        )


class InterjectionBuffer:
    """Thread-safe FIFO interjection buffer. Wraps native or fallback."""

    def __init__(self) -> None:
        ij = get_interjection()
        if NATIVE_AVAILABLE:
            self._inner = ij.InterjectionBuffer()
            self._native = True
        else:
            from . import _fallback_interjection
            self._inner = _fallback_interjection.InterjectionBuffer()
            self._native = False

    def push(self, text: str, attachment: Optional[str] = None) -> int:
        """Push a new interjection. Returns its assigned id."""
        return int(self._inner.push(text, attachment))

    def drain(self) -> List[InterjectionEntry]:
        """Drain all pending interjections, returning a list of InterjectionEntry."""
        if self._native:
            raw = self._inner.drain()
            return [
                InterjectionEntry(
                    id=int(e["id"]),
                    received_at_unix_millis=int(e["received_at_unix_millis"]),
                    raw_text=str(e["raw_text"]),
                    truncated=bool(e["truncated"]),
                    attachment=e.get("attachment"),
                )
                for e in raw
            ]
        else:
            return [
                InterjectionEntry(
                    id=e.id,
                    received_at_unix_millis=e.received_at_unix_millis,
                    raw_text=e.raw_text,
                    truncated=e.truncated,
                    attachment=e.attachment,
                )
                for e in self._inner.drain()
            ]

    def drain_formatted(self) -> Optional[str]:
        """Drain and return all pending interjections as a single combined
        synthetic message body, or None if empty.

        Note: This combines multiple entries into one message. For individual
        entry access, use drain() instead.
        """
        result = self._inner.drain_formatted()
        if result is None:
            return None
        return str(result)

    def pending_count(self) -> int:
        """Number of pending interjections (without draining)."""
        return int(self._inner.pending_count())


def render_entry(text: str, truncated: bool) -> str:
    """Render a single interjection entry body (for testing)."""
    ij = get_interjection()
    return str(ij.render_entry(text, truncated))
