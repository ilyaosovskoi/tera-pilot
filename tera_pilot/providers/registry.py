"""
Provider Registry — the single source of truth for "which provider is active".

The web bridge calls:
    registry.set_active("openrouter")
    registry.configure("openrouter", ProviderConfig(...))
    registry.active.generate(messages, skill=...)

Switching providers is O(1) and does not require restarting the agent.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Type

from .base import (
    Provider,
    ProviderConfig,
    ProviderError,
)

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Holds instances of every provider; routes calls to the active one."""

    def __init__(self) -> None:
        self._classes: Dict[str, Type[Provider]] = {}
        self._instances: Dict[str, Provider] = {}
        self._configs: Dict[str, ProviderConfig] = {}
        self._active_id: Optional[str] = None
        self._lock = threading.RLock()

    # ── Registration ───────────────────────────────────────────────

    def register(self, provider_cls: Type[Provider]) -> None:
        """Register a Provider class. Does not instantiate it yet."""
        pid = provider_cls.provider_id
        if not pid:
            raise ValueError(f"{provider_cls} has no provider_id")
        with self._lock:
            self._classes[pid] = provider_cls
            logger.info(f"[registry] registered provider class: {pid}")

    def has_provider(self, provider_id: str) -> bool:
        """Return True iff *provider_id* is registered. (v1.0.5-hotfix)"""
        with self._lock:
            return provider_id in self._classes

    def register_default(self) -> None:
        """Register all built-in providers."""
        from .lmstudio import LMStudioProvider
        from .openai_provider import OpenAIProvider
        from .anthropic import AnthropicProvider
        from .openrouter import OpenRouterProvider
        from .groq import GroqProvider
        from .deepseek import DeepSeekProvider
        from .zai import ZAIProvider
        from .gemini import GeminiProvider
        from .mistral import MistralProvider
        from .together import TogetherProvider
        from .fireworks import FireworksProvider
        from .xai import XAIProvider
        from .cerebras import CerebrasProvider
        from .sambanova import SambaNovaProvider
        from .ollama import OllamaProvider
        from .nvidia_nim import NvidiaNIMProvider
        from .local import LocalProvider

        for cls in (LMStudioProvider, OpenAIProvider, AnthropicProvider,
                    OpenRouterProvider, GroqProvider, DeepSeekProvider,
                    ZAIProvider, GeminiProvider, MistralProvider, TogetherProvider,
                    FireworksProvider, XAIProvider, CerebrasProvider,
                    SambaNovaProvider, OllamaProvider, NvidiaNIMProvider,
                    LocalProvider):
            self.register(cls)

    # ── Configuration ─────────────────────────────────────────────

    def configure(self, provider_id: str, config: ProviderConfig) -> None:
        """Set/replace the config for a provider. Unloads the old instance."""
        with self._lock:
            if provider_id not in self._classes:
                raise ProviderError(f"Unknown provider: {provider_id}")

            # Unload existing instance if any
            old = self._instances.pop(provider_id, None)
            if old and old.is_loaded:
                try:
                    old.unload()
                except Exception as e:
                    logger.warning(f"[registry] unload error: {e}")

            self._configs[provider_id] = config
            logger.info(f"[registry] configured {provider_id} · model={config.model}")

    # ── Active provider ───────────────────────────────────────────

    def set_active(self, provider_id: str) -> None:
        with self._lock:
            if provider_id not in self._classes:
                raise ProviderError(f"Unknown provider: {provider_id}")
            self._active_id = provider_id
            logger.info(f"[registry] active provider → {provider_id}")

    @property
    def active_id(self) -> Optional[str]:
        return self._active_id

    @property
    def active(self) -> Provider:
        if not self._active_id:
            raise ProviderError("No active provider — call set_active() first")
        return self._get_or_create(self._active_id)

    def get(self, provider_id: str) -> Provider:
        return self._get_or_create(provider_id)

    def _get_or_create(self, provider_id: str) -> Provider:
        with self._lock:
            if provider_id in self._instances:
                return self._instances[provider_id]

            cls = self._classes.get(provider_id)
            if not cls:
                raise ProviderError(f"Provider not registered: {provider_id}")

            config = self._configs.get(provider_id) or ProviderConfig(
                provider_id=provider_id,
                model=cls.default_model,
            )
            instance = cls(config)
            self._instances[provider_id] = instance
            return instance

    # ── Introspection ─────────────────────────────────────────────

    def list_providers(self) -> List[Dict[str, object]]:
        """Return metadata for the UI switcher."""
        out = []
        for pid, cls in self._classes.items():
            config = self._configs.get(pid)
            out.append({
                "id":           pid,
                "label":        cls.label,
                "default_model": cls.default_model,
                "capabilities": [c.value for c in cls.capabilities],
                "active":       pid == self._active_id,
                "configured":   config is not None,
                "model":        config.model if config else cls.default_model,
            })
        return out

    def status(self) -> Dict[str, object]:
        return {
            "active":     self._active_id,
            "providers":  self.list_providers(),
        }


# ── Module-level singleton ─────────────────────────────────────────

_registry: Optional[ProviderRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> ProviderRegistry:
    """Get the global ProviderRegistry singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ProviderRegistry()
                _registry.register_default()
                # Load custom providers after built-ins
                from .custom_providers import register_custom_providers
                register_custom_providers(_registry)
                _registry.set_active("ollama")
    return _registry


def reload_registry() -> ProviderRegistry:
    """Force reload the registry (e.g., after adding custom providers)."""
    global _registry
    with _registry_lock:
        _registry = None
        return get_registry()
