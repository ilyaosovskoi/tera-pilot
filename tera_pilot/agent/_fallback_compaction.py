"""Pure-Python fallback for the compaction engine.

Mirrors the API of `tera_pilot_native.compaction` but uses pure Python.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)


class CompactionError(Exception):
    pass


@dataclass
class ConversationItem:
    role: str  # "user" | "assistant" | "tool" | "system"
    content: str
    tokens: int = 0
    tool_calls: List[dict] = field(default_factory=list)

    def count_tokens(self) -> int:
        if self.tokens > 0:
            return self.tokens
        return (len(self.content) + 3) // 4


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
    """Three-tier compaction engine — pure-Python."""

    def __init__(self, sampler: Callable[[str, List[ConversationItem]], str]):
        """
        Args:
            sampler: callable(prompt: str, items: List[ConversationItem]) -> summary: str
        """
        self._sampler = sampler

    def should_compact(self, total_tokens: int, context_window: int) -> bool:
        if context_window == 0:
            return False
        # Use 85% as default threshold for fallback (matches native behavior)
        # Note: The CompactionPolicy auto_compact_threshold_percent is not
        # currently propagated to the fallback engine. For full consistency,
        # the CompactionEngine wrapper in compaction_v2.py should be used,
        # which handles both native and fallback cases.
        return total_tokens >= context_window * 85 // 100

    def code_compact(self, items: List[ConversationItem]) -> Tuple[str, List[ConversationItem]]:
        if not items:
            raise CompactionError("not enough items to compact")
        prompt = (
            "Summarize the following entire conversation. Preserve:\n"
            "- the user's original goal\n"
            "- all files modified (paths + intent)\n"
            "- key decisions made\n"
            "- errors encountered and how they were resolved\n"
            "- the current state of progress\n\n"
            "Keep the summary under 1000 words."
        )
        summary = self._sampler(prompt, items)
        fresh = [
            ConversationItem(
                role="system",
                content=f"[CONVERSATION SUMMARY]\n{summary}",
                tokens=(len(summary) + 3) // 4,
            )
        ]
        return summary, fresh

    def intra_compact(
        self, items: List[ConversationItem], keep_recent: int = 6
    ) -> Tuple[str, List[ConversationItem]]:
        if len(items) <= keep_recent:
            raise CompactionError(
                f"not enough items to compact (got {len(items)}, need {keep_recent + 1})"
            )
        split = len(items) - keep_recent
        to_summarize = items[:split]
        tail = items[split:]
        prompt = (
            "Summarize the tool-call history of the current turn. Preserve:\n"
            "- Task/Intent\n- Key Findings\n- Files/Code touched\n"
            "- Errors/Fixes\n- Actions Taken\n- Current Progress\n\n"
            "If the tool-call history contains a previous compaction summary, you MUST "
            "incorporate ALL information from that previous summary. Use internal thinking "
            "channel. Preserve verbatim data (URLs, file paths, code snippets)."
        )
        summary = self._sampler(prompt, to_summarize)
        new = [
            ConversationItem(
                role="system",
                content=f"[PREVIOUS TURN SUMMARY]\n{summary}",
                tokens=(len(summary) + 3) // 4,
            )
        ] + tail
        return summary, new

    def inter_compact(
        self,
        items: List[ConversationItem],
        chunk_size: int = 10,
        keep_recent: int = 6,
    ) -> Tuple[str, List[ConversationItem]]:
        if len(items) <= keep_recent + chunk_size:
            raise CompactionError(
                f"not enough items to compact (got {len(items)}, need {keep_recent + chunk_size + 1})"
            )
        split = len(items) - keep_recent
        to_summarize = items[:split]
        tail = items[split:]

        summaries: List[str] = []
        for chunk in _chunks(to_summarize, chunk_size):
            s = self._sampler(
                "Summarize this conversation chunk concisely (under 200 words). "
                "Preserve key decisions and file paths.",
                chunk,
            )
            summaries.append(s)
        combined = "\n\n---\n\n".join(summaries)
        new = [
            ConversationItem(
                role="system",
                content=f"[CONVERSATION HISTORY SUMMARY]\n{combined}",
                tokens=(len(combined) + 3) // 4,
            )
        ] + tail
        return combined, new


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]
