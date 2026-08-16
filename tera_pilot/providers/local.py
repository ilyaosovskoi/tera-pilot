"""Local provider — keyless OpenAI-compatible endpoint (LM Studio / Ollama / llama.cpp).

Tera Pilot already special-cased ``provider_id == "local"`` in two places
(``OpenAICompatProvider.load()`` skips the missing-key warning, and the
token tracker prices the ``"local"`` model at $0), but no provider class
with that id was ever registered — so a ``config.json`` entry like
``providers.local`` failed to load with ``Unknown provider: local`` on
every startup.

This class completes the picture: a generic OpenAI-compatible client
that requires no API key and points at a local server by default
(Ollama's OpenAI-compatible endpoint at ``http://localhost:11434/v1``;
override via ``api_base`` in the config for LM Studio etc.).
"""

from __future__ import annotations

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class LocalProvider(OpenAICompatProvider):
    """Keyless OpenAI-compatible provider for any local endpoint."""

    provider_id: str = "local"
    label: str = "Local (OpenAI-compatible)"
    default_model: str = ""
    api_base: str = "http://localhost:11434/v1"
    env_var: str = ""  # no API key required
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
        ProviderCapability.OFFLINE,
    })

    def _ensure_loaded(self) -> None:
        """Skip the API key check — local endpoints are keyless.

        Mirrors OllamaProvider: a dummy key keeps the ``Authorization``
        header harmless for servers that ignore it.
        """
        if not self._loaded:
            self.load()
        if not self._api_key:
            self._api_key = "local"
