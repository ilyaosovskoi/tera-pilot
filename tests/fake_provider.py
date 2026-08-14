"""Deterministic fake provider for integration tests (P0.2).

Scripts LLM responses so the *real* AgentRuntime + ToolEngine can be
driven through the *real* TeraPilotBridge without any network or API
key. Responses are consumed in order: one text per ``generate()`` /
``stream()`` call. Optional per-call exceptions let tests simulate
provider failures (auth errors, transient 5xx) and a blocking mode
lets tests exercise Ctrl+C cancellation mid-stream.

The provider is a proper ``tera_pilot.providers.base.Provider``
subclass, so it can be registered into the real
``ProviderRegistry`` (``registry.register(FakeProvider)``) and used
through ``run_task(provider_id="fake")`` exactly like a real backend.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from tera_pilot.providers.base import (
    Provider,
    ProviderCapability,
    ProviderConfig,
    ProviderError,
    ProviderResponse,
)


class FakeProvider(Provider):
    """Scripted provider. All docstrings/comments in English (code policy)."""

    provider_id = "fake"
    label = "Fake (tests)"
    default_model = "fake-1"
    capabilities = frozenset(
        {
            ProviderCapability.CHAT,
            ProviderCapability.STREAMING,
            ProviderCapability.TOOL_CALLING,
        }
    )

    def __init__(
        self,
        config: Optional[ProviderConfig] = None,
        *,
        script: Optional[List[str]] = None,
        errors: Optional[List[Exception]] = None,
        block_at_call: Optional[int] = None,
        block_after_chunks: Optional[int] = None,
    ) -> None:
        super().__init__(config or ProviderConfig(provider_id="fake", model="fake-1"))
        #: One response text per LLM call; consumed in order.
        self._script: List[str] = list(script or [])
        #: Exceptions raised in order; once exhausted, script takes over.
        self._errors: List[Exception] = list(errors or [])
        #: If set, ``stream()`` blocks after yielding this many chunks on
        #: call ``block_at_call`` (used by the cancellation test).
        self.block_at_call = block_at_call
        self.block_after_chunks = block_after_chunks

        # Test instrumentation.
        self.call_count = 0
        self.recorded_messages: List[List[Any]] = []
        self.blocked_event = threading.Event()
        self.release_event = threading.Event()

    # ── Provider contract ─────────────────────────────────────────────

    def load(self) -> bool:
        self._loaded = True
        return True

    def unload(self) -> None:
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def generate(self, messages, model=None) -> ProviderResponse:
        self._record(messages)
        self._maybe_raise()
        text = self._next_text()
        return ProviderResponse(
            text=text,
            model=model or self.config.model,
            provider=self.provider_id,
            tokens_in=10,
            tokens_out=max(1, len(text) // 4),
        )

    def stream(self, messages, model=None):
        self._record(messages)
        self._maybe_raise()
        text = self._next_text()
        # Yield in small chunks so the runtime's per-chunk cancellation
        # check has a chance to run between tokens.
        step = 5
        chunks = [text[i : i + step] for i in range(0, len(text), step)] or [""]
        for idx, chunk in enumerate(chunks):
            if (
                self.block_at_call is not None
                and self.call_count == self.block_at_call
                and self.block_after_chunks is not None
                and idx >= self.block_after_chunks
            ):
                # Tell the test we are blocked, then wait until it either
                # releases us or times out (so a broken test can't hang
                # the suite forever).
                self.blocked_event.set()
                self.release_event.wait(timeout=15.0)
            yield chunk

    # ── Internals ─────────────────────────────────────────────────────

    def _record(self, messages) -> None:
        self.call_count += 1
        self.recorded_messages.append(list(messages))

    def _maybe_raise(self) -> None:
        if self._errors and self.call_count <= len(self._errors):
            raise self._errors[self.call_count - 1]

    def _next_text(self) -> str:
        if not self._script:
            return '{"final_answer": "fake provider: no scripted response left"}'
        idx = min(self.call_count - 1, len(self._script) - 1)
        return self._script[idx]

    # ── Convenience builders ──────────────────────────────────────────

    @staticmethod
    def tool_call(tool: str, args: Dict[str, Any]) -> str:
        """Build a scripted tool-call response the parser will accept."""
        import json

        return json.dumps({"tool": tool, "args": args}, ensure_ascii=False)

    @staticmethod
    def final_answer(text: str) -> str:
        """Build a scripted final-answer response."""
        import json

        return json.dumps({"final_answer": text}, ensure_ascii=False)


def auth_error() -> ProviderError:
    """Non-retryable auth failure (bad API key)."""
    return ProviderError(
        "Invalid API key provided (sk-test-...). Check TERA_PILOT keys "
        "in Settings -> Providers."
    )


def transient_error() -> ProviderError:
    """Transient 503 — retryable by the runtime."""
    return ProviderError("HTTP 503 Service Unavailable (upstream overloaded)")
