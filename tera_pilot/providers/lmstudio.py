"""LM Studio provider — local models via LM Studio (localhost:1234).

LM Studio exposes an OpenAI-compatible endpoint at /v1/chat/completions.
No API key required — auth is handled by LM Studio's local server.
"""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class LMStudioProvider(OpenAICompatProvider):
    provider_id: str = "lmstudio"
    label: str = "LM Studio"
    default_model: str = ""
    api_base: str = "http://localhost:1234/v1"
    env_var: str = ""  # LM Studio requires no API key
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
        ProviderCapability.OFFLINE,
    })

    def _ensure_loaded(self) -> None:
        """Skip API key check — LM Studio doesn't use keys."""
        if not self._loaded:
            self.load()
        if not self._api_key:
            self._api_key = "lmstudio"
