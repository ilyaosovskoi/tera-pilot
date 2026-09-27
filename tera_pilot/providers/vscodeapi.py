"""VSCode API provider — 40+ LLMs through one OpenAI-compatible gateway.

https://vscodeapi.com — OpenAI-compatible endpoint (``/v1``), so this
is a thin ``OpenAICompatProvider`` subclass, same pattern as
``OpenRouterProvider``.

Free plan (no card): 1M tokens/month, ALL 32+ models included,
60 req/min, streaming SSE + tool calling. Keys look like ``cc_...``.

Model IDs below are taken verbatim from vscodeapi.com (homepage
quickstart + model cards, Sep 2026):
  deepseek-v4-flash-0731, deepseek-v4-pro-0813, gpt-5.6-luna,
  gpt-5.6-sol, claude-fable-5.1, claude-opus-4.8, gemma-2-2b,
  qwen3.8-27b, seed-2.1-turbo
"""

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


class VSCodeAPIProvider(OpenAICompatProvider):
    provider_id: str = "vscodeapi"
    label: str = "VSCode API"
    # Cheap + strong at coding, 1M context — safe default that barely
    # dents the 1M/month free allowance. Switch per-task with
    # /model vscodeapi <id>, e.g. claude-fable-5.1 for hard frontend work.
    default_model: str = "deepseek-v4-pro-0813"
    api_base: str = "https://vscodeapi.com/v1"
    env_var: str = "VSCODEAPI_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # Context windows from GET /v1/models (live API data, not the
    # marketing homepage — the two disagree). Matched by substring
    # against the configured model name, first match wins, so keep
    # specific prefixes before general ones.
    _MODEL_CONTEXT_WINDOWS = {
        "qwen3.8-27b": 262_144,
        "seed-2.1": 262_144,
        "gemma-2-2b": 8_192,
        "grok-4": 500_000,
        "deepseek-v4": 1_048_576,
        "gpt-5": 1_050_000,
        "kimi": 1_000_000,
        "gemini-3": 1_048_576,
        "glm-5": 1_000_000,
        "qwen": 1_000_000,
        "claude": 1_000_000,
    }
    context_window: int = 1_048_576
