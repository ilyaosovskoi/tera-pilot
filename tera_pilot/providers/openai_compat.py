"""
OpenAI-compatible base for HTTP API providers.

OpenAI, OpenRouter, Groq all speak the OpenAI Chat Completions API.
This base class implements the shared HTTP/streaming logic; concrete
providers just override `provider_id`, `label`, `default_model`,
`api_base`, and `env_var`.

Anthropic has its own message format — see anthropic.py.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any, Dict, Generator, List, Optional

from .base import (
    Provider,
    ProviderMessage,
    ProviderResponse,
    ProviderCapability,
    ProviderError,
)

logger = logging.getLogger(__name__)


class OpenAICompatProvider(Provider):
    """Base for any provider that speaks the OpenAI Chat Completions API."""

    provider_id: str = "openai_compat"
    label: str = "OpenAI-Compatible"
    default_model: str = ""
    api_base: str = "https://api.openai.com/v1"
    env_var: str = ""        # environment variable that holds the API key
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # v1.2.1-fix (review §4.3): conservative default context window
    # for OpenAI-compatible endpoints. Concrete subclasses (OpenAI,
    # Groq, OpenRouter, …) override this with the actual window for
    # their default model. The number is also overridable per-config
    # via ProviderConfig.extra["context_window"].
    context_window: int = 16_384

    # v1.2.1-fix (review §4.3): map of known model name substrings to
    # their context windows. Used by get_context_window() to return
    # the actual window for the configured model instead of the
    # class-level default. Subclasses can extend this dict.
    _MODEL_CONTEXT_WINDOWS: Dict[str, int] = {}

    def get_context_window(self) -> int:
        """v1.2.1-fix (review §4.3): return the context window for the
        configured model, falling back to the class-level default.

        We do a substring match against ``_MODEL_CONTEXT_WINDOWS`` so
        e.g. ``gpt-4o-2024-08-06`` matches the ``gpt-4o`` entry. This
        is intentionally fuzzy — exact model versioning is left to the
        provider's API documentation; here we just need a reasonable
        approximation so AgentRuntime can size its memory + context
        budgets proportionally.
        """
        # Per-config override wins.
        override = self.config.extra.get("context_window") if self.config.extra else None
        if isinstance(override, int) and override > 0:
            return override
        model = (self.config.model or "").lower()
        for prefix, window in self._MODEL_CONTEXT_WINDOWS.items():
            if prefix in model:
                return window
        return self.context_window

    # ── Lifecycle ──────────────────────────────────────────────────

    def load(self) -> bool:
        if self._loaded:
            return True

        key = self.config.api_key or (os.environ.get(self.env_var) if self.env_var else None)
        if not key and self.provider_id != "local":
            logger.warning(f"[{self.provider_id}] No API key (set {self.env_var})")
            # We don't fail load() — user might configure the key later.
            # generate() will raise a clear error.
        self._api_key = key
        self._loaded = True
        self._info = {
            "provider": self.provider_id,
            "model":    self.config.model,
            "api_base": self.config.api_base or self.api_base,
        }
        return True

    def unload(self) -> None:
        self._loaded = False
        self._api_key = None
        self._info = {}

    # ── Generation ────────────────────────────────────────────────

    def generate(
        self,
        messages: List[ProviderMessage],
        *,
        skill: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        stop: Optional[List[str]] = None,
        model: Optional[str] = None,
    ) -> ProviderResponse:
        """Call the chat-completions endpoint (non-streaming).

        v1.0.5-correctness: defensive response parsing. Previously a
        200-OK response with ``choices: []`` (some providers do this on
        content filter) raised uncaught ``KeyError``/``IndexError``, and
        a tool-calls-only response (``content: null``) raised
        ``TypeError`` on the subscript. We now return a clean
        ``ProviderResponse`` with an empty text and a descriptive
        ``finish_reason`` instead of crashing the agent loop
        (BUGS_REPORT M-AUTO-5).

        v2.4.1-fix: ``model`` (per-call override, G20b) is now accepted
        and threaded into the payload. Before, callers passing
        ``model=...`` crashed with TypeError.

        v2.4.1-fix: tool_calls returned by the API are ALSO serialized
        into ``text`` (as ``{"tool": ..., "args": ...}`` JSON) so the
        agent runtime's OutputParser — which only parses text — can
        see them. Native ``tool_calls`` alone were previously invisible
        to the legacy ReAct loop: ``content`` is null for a
        tool-calls-only response, so the tool call was silently dropped.
        """
        self._ensure_loaded()
        messages = self._inject_skill(messages, skill)

        payload = self._build_payload(messages, tools, stop, stream=False, model=model)
        data = self._post(payload, stream=False)

        choices = data.get("choices") or []
        if not choices:
            # Some providers return an empty choices array when the
            # content filter triggers. Surface this as a clean error
            # with the provider's error message if available.
            err_msg = (data.get("error", {}) or {}).get("message") \
                or "empty choices array (content filter?)"
            raise ProviderError(f"{self.provider_id}: {err_msg}")

        choice = choices[0]
        message = choice.get("message") or {}
        # content can be None for tool-calls-only responses.
        text = message.get("content") or ""
        usage = data.get("usage") or {}
        finish_reason = choice.get("finish_reason") or "stop"

        # If content is empty AND there are no tool_calls, surface the
        # finish_reason so the caller can distinguish "filtered" /
        # "length" / "stop" instead of seeing an empty string.
        if not text and not message.get("tool_calls"):
            text = f"[{finish_reason}] no content returned"

        # v2.4.1-fix: serialize native tool_calls into the text-based
        # JSON format the agent runtime's OutputParser understands
        # ({"tool": name, "args": {...}}), appended after any text.
        native_tool_calls = message.get("tool_calls")
        if native_tool_calls:
            serialized = []
            for tc in native_tool_calls:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if name:
                    serialized.append(json.dumps({"tool": name, "args": args}))
            if serialized:
                text = (text + "\n" if text else "") + "\n".join(serialized)

        return ProviderResponse(
            text=text,
            model=data.get("model", self.config.model),
            provider=self.provider_id,
            finish_reason=finish_reason,
            tokens_in=usage.get("prompt_tokens", 0) or 0,
            tokens_out=usage.get("completion_tokens", 0) or 0,
            tool_calls=message.get("tool_calls"),
            raw=data,
        )

    def stream(
        self,
        messages: List[ProviderMessage],
        *,
        skill: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        stop: Optional[List[str]] = None,
        model: Optional[str] = None,
    ) -> Generator[str, None, None]:
        self._ensure_loaded()
        messages = self._inject_skill(messages, skill)

        payload = self._build_payload(messages, tools, stop, stream=True, model=model)
        for line in self._post_stream(payload):
            if not line or not line.startswith("data: "):
                continue
            payload_str = line[len("data: "):].strip()
            if payload_str == "[DONE]":
                break
            try:
                chunk = json.loads(payload_str)
                delta = chunk["choices"][0].get("delta", {}).get("content", "")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                logger.warning(f"[{self.provider_id}] bad SSE chunk: {e}")

    # ── Helpers ───────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            logger.info("[%s] not loaded, calling load()", self.provider_id)
            if not self.load():
                raise ProviderError(f"{self.label} not loaded")
        if not self._api_key:
            raise ProviderError(
                f"{self.label} requires an API key. "
                f"Set ${self.env_var} or pass api_key in ProviderConfig."
            )
        logger.debug("[%s] _ensure_loaded OK — model=%s api_base=%s",
                      self.provider_id, self.config.model,
                      self.config.api_base or self.api_base)

    def _build_payload(self, messages, tools, stop, *, stream: bool,
                       model: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model or self.config.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "top_p": self.config.top_p,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
        if stop:
            payload["stop"] = stop
        return payload

    def _url(self) -> str:
        base = self.config.api_base or self.api_base
        return base.rstrip("/") + "/chat/completions"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Tera Pilot/1.0 (https://github.com/nicepkg/tera_pilot)",
        }

    def _post(self, payload: Dict[str, Any], *, stream: bool) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(),
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise ProviderError(f"{self.label} HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise ProviderError(f"{self.label} network error: {e.reason}") from e

    def _post_stream(self, payload: Dict[str, Any]) -> Generator[str, None, None]:
        body = json.dumps(payload).encode("utf-8")
        url = self._url()
        logger.info("[%s] POST stream → url=%s  model=%s  timeout=%.0fs",
                     self.provider_id, url, self.config.model, self.config.timeout)
        req = urllib.request.Request(
            url,
            data=body,
            headers={**self._headers(), "Accept": "text/event-stream"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as r:
                logger.info("[%s] connection established, reading SSE…",
                             self.provider_id)
                line_count = 0
                for raw_line in r:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    if line:
                        line_count += 1
                        yield line
                logger.info("[%s] SSE stream closed — %d lines received",
                             self.provider_id, line_count)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise ProviderError(f"{self.label} stream HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise ProviderError(f"{self.label} stream network error: {e.reason}") from e



