"""Cerebras provider — ultra-fast inference for supported models."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class CerebrasProvider(OpenAICompatProvider):
    provider_id: str = "cerebras"
    label: str = "Cerebras"
    default_model: str = "llama-4-scout-17b-16e-instruct"
    api_base: str = "https://api.cerebras.ai/v1"
    env_var: str = "CEREBRAS_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })