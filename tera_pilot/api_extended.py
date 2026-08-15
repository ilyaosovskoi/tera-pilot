"""
tera_pilot.api_extended — v2.2.1 extended REST API endpoints.

Adds ~70 new endpoints to :class:`tera_pilot.api_server.TeraPilotAPIHandler` so the
browser GUI can reach every backend capability that was previously only
exposed through the TUI bridge (``tera_pilot_tui.bridge.TeraPilotBridge``).

This module is loaded by :mod:`tera_pilot.api_server` at import time (via
:func:`install`) and patches ``do_GET`` / ``do_POST`` / ``do_DELETE`` so
the new ``/api/...`` routes are dispatched alongside the legacy ones.

Why a separate module?
----------------------
``tera_pilot/api_server.py`` is already 2200+ lines. Adding the new endpoints
inline would push it past 3500 lines and make the diff for v2.2.1
unreviewable. Keeping the extensions isolated also makes it trivial to
disable them (just don't call :func:`install`).

Endpoint groups
---------------
* ``/api/providers/custom/*``    — user-defined providers (add/list/remove/update)
* ``/api/providers/templates``    — built-in templates incl. Nvidia NIM
* ``/api/capabilities/*``        — capability catalog (browse/run templates)
* ``/api/second_opinion/*``      — cross-model second-opinion review
* ``/api/budget/*``              — token-budget knobs (caps / efficiency / etc.)
* ``/api/verify/*``              — cross-model verification of last response
* ``/api/agents/*`` + ``/api/audit/*`` — agent identity + audit trail
* ``/api/handoff/*``             — post-task editable handoff documents
* ``/api/cost/*``                — cost-aware provider routing
* ``/api/spend/*``               — team spend dashboard
* ``/api/hooks/*``               — pre/post-tool hook system
* ``/api/checkpoint/*``          — checkpoint / rewind system
* ``/api/github/*``              — GitHub PR/issue automation
* ``/api/consensus/*``           — multi-provider consensus engine
* ``/api/learnings/*``           — automatic learning loop
* ``/api/websearch/*``           — web-search tool status
* ``/api/persona/*``             — system-prompt persona editor
* ``/api/router/*``              — AutoRouter mode (auto/manual/...)
* ``/api/mcp_server/*``          — Tera Pilot-as-MCP-server status
* ``/api/notify/*``              — notification backends (Telegram/Discord/Slack)
* ``/api/daemon/*``              — background daemon task submission
* ``/api/pro/*``                 — pro toggle (gates Second Opinion / Consensus)
* ``/api/collaboration/*``       — swarm collaboration modes
* ``/api/usage`` / ``/api/compaction/*`` / ``/api/persistence/*``
* ``/api/slash_commands/*``      — TUI slash command catalog mirror
* ``/api/section/*``             — legacy section get/set (now always "general")

Design rules
------------
* Every handler returns either a dict (auto-JSON-encoded by ``_json``) or
  calls ``self._json(...)`` directly for streaming / file responses.
* All handlers catch their own exceptions and return ``{ok: False, error: ...}``
  so a single broken endpoint never crashes the whole server.
* Auth is enforced centrally by :meth:`TeraPilotAPIHandler._check_auth` — the
  patched ``do_POST`` keeps that check before dispatching here.
* The lazy ``AgentRuntime`` accessor on :class:`ServerContext` is reused —
  we never spin up a second runtime.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


# ── Route table ────────────────────────────────────────────────────────
_ROUTES_GET: Dict[str, Callable[..., Any]] = {}
_ROUTES_POST: Dict[str, Callable[..., Any]] = {}
_ROUTES_DELETE: Dict[str, Callable[..., Any]] = {}


def _get_route(path: str):
    def deco(fn):
        _ROUTES_GET[path] = fn
        return fn
    return deco


def _post_route(path: str):
    def deco(fn):
        _ROUTES_POST[path] = fn
        return fn
    return deco


def _delete_route(path: str):
    def deco(fn):
        _ROUTES_DELETE[path] = fn
        return fn
    return deco


# ── Helpers ────────────────────────────────────────────────────────────

_bridge_inst = None


def _bridge():
    """Return a shared ``tera_pilot_tui.bridge.TeraPilotBridge`` instance.

    The bridge owns an ``AgentRuntime`` exactly the way the TUI does, so
    every backend capability (capabilities, hooks, checkpoints, github,
    handoffs, cost router, spend dashboard, consensus, learnings,
    second_opinion, budget, verify, persona, router mode, mcp_server,
    notify, daemon) is reachable through the same code path the TUI uses.
    """
    global _bridge_inst
    if _bridge_inst is None:
        from tera_pilot_tui.bridge import TeraPilotBridge
        _bridge_inst = TeraPilotBridge(workspace=None, section="general")
    return _bridge_inst


def _ok(data: Any = None) -> Dict[str, Any]:
    if isinstance(data, dict) and "ok" in data:
        return data
    out: Dict[str, Any] = {"ok": True}
    if isinstance(data, dict):
        out.update(data)
    elif data is not None:
        out["data"] = data
    return out


def _err(msg: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "error": msg}
    out.update(extra)
    return out


def _ws() -> str:
    b = _bridge()
    return b.workspace or ""


# ════════════════════════════════════════════════════════════════════
# Custom providers — add / list / remove / update (incl. Nvidia NIM)
# ════════════════════════════════════════════════════════════════════
#
# These endpoints wrap tera_pilot.providers.custom_providers' YAML config so
# the GUI can manage user-defined providers (including Nvidia NIM and
# any other OpenAI-compatible endpoint) without ever touching a file.
#
# Schema (stored at ~/.tera_pilot/providers.yaml):
#   providers:
#     - provider_id: "my_nim"
#       type: "openai_compatible"
#       label: "My NIM"
#       default_model: "meta/llama-3.1-8b-instruct"
#       api_base: "https://integrate.api.nvidia.com/v1"
#       env_var: "MY_NIM_API_KEY"        # API key read from this env var
#       api_key:    "nvapi-..."          # OR hard-coded (NOT recommended)
#       context_window: 131072
#       capabilities: [chat, streaming, tool_calling, system_prompt, skills]

def _cp_load_all() -> list:
    from tera_pilot.providers.custom_providers import load_custom_providers_config
    return load_custom_providers_config() or []


def _cp_save_all(providers: list) -> None:
    from tera_pilot.providers.custom_providers import get_config_path, _yaml_dump
    cfg_path = get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(_yaml_dump({"providers": providers}))


def _cp_to_yaml_entry(body: dict) -> dict:
    """Translate a GUI payload into the YAML config schema."""
    pid = (body.get("provider_id") or "").strip()
    ptype = (body.get("provider_type") or "openai_compat").strip()
    entry = {
        "provider_id": pid,
        "type": "openai_compatible" if ptype in ("openai_compat", "nvidia_nim") else ptype,
        "label": body.get("name") or pid.title(),
        "default_model": body.get("model", ""),
        "api_base": body.get("base_url", ""),
        "env_var": body.get("env_var") or f"{pid.upper()}_API_KEY",
        "context_window": int(body.get("context_window", 16384) or 16384),
        "capabilities": body.get("capabilities") or [
            "chat", "streaming", "tool_calling", "system_prompt", "skills"
        ],
    }
    if body.get("api_key"):
        entry["api_key"] = body["api_key"]
    if body.get("description"):
        entry["description"] = body["description"]
    return entry


@_get_route("/api/providers/custom/list")
def _custom_providers_list(handler, body=None):
    try:
        providers = _cp_load_all()
        # Mask api_key in the response — never send secrets to the browser.
        for p in providers:
            if "api_key" in p and p["api_key"]:
                p["api_key_masked"] = p["api_key"][:4] + "…" + p["api_key"][-4:]
                p["api_key"] = ""
        return _ok({"providers": providers})
    except Exception as e:
        logger.warning("[api_ext] custom_providers_list failed: %s", e)
        return _err(str(e))


@_post_route("/api/providers/custom/add")
def _custom_providers_add(handler, body=None):
    """Add a user-defined provider.

    Body:
        provider_id:    str  — unique slug, e.g. "my-nim"
        name:           str  — display name
        base_url:       str  — OpenAI-compatible chat completions base URL
        api_key:        str  — secret key (stored in ~/.tera_pilot/providers.yaml)
        model:          str  — default model id
        provider_type:  str  — "openai_compat" (default) | "nvidia_nim"
        description:    str  — optional
        env_var:        str  — env var name for api_key (defaults to
                              "<PROVIDER_ID>_API_KEY")
        context_window: int  — max context (defaults to 16384)
        capabilities:   list — capability strings
    """
    body = body or {}
    pid = (body.get("provider_id") or "").strip()
    if not pid:
        return _err("provider_id is required")
    try:
        providers = _cp_load_all()
        if any(p.get("provider_id") == pid for p in providers):
            return _err(f"provider '{pid}' already exists")
        entry = _cp_to_yaml_entry(body)
        providers.append(entry)
        _cp_save_all(providers)
        # Hot-reload the registry so the new provider shows up immediately.
        try:
            from tera_pilot.providers import get_registry
            from tera_pilot.providers.custom_providers import register_custom_providers
            register_custom_providers(get_registry())
        except Exception as e:
            logger.warning("[api_ext] registry hot-reload failed: %s", e)
        return _ok({"provider_id": pid})
    except Exception as e:
        logger.warning("[api_ext] custom_providers_add failed: %s", e)
        return _err(str(e))


@_post_route("/api/providers/custom/update")
def _custom_providers_update(handler, body=None):
    body = body or {}
    pid = (body.get("provider_id") or "").strip()
    if not pid:
        return _err("provider_id is required")
    try:
        providers = _cp_load_all()
        found = False
        for i, p in enumerate(providers):
            if p.get("provider_id") == pid:
                # Preserve api_key if the caller didn't send a new one.
                existing_key = p.get("api_key", "")
                new_entry = _cp_to_yaml_entry(body)
                if not new_entry.get("api_key") and existing_key:
                    new_entry["api_key"] = existing_key
                providers[i] = new_entry
                found = True
                break
        if not found:
            return _err(f"provider '{pid}' not found")
        _cp_save_all(providers)
        try:
            from tera_pilot.providers import get_registry
            from tera_pilot.providers.custom_providers import register_custom_providers
            register_custom_providers(get_registry())
        except Exception:
            pass
        return _ok({"provider_id": pid})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/providers/custom/remove")
def _custom_providers_remove(handler, body=None):
    body = body or {}
    pid = (body.get("provider_id") or "").strip()
    if not pid:
        return _err("provider_id is required")
    try:
        providers = _cp_load_all()
        new_list = [p for p in providers if p.get("provider_id") != pid]
        if len(new_list) == len(providers):
            return _err(f"provider '{pid}' not found")
        _cp_save_all(new_list)
        return _ok({"provider_id": pid})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/providers/custom/test")
def _custom_providers_test(handler, body=None):
    """Test a custom-provider config WITHOUT saving it.

    Constructs an OpenAICompatProvider on-the-fly and sends a "ping"
    message — returns the response (truncated) on success.
    """
    body = body or {}
    try:
        from tera_pilot.providers.openai_compat import OpenAICompatProvider
        from tera_pilot.providers.base import ProviderConfig, ProviderCapability
        pid = body.get("provider_id") or "test"
        ptype = body.get("provider_type") or "openai_compat"

        # For nvidia_nim template, use the built-in class (it has the
        # correct api_base + env_var defaults).
        if ptype == "nvidia_nim":
            from tera_pilot.providers.nvidia_nim import NvidiaNIMProvider
            prov = NvidiaNIMProvider()
        else:
            # Build a one-shot OpenAICompatProvider subclass.
            capabilities = frozenset(
                ProviderCapability(c) for c in body.get("capabilities") or [
                    "chat", "streaming", "tool_calling", "system_prompt", "skills"
                ]
            )
            cls = type(
                f"Test{pid.title().replace('-', '')}Provider",
                (OpenAICompatProvider,),
                {
                    "provider_id": pid,
                    "label": body.get("name") or pid.title(),
                    "default_model": body.get("model", ""),
                    "api_base": body.get("base_url", ""),
                    "env_var": body.get("env_var") or f"{pid.upper()}_API_KEY",
                    "capabilities": capabilities,
                    "context_window": int(body.get("context_window", 16384) or 16384),
                },
            )
            prov = cls()

        # Apply the API key from the request body (don't rely on env).
        cfg = ProviderConfig(
            provider_id=pid,
            model=body.get("model", ""),
            api_key=body.get("api_key") or None,
            api_base=body.get("base_url") or None,
        )
        prov.configure(cfg)
        resp = prov.generate([{"role": "user", "content": "ping"}])
        content = getattr(resp, "content", None) or str(resp)
        return _ok({"response": content[:200]})
    except Exception as e:
        return _err(str(e))


@_get_route("/api/providers/templates")
def _provider_templates(handler, body=None):
    """Return built-in provider templates the user can clone & customize.

    v2.2.3: Expanded from 4 to 22 templates — covers every major cloud,
    every notable open-model host, every local runner, and the enterprise
    clouds. Each template pre-fills the wizard with sensible defaults so
    the user only needs to paste an API key.
    """
    templates = [
        # ── Local (no key) ──────────────────────────────────────────
        {
            "id": "ollama_local",
            "name": "Ollama (local)",
            "provider_type": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model": "llama3.1",
            "description": (
                "Run models locally via Ollama. No API key required when "
                "Ollama is on the same machine."
            ),
            "docs_url": "https://ollama.com/library",
        },
        {
            "id": "lmstudio_local",
            "name": "LM Studio (local)",
            "provider_type": "openai_compat",
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "local-model",
            "description": (
                "Run models locally via LM Studio's OpenAI-compatible server."
            ),
            "docs_url": "https://lmstudio.ai/docs/local-server",
        },
        {
            "id": "vllm_local",
            "name": "vLLM (local)",
            "provider_type": "openai_compat",
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
            "description": (
                "Self-hosted OpenAI-compatible server with PagedAttention. "
                "Start vLLM with --api-key <key> if you want auth."
            ),
            "docs_url": "https://docs.vllm.ai/en/stable/serving/openai_compatible_server.html",
        },
        {
            "id": "koboldcpp_local",
            "name": "KoboldCpp (local)",
            "provider_type": "openai_compat",
            "base_url": "http://127.0.0.1:5001/v1",
            "model": "local-model",
            "description": (
                "Local GGUF server with an OpenAI-compatible API. Start "
                "KoboldCpp and load any model."
            ),
            "docs_url": "https://github.com/LostRuins/koboldcpp",
        },
        {
            "id": "llamafile_local",
            "name": "llamafile (local)",
            "provider_type": "openai_compat",
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "local-model",
            "description": (
                "Mozilla's single-file LLM runner. Start with --server and "
                "load any .llamafile."
            ),
            "docs_url": "https://github.com/Mozilla-Ocho/llamafile",
        },
        # ── Major cloud providers ───────────────────────────────────
        {
            "id": "openai",
            "name": "OpenAI",
            "provider_type": "openai_compat",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
            "env_var": "OPENAI_API_KEY",
            "description": "Official OpenAI API. GPT-4o, GPT-4 Turbo, GPT-3.5 Turbo, o1.",
            "docs_url": "https://platform.openai.com/api-keys",
        },
        {
            "id": "anthropic",
            "name": "Anthropic",
            "provider_type": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "model": "claude-3-5-sonnet-20241022",
            "env_var": "ANTHROPIC_API_KEY",
            "description": "Claude 3.5 Sonnet, Haiku, Opus. Best for coding and long-context tasks.",
            "docs_url": "https://console.anthropic.com/settings/keys",
        },
        {
            "id": "google_gemini",
            "name": "Google Gemini (AI Studio)",
            "provider_type": "openai_compat",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-1.5-pro",
            "env_var": "GEMINI_API_KEY",
            "description": "Gemini 1.5 Pro / Flash via the OpenAI-compatible endpoint. Free tier.",
            "docs_url": "https://aistudio.google.com/apikey",
        },
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "provider_type": "openai_compat",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "env_var": "DEEPSEEK_API_KEY",
            "description": "Low-cost, strong coding performance. DeepSeek-V3, DeepSeek-Coder.",
            "docs_url": "https://platform.deepseek.com/api_keys",
        },
        {
            "id": "zai",
            "name": "Z.ai (GLM)",
            "provider_type": "openai_compat",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4",
            "env_var": "ZAI_API_KEY",
            "description": "GLM-4, GLM-4-Flash (free), GLM-4V from Z.ai.",
            "docs_url": "https://open.bigmodel.cn/usercenter/apikeys",
        },
        {
            "id": "mistral",
            "name": "Mistral AI",
            "provider_type": "openai_compat",
            "base_url": "https://api.mistral.ai/v1",
            "model": "mistral-large-latest",
            "env_var": "MISTRAL_API_KEY",
            "description": "European provider. Mistral Large, Medium, Small, Codestral, Pixtral.",
            "docs_url": "https://console.mistral.ai/api-keys",
        },
        {
            "id": "xai",
            "name": "xAI (Grok)",
            "provider_type": "openai_compat",
            "base_url": "https://api.x.ai/v1",
            "model": "grok-2-latest",
            "env_var": "XAI_API_KEY",
            "description": "Grok models from xAI.",
            "docs_url": "https://console.x.ai",
        },
        {
            "id": "cohere",
            "name": "Cohere",
            "provider_type": "openai_compat",
            "base_url": "https://api.cohere.ai/v1",
            "model": "command-r-plus-08-2024",
            "env_var": "COHERE_API_KEY",
            "description": "Command R+ — strong on RAG and multilingual.",
            "docs_url": "https://dashboard.cohere.com/api-keys",
        },
        {
            "id": "perplexity",
            "name": "Perplexity",
            "provider_type": "openai_compat",
            "base_url": "https://api.perplexity.ai",
            "model": "llama-3.1-sonar-large-128k-online",
            "env_var": "PPLX_API_KEY",
            "description": "Online models with built-in web search.",
            "docs_url": "https://www.perplexity.ai/settings/api",
        },
        {
            "id": "ai21",
            "name": "AI21 Labs (Jamba)",
            "provider_type": "openai_compat",
            "base_url": "https://api.ai21.com/studio/v1",
            "model": "jamba-1.5-large",
            "env_var": "AI21_API_KEY",
            "description": "SSM-Transformer hybrid, very long context.",
            "docs_url": "https://studio.ai21.com/account/api-key",
        },
        # ── Fast inference (open models hosted) ─────────────────────
        {
            "id": "groq",
            "name": "Groq",
            "provider_type": "openai_compat",
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile",
            "env_var": "GROQ_API_KEY",
            "description": "Generous free tier, extremely fast responses.",
            "docs_url": "https://console.groq.com/keys",
        },
        {
            "id": "cerebras",
            "name": "Cerebras",
            "provider_type": "openai_compat",
            "base_url": "https://api.cerebras.ai/v1",
            "model": "llama-3.3-70b",
            "env_var": "CEREBRAS_API_KEY",
            "description": "Fastest inference speeds available anywhere. Free tier.",
            "docs_url": "https://cloud.cerebras.ai",
        },
        {
            "id": "sambanova",
            "name": "SambaNova",
            "provider_type": "openai_compat",
            "base_url": "https://api.sambanova.ai/v1",
            "model": "Meta-Llama-3.1-70B-Instruct",
            "env_var": "SAMBANOVA_API_KEY",
            "description": "Free tier with no credit card required.",
            "docs_url": "https://cloud.sambanova.ai/apis",
        },
        # ── Open-model hosting / aggregators ────────────────────────
        {
            "id": "openrouter",
            "name": "OpenRouter",
            "provider_type": "openai_compat",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "anthropic/claude-3.5-sonnet",
            "env_var": "OPENROUTER_API_KEY",
            "description": "One key, access to almost every model from every provider.",
            "docs_url": "https://openrouter.ai/keys",
        },
        {
            "id": "together",
            "name": "Together AI",
            "provider_type": "openai_compat",
            "base_url": "https://api.together.xyz/v1",
            "model": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "env_var": "TOGETHER_API_KEY",
            "description": "Wide catalog of open-source models.",
            "docs_url": "https://api.together.ai/settings/api-keys",
        },
        {
            "id": "fireworks",
            "name": "Fireworks AI",
            "provider_type": "openai_compat",
            "base_url": "https://api.fireworks.ai/inference/v1",
            "model": "accounts/fireworks/models/llama-v3p1-70b-instruct",
            "env_var": "FIREWORKS_API_KEY",
            "description": "Fast hosted inference for open models.",
            "docs_url": "https://app.fireworks.ai/settings/users/api-keys",
        },
        {
            "id": "novita",
            "name": "Novita AI",
            "provider_type": "openai_compat",
            "base_url": "https://api.novita.ai/v3/openai",
            "model": "meta-llama/llama-3.1-70b-instruct",
            "env_var": "NOVITA_API_KEY",
            "description": "Cheap hosted open models. Free trial credits.",
            "docs_url": "https://novita.ai/get-key",
        },
        {
            "id": "hyperbolic",
            "name": "Hyperbolic",
            "provider_type": "openai_compat",
            "base_url": "https://api.hyperbolic.xyz/v1",
            "model": "meta-llama/Meta-Llama-3.1-70B-Instruct",
            "env_var": "HYPERBOLIC_API_KEY",
            "description": "Low-cost GPU inference for open models.",
            "docs_url": "https://app.hyperbolic.xyz/settings",
        },
        {
            "id": "lepton",
            "name": "Lepton AI",
            "provider_type": "openai_compat",
            "base_url": "https://api.lepton.ai/api/v1",
            "model": "llama3-8b",
            "env_var": "LEPTON_API_KEY",
            "description": "Serverless open-model inference.",
            "docs_url": "https://dashboard.lepton.ai/tokens",
        },
        {
            "id": "siliconflow",
            "name": "SiliconFlow",
            "provider_type": "openai_compat",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "Qwen/Qwen2.5-72B-Instruct",
            "env_var": "SILICONFLOW_API_KEY",
            "description": "Chinese aggregator with many open models. Free tier.",
            "docs_url": "https://cloud.siliconflow.cn/account/ak",
        },
        {
            "id": "friendli",
            "name": "Friendli AI",
            "provider_type": "openai_compat",
            "base_url": "https://api.friendli.ai/api/v1",
            "model": "meta-llama-3.1-8b-instruct",
            "env_var": "FRIENDLI_API_KEY",
            "description": "High-throughput inference engine for open models.",
            "docs_url": "https://friendli.ai/webapp-api-keys",
        },
        # ── ML platforms / model hubs ───────────────────────────────
        {
            "id": "huggingface",
            "name": "Hugging Face",
            "provider_type": "openai_compat",
            "base_url": "https://api-inference.huggingface.co/v1",
            "model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
            "env_var": "HF_TOKEN",
            "description": "Serverless inference API for thousands of models. Free tier.",
            "docs_url": "https://huggingface.co/docs/api-inference",
        },
        {
            "id": "replicate",
            "name": "Replicate",
            "provider_type": "openai_compat",
            "base_url": "https://api.replicate.com/v1",
            "model": "meta/llama-3.1-8b-instruct",
            "env_var": "REPLICATE_API_TOKEN",
            "description": "Per-second billing for open models. Free trial credit.",
            "docs_url": "https://replicate.com/account/api-tokens",
        },
        # ── Enterprise cloud ────────────────────────────────────────
        {
            "id": "azure_openai",
            "name": "Azure OpenAI",
            "provider_type": "openai_compat",
            "base_url": "https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT",
            "model": "gpt-4o",
            "env_var": "AZURE_OPENAI_API_KEY",
            "description": "Your own Azure deployment of OpenAI models. Replace YOUR-RESOURCE and YOUR-DEPLOYMENT in the URL.",
            "docs_url": "https://learn.microsoft.com/azure/ai-services/openai",
        },
        # ── Nvidia NIM ──────────────────────────────────────────────
        {
            "id": "nvidia_nim",
            "name": "Nvidia NIM",
            "provider_type": "nvidia_nim",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model": "meta/llama-3.1-8b-instruct",
            "env_var": "NVIDIA_API_KEY",
            "description": (
                "Nvidia NIM (No Infrastructure Models) — run Nvidia-hosted "
                "Llama, Mistral, and Nemotron models via an OpenAI-compatible "
                "endpoint. Get a free API key at https://build.nvidia.com/."
            ),
            "docs_url": "https://docs.nvidia.com/nim/",
        },
        # ── Generic fallback ────────────────────────────────────────
        {
            "id": "openai_compat",
            "name": "OpenAI-compatible (generic)",
            "provider_type": "openai_compat",
            "base_url": "https://api.example.com/v1",
            "model": "gpt-3.5-turbo",
            "description": (
                "Any endpoint that exposes POST /v1/chat/completions with "
                "the OpenAI request/response schema."
            ),
        },
    ]
    return _ok({"templates": templates})


# ════════════════════════════════════════════════════════════════════
# Capability catalog
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/capabilities/list")
def _caps_list(handler, body=None):
    cat = handler._query("category") if handler else None
    try:
        return _ok({"capabilities": _bridge().list_capabilities(category=cat)})
    except Exception as e:
        return _err(str(e))


@_get_route("/api/capabilities/categories")
def _caps_categories(handler, body=None):
    try:
        return _ok({"categories": _bridge().list_capability_categories()})
    except Exception as e:
        return _err(str(e))


@_get_route("/api/capabilities/get")
def _caps_get(handler, body=None):
    cap_id = handler._query("id") if handler else ""
    if not cap_id:
        return _err("id is required")
    try:
        cap = _bridge().get_capability(cap_id)
        return _ok({"capability": cap})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/capabilities/run")
def _caps_run(handler, body=None):
    body = body or {}
    cap_id = body.get("id")
    if not cap_id:
        return _err("id is required")
    try:
        result = _bridge().fill_capability_template(
            cap_id, body.get("variables") or {}
        )
        return _ok(result)
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Second Opinion
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/second_opinion/config")
def _so_config_get(handler, body=None):
    try:
        return _ok(_bridge().get_second_opinion_config())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/second_opinion/config")
def _so_config_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_second_opinion_config(**body))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/second_opinion/run")
def _so_run(handler, body=None):
    body = body or {}
    try:
        result = _bridge().run_second_opinion(
            prompt=body.get("prompt", ""),
            response=body.get("response", ""),
            provider_id=body.get("provider_id"),
            model=body.get("model"),
        )
        return _ok(result)
    except Exception as e:
        return _err(str(e))


@_get_route("/api/second_opinion/providers")
def _so_providers(handler, body=None):
    try:
        return _ok({"providers": _bridge().list_second_opinion_providers()})
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Token Budget
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/budget/get")
def _budget_get(handler, body=None):
    try:
        return _ok(_bridge().get_token_budget())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/budget/set")
def _budget_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_token_budget(**body))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/budget/reset")
def _budget_reset(handler, body=None):
    try:
        return _ok(_bridge().reset_token_budget())
    except Exception as e:
        return _err(str(e))


@_get_route("/api/budget/check")
def _budget_check(handler, body=None):
    try:
        return _ok(_bridge().check_budget())
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Cross-model verification
# ════════════════════════════════════════════════════════════════════

@_post_route("/api/verify/run")
def _verify_run(handler, body=None):
    body = body or {}
    try:
        result = _bridge().verify_last_response(
            verifier_provider=body.get("verifier_provider"),
            verifier_model=body.get("verifier_model"),
            user_request=body.get("user_request", ""),
            agent_response=body.get("agent_response", ""),
        )
        return _ok(result)
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Agents / Audit
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/agents/identity")
def _agent_identity(handler, body=None):
    try:
        return _ok(_bridge().get_agent_identity())
    except Exception as e:
        return _err(str(e))


@_get_route("/api/agents/list")
def _agents_list(handler, body=None):
    try:
        return _ok({"agents": _bridge().list_agents()})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/agents/spawn")
def _agents_spawn(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().spawn_subidentity(
            role=body.get("role", "researcher"),
            name=body.get("name", ""),
        ))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/audit/summary")
def _audit_summary(handler, body=None):
    agent_id = handler._query("agent_id") if handler else None
    try:
        return _ok(_bridge().get_agent_audit_summary(agent_id=agent_id))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/audit/filter")
def _audit_filter(handler, body=None):
    agent_id = handler._query("agent_id") if handler else None
    try:
        return _ok(_bridge().filter_audit_by_agent(agent_id=agent_id))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/audit/export_json")
def _audit_export_json(handler, body=None):
    try:
        return _ok(_bridge().export_audit_json(with_fingerprints=True))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/audit/export_csv")
def _audit_export_csv(handler, body=None):
    try:
        return _ok(_bridge().export_audit_csv())
    except Exception as e:
        return _err(str(e))


@_get_route("/api/audit/signed_export")
def _audit_signed_export(handler, body=None):
    try:
        return _ok(_bridge().export_audit_signed_json())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/audit/signed_verify")
def _audit_signed_verify(handler, body=None):
    body = body or {}
    path = body.get("path", "")
    if not path:
        return _err("path is required")
    try:
        return _ok(_bridge().verify_audit_signed_file(path))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Handoff documents
# ════════════════════════════════════════════════════════════════════

@_post_route("/api/handoff/create")
def _handoff_create(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().create_handoff(
            agent_output=body.get("agent_output", ""),
            title=body.get("title", ""),
            source_section=body.get("source_section", "general"),
        ))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/handoff/list")
def _handoff_list(handler, body=None):
    try:
        return _ok({"handoffs": _bridge().list_handoffs(limit=50)})
    except Exception as e:
        return _err(str(e))


@_get_route("/api/handoff/get")
def _handoff_get(handler, body=None):
    doc_id = handler._query("id") if handler else ""
    if not doc_id:
        return _err("id is required")
    try:
        return _ok({"handoff": _bridge().get_handoff(doc_id)})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/handoff/block_status")
def _handoff_block_status(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_handoff_block_status(
            doc_id=body.get("doc_id", ""),
            block_id=body.get("block_id", ""),
            status=body.get("status", "pending"),
            edited_content=body.get("edited_content"),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/handoff/todo_toggle")
def _handoff_todo_toggle(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().toggle_handoff_todo(
            doc_id=body.get("doc_id", ""),
            block_id=body.get("block_id", ""),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/handoff/reorder")
def _handoff_reorder(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().reorder_handoff_blocks(
            doc_id=body.get("doc_id", ""),
            new_order=body.get("new_order", []),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/handoff/delete")
def _handoff_delete(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().delete_handoff(body.get("doc_id", "")))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/handoff/revision_prompt")
def _handoff_revision_prompt(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().build_handoff_revision_prompt(body.get("doc_id", "")))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/handoff/export_md")
def _handoff_export_md(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().export_handoff_markdown(body.get("doc_id", "")))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Cost Router
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/cost/config")
def _cost_config_get(handler, body=None):
    try:
        return _ok(_bridge().get_cost_router_config())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/cost/config")
def _cost_config_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_cost_router_config(**body))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/cost/cap")
def _cost_cap_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_cost_cap(
            complexity=body.get("complexity", "medium"),
            usd=float(body.get("usd", 0.0)),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/cost/route")
def _cost_route(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().cost_route(
            prompt=body.get("prompt", ""),
            complexity=body.get("complexity", "medium"),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/cost/apply")
def _cost_apply(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().apply_cost_route_decision(decision=body.get("decision", {})))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Spend Dashboard
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/spend/identity")
def _spend_identity_get(handler, body=None):
    try:
        return _ok(_bridge().get_user_identity())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/spend/team")
def _spend_team_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_user_team(body.get("team", "")))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/spend/budget")
def _spend_budget_get(handler, body=None):
    team = handler._query("team") if handler else None
    try:
        return _ok(_bridge().get_team_budget(team=team))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/spend/budget")
def _spend_budget_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_team_budget(
            monthly_usd=float(body.get("monthly_usd", 0.0)),
            alert_pct=float(body.get("alert_pct", 80.0)),
        ))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/spend/report")
def _spend_report(handler, body=None):
    try:
        days = int(handler._query("days") or "30") if handler else 30
    except (ValueError, TypeError):
        days = 30
    try:
        return _ok(_bridge().get_team_spend_report(days=days))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/spend/sources_add")
def _spend_sources_add(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().add_spend_source(body.get("path", "")))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/spend/sources")
def _spend_sources_list(handler, body=None):
    try:
        return _ok({"sources": _bridge().list_spend_sources()})
    except Exception as e:
        return _err(str(e))


@_get_route("/api/spend/export_json")
def _spend_export_json(handler, body=None):
    try:
        days = int(handler._query("days") or "30") if handler else 30
    except (ValueError, TypeError):
        days = 30
    try:
        return _ok(_bridge().export_spend_report_json(days=days))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/spend/export_csv")
def _spend_export_csv(handler, body=None):
    try:
        days = int(handler._query("days") or "30") if handler else 30
    except (ValueError, TypeError):
        days = 30
    try:
        return _ok(_bridge().export_spend_report_csv(days=days))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Hooks
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/hooks/list")
def _hooks_list(handler, body=None):
    hook_type = handler._query("type") if handler else None
    try:
        return _ok({"hooks": _bridge().list_hooks(hook_type=hook_type)})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/hooks/register")
def _hooks_register(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().register_hook(
            hook_type=body.get("hook_type", "pre_tool_use"),
            name=body.get("name", ""),
            code=body.get("code", ""),
            priority=int(body.get("priority", 100)),
            enabled=bool(body.get("enabled", True)),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/hooks/remove")
def _hooks_remove(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().remove_hook(body.get("hook_id", "")))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/hooks/toggle")
def _hooks_toggle(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_hook_enabled(
            hook_id=body.get("hook_id", ""),
            enabled=bool(body.get("enabled", True)),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/hooks/test")
def _hooks_test(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().test_hook(
            hook_id=body.get("hook_id", ""),
            event_type=body.get("event_type", "pre_tool_use"),
            **body.get("kwargs", {}),
        ))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/hooks/stats")
def _hooks_stats(handler, body=None):
    try:
        return _ok(_bridge().get_hook_stats())
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Checkpoints / Rewind
# ════════════════════════════════════════════════════════════════════

@_post_route("/api/checkpoint/create")
def _cp_create(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().create_checkpoint(label=body.get("label", "")))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/checkpoint/list")
def _cp_list(handler, body=None):
    try:
        limit = int(handler._query("limit") or "50") if handler else 50
    except (ValueError, TypeError):
        limit = 50
    try:
        return _ok({"checkpoints": _bridge().list_checkpoints(limit=limit)})
    except Exception as e:
        return _err(str(e))


@_get_route("/api/checkpoint/get")
def _cp_get(handler, body=None):
    cp_id = handler._query("id") if handler else ""
    if not cp_id:
        return _err("id is required")
    try:
        return _ok({"checkpoint": _bridge().get_checkpoint(cp_id)})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/checkpoint/rewind")
def _cp_rewind(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().rewind_checkpoint(n=int(body.get("n", 1))))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/checkpoint/rewind_to")
def _cp_rewind_to(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().rewind_to_checkpoint(body.get("checkpoint_id", "")))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/checkpoint/diff")
def _cp_diff(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().diff_checkpoints(
            from_id=body.get("from_id", ""),
            to_id=body.get("to_id", ""),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/checkpoint/auto")
def _cp_auto(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_auto_checkpoint(bool(body.get("enabled", True))))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/checkpoint/stats")
def _cp_stats(handler, body=None):
    try:
        return _ok(_bridge().get_checkpoint_stats())
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# GitHub automation
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/github/status")
def _gh_status(handler, body=None):
    try:
        return _ok(_bridge().github_status())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/github/set_token")
def _gh_set_token(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().github_set_token(body.get("token", "")))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/github/set_repo")
def _gh_set_repo(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().github_set_repo(
            owner=body.get("owner", ""),
            repo=body.get("repo", ""),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/github/detect_repo")
def _gh_detect_repo(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().github_auto_detect_repo(
            workspace=body.get("workspace") or _ws(),
        ))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/github/list_prs")
def _gh_list_prs(handler, body=None):
    try:
        state = handler._query("state") or "open"
        limit = int(handler._query("limit") or "10")
    except (ValueError, TypeError):
        state, limit = "open", 10
    try:
        return _ok(_bridge().github_list_prs(state=state, limit=limit))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/github/get_pr")
def _gh_get_pr(handler, body=None):
    try:
        number = int(handler._query("number") or "0")
    except (ValueError, TypeError):
        return _err("number is required")
    try:
        return _ok(_bridge().github_get_pr(number))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/github/create_pr")
def _gh_create_pr(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().github_create_pr(
            title=body.get("title", ""),
            body=body.get("body", ""),
            head=body.get("head", ""),
            base=body.get("base", "main"),
        ))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/github/pr_context")
def _gh_pr_context(handler, body=None):
    try:
        number = int(handler._query("number") or "0")
    except (ValueError, TypeError):
        return _err("number is required")
    try:
        return _ok(_bridge().github_get_pr_context(number))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/github/list_issues")
def _gh_list_issues(handler, body=None):
    try:
        state = handler._query("state") or "open"
        limit = int(handler._query("limit") or "10")
        labels = handler._query("labels") or ""
    except (ValueError, TypeError):
        state, limit, labels = "open", 10, ""
    try:
        return _ok(_bridge().github_list_issues(
            state=state, limit=limit, labels=labels,
        ))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/github/get_issue")
def _gh_get_issue(handler, body=None):
    try:
        number = int(handler._query("number") or "0")
    except (ValueError, TypeError):
        return _err("number is required")
    try:
        return _ok(_bridge().github_get_issue(number))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/github/create_issue")
def _gh_create_issue(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().github_create_issue(
            title=body.get("title", ""),
            body=body.get("body", ""),
            labels=body.get("labels"),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/github/comment_pr")
def _gh_comment_pr(handler, body=None):
    body = body or {}
    try:
        number = int(body.get("number", 0))
    except (ValueError, TypeError):
        return _err("number is required")
    try:
        return _ok(_bridge().github_comment_on_pr(
            number=number, body=body.get("body", ""),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/github/generate_action")
def _gh_gen_action(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().github_generate_action(
            trigger=body.get("trigger", "pull_request"),
        ))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Consensus engine
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/consensus/config")
def _consensus_config_get(handler, body=None):
    try:
        return _ok(_bridge().get_consensus_config())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/consensus/config")
def _consensus_config_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_consensus_config(**body))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/consensus/run")
def _consensus_run(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().run_consensus(body.get("prompt", "")))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Learnings
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/learnings/list")
def _learnings_list(handler, body=None):
    try:
        return _ok(_bridge().handle_learnings_command(_ws(), "list"))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/learnings/dismissed")
def _learnings_dismissed(handler, body=None):
    try:
        return _ok(_bridge().handle_learnings_command(_ws(), "dismissed"))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/learnings/show")
def _learnings_show(handler, body=None):
    name = handler._query("name") if handler else ""
    if not name:
        return _err("name is required")
    try:
        return _ok(_bridge().handle_learnings_command(_ws(), f"show {name}"))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/learnings/dismiss")
def _learnings_dismiss(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().handle_learnings_command(
            _ws(), f"dismiss {body.get('name', '')}",
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/learnings/restore")
def _learnings_restore(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().handle_learnings_command(
            _ws(), f"restore {body.get('name', '')}",
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/learnings/scan")
def _learnings_scan(handler, body=None):
    try:
        return _ok(_bridge().handle_learnings_command(_ws(), "scan"))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Web search status
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/websearch/status")
def _websearch_status(handler, body=None):
    try:
        return _ok(_bridge().get_websearch_status())
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Persona (system prompt editor)
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/persona/get")
def _persona_get(handler, body=None):
    try:
        return _ok(_bridge().get_persona())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/persona/set")
def _persona_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_persona(body.get("content", "")))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/persona/reset")
def _persona_reset(handler, body=None):
    try:
        return _ok(_bridge().reset_persona())
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Router mode (auto / manual)
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/router/mode")
def _router_mode_get(handler, body=None):
    try:
        return _ok(_bridge().get_router_mode())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/router/mode")
def _router_mode_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_router_mode(body.get("mode", "auto")))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Launch TUI from Web UI
# ════════════════════════════════════════════════════════════════════

@_post_route("/api/launch_tui")
def _launch_tui(handler, body=None):
    try:
        return _ok(_bridge().launch_tui())
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Open Project (native file picker)
# ════════════════════════════════════════════════════════════════════

@_post_route("/api/open_project")
def _open_project(handler, body=None):
    """Open a native file picker dialog to select a project directory.
    This is called from the Web UI when the user clicks 'Open project'.
    Returns the selected path.
    """
    try:
        # The actual file picker is handled client-side via the browser's
        # showDirectoryPicker API. This endpoint just changes the workspace.
        # The body should contain the selected path.
        body = body or {}
        path = body.get("path", "")
        if not path:
            return _err("No path provided")
        return _ok(_bridge().change_workspace(path))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# MCP server mode (Tera Pilot-as-MCP-server)
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/mcp_server/list_tools")
def _mcp_server_tools(handler, body=None):
    try:
        return _ok(_bridge().mcp_server_list_tools())
    except Exception as e:
        return _err(str(e))


@_get_route("/api/mcp_server/status")
def _mcp_server_status(handler, body=None):
    try:
        return _ok(_bridge().mcp_server_status())
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Native File Picker (server-side with tkinter)
# ════════════════════════════════════════════════════════════════════

@_post_route("/api/native_file_picker")
def _native_file_picker(handler, body=None):
    """Open a native file picker dialog on the server to select a project directory.
    Uses tkinter which is available on most Python installations.
    Returns the selected directory path.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        # Create a hidden root window
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)  # Bring dialog to front

        # Open directory picker
        path = filedialog.askdirectory(
            title="Select Project Directory",
            initialdir=str(Path.home() / "Documents")
        )

        root.destroy()

        if not path:
            return _err("No directory selected")

        return _ok({"path": path})
    except ImportError:
        return _err("tkinter not available on this system")
    except Exception as e:
        return _err(f"File picker failed: {str(e)}")


# ════════════════════════════════════════════════════════════════════
# Notifications (Telegram / Discord / Slack)
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/notify/backends")
def _notify_backends(handler, body=None):
    try:
        return _ok({"backends": _bridge().notify_list_backends()})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/notify/configure")
def _notify_configure(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().notify_configure_backend(
            name=body.get("name", ""),
            config=body.get("config", {}),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/notify/toggle")
def _notify_toggle(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().notify_set_enabled(
            name=body.get("name", ""),
            enabled=bool(body.get("enabled", True)),
        ))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/notify/test")
def _notify_test(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().notify_test(body.get("name", "")))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/notify/test_all")
def _notify_test_all(handler, body=None):
    try:
        return _ok(_bridge().notify_test_all())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/notify/set_events")
def _notify_set_events(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().notify_set_events(
            name=body.get("name", ""),
            events=body.get("events", []),
        ))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/notify/status")
def _notify_status(handler, body=None):
    try:
        return _ok(_bridge().notify_status())
    except Exception as e:
        return _err(str(e))


@_post_route("/api/notify/remove")
def _notify_remove(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().notify_remove_backend(body.get("name", "")))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Daemon (background task queue)
# ════════════════════════════════════════════════════════════════════

@_post_route("/api/daemon/submit")
def _daemon_submit(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().daemon_submit_task(
            prompt=body.get("prompt", ""),
            workspace=body.get("workspace") or _ws(),
        ))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/daemon/status")
def _daemon_status(handler, body=None):
    try:
        return _ok(_bridge().daemon_status())
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Pro toggle (gates Second Opinion / Consensus)
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/pro/status")
def _pro_status(handler, body=None):
    try:
        return _ok({"pro_enabled": _bridge().is_pro_enabled()})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/pro/toggle")
def _pro_toggle(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_pro_enabled(bool(body.get("enabled", False))))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Collaboration modes (Swarm)
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/collaboration/modes")
def _collab_modes(handler, body=None):
    try:
        return _ok({"modes": _bridge().list_collaboration_modes()})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/collaboration/run")
def _collab_run(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().run_collaboration(
            mode=body.get("mode", ""),
            task=body.get("task", ""),
        ))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Persistence / Compaction / Usage stats
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/usage/get")
def _usage_get(handler, body=None):
    try:
        return _ok(_bridge().get_usage())
    except Exception as e:
        return _err(str(e))


@_get_route("/api/compaction/stats")
def _compaction_stats(handler, body=None):
    try:
        return _ok(_bridge().get_compaction_stats() or {})
    except Exception as e:
        return _err(str(e))


@_get_route("/api/persistence/backend")
def _persistence_backend_get(handler, body=None):
    try:
        return _ok({"backend": _bridge().get_persistence_backend()})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/persistence/backend")
def _persistence_backend_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_persistence_backend(body.get("backend", "json")))
    except Exception as e:
        return _err(str(e))


@_get_route("/api/persistence/sessions")
def _persistence_sessions(handler, body=None):
    try:
        return _ok({"sessions": _bridge().list_sqlite_sessions()})
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Slash command catalog (mirror of TUI /<cmd>)
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/slash_commands/list")
def _slash_list(handler, body=None):
    try:
        return _ok({"commands": _bridge().list_slash_commands()})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/slash_commands/resolve")
def _slash_resolve(handler, body=None):
    body = body or {}
    try:
        return _ok({"resolved": _bridge().resolve_slash_command(body.get("text", ""))})
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Section switch (legacy compat — section is now "general" always)
# ════════════════════════════════════════════════════════════════════

@_get_route("/api/section/get")
def _section_get(handler, body=None):
    return _ok({"section": "general"})


@_post_route("/api/section/set")
def _section_set(handler, body=None):
    body = body or {}
    try:
        return _ok(_bridge().set_section(body.get("section", "general")))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Context management — /context, /clear, /compact, /reload, /pin, /unpin
# (mirrors the TUI's native slash commands over HTTP)
# ════════════════════════════════════════════════════════════════════


def _runtime(handler):
    """Return the API server's SHARED AgentRuntime — the same instance that
    serves /api/agent/stream — so context operations here affect the exact
    conversation the GUI is streaming (not a second, orphaned runtime)."""
    if handler is None:
        return None
    ws = (handler.ctx.config.get("project_root") or "") or None
    return handler.ctx.get_agent_runtime(ws)


def _workspace_root(handler) -> "Path | None":
    ws = (handler.ctx.config.get("project_root") or "") if handler else ""
    return Path(ws).resolve() if ws else None


@_get_route("/api/context/status")
def _context_status(handler, body=None):
    try:
        rt = _runtime(handler)
        if rt is None:
            return _err("backend not ready")
        return _ok(rt.context_status())
    except Exception as e:
        logger.warning("[api_ext] context/status failed: %s", e)
        return _err(str(e))


@_post_route("/api/context/clear")
def _context_clear(handler, body=None):
    try:
        rt = _runtime(handler)
        if rt is None:
            return _err("backend not ready")
        return rt.clear_context()
    except Exception as e:
        return _err(str(e))


@_post_route("/api/context/compact")
def _context_compact(handler, body=None):
    try:
        rt = _runtime(handler)
        if rt is None:
            return _err("backend not ready")
        return rt.compact_context()
    except Exception as e:
        return _err(str(e))


@_post_route("/api/context/reload")
def _context_reload(handler, body=None):
    try:
        rt = _runtime(handler)
        if rt is None:
            return _err("backend not ready")
        pc = rt._project_context
        pc.reload()
        pc.instructions()  # force re-read from disk
        status = pc.status() or {}
        return _ok({
            "sources": status.get("sources", []),
            "total_chars": status.get("total_chars", 0),
        })
    except Exception as e:
        return _err(str(e))


@_get_route("/api/context/pin")
def _context_pin(handler, body=None):
    path = (handler._query("path") or "").strip() if handler else ""
    if not path:
        return _err("path is required")
    try:
        rt = _runtime(handler)
        if rt is None:
            return _err("backend not ready")
        rt._context_manager.pin_file(path)
        return _ok({"path": path})
    except Exception as e:
        return _err(str(e))


@_get_route("/api/context/unpin")
def _context_unpin(handler, body=None):
    path = (handler._query("path") or "").strip() if handler else ""
    if not path:
        return _err("path is required")
    try:
        rt = _runtime(handler)
        if rt is None:
            return _err("backend not ready")
        rt._context_manager.unpin_file(path)
        return _ok({"path": path})
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Project memory file editor — TERA_PILOT.md primary, CLAUDE.md fallback
# ════════════════════════════════════════════════════════════════════

_MEMORY_FILE_NAMES = (
    "TERA_PILOT.md", "tera_pilot.md", ".tera_pilot.md",
    "CLAUDE.md", "claude.md", ".claude.md",
)


def _memory_file_target(handler, preferred: str = "") -> Path:
    """Resolve the memory file to read/write.

    Priority: an existing project file (any accepted name), then a fresh
    project ``TERA_PILOT.md``, then the global ``~/.tera_pilot/TERA_PILOT.md``.
    A client-supplied ``preferred`` path is honoured only when it resolves
    inside the workspace (or inside the global memory dir) — so a
    compromised/buggy client can never redirect the write elsewhere.
    """
    base = _workspace_root(handler)
    candidates: list = []
    if base is not None:
        candidates = [base / n for n in _MEMORY_FILE_NAMES]
        if preferred:
            pref = Path(preferred)
            try:
                if pref.is_absolute():
                    pref_r = pref.resolve()
                    if str(pref_r).startswith(str(base) + os.sep):
                        candidates.insert(0, pref_r)
                else:
                    candidates.insert(0, base / pref)
            except Exception:
                pass
    else:
        gdir = Path.home() / ".tera_pilot"
        candidates = [gdir / n for n in _MEMORY_FILE_NAMES]
        if preferred:
            try:
                pref_r = Path(preferred).resolve()
                if str(pref_r).startswith(str(gdir.resolve()) + os.sep):
                    candidates.insert(0, pref_r)
            except Exception:
                pass
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


@_get_route("/api/memory/read")
def _memory_read(handler, body=None):
    try:
        target = _memory_file_target(handler)
        content = ""
        if target.exists():
            content = target.read_text(encoding="utf-8", errors="replace")
        source = "none"
        if target.exists():
            base = _workspace_root(handler)
            source = "project" if base and str(target.resolve()).startswith(str(base) + os.sep) else "global"
        return _ok({"content": content, "path": str(target), "source": source})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/memory/write")
def _memory_write(handler, body=None):
    body = body or {}
    try:
        target = _memory_file_target(handler, preferred=str(body.get("path") or ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(body.get("content") or ""), encoding="utf-8")
        return _ok({"path": str(target)})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/memory/append_lesson")
def _memory_append_lesson(handler, body=None):
    body = body or {}
    title = (body.get("title") or "").strip()
    body_text = (body.get("body") or "").strip()
    if not title or not body_text:
        return _err("Both title and body are required")
    try:
        target = _memory_file_target(handler)
        target.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"\n## Lesson: {title} ({ts})\n\n{body_text}\n")
        return _ok({"path": str(target)})
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Project file access — read / write / list (safe, workspace-scoped)
# ════════════════════════════════════════════════════════════════════


def _code_viewer(handler):
    from .code_viewer import CodeViewerService
    base = _workspace_root(handler)
    return CodeViewerService(root=str(base) if base else None)


@_post_route("/api/files/read")
def _files_read(handler, body=None):
    body = body or {}
    raw = body.get("path") or (body.get("args") or [None])[0]
    raw = str(raw or "").strip()
    if not raw:
        return _err("path is required")
    try:
        return _ok(_code_viewer(handler).read_file(raw))
    except Exception as e:
        return _err(str(e))


@_post_route("/api/files/write")
def _files_write(handler, body=None):
    body = body or {}
    raw = str(body.get("path") or "").strip()
    content = str(body.get("content") or "")
    if not raw:
        return _err("path is required")
    base = _workspace_root(handler)
    if base is None:
        return _err("No project open — open a project folder first.")
    try:
        p = (base / raw).resolve()
        if p != base and not str(p).startswith(str(base) + os.sep):
            return _err("Path escapes the project root.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return _ok({"path": str(p)})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/files/list")
def _files_list(handler, body=None):
    try:
        return _code_viewer(handler).list_files()
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Settings & agent controls
# ════════════════════════════════════════════════════════════════════


@_post_route("/api/settings/save")
def _settings_save(handler, body=None):
    body = body or {}
    try:
        return handler._save_settings(body)
    except Exception as e:
        return _err(str(e))


@_post_route("/api/chat/stop")
def _chat_stop(handler, body=None):
    try:
        handler.ctx.cancel_chat()
        return _ok({"message": "Generation stopped"})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/agent/undo")
def _agent_undo(handler, body=None):
    """Undo the most recent agent changes via the checkpoint system."""
    try:
        from .checkpoint import CheckpointManager
        base = _workspace_root(handler)
        cm = CheckpointManager(session_id="gui", workspace=str(base) if base else None)
        result = cm.rewind(n=1)
        if result.get("ok"):
            return _ok({"method": "rewind", "restored": result.get("restored", [])})
        return _err(result.get("error") or "Nothing to revert")
    except Exception as e:
        return _err(str(e))


@_post_route("/api/agent/autonomy")
def _agent_autonomy(handler, body=None):
    body = body or {}
    level = body.get("level") or (body.get("args") or [None])[0]
    level = str(level or "").strip()
    if not level:
        return _err("level is required")
    try:
        return handler._save_advanced_agent_settings({"agent": {"autonomy": level}})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/agent/guardian")
def _agent_guardian(handler, body=None):
    body = body or {}
    level = body.get("level") or (body.get("args") or [None])[0]
    level = str(level or "").strip()
    valid = {"off", "dangerous_only", "all"}
    if level not in valid:
        return _err(f"Invalid level: {level}. Valid: {', '.join(sorted(valid))}")
    try:
        with handler.ctx._config_lock:
            handler.ctx.config["guardian_level"] = level
            from .api_server import _save_config
            _save_config(handler.ctx.config)
        return _ok({"level": level})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/agent/advanced_settings/save")
def _agent_advanced_save(handler, body=None):
    body = body or {}
    try:
        return handler._save_advanced_agent_settings(body)
    except Exception as e:
        return _err(str(e))


@_post_route("/api/diff/respond")
def _diff_respond(handler, body=None):
    """Respond to a pending diff-review request.

    The modern HTTP flow always sends ``review_id`` (routed to the exact
    agent thread). The legacy Qt-bridge flow sent no id — fall back to
    the most recently created pending review.
    """
    body = body or {}
    accepted = bool(body.get("accepted", False))
    review_id = str(body.get("review_id") or "")
    try:
        ctx = handler.ctx
        with ctx._diff_review_lock:
            entry = None
            if review_id:
                entry = ctx._diff_review_pending.get(review_id)
            else:
                newest = None
                for rid, e in ctx._diff_review_pending.items():
                    if newest is None or e.get("ts", 0) >= newest.get("ts", 0):
                        newest = e
                        review_id = rid
                entry = newest
            if entry is None:
                return _err("no pending diff review for that review_id")
            entry["accepted"] = bool(accepted)
            entry["event"].set()
        logger.info("[api_ext] diff/respond: review_id=%s accepted=%s", review_id, accepted)
        return _ok({"accepted": bool(accepted), "review_id": review_id})
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Queue / updates / provider health / pricing
# ════════════════════════════════════════════════════════════════════


@_get_route("/api/queue/stats")
def _queue_stats(handler, body=None):
    try:
        from .request_queue import get_queue_registry
        return _ok({"stats": get_queue_registry().stats()})
    except Exception as e:
        return _err(str(e))


@_get_route("/api/updates/check")
def _updates_check(handler, body=None):
    try:
        from .auto_updater import AutoUpdater, get_current_version
        from .api_server import _tera_pilot_home
        updater = AutoUpdater(current_version=get_current_version())
        repo = (handler.ctx.config.get("update_repo") or "") if handler else ""
        updater.set_repo(repo or None)
        updater.check_for_updates(get_current_version())
        return _ok({"update_available": False, "checked": True})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/providers/health")
def _providers_health(handler, body=None):
    """Synchronous provider health probe (mirrors the SSE test endpoint)."""
    body = body or {}
    pid = str(body.get("provider_id") or (body.get("args") or [None])[0] or "").strip()
    if not pid:
        return _err("provider_id is required")
    try:
        from .providers.types import ProviderMessage, ProviderConfig
        cfg = handler.ctx.config.get("providers", {}).get(pid, {})
        prov_cfg = ProviderConfig(
            provider_id=pid,
            model=cfg.get("model", ""),
            api_key=cfg.get("api_key") or None,
            api_base=cfg.get("api_base") or None,
            temperature=0.2,
            max_tokens=100,
        )
        import time as _time
        start = _time.time()
        try:
            resp = handler.ctx.registry.get(pid).generate([
                ProviderMessage(role="user", content="Say hello in one sentence."),
            ])
            latency_ms = int((_time.time() - start) * 1000)
            return _ok({"latency_ms": latency_ms, "key_valid": True, "provider_id": pid})
        except Exception as exc:
            msg = str(exc).lower()
            return {
                "ok": False,
                "provider_id": pid,
                "rate_limited": "429" in msg or "rate" in msg,
                "key_valid": "401" not in msg and "invalid" not in msg and "api key" not in msg,
                "error": str(exc),
            }
    except Exception as e:
        return _err(str(e))


@_get_route("/api/pricing/table")
def _pricing_table(handler, body=None):
    try:
        costs = {
            "openai": 0.00003, "anthropic": 0.000015, "deepseek": 0.00001,
            "gemini": 0.00003, "groq": 0.00008, "mistral": 0.00003,
            "openrouter": 0.00003, "together": 0.00004, "zai": 0.00002,
        }
        providers = []
        if handler is not None:
            for p in handler.ctx.registry.list_providers():
                providers.append({
                    "id": p.get("id"),
                    "label": p.get("label", p.get("id")),
                    "model": p.get("model", ""),
                    "usd_per_1k_tokens": costs.get(p.get("id", ""), 0.03),
                })
        return _ok({"live": False, "fetched_at": None, "providers": providers})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/pricing/fetch")
def _pricing_fetch(handler, body=None):
    """Live pricing requires a network pricing source; we ship a bundled
    snapshot, so report that honestly instead of pretending to refresh."""
    return _err("Live pricing source not configured — using bundled snapshot", count=0)


# ════════════════════════════════════════════════════════════════════
# Snippets (stored in config)
# ════════════════════════════════════════════════════════════════════


@_get_route("/api/snippets")
def _snippets_list(handler, body=None):
    try:
        return _ok({"snippets": handler.ctx.config.get("snippets", [])})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/snippets/save")
def _snippets_save(handler, body=None):
    body = body or {}
    name = str(body.get("name") or (body.get("args") or [None, None, None])[0] or "").strip()
    content = str(body.get("content") or (body.get("args") or [None, None, None])[1] or "")
    lang = str(body.get("language") or (body.get("args") or [None, None, None])[2] or "text")
    if not name or not content:
        return _err("name and content are required")
    try:
        cfg = handler.ctx.config
        with handler.ctx._config_lock:
            snippets = [s for s in cfg.get("snippets", []) if s.get("name") != name]
            snippets.append({"name": name, "content": content, "language": lang})
            cfg["snippets"] = snippets
            from .api_server import _save_config
            _save_config(cfg)
        return _ok({"name": name})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/snippets/delete")
def _snippets_delete(handler, body=None):
    body = body or {}
    name = str(body.get("name") or (body.get("args") or [None])[0] or "").strip()
    if not name:
        return _err("name is required")
    try:
        cfg = handler.ctx.config
        with handler.ctx._config_lock:
            cfg["snippets"] = [s for s in cfg.get("snippets", []) if s.get("name") != name]
            from .api_server import _save_config
            _save_config(cfg)
        return _ok({"name": name})
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Auto-router toggle + prompt classification
# ════════════════════════════════════════════════════════════════════


@_post_route("/api/router/toggle")
def _router_toggle(handler, body=None):
    body = body or {}
    enabled = bool(body.get("enabled") if body.get("enabled") is not None else (body.get("args") or [True])[0])
    try:
        cfg = handler.ctx.config
        with handler.ctx._config_lock:
            cfg["auto_route"] = enabled
            from .api_server import _save_config
            _save_config(cfg)
        return _ok({"enabled": enabled})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/router/classify")
def _router_classify(handler, body=None):
    body = body or {}
    text = str(body.get("text") or (body.get("args") or [None])[0] or "").strip()
    if not text:
        return _err("text is required")
    try:
        from .auto_router import get_auto_router
        return _ok(get_auto_router().classify_explain(text))
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Cross-chat memory save / RAG search / chat title / external open
# ════════════════════════════════════════════════════════════════════


@_post_route("/api/memory/save")
def _memory_save(handler, body=None):
    body = body or {}
    session_id = str(body.get("chat_id") or (body.get("args") or [None])[0] or "session")
    title = str(body.get("title") or (body.get("args") or [None, None])[1] or "")
    summary = str(body.get("summary") or (body.get("args") or [None, None, None])[2] or "")
    if not summary:
        return _err("summary is required")
    try:
        from .memory_service import MemoryService
        from .api_server import _tera_pilot_home
        base = _workspace_root(handler)
        mem = MemoryService(persist_path=str(_tera_pilot_home() / "cross_chat_memory.md"))
        result = mem.save_context(
            session_id=session_id,
            title=title or session_id,
            content=summary,
            project_root=str(base) if base else None,
            chat_id=session_id,
        )
        return result
    except Exception as e:
        return _err(str(e))


@_post_route("/api/rag/search")
def _rag_search(handler, body=None):
    """Grep-based project search — returns a flat result array, matching
    the legacy Qt-bridge contract app.js expects (results.length, r.path,
    r.line, r.text, r.source)."""
    body = body or {}
    text = str(body.get("text") or (body.get("args") or [None])[0] or "").strip()
    if not text:
        return []
    try:
        results = _code_viewer(handler).search(text, max_results=50)
        for r in results:
            r["source"] = "grep"
        return results
    except Exception as e:
        logger.warning("[api_ext] rag/search failed: %s", e)
        return []


@_post_route("/api/oneshot/enhance")
def _oneshot_enhance(handler, body=None):
    """Legacy Qt-bridge alias for the prompt-enhancer (SSE)."""
    body = body or {}
    args = body.get("args") or []
    request_id = str(body.get("request_id") or (args[0] if len(args) > 0 else "") or "")
    text = str(body.get("text") or (args[1] if len(args) > 1 else "") or "").strip()
    if not text:
        return _err("text is required")
    return handler._handle_oneshot({
        "request_id": request_id,
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": "You are a prompt engineer. Rewrite the user's request as a structured prompt with these sections: [INTENT], [CONTEXT], [CONSTRAINTS], [DELIVERABLES]. Be concise. Output only the structured prompt, nothing else."},
            {"role": "user", "content": text},
        ],
    })


@_post_route("/api/chat/generate_title")
def _chat_generate_title(handler, body=None):
    """Derive a chat title from its first user message (no LLM round-trip)."""
    body = body or {}
    chat_id = str(body.get("chat_id") or (body.get("args") or [None])[0] or "")
    if not chat_id:
        return _err("chat_id is required")
    try:
        from .api_server import _load_chat, _save_chat
        chat = _load_chat(chat_id)
        if not chat:
            return _err("chat not found")
        first_user = ""
        for m in chat.get("messages", []):
            if m.get("role") == "user":
                first_user = m.get("content", "")
                break
        title = " ".join(first_user.split())[:60] or "New chat"
        chat["title"] = title
        _save_chat(chat)
        return _ok({"title": title, "chat_id": chat_id})
    except Exception as e:
        return _err(str(e))


@_post_route("/api/external/open")
def _external_open(handler, body=None):
    """Open a URL in the user's default browser (server runs locally)."""
    body = body or {}
    url = str(body.get("url") or (body.get("args") or [None])[0] or "").strip()
    if not url:
        return _err("url is required")
    if not url.startswith(("http://", "https://")):
        return _err("Only http/https URLs can be opened")
    try:
        import webbrowser
        webbrowser.open(url, new=2)
        return _ok({"url": url})
    except Exception as e:
        return _err(str(e))


# ════════════════════════════════════════════════════════════════════
# Swarm agents — lightweight in-memory registry (spawn/list/remove/cleanup)
# ════════════════════════════════════════════════════════════════════

_SWARM_AGENTS: dict = {}
_SWARM_LOCK = threading.Lock()
_SWARM_COUNTER = [0]


@_post_route("/api/swarm/spawn")
def _swarm_spawn(handler, body=None):
    body = body or {}
    args = body.get("args") or []
    name = str(body.get("name") or (args[0] if len(args) > 0 else "") or "").strip()
    goal = str(body.get("goal") or (args[1] if len(args) > 1 else "") or "").strip()
    role = str(body.get("role") or (args[2] if len(args) > 2 else "") or "researcher").strip()
    if not goal:
        return _err("goal is required")
    with _SWARM_LOCK:
        _SWARM_COUNTER[0] += 1
        aid = f"agent-{_SWARM_COUNTER[0]:04d}"
        _SWARM_AGENTS[aid] = {
            "id": aid,
            "name": name or f"Agent #{_SWARM_COUNTER[0]}",
            "role": role,
            "goal": goal,
            "status": "idle",
            "created_at": None,
        }
    return _ok({"id": aid})


@_get_route("/api/swarm/list")
def _swarm_list(handler, body=None):
    with _SWARM_LOCK:
        return _ok({"agents": list(_SWARM_AGENTS.values())})


@_post_route("/api/swarm/remove")
def _swarm_remove(handler, body=None):
    body = body or {}
    aid = str(body.get("id") or (body.get("args") or [None])[0] or "")
    with _SWARM_LOCK:
        _SWARM_AGENTS.pop(aid, None)
    return _ok({"id": aid})


@_post_route("/api/swarm/cleanup")
def _swarm_cleanup(handler, body=None):
    with _SWARM_LOCK:
        _SWARM_AGENTS.clear()
    return _ok({})


# ════════════════════════════════════════════════════════════════════
# Installer — patches TeraPilotAPIHandler
# ════════════════════════════════════════════════════════════════════

def install() -> None:
    """Monkey-patch :class:`tera_pilot.api_server.TeraPilotAPIHandler` so the new
    routes are dispatched alongside the legacy ones.

    Also extends :attr:`ServerContext.MUTATING_PATHS` with the new POST
    routes so the existing bearer-token auth guard protects them.

    Called automatically at the bottom of :mod:`tera_pilot.api_server` —
    users never need to invoke this directly.
    """
    from . import api_server as _as

    TeraPilotAPIHandler = _as.TeraPilotAPIHandler
    _orig_do_get = TeraPilotAPIHandler.do_GET
    _orig_do_post = TeraPilotAPIHandler.do_POST
    _orig_do_delete = TeraPilotAPIHandler.do_DELETE

    # Extend MUTATING_PATHS so the new POST routes are protected by the
    # existing bearer-token auth guard. The original set lives on
    # ServerContext as a frozenset, so we replace it with a new frozenset
    # that includes our additions.
    try:
        existing = set(_as.ServerContext.MUTATING_PATHS)
        existing.update(_ROUTES_POST.keys())
        _as.ServerContext.MUTATING_PATHS = frozenset(existing)
    except Exception as e:
        logger.warning("[api_ext] failed to extend MUTATING_PATHS: %s", e)

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in _ROUTES_GET:
            try:
                result = _ROUTES_GET[path](self)
                if result is not None:
                    self._json(result)
            except Exception as e:
                logger.exception("[api_ext] GET %s failed", path)
                self._json(_err(f"internal error: {e}"), 500)
            return
        return _orig_do_get(self)

    def do_POST(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in _ROUTES_POST:
            if not self._check_auth(path):
                self._json({"error": "unauthorised"}, 401)
                return
            try:
                body = self._read_json()
            except Exception as e:
                self._json({"error": f"Invalid JSON body: {e}"}, 400)
                return
            try:
                result = _ROUTES_POST[path](self, body)
                if result is not None:
                    self._json(result)
            except Exception as e:
                logger.exception("[api_ext] POST %s failed", path)
                self._json(_err(f"internal error: {e}"), 500)
            return
        return _orig_do_post(self)

    def do_DELETE(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in _ROUTES_DELETE:
            if not self._check_auth(path):
                self._json({"error": "unauthorised"}, 401)
                return
            try:
                body = self._read_json()
            except Exception:
                body = {}
            try:
                result = _ROUTES_DELETE[path](self, body)
                if result is not None:
                    self._json(result)
            except Exception as e:
                logger.exception("[api_ext] DELETE %s failed", path)
                self._json(_err(f"internal error: {e}"), 500)
            return
        return _orig_do_delete(self)

    TeraPilotAPIHandler.do_GET = do_GET
    TeraPilotAPIHandler.do_POST = do_POST
    TeraPilotAPIHandler.do_DELETE = do_DELETE
    logger.info(
        "[api_ext] installed %d GET + %d POST + %d DELETE routes "
        "(MUTATING_PATHS: %s entries)",
        len(_ROUTES_GET), len(_ROUTES_POST), len(_ROUTES_DELETE),
        len(getattr(_as.ServerContext, "MUTATING_PATHS", []))
            if hasattr(_as, "ServerContext") else "n/a",
    )


__all__ = ["install"]
