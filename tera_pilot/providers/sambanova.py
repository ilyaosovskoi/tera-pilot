"""SambaNova provider — fast enterprise inference for open-source models."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class SambaNovaProvider(OpenAICompatProvider):
    provider_id: str = "sambanova"
    label: str = "SambaNova"
    default_model: str = "Meta-Llama-3.3-70B-Instruct"
    api_base: str = "https://api.sambanova.ai/v1"
    env_var: str = "SAMBANOVA_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })