"""
Provider base class — every backend implements this.

The contract is intentionally minimal:
  - load()    → warm up model / validate API key
  - generate()→ blocking, returns a single ProviderResponse
  - stream()  → generator yielding token chunks
  - unload()  → release resources
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────

class ProviderCapability(str, Enum):
    """What a provider can do — used by the UI to grey out unsupported features."""
    CHAT          = "chat"
    STREAMING     = "streaming"
    TOOL_CALLING  = "tool_calling"
    VISION        = "vision"
    JSON_MODE     = "json_mode"
    SYSTEM_PROMPT = "system_prompt"
    SKILLS        = "skills"           # accepts a skill injection in system prompt
    OFFLINE       = "offline"          # works without internet


@dataclass
class ProviderMessage:
    """One message in a conversation."""
    role: str                       # "system" | "user" | "assistant" | "tool"
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:           d["name"] = self.name
        if self.tool_call_id:   d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:     d["tool_calls"] = self.tool_calls
        return d


@dataclass
class ProviderConfig:
    """Configuration for a provider instance."""
    provider_id: str                # "ollama" | "lmstudio" | "openai" | "anthropic" | "openrouter" | "groq"
    model: str                      # provider-specific model identifier
    api_key: Optional[str] = None
    api_base: Optional[str] = None  # override base URL
    temperature: float = 0.2
    max_tokens: int = 4096
    top_p: float = 0.95
    stream: bool = True
    timeout: float = 120.0          # seconds

    # Extras (free-form per provider)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderResponse:
    """Response from a non-streaming generate() call."""
    text: str
    model: str
    provider: str
    finish_reason: str = "stop"
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: Optional[List[Dict[str, Any]]] = None
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProviderError(Exception):
    """Raised when a provider fails to load or generate."""


# ── Abstract Provider ───────────────────────────────────────────────

class Provider(ABC):
    """Abstract base — all backends implement this."""

    #: Stable identifier ("local", "openai", …) — matches ProviderConfig.provider_id
    provider_id: str = "base"

    #: Human-readable label shown in the UI switcher
    label: str = "Base"

    #: Default model when user picks this provider without specifying one
    default_model: str = ""

    #: Capabilities advertised to the UI
    capabilities: frozenset = frozenset({ProviderCapability.CHAT})

    # v1.2.1-fix (review §4.3): default context window in tokens. Used
    # by AgentRuntime to size ContextMemory.max_tokens and
    # ContextManager._token_budget proportionally to the ACTUAL model
    # window instead of the old hardcoded 8K/6K constants. Concrete
    # providers override this with their real window (e.g. OpenAI
    # gpt-4o = 128_000, Anthropic claude-3-5-sonnet = 200_000, Groq
    # llama-3.3-70b = 128_000). The number is the MAXIMUM the model
    # CAN accept — AgentRuntime still applies its own fraction (e.g.
    # memory = window // 4) to leave room for system prompt + tools.
    context_window: int = 8_192

    def __init__(self, config: ProviderConfig):
        self.config = config
        self._loaded = False
        self._lock = threading.RLock()
        self._info: Dict[str, Any] = {}
        logger.info(f"[{self.provider_id}] Provider instantiated · model={config.model}")

    # ── Lifecycle ──────────────────────────────────────────────────

    @abstractmethod
    def load(self) -> bool:
        """Warm up the model / validate credentials. Return True on success."""
        raise NotImplementedError

    @abstractmethod
    def unload(self) -> None:
        """Release model weights / close HTTP sessions."""
        raise NotImplementedError

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def info(self) -> Dict[str, Any]:
        """Runtime info (RAM used, model path, latency, …)."""
        return self._info

    # ── Context window ────────────────────────────────────────────

    def get_context_window(self) -> int:
        """v1.2.1-fix (review §4.3): return the model's maximum context
        window in tokens.

        Concrete providers SHOULD override this to return the actual
        window for the configured model (e.g. OpenAI gpt-4o = 128_000).
        The base implementation falls back to the class-level
        ``context_window`` attribute, which itself defaults to 8_192
        (a conservative window that works for any model).

        AgentRuntime uses this to size ContextMemory.max_tokens and
        ContextManager._token_budget proportionally — instead of the
        old hardcoded 8K/6K constants that were on order of magnitude
        smaller than what modern models actually support.

        Subclasses can also peek at ``self.config.extra.get("context_window")``
        to let users override the window per-config (useful for custom
        OpenAI-compatible endpoints with non-standard windows).
        """
        # Allow per-config override via ProviderConfig.extra.
        override = self.config.extra.get("context_window") if self.config.extra else None
        if isinstance(override, int) and override > 0:
            return override
        return self.context_window

    # ── Generation ────────────────────────────────────────────────

    @abstractmethod
    def generate(
        self,
        messages: List[ProviderMessage],
        *,
        skill: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        stop: Optional[List[str]] = None,
        model: Optional[str] = None,
    ) -> ProviderResponse:
        """Blocking generation — returns a single response.

        ``model`` optionally overrides the configured model for THIS call
        only (G20b per-subtask routing: the task-decomposition router
        places each subtask on a different model without touching the
        provider's config). Subclasses MUST accept it and use it in
        place of ``self.config.model`` when non-None.

        v2.4.1-fix: several callers (consensus_engine, guardian,
        second_opinion, persona_memory, task_decomposition_router, and
        AgentRuntime's G20b override path) were passing ``model=`` here
        — but no provider implemented the kwarg, so every one of those
        calls crashed with ``TypeError: unexpected keyword argument
        'model'`` (the Guardian LLM review, the consensus run, and the
        second-opinion check silently degraded to APPROVE; the
        decomposition router always fell back to single-model).
        """
        raise NotImplementedError

    @abstractmethod
    def stream(
        self,
        messages: List[ProviderMessage],
        *,
        skill: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        stop: Optional[List[str]] = None,
        model: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """Streaming generation — yields token chunks.

        ``model`` optionally overrides the configured model for THIS call
        only (same contract as :meth:`generate`).
        """
        raise NotImplementedError

    # ── Helpers ───────────────────────────────────────────────────

    def _inject_skill(self, messages: List[ProviderMessage], skill: Optional[str]) -> List[ProviderMessage]:
        """
        Inject a Skill into the system prompt.

        A Skill is a structured instruction block (like the one you sent me
        at the start of this conversation) that sharpens the model on one
        capability without fine-tuning. We prepend it to any existing
        system message so the skill reads as the model's "job description".
        """
        if not skill:
            return messages

        skill_block = (
            "# ACTIVE SKILL\n\n"
            f"{skill.strip()}\n\n"
            "# END SKILL\n"
            "Follow the skill above for the duration of this conversation.\n"
        )

        if messages and messages[0].role == "system":
            new_sys = ProviderMessage(
                role="system",
                content=messages[0].content + "\n\n" + skill_block,
            )
            return [new_sys] + messages[1:]
        return [ProviderMessage(role="system", content=skill_block)] + messages

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} model={self.config.model!r} loaded={self._loaded}>"
