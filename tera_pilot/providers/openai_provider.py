"""OpenAI provider — GPT-4o, o1, etc."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class OpenAIProvider(OpenAICompatProvider):
    provider_id: str = "openai"
    label: str = "OpenAI"
    default_model: str = "gpt-5.5"
    api_base: str = "https://api.openai.com/v1"
    env_var: str = "OPENAI_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.VISION,
        ProviderCapability.JSON_MODE,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # v1.2.1-fix (review §4.3): context windows for current OpenAI
    # models. Used by get_context_window() to size ContextMemory and
    # ContextManager budgets. Numbers are taken from OpenAI's public
    # docs as of 2026-07. gpt-4o family = 128K, o1 family = 200K,
    # gpt-4-turbo = 128K, gpt-3.5-turbo = 16K.
    _MODEL_CONTEXT_WINDOWS = {
        "gpt-4o": 128_000,
        "gpt-4-turbo": 128_000,
        "gpt-4-1106": 128_000,
        "gpt-4-0125": 128_000,
        "o1": 200_000,
        "o3": 200_000,
        "gpt-4.1": 1_000_000,
        "gpt-5": 400_000,
        "gpt-3.5-turbo": 16_385,
    }
