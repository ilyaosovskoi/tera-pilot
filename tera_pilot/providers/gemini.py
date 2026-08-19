"""Google Gemini provider — via OpenAI-compatible endpoint."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class GeminiProvider(OpenAICompatProvider):
    provider_id: str = "gemini"
    label: str = "Google Gemini"
    default_model: str = "gemini-3.1-pro"
    api_base: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    env_var: str = "GOOGLE_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.VISION,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # v1.2.1-fix (review §4.3): Gemini context windows. 2.5 Pro / Flash
    # = 1M tokens (long-context), 2.0 / 1.5 Pro = 2M, 1.5 Flash = 1M.
    _MODEL_CONTEXT_WINDOWS = {
        "gemini-3.5": 1_000_000,
        "gemini-3.1": 1_000_000,
        "gemini-3": 1_000_000,
        "gemini-2.5-pro": 1_000_000,
        "gemini-2.5-flash": 1_000_000,
        "gemini-2.0": 1_000_000,
        "gemini-1.5-pro": 2_000_000,
        "gemini-1.5-flash": 1_000_000,
    }
    context_window: int = 1_000_000