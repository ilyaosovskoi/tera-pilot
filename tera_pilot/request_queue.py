"""Request serialization queues — Issue #9.

Most LLM providers enforce a per-key concurrency limit (typical
values: 1 for free tiers, 5–10 for paid). Hitting that limit returns
an HTTP 429 / "rate limit" error and — without retry — kills the
agent's current turn.

This module ships a :class:`RequestQueue` that:

1. Caps concurrent in-flight requests per provider (configurable).
2. Queues overflow requests in FIFO order.
3. Tracks 429 responses and temporarily gates the provider for a
   ``cooldown_secs`` window before letting the next request through.
4. Re-raises genuine errors (auth, network) instead of silently
   retrying them forever.

The queue is provider-scoped: each provider gets its own queue, so
a slow OpenRouter call does not block an Ollama call.

Two integration points:

- :class:`RequestQueue` — standalone, can wrap any callable.
- :func:`wrap_provider` — convenience wrapper that monkey-patches a
  :class:`tera_pilot.providers.base.Provider` instance so its ``generate``
  and ``stream`` methods go through the queue.

The wrapper is opt-in: existing code that calls ``provider.generate``
directly continues to work. To enable serialization:

    from tera_pilot.request_queue import wrap_provider
    wrap_provider(provider, max_concurrency=2)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────


@dataclass
class QueueConfig:
    """Tunable knobs for :class:`RequestQueue`.

    - ``max_concurrency``: how many in-flight requests the provider
      will accept. 1 = strict serialisation. Defaults to 1 because
      most free-tier providers allow exactly one concurrent request.
    - ``max_queue_size``: hard cap on the number of waiting requests.
      Beyond this, ``submit`` raises :class:`QueueFullError` instead
      of blocking forever (which would let a runaway agent exhaust
      memory).
    - ``cooldown_secs``: how long to gate the provider after a 429.
      Subsequent requests during the cooldown block until it expires.
    - ``max_retries``: how many times to retry a 429'd request before
      giving up and propagating the error.
    - ``retry_backoff_secs``: initial backoff between retries; doubled
      on each consecutive 429 (capped at ``retry_backoff_cap_secs``).
    - ``retry_backoff_cap_secs``: upper bound on the per-retry backoff.
    """

    max_concurrency: int = 1
    max_queue_size: int = 64
    cooldown_secs: float = 5.0
    max_retries: int = 3
    retry_backoff_secs: float = 0.5
    retry_backoff_cap_secs: float = 8.0


class QueueFullError(Exception):
    """Raised when the queue is full and ``submit`` cannot accept work."""


class CooldownError(Exception):
    """Raised when ``wait=False`` is requested during a cooldown."""


# ── Rate-limit detection ──────────────────────────────────────────────────


def looks_like_rate_limit(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like a rate-limit / 429 error."""
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in (
            "rate limit",
            "rate_limit",
            "ratelimit",
            "too many requests",
            "429",
            "quota exceeded",
            "throttl",
        )
    )


# ── Queue ─────────────────────────────────────────────────────────────────


class RequestQueue:
    """Provider-scoped request serialization queue.

    The queue is thread-safe and supports both sync and async callables.
    For async callables, use :meth:`submit_async`; for sync, use
    :meth:`submit_sync`.
    """

    def __init__(self, config: Optional[QueueConfig] = None, name: str = "default"):
        self._config = config or QueueConfig()
        self._name = name
        # Semaphore caps in-flight work.
        self._sem = threading.Semaphore(self._config.max_concurrency)
        # Lock guards the cooldown state.
        self._lock = threading.Lock()
        self._cooldown_until: float = 0.0
        # Queue stats (readable via ``stats()``).
        self._stats = {
            "submitted": 0,
            "completed": 0,
            "rate_limited": 0,
            "retried": 0,
            "errors": 0,
        }
        # Pending count is tracked separately so we can enforce
        # max_queue_size without holding the semaphore.
        self._pending = 0
        self._pending_lock = threading.Lock()

    # ── Stats ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            cooldown_remaining = max(0.0, self._cooldown_until - time.time())
            stats = dict(self._stats)
            stats["cooldown_remaining_secs"] = cooldown_remaining
            stats["max_concurrency"] = self._config.max_concurrency
        with self._pending_lock:
            stats["pending"] = self._pending
        return stats

    # ── Cooldown ──────────────────────────────────────────────────────

    def _enter_cooldown(self, secs: Optional[float] = None) -> None:
        with self._lock:
            self._cooldown_until = time.time() + (secs if secs is not None else self._config.cooldown_secs)

    def _wait_for_cooldown(self, wait: bool = True) -> bool:
        """Block until cooldown expires. Returns True if waited, False if
        ``wait=False`` and we're in cooldown."""
        while True:
            with self._lock:
                remaining = self._cooldown_until - time.time()
            if remaining <= 0:
                return True
            if not wait:
                return False
            time.sleep(min(remaining, 0.1))

    # ── Sync submit ──────────────────────────────────────────────────

    def submit_sync(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Run ``fn(*args, **kwargs)`` under the queue's concurrency cap.

        - If the queue is full, raises :class:`QueueFullError`.
        - If the provider is in cooldown, blocks until it expires.
        - If ``fn`` raises a rate-limit error, retries up to
          ``config.max_retries`` times with exponential backoff.
        """
        if not self._acquire_pending_slot():
            raise QueueFullError(f"queue {self._name!r} is full")

        try:
            self._stats["submitted"] += 1
            self._wait_for_cooldown(wait=True)
            return self._run_with_retries(fn, args, kwargs, is_async=False)
        finally:
            self._release_pending_slot()

    # ── Async submit ──────────────────────────────────────────────────

    async def submit_async(self, fn: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any:
        """Async variant of :meth:`submit_sync`."""
        if not self._acquire_pending_slot():
            raise QueueFullError(f"queue {self._name!r} is full")

        try:
            self._stats["submitted"] += 1
            await self._wait_for_cooldown_async()
            return await self._run_with_retries_async(fn, args, kwargs)
        finally:
            self._release_pending_slot()

    # ── Internal helpers ─────────────────────────────────────────────

    def _acquire_pending_slot(self) -> bool:
        with self._pending_lock:
            if self._pending >= self._config.max_queue_size:
                return False
            self._pending += 1
            return True

    def _release_pending_slot(self) -> None:
        with self._pending_lock:
            self._pending -= 1

    def _run_with_retries(
        self,
        fn: Callable[..., Any],
        args: tuple,
        kwargs: dict,
        is_async: bool,
    ) -> Any:
        last_exc: Optional[BaseException] = None
        backoff = self._config.retry_backoff_secs
        for attempt in range(self._config.max_retries + 1):
            self._sem.acquire()
            try:
                self._wait_for_cooldown(wait=True)
                result = fn(*args, **kwargs)
                self._stats["completed"] += 1
                return result
            except BaseException as exc:
                last_exc = exc
                if looks_like_rate_limit(exc):
                    self._stats["rate_limited"] += 1
                    self._enter_cooldown()
                    if attempt < self._config.max_retries:
                        self._stats["retried"] += 1
                        logger.warning(
                            "[queue:%s] rate-limited on attempt %d/%d, backing off %.2fs",
                            self._name, attempt + 1, self._config.max_retries + 1, backoff,
                        )
                        time.sleep(backoff)
                        backoff = min(backoff * 2, self._config.retry_backoff_cap_secs)
                        continue
                self._stats["errors"] += 1
                raise
            finally:
                self._sem.release()
        # Should not reach here, but just in case.
        if last_exc:
            raise last_exc

    async def _run_with_retries_async(
        self,
        fn: Callable[..., Awaitable[Any]],
        args: tuple,
        kwargs: dict,
    ) -> Any:
        last_exc: Optional[BaseException] = None
        backoff = self._config.retry_backoff_secs
        for attempt in range(self._config.max_retries + 1):
            # Use asyncio-friendly semaphore for async path.
            await self._acquire_async()
            try:
                await self._wait_for_cooldown_async()
                result = await fn(*args, **kwargs)
                self._stats["completed"] += 1
                return result
            except BaseException as exc:
                last_exc = exc
                if looks_like_rate_limit(exc):
                    self._stats["rate_limited"] += 1
                    self._enter_cooldown()
                    if attempt < self._config.max_retries:
                        self._stats["retried"] += 1
                        logger.warning(
                            "[queue:%s] rate-limited on attempt %d/%d, backing off %.2fs",
                            self._name, attempt + 1, self._config.max_retries + 1, backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, self._config.retry_backoff_cap_secs)
                        continue
                self._stats["errors"] += 1
                raise
            finally:
                self._sem.release()
        if last_exc:
            raise last_exc

    async def _acquire_async(self) -> None:
        """Async-friendly acquire on the threading semaphore.

        We run the blocking acquire in a thread via ``asyncio.to_thread``
        so the event loop is not blocked.
        """
        await asyncio.to_thread(self._sem.acquire)

    async def _wait_for_cooldown_async(self) -> None:
        while True:
            with self._lock:
                remaining = self._cooldown_until - time.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.1))


# ── Registry: one queue per provider ──────────────────────────────────────


class QueueRegistry:
    """Holds one :class:`RequestQueue` per provider id.

    Singleton-style: use :func:`get_queue_registry` to access the
    shared instance.
    """

    def __init__(self):
        self._queues: Dict[str, RequestQueue] = {}
        self._lock = threading.Lock()
        self._default_config = QueueConfig()

    def get_or_create(
        self, provider_id: str, config: Optional[QueueConfig] = None
    ) -> RequestQueue:
        with self._lock:
            q = self._queues.get(provider_id)
            if q is None:
                q = RequestQueue(config or self._default_config, name=provider_id)
                self._queues[provider_id] = q
            return q

    def set_default_config(self, config: QueueConfig) -> None:
        with self._lock:
            self._default_config = config

    def configure(self, provider_id: str, config: QueueConfig) -> RequestQueue:
        """Replace the queue for ``provider_id`` with a new one.

        Existing in-flight requests on the old queue are NOT cancelled;
        the old queue is simply dropped from the registry so subsequent
        ``get_or_create`` calls return the new one.
        """
        with self._lock:
            q = RequestQueue(config, name=provider_id)
            self._queues[provider_id] = q
            return q

    def stats(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {pid: q.stats() for pid, q in self._queues.items()}

    def reset(self) -> None:
        """Drop all queues (used by tests)."""
        with self._lock:
            self._queues.clear()


_registry: Optional[QueueRegistry] = None
_registry_lock = threading.Lock()


def get_queue_registry() -> QueueRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = QueueRegistry()
    return _registry


# ── Provider wrapper ──────────────────────────────────────────────────────


def wrap_provider(
    provider,
    config: Optional[QueueConfig] = None,
) -> RequestQueue:
    """Wrap a :class:`Provider`'s ``generate`` and ``stream`` methods so
    every call goes through a :class:`RequestQueue`.

    Returns the queue (so the caller can inspect stats or reconfigure).

    The wrapper stores the original methods on the instance as
    ``_unwrapped_generate`` / ``_unwrapped_stream`` so it can be
    removed later (``unwrap_provider``).
    """
    pid = getattr(provider, "provider_id", "unknown")
    queue = get_queue_registry().get_or_create(pid, config)

    if hasattr(provider, "_unwrapped_generate"):
        # Already wrapped — no-op.
        return queue

    provider._unwrapped_generate = provider.generate
    has_stream = hasattr(provider, "stream")
    if has_stream:
        provider._unwrapped_stream = provider.stream

    @functools.wraps(provider.generate)
    async def wrapped_generate(*args, **kwargs):
        return await queue.submit_async(provider._unwrapped_generate, *args, **kwargs)

    provider.generate = wrapped_generate
    if has_stream:
        @functools.wraps(provider._unwrapped_stream)
        def wrapped_stream(*args, **kwargs):
            # Streaming is sync-generator-based; wrap each call in a sync
            # submit that returns the generator object.
            return queue.submit_sync(provider._unwrapped_stream, *args, **kwargs)

        provider.stream = wrapped_stream
    provider._request_queue = queue
    return queue


def unwrap_provider(provider) -> None:
    """Reverse :func:`wrap_provider`."""
    if not hasattr(provider, "_unwrapped_generate"):
        return
    provider.generate = provider._unwrapped_generate
    if hasattr(provider, "_unwrapped_stream"):
        provider.stream = provider._unwrapped_stream
        del provider._unwrapped_stream
    del provider._unwrapped_generate
    if hasattr(provider, "_request_queue"):
        del provider._request_queue
