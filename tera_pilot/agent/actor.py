"""Asyncio ChatStateActor — port of Grok Build's ChatStateActor pattern.

The actor owns ALL conversation state in a single asyncio task. Mutations flow
in via an asyncio.Queue of ChatStateCommand instances; queries return futures.
No locks are needed because only the actor task mutates state.

This is the v2 replacement for the `ContextMemory` class in legacy
`agent_runtime.py`. The legacy class is preserved; v2 callers can opt-in
via `AgentRuntimeV2`.

Compatible with Qt's event loop (run via qasync if Qt is in charge, or via
asyncio.run in headless CLI mode).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

from .compaction_v2 import CompactionEngine, ConversationItem
from .interjection import InterjectionBuffer
from .native import get_actor, NATIVE_AVAILABLE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CancelToken — wraps native or fallback.
# ---------------------------------------------------------------------------


class CancelToken:
    """AbortSignal-pattern cancel token. First reason wins; chained parent->child."""

    def __init__(self) -> None:
        if NATIVE_AVAILABLE:
            self._inner = get_actor().CancelToken()
        else:
            from . import _fallback_actor
            self._inner = _fallback_actor.CancelToken()

    def is_cancelled(self) -> bool:
        return bool(self._inner.is_cancelled())

    def cancel(self, reason: str = "") -> None:
        self._inner.cancel(reason)

    @property
    def reason(self) -> Optional[str]:
        r = self._inner.reason
        return str(r) if r is not None else None

    def child(self) -> "CancelToken":
        """Spawn a child token that is auto-cancelled when self is cancelled."""
        # Wire parent->child propagation. For the native impl this is already
        # done by `child()`. For the fallback, also done. We just need to forward.
        # But the API for native has `child()` that returns a new PyCancelToken
        # directly. For consistency we wrap it.
        if NATIVE_AVAILABLE:
            native_child = self._inner.child()
            return CancelToken._from_native(native_child)
        else:
            # Fallback already creates a child internally via threading.
            return CancelToken._from_fallback(self._inner.child())

    @classmethod
    def _from_native(cls, inner) -> "CancelToken":
        t = cls.__new__(cls)
        t._inner = inner
        return t

    @classmethod
    def _from_fallback(cls, inner) -> "CancelToken":
        t = cls.__new__(cls)
        t._inner = inner
        return t


# ---------------------------------------------------------------------------
# ChatStateCommand — the actor's command set.
# ---------------------------------------------------------------------------


@dataclass
class ChatStateCommand:
    """One command for the ChatStateActor.

    `kind` selects the variant; `payload` carries the data; `reply` is an
    optional asyncio.Future for ask-pattern queries.
    """

    kind: str
    payload: Any = None
    reply: Optional[asyncio.Future] = None


class ChatStateActor:
    """Single-task-owner of all conversation state. No locks needed.

    Usage:
        actor = ChatStateActor()
        await actor.start()
        await actor.push_user_message("hello")
        items = await actor.get_items()
        await actor.stop()

    Commands are processed FIFO. The actor runs as a single asyncio.Task;
    only that task mutates `self._items` and `self._compaction_summary`.
    """

    def __init__(
        self,
        compaction_engine: Optional[CompactionEngine] = None,
        cancel_token: Optional[CancelToken] = None,
    ):
        self._items: List[ConversationItem] = []
        self._compaction_summary: str = ""
        self._total_tokens: int = 0
        self._compaction_engine = compaction_engine
        self._cancel_token = cancel_token or CancelToken()
        self._interjection_buffer = InterjectionBuffer()
        self._queue: asyncio.OptionalQueue = None  # type: ignore
        self._task: Optional[asyncio.Task] = None
        self._started = False
        self._stop_event: Optional[asyncio.Event] = None

    async def start(self) -> None:
        """Start the actor task. Safe to call multiple times."""
        if self._started:
            return
        self._queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="tera-pilot-chat-state-actor")
        self._started = True
        logger.debug("ChatStateActor started")

    async def stop(self) -> None:
        """Stop the actor task. Waits for in-flight commands to complete."""
        if not self._started:
            return
        if self._stop_event:
            self._stop_event.set()
        # Send a sentinel to unblock the queue.
        await self._queue.put(ChatStateCommand(kind="__stop__"))
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._started = False
        logger.debug("ChatStateActor stopped")

    async def _run(self) -> None:
        assert self._queue is not None
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                cmd = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if self._cancel_token.is_cancelled():
                    break
                continue
            if cmd.kind == "__stop__":
                break
            try:
                self._handle_command(cmd)
            except Exception as e:
                logger.exception("ChatStateActor command %s failed: %s", cmd.kind, e)
                if cmd.reply is not None and not cmd.reply.done():
                    cmd.reply.set_exception(e)

    def _handle_command(self, cmd: ChatStateCommand) -> None:
        if cmd.kind == "push_user_message":
            self._push_user_message(cmd.payload["content"])
            self._reply_if_needed(cmd, None)
        elif cmd.kind == "push_assistant_message":
            self._push_assistant_message(
                cmd.payload["content"], cmd.payload.get("tool_calls", [])
            )
            self._reply_if_needed(cmd, None)
        elif cmd.kind == "push_tool_result":
            self._push_tool_result(
                cmd.payload["tool_call_id"], cmd.payload["content"]
            )
            self._reply_if_needed(cmd, None)
        elif cmd.kind == "get_items":
            self._reply_if_needed(cmd, list(self._items))
        elif cmd.kind == "get_total_tokens":
            self._reply_if_needed(cmd, self._total_tokens)
        elif cmd.kind == "replace_conversation":
            self._items = list(cmd.payload["items"])
            self._recompute_total_tokens()
            self._reply_if_needed(cmd, None)
        elif cmd.kind == "check_auto_compact_needed":
            ctx_window = cmd.payload["context_window"]
            needed = (
                self._compaction_engine is not None
                and self._compaction_engine.should_compact(self._total_tokens, ctx_window)
            )
            self._reply_if_needed(cmd, needed)
        elif cmd.kind == "drain_interjections":
            formatted = self._interjection_buffer.drain_formatted()
            self._reply_if_needed(cmd, formatted)
        elif cmd.kind == "push_interjection":
            self._interjection_buffer.push(
                cmd.payload["text"], cmd.payload.get("attachment")
            )
            self._reply_if_needed(cmd, None)
        else:
            logger.warning("unknown ChatStateCommand kind: %s", cmd.kind)

    def _reply_if_needed(self, cmd: ChatStateCommand, value: Any) -> None:
        if cmd.reply is not None and not cmd.reply.done():
            cmd.reply.set_result(value)

    # ------------------------------------------------------------------
    # State mutators — called from the actor task only.
    # ------------------------------------------------------------------

    def _push_user_message(self, content: str) -> None:
        item = ConversationItem(role="user", content=content, tokens=(len(content) + 3) // 4)
        self._items.append(item)
        self._total_tokens += item.count_tokens()

    def _push_assistant_message(self, content: str, tool_calls: List[dict]) -> None:
        item = ConversationItem(
            role="assistant",
            content=content,
            tokens=(len(content) + 3) // 4,
            tool_calls=tool_calls,
        )
        self._items.append(item)
        self._total_tokens += item.count_tokens()

    def _push_tool_result(self, tool_call_id: str, content: str) -> None:
        item = ConversationItem(
            role="tool",
            content=content,
            tokens=0,  # placeholder — recomputed after content mutation below
        )
        # Stash tool_call_id in content prefix for compatibility.
        item.content = f"[tool_call_id={tool_call_id}]\n{content}"
        # Compute token count AFTER the content mutation so the prefix
        # "[tool_call_id=...]\n" is included in the estimate.
        item.tokens = (len(item.content) + 3) // 4
        self._items.append(item)
        self._total_tokens += item.count_tokens()

    def _recompute_total_tokens(self) -> None:
        self._total_tokens = sum(it.count_tokens() for it in self._items)

    # ------------------------------------------------------------------
    # Public API — async, safe to call from any coroutine.
    # ------------------------------------------------------------------

    async def push_user_message(self, content: str) -> None:
        await self._send_command(ChatStateCommand(
            kind="push_user_message",
            payload={"content": content},
        ))

    async def push_assistant_message(self, content: str, tool_calls: Optional[List[dict]] = None) -> None:
        await self._send_command(ChatStateCommand(
            kind="push_assistant_message",
            payload={"content": content, "tool_calls": tool_calls or []},
        ))

    async def push_tool_result(self, tool_call_id: str, content: str) -> None:
        await self._send_command(ChatStateCommand(
            kind="push_tool_result",
            payload={"tool_call_id": tool_call_id, "content": content},
        ))

    async def get_items(self) -> List[ConversationItem]:
        return await self._ask("get_items")

    async def get_total_tokens(self) -> int:
        return await self._ask("get_total_tokens")

    async def replace_conversation(self, items: List[ConversationItem]) -> None:
        await self._send_command(ChatStateCommand(
            kind="replace_conversation",
            payload={"items": items},
        ))

    async def check_auto_compact_needed(self, context_window: int) -> bool:
        return await self._ask("check_auto_compact_needed", {"context_window": context_window})

    async def push_interjection(self, text: str, attachment: Optional[str] = None) -> None:
        await self._send_command(ChatStateCommand(
            kind="push_interjection",
            payload={"text": text, "attachment": attachment},
        ))

    async def drain_interjections(self) -> Optional[str]:
        """Drain and return a combined synthetic message body, or None if empty."""
        return await self._ask("drain_interjections")

    @property
    def interjection_buffer(self) -> InterjectionBuffer:
        return self._interjection_buffer

    @property
    def cancel_token(self) -> CancelToken:
        return self._cancel_token

    async def _send_command(self, cmd: ChatStateCommand) -> None:
        if not self._started:
            raise RuntimeError("ChatStateActor not started; call .start() first")
        await self._queue.put(cmd)

    async def _ask(self, kind: str, payload: Any = None) -> Any:
        if not self._started:
            raise RuntimeError("ChatStateActor not started; call .start() first")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await self._queue.put(ChatStateCommand(kind=kind, payload=payload, reply=fut))
        return await fut
