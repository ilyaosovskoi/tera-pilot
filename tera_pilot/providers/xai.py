"""xAI provider — Grok models via x.ai API."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class XAIProvider(OpenAICompatProvider):
    provider_id: str = "xai"
    label: str = "xAI"
    default_model: str = "grok-2"
    api_base: str = "https://api.x.ai/v1"
    env_var: str = "XAI_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })