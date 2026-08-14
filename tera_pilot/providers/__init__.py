"""
Tera Pilot v1.0.4 — Unified Provider Interface.

A single abstraction across all model backends:
  - OpenRouterProvider   (any model via openrouter.ai)
  - GroqProvider         (LPU-accelerated Llama / Mixtral)
  - OpenAIProvider       (GPT-4o, o1, …)
  - AnthropicProvider    (Claude family)
  - DeepSeekProvider     (DeepSeek-V3, DeepSeek-R1)
  - ZAIProvider          (GLM models via z.ai)
  - GeminiProvider       (Google Gemini 2.5 Pro)
  - MistralProvider      (Mistral Large, Codestral)
  - TogetherProvider     (open-source models via Together AI)
  - FireworksProvider     (fast open-source model inference)
  - XAIProvider           (Grok models via x.ai)
  - CerebrasProvider      (ultra-fast inference)
  - SambaNovaProvider     (enterprise fast inference)
  - OllamaProvider        (local models via Ollama)
  - LMStudioProvider      (local models via LM Studio)

The Composer (HTML) talks to ProviderRegistry; the registry routes
generate() / stream() to the active provider. Switching providers
is a no-op — no UI rebuild, no agent restart.
"""

from .base import (
    Provider,
    ProviderConfig,
    ProviderMessage,
    ProviderResponse,
    ProviderCapability,
    ProviderError,
)
from .registry import ProviderRegistry, get_registry
from .openai_provider import OpenAIProvider
from .anthropic import AnthropicProvider
from .openrouter import OpenRouterProvider
from .groq import GroqProvider
from .deepseek import DeepSeekProvider
from .zai import ZAIProvider
from .gemini import GeminiProvider
from .mistral import MistralProvider
from .together import TogetherProvider
from .fireworks import FireworksProvider
from .xai import XAIProvider
from .cerebras import CerebrasProvider
from .sambanova import SambaNovaProvider
from .ollama import OllamaProvider
from .lmstudio import LMStudioProvider
from .nvidia_nim import NvidiaNIMProvider

__all__ = [
    "Provider",
    "ProviderConfig",
    "ProviderMessage",
    "ProviderResponse",
    "ProviderCapability",
    "ProviderError",
    "ProviderRegistry",
    "get_registry",
    "OpenAIProvider",
    "AnthropicProvider",
    "OpenRouterProvider",
    "GroqProvider",
    "DeepSeekProvider",
    "ZAIProvider",
    "GeminiProvider",
    "MistralProvider",
    "TogetherProvider",
    "FireworksProvider",
    "XAIProvider",
    "CerebrasProvider",
    "SambaNovaProvider",
    "OllamaProvider",
    "LMStudioProvider",
    "NvidiaNIMProvider",
]