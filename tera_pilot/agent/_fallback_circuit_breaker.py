"""Pure-Python fallback for the circuit breaker.

Mirrors the API of `tera_pilot_native.circuit_breaker` but uses threading.Lock
instead of parking_lot::Mutex, and time.monotonic() instead of Instant.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class BreakerState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerConfig:
    min_samples: int = 10
    error_rate_threshold: float = 0.5
    window_secs: float = 60.0
    open_duration_secs: float = 15.0


@dataclass
class Sample:
    timestamp: float
    success: bool
    rate_limited: bool = False


@dataclass
class BreakerMetrics:
    key: str
    state: str
    window_samples: int
    window_successes: int
    window_failures: int
    window_rate_limited: int
    lifetime_success: int
    lifetime_failure: int
    lifetime_rate_limited: int
    generation: int


class CircuitBreaker:
    """Sliding-window circuit breaker — pure-Python implementation."""

    def __init__(self, key: str, cfg: BreakerConfig):
        self.key = key
        self.cfg = cfg
        self._lock = threading.Lock()
        self._samples: deque = deque()
        self._state: str = BreakerState.CLOSED
        self._opened_at: Optional[float] = None
        self._probe_claimed_at: Optional[float] = None
        self._lifetime_success = 0
        self._lifetime_failure = 0
        self._lifetime_rate_limited = 0
        self._generation = 0

    @property
    def is_open(self) -> bool:
        return self._state == BreakerState.OPEN

    @property
    def state(self) -> str:
        return self._state

    def try_claim(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            if self._state == BreakerState.CLOSED:
                return True
            if self._state == BreakerState.OPEN:
                if self._opened_at is not None and now - self._opened_at >= self.cfg.open_duration_secs:
                    self._state = BreakerState.HALF_OPEN
                    self._probe_claimed_at = now
                    self._generation += 1
                    logger.info("breaker[%s]: open -> half_open", self.key)
                    return True
                return False
            # HALF_OPEN
            if self._probe_claimed_at is not None:
                if now - self._probe_claimed_at >= self.cfg.open_duration_secs:
                    self._probe_claimed_at = now
                    return True
                return False
            self._probe_claimed_at = now
            return True

    def record(self, ok: bool, rate_limited: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._samples.append(Sample(timestamp=now, success=ok, rate_limited=rate_limited))
            if ok:
                self._lifetime_success += 1
            elif rate_limited:
                self._lifetime_rate_limited += 1
            else:
                self._lifetime_failure += 1
            self._probe_claimed_at = None
            self._generation += 1

            if self._state == BreakerState.HALF_OPEN:
                if ok:
                    self._state = BreakerState.CLOSED
                    self._opened_at = None
                    logger.info("breaker[%s]: half_open -> closed", self.key)
                else:
                    self._state = BreakerState.OPEN
                    self._opened_at = now
                    logger.warning("breaker[%s]: half_open -> open (probe failed)", self.key)
            elif self._state == BreakerState.CLOSED:
                if len(self._samples) >= self.cfg.min_samples:
                    errors = sum(1 for s in self._samples if not s.success)
                    rate = errors / len(self._samples)
                    if rate >= self.cfg.error_rate_threshold:
                        self._state = BreakerState.OPEN
                        self._opened_at = now
                        logger.warning(
                            "breaker[%s]: closed -> open (error_rate=%.2f)", self.key, rate
                        )

    def metrics(self) -> BreakerMetrics:
        with self._lock:
            successes = sum(1 for s in self._samples if s.success)
            failures = sum(1 for s in self._samples if not s.success and not s.rate_limited)
            rate_limited = sum(1 for s in self._samples if s.rate_limited)
            return BreakerMetrics(
                key=self.key,
                state=self._state,
                window_samples=len(self._samples),
                window_successes=successes,
                window_failures=failures,
                window_rate_limited=rate_limited,
                lifetime_success=self._lifetime_success,
                lifetime_failure=self._lifetime_failure,
                lifetime_rate_limited=self._lifetime_rate_limited,
                generation=self._generation,
            )

    def _prune(self, now: float) -> None:
        cutoff = now - self.cfg.window_secs
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()


class CircuitBreakerRegistry:
    """Per-key registry."""

    def __init__(self, default_cfg: Optional[BreakerConfig] = None):
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()
        self._default_cfg = default_cfg or BreakerConfig()

    def get(self, key: str) -> CircuitBreaker:
        with self._lock:
            if key not in self._breakers:
                self._breakers[key] = CircuitBreaker(key, self._default_cfg)
            return self._breakers[key]

    def configure(self, key: str, cfg: BreakerConfig) -> CircuitBreaker:
        with self._lock:
            b = CircuitBreaker(key, cfg)
            self._breakers[key] = b
            return b

    def all_metrics(self) -> list:
        with self._lock:
            return [b.metrics() for b in self._breakers.values()]


def retry_disposition_server(status: int) -> str:
    if status == 429 or 500 <= status < 600:
        return "retryable"
    if status in (401, 403):
        return "auth_refresh"
    return "terminal"


def retry_disposition_client_storage(status: int) -> str:
    if status in (400, 403, 404):
        return "terminal"
    if status == 401:
        return "auth_refresh"
    return "retryable"
