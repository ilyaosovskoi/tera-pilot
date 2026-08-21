"""LM Studio provider — local models via LM Studio (localhost:1234).

LM Studio exposes an OpenAI-compatible endpoint at /v1/chat/completions.
No API key required — auth is handled by LM Studio's local server.
"""

from typing import Optional

from .openai_compat import OpenAICompatProvider
from .base import ProviderCapability


# LM Studio accepts these top-level reasoning_effort values via its
# OpenAI-compatible chat.completions endpoint (ported from Hermes Agent's
# lmstudio_reasoning module, MIT). Toggle-style models publish
# allowed_options as ["off", "on"] — map them onto the request
# vocabulary.
_LM_VALID_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
_LM_EFFORT_ALIASES = {"off": "none", "on": "medium"}
_LM_EFFORT_CLAMP = {"max": "xhigh", "ultra": "xhigh"}


class LMStudioProvider(OpenAICompatProvider):
    provider_id: str = "lmstudio"
    label: str = "LM Studio"
    default_model: str = ""
    api_base: str = "http://localhost:1234/v1"
    env_var: str = ""  # LM Studio requires no API key
    # v2.3.5-fix (LM Studio / LFM 2.5): LM Studio's engine VALIDATES
    # generated native tool calls and hard-400s any call whose string
    # content contains quotes (e.g. ``query = f"SELECT ... '{username}'"``
    # — reproduced with plain chat.completions requests, every tool name
    # and both stream modes). The agent runtime must NOT advertise the
    # ``tools`` schema to LM Studio; the model's native
    # ``<|tool_call_start|>[name(arg='...')]<|tool_call_end|>`` text is
    # parsed instead (see agent_runtime/parser.py).
    emits_native_tool_calls: bool = False
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
        ProviderCapability.OFFLINE,
    })

    def _resolved_reasoning_effort(self) -> Optional[str]:
        """LM Studio vocabulary clamp for ``reasoning_effort``.

        Generic levels beyond LM Studio's ladder ("max", "ultra") are
        clamped to its ceiling; "off"/"on" (toggle-style models) are
        mapped to "none"/"medium". Anything outside the supported set
        is dropped (None) so the server's default effort applies rather
        than a 400.
        """
        effort = super()._resolved_reasoning_effort()
        if not effort:
            return None
        effort = _LM_EFFORT_ALIASES.get(effort, effort)
        effort = _LM_EFFORT_CLAMP.get(effort, effort)
        if effort not in _LM_VALID_EFFORTS:
            return None
        return effort

    def _ensure_loaded(self) -> None:
        """Skip API key check — LM Studio doesn't use keys."""
        if not self._loaded:
            self.load()
        if not self._api_key:
            self._api_key = "lmstudio"
