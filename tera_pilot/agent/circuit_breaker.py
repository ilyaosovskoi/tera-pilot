"""Circuit breaker — thin wrapper around `tera_pilot_native.circuit_breaker` (Rust) or pure-Python fallback.

Used to wrap every provider call (LLM, MCP) so that flaky endpoints don't
hammer the network indefinitely. Replaces Tera Pilot v1's heuristic string-matching
`SubagentBatch._is_rate_limit_error` with real sliding-window error-rate
tracking per (provider, model) key.

Usage:
    from tera_pilot.agent.circuit_breaker import CircuitBreakerRegistry

    registry = CircuitBreakerRegistry()
    breaker = registry.get("openai/gpt-4o")
    if breaker.try_claim():
        try:
            resp = call_provider(...)
            breaker.record(ok=True)
        except RateLimitError:
            breaker.record(ok=False, rate_limited=True)
        except Exception:
            breaker.record(ok=False)
    else:
        raise CircuitOpenError("openai/gpt-4o is open")
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .native import get_circuit_breaker, NATIVE_AVAILABLE

logger = logging.getLogger(__name__)


class RetryDisposition:
    RETRYABLE = "retryable"
    AUTH_REFRESH = "auth_refresh"
    TERMINAL = "terminal"


class CircuitOpenError(Exception):
    """Raised when a call is short-circuited because the breaker is Open."""


class _NativeBreakerAdapter:
    """Adapts the Rust `tera_pilot_native.circuit_breaker.CircuitBreaker` to our Python API."""

    def __init__(self, native_breaker):
        self._b = native_breaker

    @property
    def key(self) -> str:
        return self._b.key

    @property
    def is_open(self) -> bool:
        return bool(self._b.is_open)

    @property
    def state(self) -> str:
        return str(self._b.state)

    def try_claim(self) -> bool:
        return bool(self._b.try_claim())

    def record(self, ok: bool, rate_limited: bool = False) -> None:
        self._b.record(ok, rate_limited)

    def metrics(self) -> dict:
        return dict(self._b.metrics())


class _NativeRegistryAdapter:
    """Adapts the Rust `tera_pilot_native.circuit_breaker.CircuitBreakerRegistry`."""

    def __init__(self, native_registry):
        self._r = native_registry

    def get(self, key: str) -> _NativeBreakerAdapter:
        return _NativeBreakerAdapter(self._r.get(key))

    def all_metrics(self) -> List[dict]:
        return [dict(m) for m in self._r.all_metrics()]


class CircuitBreaker:
    """Public CircuitBreaker — wraps either native or fallback."""

    def __init__(self, inner):
        self._inner = inner

    @property
    def key(self) -> str:
        return self._inner.key

    @property
    def is_open(self) -> bool:
        return self._inner.is_open

    @property
    def state(self) -> str:
        return self._inner.state

    def try_claim(self) -> bool:
        return self._inner.try_claim()

    def record(self, ok: bool, rate_limited: bool = False) -> None:
        self._inner.record(ok, rate_limited)

    def metrics(self) -> dict:
        m = self._inner.metrics()
        if hasattr(m, "__dict__"):
            return {
                "key": m.key,
                "state": m.state,
                "window_samples": m.window_samples,
                "window_successes": m.window_successes,
                "window_failures": m.window_failures,
                "window_rate_limited": m.window_rate_limited,
                "lifetime_success": m.lifetime_success,
                "lifetime_failure": m.lifetime_failure,
                "lifetime_rate_limited": m.lifetime_rate_limited,
                "generation": m.generation,
            }
        return dict(m)


class CircuitBreakerRegistry:
    """Per-key registry. One breaker per (provider, model) or (mcp_server, tool)."""

    def __init__(
        self,
        min_samples: int = 10,
        error_rate_threshold: float = 0.5,
        window_secs: int = 60,
        open_duration_secs: int = 15,
    ):
        self._cfg = {
            "min_samples": min_samples,
            "error_rate_threshold": error_rate_threshold,
            "window_secs": window_secs,
            "open_duration_secs": open_duration_secs,
        }
        cb = get_circuit_breaker()
        if NATIVE_AVAILABLE:
            self._inner = _NativeRegistryAdapter(
                cb.CircuitBreakerRegistry(**self._cfg)
            )
        else:
            from . import _fallback_circuit_breaker
            self._inner = _fallback_circuit_breaker.CircuitBreakerRegistry(
                _fallback_circuit_breaker.BreakerConfig(
                    min_samples=min_samples,
                    error_rate_threshold=error_rate_threshold,
                    window_secs=float(window_secs),
                    open_duration_secs=float(open_duration_secs),
                )
            )

    def get(self, key: str) -> CircuitBreaker:
        return CircuitBreaker(self._inner.get(key))

    def all_metrics(self) -> List[dict]:
        return self._inner.all_metrics()


def retry_disposition_server(status: int) -> str:
    """Provider-side retry policy: 429 and 5xx retryable, 401/403 auth-refresh, else terminal."""
    cb = get_circuit_breaker()
    return str(cb.retry_disposition_server(status))


def retry_disposition_client_storage(status: int) -> str:
    """Storage-side: 400/403/404 terminal-drop, 401 auth-refresh, everything else (incl. other 4xx and 5xx) retryable."""
    cb = get_circuit_breaker()
    return str(cb.retry_disposition_client_storage(status))
