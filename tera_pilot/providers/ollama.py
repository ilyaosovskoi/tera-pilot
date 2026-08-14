"""Ollama provider — local models via Ollama (localhost:11434).

Ollama exposes an OpenAI-compatible endpoint at /v1/chat/completions.
No API key required — auth is handled by Ollama's local security model.
"""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class OllamaProvider(OpenAICompatProvider):
    provider_id: str = "ollama"
    label: str = "Ollama"
    default_model: str = "llama3.1"
    api_base: str = "http://localhost:11434/v1"
    env_var: str = ""  # Ollama requires no API key
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
        ProviderCapability.OFFLINE,
    })

    def _ensure_loaded(self) -> None:
        """Skip API key check — Ollama doesn't use keys."""
        if not self._loaded:
            self.load()
        if not self._api_key:
            # Ollama passes a dummy key so the Bearer header is harmless
            self._api_key = "ollama"