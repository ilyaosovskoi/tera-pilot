"""Z.ai provider — GLM models via z.ai API (OpenAI-compatible)."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class ZAIProvider(OpenAICompatProvider):
    provider_id: str = "zai"
    label: str = "Z.ai"
    default_model: str = "glm-5.1"
    api_base: str = "https://open.bigmodel.cn/api/paas/v4"
    env_var: str = "ZAI_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # v1.2.1-fix (review §4.3): Z.ai GLM context windows.
    # GLM-4-Plus / GLM-4-Air / GLM-4-Long = 128K, GLM-4-Flash = 128K,
    # GLM-4.5 = 128K, GLM-4-9B = 128K.
    _MODEL_CONTEXT_WINDOWS = {
        "glm-4-plus": 128_000,
        "glm-4-air": 128_000,
        "glm-4-long": 1_000_000,
        "glm-4-flash": 128_000,
        "glm-4.5": 128_000,
        "glm-4-9b": 128_000,
        "glm-4v": 8_192,
    }
    context_window: int = 128_000