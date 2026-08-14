"""DeepSeek provider — DeepSeek-V3, DeepSeek-R1, etc."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class DeepSeekProvider(OpenAICompatProvider):
    provider_id: str = "deepseek"
    label: str = "DeepSeek"
    default_model: str = "deepseek-chat"
    api_base: str = "https://api.deepseek.com/v1"
    env_var: str = "DEEPSEEK_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # v1.2.1-fix (review §4.3): DeepSeek context windows.
    # deepseek-chat (V3) = 64K, deepseek-reasoner (R1) = 64K.
    _MODEL_CONTEXT_WINDOWS = {
        "deepseek-chat": 64_000,
        "deepseek-reasoner": 64_000,
        "deepseek-r1": 64_000,
        "deepseek-coder": 16_384,
    }
    context_window: int = 64_000