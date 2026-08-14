"""Mistral AI provider — Mistral Large, Codestral, etc."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class MistralProvider(OpenAICompatProvider):
    provider_id: str = "mistral"
    label: str = "Mistral"
    default_model: str = "mistral-large-latest"
    api_base: str = "https://api.mistral.ai/v1"
    env_var: str = "MISTRAL_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # v1.2.1-fix (review §4.3): Mistral context windows.
    # Large / Medium / Small = 128K, Codestral = 256K, Nemo = 128K.
    _MODEL_CONTEXT_WINDOWS = {
        "mistral-large": 128_000,
        "mistral-medium": 32_768,
        "mistral-small": 32_768,
        "mistral-tiny": 32_768,
        "codestral": 256_000,
        "mistral-nemo": 128_000,
        "open-mixtral-8x7b": 32_768,
        "open-mistral-7b": 32_768,
    }
    context_window: int = 128_000