"""OpenRouter provider — routes to any model (Claude, Gemini, Mistral, …)."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class OpenRouterProvider(OpenAICompatProvider):
    provider_id: str = "openrouter"
    label: str = "OpenRouter"
    default_model: str = "anthropic/claude-sonnet-4.6"
    api_base: str = "https://openrouter.ai/api/v1"
    env_var: str = "OPENROUTER_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # v1.2.1-fix (review §4.3): OpenRouter routes to many providers, so
    # we match on the model name substring. Defaults are conservative
    # (128K) since most routed models are >= 128K. The user can override
    # per-config via ProviderConfig.extra["context_window"].
    _MODEL_CONTEXT_WINDOWS = {
        "claude-3.5-sonnet": 200_000,
        "claude-3.5-haiku": 200_000,
        "claude-3-opus": 200_000,
        "claude-sonnet-4": 200_000,
        "claude-opus-4": 200_000,
        "gpt-4o": 128_000,
        "gpt-4-turbo": 128_000,
        "gpt-4.1": 1_000_000,
        "gpt-5": 400_000,
        "o1": 200_000,
        "o3": 200_000,
        "gemini-2.5-pro": 1_000_000,
        "gemini-2.0": 1_000_000,
        "gemini-1.5-pro": 2_000_000,
        "llama-3.3-70b": 128_000,
        "llama-3.1-": 128_000,
        "deepseek-chat": 64_000,
        "deepseek-r1": 64_000,
        "mistral-large": 128_000,
        "qwen-2.5-": 128_000,
    }
    context_window: int = 128_000

    def _headers(self):
        # OpenRouter wants extra headers for attribution
        headers = super()._headers()
        headers["HTTP-Referer"] = "https://tera_pilot.app"
        headers["X-Title"] = "Tera Pilot"
        return headers
