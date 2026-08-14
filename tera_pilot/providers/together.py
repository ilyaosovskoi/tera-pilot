"""Together AI provider — open-source models via Together."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class TogetherProvider(OpenAICompatProvider):
    provider_id: str = "together"
    label: str = "Together AI"
    default_model: str = "meta-llama/Llama-3-70b-chat-hf"
    api_base: str = "https://api.together.xyz/v1"
    env_var: str = "TOGETHER_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })