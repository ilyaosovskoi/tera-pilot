"""Three-tier conversation compaction engine.

Ported from Grok Build's `xai-grok-compaction` crate:
- **code**: full-session replace — summarize entire history, rebuild fresh.
- **intra**: tail-keep, per-turn — summarize tool-call history of the current
  turn while keeping the tail (last `keep_recent` messages).
- **inter**: chunked, between-turn summarization pipeline. Each chunk of
  `chunk_size` items is summarized separately, then summaries are concatenated.

The sampler is a Python callable: `sampler(prompt: str, items: List[ConversationItem]) -> str`.
The host (provider layer) implements this and calls the LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .native import get_compaction, NATIVE_AVAILABLE

logger = logging.getLogger(__name__)


class CompactionError(Exception):
    pass


@dataclass
class ConversationItem:
    """A single conversation message — host-agnostic."""

    role: str  # "user" | "assistant" | "tool" | "system"
    content: str
    tokens: int = 0
    tool_calls: List[dict] = field(default_factory=list)

    def count_tokens(self) -> int:
        if self.tokens > 0:
            return self.tokens
        return (len(self.content) + 3) // 4

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "tokens": self.tokens,
            "tool_calls": self.tool_calls,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationItem":
        return cls(
            role=d["role"],
            content=d["content"],
            tokens=d.get("tokens", 0),
            tool_calls=d.get("tool_calls", []),
        )


@dataclass
class CompactionPolicy:
    strategy: str = "inter"  # "code" | "intra" | "inter"
    auto_compact_threshold_percent: int = 85
    wall_clock_budget_secs: int = 300
    two_pass_enabled: bool = False
    keep_recent: int = 6
    chunk_size: int = 10

    @staticmethod
    def code() -> "CompactionPolicy":
        return CompactionPolicy(strategy="code")

    @staticmethod
    def intra(keep_recent: int = 6) -> "CompactionPolicy":
        return CompactionPolicy(strategy="intra", keep_recent=keep_recent)

    @staticmethod
    def inter(keep_recent: int = 6, chunk_size: int = 10) -> "CompactionPolicy":
        return CompactionPolicy(strategy="inter", keep_recent=keep_recent, chunk_size=chunk_size)


class CompactionEngine:
    """Three-tier compaction engine. Wraps native or fallback."""

    def __init__(self, sampler: Callable[[str, List[ConversationItem]], str],
                 policy: Optional[CompactionPolicy] = None):
        """Construct the engine with a host-provided sampler callable.

        Args:
            sampler: callable(prompt: str, items: List[ConversationItem]) -> summary: str
            policy: optional CompactionPolicy to control compaction behavior.
                   If None, uses default policy with 85% threshold.
        """
        self._sampler = sampler
        self._policy = policy or CompactionPolicy()
        self._native = NATIVE_AVAILABLE
        if self._native:
            compaction = get_compaction()
            self._inner = compaction.CompactionEngine(_NativeSamplerShim(sampler))
        else:
            from . import _fallback_compaction
            self._inner = _fallback_compaction.CompactionEngine(
                lambda prompt, items: sampler(prompt, items)
            )

    def should_compact(self, total_tokens: int, context_window: int) -> bool:
        if context_window == 0:
            return False
        threshold_percent = self._policy.auto_compact_threshold_percent
        return total_tokens >= context_window * threshold_percent // 100

    def code_compact(
        self, items: List[ConversationItem]
    ) -> Tuple[str, List[ConversationItem]]:
        """Full-replace: summarize everything, rebuild fresh history."""
        if self._native:
            compaction = get_compaction()
            py_items = [compaction.ConversationItem(role=i.role, content=i.content, tokens=i.tokens) for i in items]
            summary, fresh = self._inner.code_compact(py_items)
            return str(summary), [ConversationItem(role=i.role, content=i.content, tokens=i.tokens) for i in fresh]
        else:
            return self._inner.code_compact(items)

    def intra_compact(
        self, items: List[ConversationItem], keep_recent: int = 6
    ) -> Tuple[str, List[ConversationItem]]:
        """Tail-keep: summarize tool-call history, keep the last `keep_recent` items."""
        if self._native:
            compaction = get_compaction()
            py_items = [compaction.ConversationItem(role=i.role, content=i.content, tokens=i.tokens) for i in items]
            summary, fresh = self._inner.intra_compact(py_items, keep_recent)
            return str(summary), [ConversationItem(role=i.role, content=i.content, tokens=i.tokens) for i in fresh]
        else:
            return self._inner.intra_compact(items, keep_recent)

    def inter_compact(
        self,
        items: List[ConversationItem],
        chunk_size: int = 10,
        keep_recent: int = 6,
    ) -> Tuple[str, List[ConversationItem]]:
        """Chunked, between-turn summarization. Each chunk summarized separately."""
        if self._native:
            compaction = get_compaction()
            py_items = [compaction.ConversationItem(role=i.role, content=i.content, tokens=i.tokens) for i in items]
            summary, fresh = self._inner.inter_compact(py_items, chunk_size, keep_recent)
            return str(summary), [ConversationItem(role=i.role, content=i.content, tokens=i.tokens) for i in fresh]
        else:
            return self._inner.inter_compact(items, chunk_size, keep_recent)


class _NativeSamplerShim:
    """Adapter so the Rust engine can call back into a Python sampler callable.

    The Rust side expects a Python callable that accepts (prompt: str, items: list of dicts).
    """

    def __init__(self, sampler: Callable[[str, List[ConversationItem]], str]):
        self._sampler = sampler

    def __call__(self, prompt: str, items):
        # `items` here is a list of native ConversationItem objects (from Rust).
        # Convert to our Python ConversationItem wrapper so the sampler sees a familiar API.
        py_items = []
        for it in items:
            # The native objects expose .role and .content getters.
            py_items.append(
                ConversationItem(
                    role=str(it.role),
                    content=str(it.content),
                    tokens=int(it.tokens),
                )
            )
        return self._sampler(prompt, py_items)
