"""Fireworks AI provider — fast open-source model inference."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class FireworksProvider(OpenAICompatProvider):
    provider_id: str = "fireworks"
    label: str = "Fireworks"
    default_model: str = "accounts/fireworks/models/llama-v3p1-70b-instruct"
    api_base: str = "https://api.fireworks.ai/inference/v1"
    env_var: str = "FIREWORKS_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })