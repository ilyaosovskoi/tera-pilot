"""Groq provider — LPU-accelerated Llama / Mixtral / Gemma."""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class GroqProvider(OpenAICompatProvider):
    provider_id: str = "groq"
    label: str = "Groq"
    default_model: str = "meta-llama/llama-4-maverick-17b-128e-instruct"
    api_base: str = "https://api.groq.com/openai/v1"
    env_var: str = "GROQ_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # v1.2.1-fix (review §4.3): context windows for current Groq-hosted
    # models. Numbers from Groq's public docs as of 2026-07.
    _MODEL_CONTEXT_WINDOWS = {
        "llama-3.3-70b": 128_000,
        "llama-3.1-": 128_000,
        "llama-3.2-": 128_000,
        "gemma2-9b": 8_192,
        "gemma-7b": 8_192,
        "mixtral-8x7b": 32_768,
        "deepseek-r1": 128_000,
        "qwen-2.5-": 128_000,
    }
    context_window: int = 128_000
