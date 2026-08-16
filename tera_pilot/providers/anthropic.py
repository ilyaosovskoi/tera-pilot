"""
Anthropic provider — Claude family.

Anthropic uses its own message format (separate `system` field, content
blocks instead of plain strings). We translate to/from our ProviderMessage.
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

ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    """Claude family — strong reasoning and coding."""

    provider_id: str = "anthropic"
    label: str = "Anthropic"
    default_model: str = "claude-3-5-sonnet-20241022"
    api_base: str = "https://api.anthropic.com"
    env_var: str = "ANTHROPIC_API_KEY"
    capabilities: frozenset = frozenset({
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.VISION,
        ProviderCapability.SYSTEM_PROMPT,
        ProviderCapability.SKILLS,
    })

    # v1.2.1-fix (review §4.3): context windows for current Anthropic
    # models. Numbers from Anthropic's public docs as of 2026-07.
    # Claude 4 / 3.7 / 3.5 family = 200K, Claude 3 Opus = 200K,
    # Claude 3 Haiku = 200K, Claude 2.x = 100K (legacy).
    _MODEL_CONTEXT_WINDOWS = {
        "claude-opus-4": 200_000,
        "claude-sonnet-4": 200_000,
        "claude-3-7": 200_000,
        "claude-3-5": 200_000,
        "claude-3-opus": 200_000,
        "claude-3-haiku": 200_000,
        "claude-3-sonnet": 200_000,
        "claude-2": 100_000,
    }
    context_window: int = 200_000

    def get_context_window(self) -> int:
        """v1.2.1-fix (review §4.3): Anthropic-specific override.

        Substring-matches the configured model name against
        ``_MODEL_CONTEXT_WINDOWS`` so e.g. ``claude-3-5-sonnet-20241022``
        matches the ``claude-3-5`` entry (200K).
        """
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
        key = self.config.api_key or os.environ.get(self.env_var)
        if not key:
            logger.warning(f"[{self.provider_id}] No API key (set {self.env_var})")
        self._api_key = key
        self._loaded = True
        self._info = {
            "provider": self.provider_id,
            "model": self.config.model,
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
        self._ensure_loaded()
        messages = self._inject_skill(messages, skill)

        system, conv = self._split_system(messages)
        payload = self._build_payload(system, conv, tools, stop, stream=False, model=model)
        data = self._post(payload)

        # Extract text and tool_use from content blocks.
        # M-AUTO-6: previously only text blocks were extracted — tool_use
        # blocks were silently dropped in non-streaming mode, causing the
        # agent runtime to never see tool calls from Claude.
        text_parts = []
        tool_calls_raw = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls_raw.append(block)

        text = "".join(text_parts)

        # If there are tool_use blocks, serialize them as structured
        # text that the agent runtime's OutputParser can parse.
        # v2.4.1-fix: the old format was
        # ``⌘{"name": ..., "id": ..., "arguments": ...}⌠`` which the
        # OutputParser never understood (it looks for ``"tool"`` /
        # ``"args"`` keys), so every Anthropic tool call was silently
        # dropped in non-streaming mode (headless daemon / CLI runs).
        # We now emit the exact parser format ``{"tool": ..., "args": ...}``.
        if tool_calls_raw:
            tool_text_parts = []
            for tc in tool_calls_raw:
                tc_name = tc.get("name", "")
                tc_input = tc.get("input", {}) or {}
                tool_text_parts.append(
                    json.dumps({"tool": tc_name, "args": tc_input})
                )
            if text:
                text = text + "\n" + "\n".join(tool_text_parts)
            else:
                text = "\n".join(tool_text_parts)

        usage = data.get("usage", {})
        return ProviderResponse(
            text=text,
            model=data.get("model", self.config.model),
            provider=self.provider_id,
            finish_reason=data.get("stop_reason", "stop"),
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
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

        system, conv = self._split_system(messages)
        payload = self._build_payload(system, conv, tools, stop, stream=True, model=model)

        for event in self._post_stream(payload):
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield text

    # ── Helpers ───────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            logger.info("[anthropic] not loaded, calling load()")
            self.load()
        if not self._api_key:
            raise ProviderError(
                f"{self.label} requires an API key. Set ${self.env_var} "
                f"or pass api_key in ProviderConfig."
            )
        logger.debug("[anthropic] _ensure_loaded OK — model=%s", self.config.model)

    def _split_system(self, messages) -> tuple[str, List[ProviderMessage]]:
        """Pull the system message out — Anthropic takes it as a top-level field."""
        sys_parts = []
        conv = []
        for m in messages:
            if m.role == "system":
                sys_parts.append(m.content)
            else:
                conv.append(m)
        return "\n\n".join(sys_parts), conv

    def _build_payload(self, system, conv, tools, stop, *, stream: bool,
                       model: Optional[str] = None) -> Dict[str, Any]:
        # Translate to Anthropic's format: messages must alternate user/assistant
        anthropic_msgs = []
        for m in conv:
            anthropic_msgs.append({
                "role": m.role,        # "user" | "assistant"
                "content": m.content,
            })

        payload: Dict[str, Any] = {
            "model": model or self.config.model,
            "messages": anthropic_msgs,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "stream": stream,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools
        if stop:
            payload["stop_sequences"] = stop
        return payload

    def _url(self) -> str:
        base = self.config.api_base or self.api_base
        return base.rstrip("/") + "/v1/messages"

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "Content-Type": "application/json",
            "User-Agent": "Tera Pilot/1.0 (https://github.com/nicepkg/tera_pilot)",
        }

    def _post(self, payload) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(), data=body, headers=self._headers(), method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise ProviderError(f"{self.label} HTTP {e.code}: {err}") from e
        except urllib.error.URLError as e:
            raise ProviderError(f"{self.label} network error: {e.reason}") from e

    def _post_stream(self, payload) -> Generator[Dict[str, Any], None, None]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(),
            data=body,
            headers={**self._headers(), "Accept": "text/event-stream"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as r:
                event_type = None
                data_buf = []
                for raw in r:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line.startswith("event: "):
                        event_type = line[len("event: "):].strip()
                    elif line.startswith("data: "):
                        data_buf.append(line[len("data: "):])
                    elif line == "" and data_buf:
                        try:
                            evt = json.loads("\n".join(data_buf))
                            evt["type"] = event_type or evt.get("type", "")
                            yield evt
                        except json.JSONDecodeError:
                            pass
                        data_buf = []
                        event_type = None
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise ProviderError(f"{self.label} stream HTTP {e.code}: {err}") from e
        except urllib.error.URLError as e:
            raise ProviderError(f"{self.label} stream network error: {e.reason}") from e
