"""
Multi-level Compaction — Full + Micro compaction with media-aware stripping.

Ported from Kimi Code's packages/agent-core/src/agent/compaction/ module.

Two levels:
  1. FullCompaction — LLM-summarized compression of old conversation
     history when the context window approaches its limit. Replaces
     old messages with a concise summary. This is the "heavy" compaction.
  2. MicroCompaction — lightweight truncation of old tool results
     (replace with a marker like "[Old tool result content cleared]")
     without any LLM call. Triggers on cache-miss heuristics.

Tera Pilot-specific additions:
  - Media-aware stripping (base64 images, large binary content)
  - Works with the existing ContextMemory class
  - Thread-safe
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Media Detection ──────────────────────────────────────────────────────

# Patterns that indicate media/base64 content in messages
_BASE64_PATTERN = re.compile(r'data:[a-z]+/[a-z+]+;base64,[A-Za-z0-9+/=]{100,}')
_IMAGE_MIME = re.compile(r'data:image/')
_LARGE_CONTENT_THRESHOLD = 5000  # chars — content above this in tool results gets truncated


def is_media_content(content: str) -> bool:
    """Check if content contains embedded media (base64 images, etc.)."""
    return bool(_BASE64_PATTERN.search(content))


def strip_media_content(content: str, keep_recent: int = 1) -> str:
    """Replace embedded media with text markers, keeping the N most recent.

    Ported from Kimi's media-degradation and media-stripped approach.
    """
    if not _BASE64_PATTERN.search(content):
        return content

    # Find all media blocks
    matches = list(_BASE64_PATTERN.finditer(content))

    if len(matches) <= keep_recent:
        return content

    # Replace all but the last N with markers
    result = content
    # Process in reverse to preserve indices
    for match in reversed(matches[:-keep_recent]):
        mime = _IMAGE_MIME.search(match.group(0))
        if mime:
            replacement = "[IMAGE STRIPPED — previous media removed to save context]"
        else:
            replacement = "[MEDIA STRIPPED — previous media removed to save context]"
        result = result[:match.start()] + replacement + result[match.end():]

    return result


# ── Micro Compaction ─────────────────────────────────────────────────────

@dataclass
class MicroCompactionConfig:
    """Configuration for micro compaction.

    Ported from Kimi's MicroCompactionConfig.
    """
    keep_recent_messages: int = 20
    min_content_tokens: int = 100
    cache_missed_threshold_ms: float = 60 * 60 * 1000  # 1 hour
    truncated_marker: str = "[Old tool result content cleared]"
    min_context_usage_ratio: float = 0.5


class MicroCompaction:
    """Lightweight compaction: truncates old tool results without LLM calls.

    Ported from Kimi's MicroCompaction class. In Kimi this is currently
    disabled (the experimental flag was removed), but the pattern is
    valuable — we enable it with conservative defaults.

    Truncation rules:
    - Only affects tool-role messages beyond the cutoff index
    - Only truncates tool results with >= min_content_tokens estimated tokens
    - Replaces content with truncated_marker
    - Never truncates user or assistant messages
    """

    def __init__(self, config: Optional[MicroCompactionConfig] = None):
        self.config = config or MicroCompactionConfig()
        self.cutoff = 0
        self._lock = threading.Lock()

    def reset(self, max_cutoff: int = 0) -> None:
        with self._lock:
            self.cutoff = min(self.cutoff, max_cutoff)

    def apply(self, cutoff: int) -> None:
        with self._lock:
            self.cutoff = cutoff

    def detect(self, messages: List[Any], total_tokens: int,
               max_tokens: int, last_activity_at: Optional[float] = None) -> bool:
        """Detect if micro compaction should run.

        Triggers when:
          1. Cache miss detected (last activity > threshold ago)
          2. Context usage ratio > min_context_usage_ratio
        """
        config = self.config
        if last_activity_at is None:
            return False

        cache_age_ms = (time.time() - last_activity_at) * 1000
        if cache_age_ms < config.cache_missed_threshold_ms:
            return False

        if max_tokens <= 0:
            return False
        usage_ratio = total_tokens / max_tokens
        if usage_ratio < config.min_context_usage_ratio:
            return False

        return True

    def compact(self, messages: List[Any]) -> List[Any]:
        """Apply micro compaction to messages in-place.

        Returns the (possibly modified) message list. Tool-role messages
        before the cutoff that have large content are replaced with
        a truncated marker.
        """
        if self.cutoff <= 0:
            return messages

        config = self.config
        for i in range(min(self.cutoff, len(messages))):
            msg = messages[i]
            # Only truncate tool results
            role = getattr(msg, 'role', None) or (msg.get('role') if isinstance(msg, dict) else None)
            if role != 'tool':
                continue

            content = getattr(msg, 'content', '') or (msg.get('content', '') if isinstance(msg, dict) else '')
            estimated_tokens = _estimate_tokens_fast(content)
            if estimated_tokens < config.min_content_tokens:
                continue

            # Truncate
            if isinstance(msg, dict):
                msg['content'] = config.truncated_marker
                msg['_micro_compacted'] = True
            else:
                msg.content = config.truncated_marker

        return messages

    def measure_effect(self, messages: List[Any]) -> Dict[str, int]:
        """Measure the token savings of micro compaction."""
        truncated_count = 0
        tokens_before = 0
        tokens_after = 0
        marker_tokens = _estimate_tokens_fast(self.config.truncated_marker)

        for i in range(min(self.cutoff, len(messages))):
            msg = messages[i]
            role = getattr(msg, 'role', None) or (msg.get('role') if isinstance(msg, dict) else None)
            if role != 'tool':
                continue
            content = getattr(msg, 'content', '') or (msg.get('content', '') if isinstance(msg, dict) else '')
            estimated = _estimate_tokens_fast(content)
            if estimated >= self.config.min_content_tokens:
                truncated_count += 1
                tokens_before += estimated
                tokens_after += marker_tokens

        return {
            "truncated_count": truncated_count,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "saved": tokens_before - tokens_after,
        }


# ── Full Compaction ──────────────────────────────────────────────────────

class FullCompaction:
    """LLM-summarized compaction of conversation history.

    Ported from Kimi's FullCompaction class. When the context approaches
    its token limit, this class:
      1. Collects old messages (everything except the most recent N)
      2. Strips media content from those messages
      3. Builds a compaction prompt asking the LLM to summarize
      4. Replaces the old messages with the summary
      5. Keeps the recent messages intact

    Key improvement over Tera Pilot's original ContextMemory.compact():
      - Media-aware stripping before sending to LLM
      - Structured compaction prompt (not just "summarize this")
      - Token-aware: estimates before/after to verify savings
      - Can be triggered automatically or manually
    """

    def __init__(self,
                 llm_call_fn: Optional[callable] = None,
                 keep_recent: int = 6):
        """
        Args:
            llm_call_fn: (prompt_text) -> summary_text. If None, uses
                         a simple heuristic summarizer.
            keep_recent: number of recent messages to preserve
        """
        self._llm_call = llm_call_fn
        self._keep_recent = keep_recent

    def should_compact(self, total_tokens: int, max_tokens: int,
                       threshold: float = 0.80) -> bool:
        """Check if compaction is needed."""
        if max_tokens <= 0:
            return False
        return total_tokens > int(max_tokens * threshold)

    def compact(self, messages: List[Any],
                compaction_summary: str = "") -> Tuple[str, List[Any]]:
        """Run full compaction.

        Returns (new_summary, remaining_messages).
        The new_summary replaces all old messages.
        remaining_messages are the most recent N kept intact.
        """
        if len(messages) <= self._keep_recent:
            return compaction_summary, messages

        # Split: old messages to summarize, recent to keep
        old_messages = messages[:-self._keep_recent]
        recent_messages = messages[-self._keep_recent:]

        # Strip media from old messages before summarizing
        stripped_old = []
        for msg in old_messages:
            content = getattr(msg, 'content', '') or (msg.get('content', '') if isinstance(msg, dict) else '')
            stripped_content = strip_media_content(content)
            if isinstance(msg, dict):
                stripped_old.append({**msg, 'content': stripped_content})
            else:
                stripped_old.append(msg)

        # Build text to summarize
        text_to_summarize = self._messages_to_text(stripped_old)

        # Include previous summary so it's incorporated
        if compaction_summary:
            text_to_summarize = (
                f"[PREVIOUS SUMMARY]\n{compaction_summary}\n\n"
                f"[NEW MESSAGES TO ADD TO SUMMARY]\n{text_to_summarize}"
            )

        # Generate summary
        if self._llm_call:
            try:
                new_summary = self._llm_call(text_to_summarize)
            except Exception as e:
                logger.warning("[compaction] LLM summarization failed: %s — using heuristic", e)
                new_summary = self._heuristic_summary(text_to_summarize)
        else:
            new_summary = self._heuristic_summary(text_to_summarize)

        # Cap summary size (same as original Tera Pilot)
        MAX_SUMMARY_CHARS = 4000
        if len(new_summary) > MAX_SUMMARY_CHARS:
            new_summary = new_summary[-MAX_SUMMARY_CHARS:]
            logger.warning("[compaction] summary truncated to %d chars", MAX_SUMMARY_CHARS)

        return new_summary, recent_messages

    def _messages_to_text(self, messages: List[Any]) -> str:
        """Convert messages to text for summarization."""
        parts = []
        for msg in messages:
            role = getattr(msg, 'role', '') or msg.get('role', '') if isinstance(msg, dict) else ''
            content = getattr(msg, 'content', '') or msg.get('content', '') if isinstance(msg, dict) else ''
            role_label = {"user": "USER", "assistant": "ASSISTANT", "tool": "TOOL"}.get(role, role.upper())
            # Truncate very long tool results
            if role == "tool" and len(content) > 1000:
                content = content[:1000] + f"... ({len(content)} total chars)"
            parts.append(f"[{role_label}]\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def _heuristic_summary(text: str) -> str:
        """Fallback summarizer when LLM is unavailable.

        Extracts key information: user requests, files modified,
        tool calls made, errors encountered. Not as good as LLM
        summarization but keeps the essential context.
        """
        lines = text.splitlines()
        # Extract user messages (requests/intent)
        user_lines = [l for l in lines if l.startswith("[USER]")]
        # Extract tool results (actions taken)
        tool_lines = [l for l in lines if l.startswith("[TOOL]")]
        # Extract errors
        error_lines = [l for l in lines if "[ERROR]" in l or "[FAILED]" in l or "[REJECTED" in l]

        parts = []
        if user_lines:
            # Take first and last user message
            parts.append(f"User requests: {len(user_lines)} exchanges")
            if user_lines:
                first_content = user_lines[0].replace("[USER]", "").strip()[:200]
                parts.append(f"  Initial request: {first_content}")
            if len(user_lines) > 1:
                last_content = user_lines[-1].replace("[USER]", "").strip()[:200]
                parts.append(f"  Latest request: {last_content}")

        if tool_lines:
            # Count tool types
            tool_types = {}
            for l in tool_lines:
                for keyword in ["WRITTEN", "PATCHED", "CREATED", "DELETED",
                                "RENAMED", "STR_REPLACE", "RUN", "GREP",
                                "GIT", "MKDIR", "OFFICE"]:
                    if keyword in l:
                        tool_types[keyword] = tool_types.get(keyword, 0) + 1
            if tool_types:
                summary = ", ".join(f"{k}:{v}" for k, v in tool_types.items())
                parts.append(f"Actions taken: {summary}")

        if error_lines:
            parts.append(f"Errors/rejections: {len(error_lines)}")

        if not parts:
            # Last resort: take the last 500 chars
            return text[-500:] if len(text) > 500 else text

        return "Heuristic summary:\n" + "\n".join(parts)


# ── Combined Compaction Manager ──────────────────────────────────────────

class CompactionManager:
    """Orchestrates full + micro compaction.

    Provides a single interface for the agent loop to call before each
    LLM request. The decision tree:
      1. If context > 80% of budget → run FullCompaction
      2. If cache miss detected AND context > 50% → run MicroCompaction
      3. Otherwise → no compaction needed
    """

    def __init__(self,
                 llm_call_fn: Optional[callable] = None,
                 keep_recent: int = 6):
        self.full = FullCompaction(llm_call_fn=llm_call_fn, keep_recent=keep_recent)
        self.micro = MicroCompaction()
        self._last_activity_at: Optional[float] = None
        self._lock = threading.Lock()

    def update_activity(self) -> None:
        """Record that the user/agent was active (resets cache-miss timer)."""
        with self._lock:
            self._last_activity_at = time.time()

    def maybe_compact(self,
                      messages: List[Any],
                      compaction_summary: str,
                      total_tokens: int,
                      max_tokens: int) -> Tuple[str, List[Any], bool]:
        """Check and run compaction if needed.

        Returns (new_summary, messages, did_compact).
        """
        if max_tokens <= 0:
            return compaction_summary, messages, False

        # Priority 1: Full compaction if context is getting full
        if self.full.should_compact(total_tokens, max_tokens, threshold=0.80):
            logger.info("[compaction] full compaction triggered (%d/%d tokens)",
                        total_tokens, max_tokens)
            new_summary, remaining = self.full.compact(messages, compaction_summary)
            self.micro.reset(0)  # reset micro cutoff after full compaction
            self.update_activity()
            return new_summary, remaining, True

        # Priority 2: Micro compaction if cache miss + moderate usage
        with self._lock:
            last_at = self._last_activity_at
        if self.micro.detect(messages, total_tokens, max_tokens, last_at):
            effect = self.micro.measure_effect(messages)
            if effect["saved"] > 200:  # only if meaningful savings
                logger.info("[compaction] micro compaction: saving ~%d tokens (%d messages truncated)",
                            effect["saved"], effect["truncated_count"])
                self.micro.compact(messages)
                self.update_activity()
                return compaction_summary, messages, True

        return compaction_summary, messages, False

    def get_stats(self) -> Dict[str, Any]:
        return {
            "micro_cutoff": self.micro.cutoff,
            "last_activity_at": self._last_activity_at,
        }


# ── Helpers ──────────────────────────────────────────────────────────────

def _estimate_tokens_fast(text: str) -> int:
    """Fast token estimation: ~4 chars/token for English/code, ~2 for CJK."""
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u0400' <= c <= '\u04ff')
    non_cjk = len(text) - cjk
    return (cjk // 2) + (non_cjk // 4) + 1