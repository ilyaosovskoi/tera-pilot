"""Regression tests for ChatLog streaming (v2.3.1-fix).

The v2.2.4 streaming implementation poked ``RichLog._children``, which does
not exist on Textual 8.x — so every ``append_token_delta`` chunk fell
through to a fresh ``self.write()`` and a stream of N chunks produced N
progressively-longer duplicate log entries. Worse, ``_on_turn_done``
skipped ``add_final()`` on streamed turns, so the final answer was never
rendered as Markdown.

These tests mount a real ``ChatLog`` inside a Textual app and assert the
observable behavior: exactly one streaming entry, and the final answer
replacing it.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult

from tera_pilot_tui.widgets.chat_log import ChatLog


class _ChatLogHarness(App):
    """Minimal app exposing the ChatLog for direct manipulation."""

    def __init__(self) -> None:
        super().__init__()
        self.chat: ChatLog | None = None

    def compose(self) -> ComposeResult:
        yield ChatLog(id="chat")

    async def on_mount(self) -> None:
        self.chat = self.query_one(ChatLog)
        # Wait for the widget to have a known size so writes render
        # immediately instead of being deferred.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if self.chat._size_known:
                break


@pytest.mark.asyncio
async def test_streaming_chunks_produce_single_entry() -> None:
    app = _ChatLogHarness()
    async with app.run_test():
        chat = app.chat
        assert chat is not None
        await asyncio.sleep(0.3)
        assert chat._size_known, "harness should have a known size"

        chat.append_token_delta("Hello ")
        chat.append_token_delta("world")
        chat.append_token_delta("!")
        await asyncio.sleep(0.1)

        assert chat._streaming_active
        assert chat._streaming_text == "Hello world!"
        # The whole stream must be a SINGLE log entry — no duplicates.
        assert len(chat.lines) == 1, (
            f"expected 1 streaming entry, got {len(chat.lines)}"
        )
        assert "Hello world!" in chat.lines[0].text


@pytest.mark.asyncio
async def test_add_final_replaces_streamed_entry() -> None:
    app = _ChatLogHarness()
    async with app.run_test():
        chat = app.chat
        assert chat is not None
        await asyncio.sleep(0.3)
        assert chat._size_known

        chat.append_token_delta("Partial streamed text")
        await asyncio.sleep(0.05)
        chat.add_final("**final answer**")
        await asyncio.sleep(0.1)

        assert not chat._streaming_active
        # The streamed plain-text entry must have been replaced by the
        # Markdown render — exactly one final entry, no leftovers.
        assert len(chat.lines) == 1, (
            f"expected 1 final entry, got {len(chat.lines)}"
        )
        assert "final answer" in chat.lines[0].text
        assert "Partial streamed text" not in chat.lines[0].text


@pytest.mark.asyncio
async def test_abort_streaming_discards_partial_text() -> None:
    app = _ChatLogHarness()
    async with app.run_test():
        chat = app.chat
        assert chat is not None
        await asyncio.sleep(0.3)
        assert chat._size_known

        chat.append_token_delta("half-written ")
        await asyncio.sleep(0.05)
        discarded = chat.abort_streaming()
        await asyncio.sleep(0.05)

        assert discarded == "half-written "
        assert not chat._streaming_active
        # The partial entry must be rolled back — log is empty again.
        assert len(chat.lines) == 0, (
            f"expected 0 entries after abort, got {len(chat.lines)}"
        )
