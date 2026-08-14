"""
Nvidia NIM Provider — OpenAI-compatible API for Nvidia's hosted models.

API docs: https://docs.nvidia.com/nim/large-language-models/latest/
Base URL: https://integrate.api.nvidia.com/v1
Authentication: NVIDIA_API_KEY environment variable or api_key in config
"""

from __future__ import annotations

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class NvidiaNIMProvider(OpenAICompatProvider):
    """Nvidia NIM (Nvidia Inference Microservices) provider.

    Uses OpenAI-compatible Chat Completions API at https://integrate.api.nvidia.com/v1
    Requires NVIDIA_API_KEY (get from https://build.nvidia.com/explore/discover)
    """

    provider_id: str = "nvidia_nim"
    label: str = "Nvidia NIM"
    default_model: str = "deepseek-ai/deepseek-v4-flash-0731"
    api_base: str = "https://integrate.api.nvidia.com/v1"
    env_var: str = "NVIDIA_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })
    context_window: int = 1_048_576  # deepseek-v4-flash-0731 context window

    # Model-specific context windows (substring match)
    _MODEL_CONTEXT_WINDOWS: dict[str, int] = {
        "llama-3.1-8b": 131_072,
        "llama-3.1-70b": 131_072,
        "llama-3.1-405b": 131_072,
        "nemotron-3-ultra": 128_000,
        "nemotron-3-ultra-256k": 256_000,
        "mistral-7b": 32_768,
        "mixtral-8x7b": 32_768,
        "mixtral-8x22b": 32_768,
        "qwen2-7b": 32_768,
        "qwen2-72b": 32_768,
        "gemma-2-9b": 8_192,
        "gemma-2-27b": 8_192,
        "phi-3-mini": 4_096,
        "phi-3-medium": 4_096,
        "phi-3.5-mini": 4_096,
        "deepseek-v4-flash": 1_048_576,
    }