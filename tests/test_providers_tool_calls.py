"""Regression tests for two provider fixes:

1. ``generate()`` / ``stream()`` must accept the ``model`` kwarg (per-call
   model override, G20b). Callers like consensus_engine, guardian,
   second_opinion, persona_memory and task_decomposition_router pass
   ``model=...`` — before the fix every one of those calls raised
   ``TypeError: unexpected keyword argument 'model'`` (Guardian silently
   defaulted to APPROVE, consensus runs always failed, the decomposition
   router always fell back to single-model routing).

2. Native tool calls returned by the APIs must be serialized into text
   in the ``{"tool": ..., "args": ...}`` format the agent runtime's
   OutputParser understands. Before the fix, Anthropic emitted
   ``{name/arguments}`` (unparseable) and OpenAI-compat left tool-calls-only
   responses with empty text — both silently dropped the tool call.
"""

import json

import pytest

from tera_pilot.agent_runtime.parser import OutputParser
from tera_pilot.providers.anthropic import AnthropicProvider
from tera_pilot.providers.base import ProviderConfig, ProviderMessage
from tera_pilot.providers.openai_compat import OpenAICompatProvider


def _cfg(pid, model="configured-model"):
    return ProviderConfig(provider_id=pid, model=model, api_key="k")


# ── model kwarg acceptance + payload override ──────────────────────────


@pytest.mark.parametrize("cls,pid", [
    (AnthropicProvider, "anthropic"),
    (OpenAICompatProvider, "openai"),
])
def test_generate_and_stream_accept_model_kwarg(cls, pid):
    prov = cls(_cfg(pid))
    msgs = [ProviderMessage(role="user", content="hi")]
    # These used to raise TypeError.
    sig_gen = cls.generate.__get__(prov)
    assert "model" in cls.generate.__code__.co_varnames or "model" in str(
        [p for p in cls.generate.__code__.co_varnames]
    )
    # generate(messages, model=...) must not raise before hitting the network.
    # We only assert the call is accepted — the mock below covers payload.
    with pytest.raises(Exception) as exc:
        prov.generate(msgs, model="override")
    assert "unexpected keyword" not in str(exc.value)


def test_openai_compat_payload_uses_model_override():
    prov = OpenAICompatProvider(_cfg("openai"))
    msgs = [ProviderMessage(role="user", content="hi")]
    payload = prov._build_payload(msgs, None, None, stream=False, model="override-model")
    assert payload["model"] == "override-model"
    payload_default = prov._build_payload(msgs, None, None, stream=False)
    assert payload_default["model"] == "configured-model"


def test_anthropic_payload_uses_model_override():
    prov = AnthropicProvider(_cfg("anthropic"))
    msgs = [ProviderMessage(role="user", content="hi")]
    payload = prov._build_payload("", msgs, None, None, stream=False, model="override-model")
    assert payload["model"] == "override-model"
    payload_default = prov._build_payload("", msgs, None, None, stream=False)
    assert payload_default["model"] == "configured-model"


# ── tool-call serialization reachable by OutputParser ──────────────────


def test_anthropic_tool_use_serialization_is_parseable():
    """The text AnthropicProvider.generate() emits for tool_use blocks
    must round-trip through OutputParser.parse_tool_call()."""
    prov = AnthropicProvider(_cfg("anthropic"))
    prov._loaded = True
    prov._api_key = "k"

    def fake_post(payload):
        return {
            "content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                 "input": {"path": "app.py"}},
            ],
            "model": "claude-x",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }

    prov._post = fake_post  # type: ignore[method-assign]
    resp = prov.generate([ProviderMessage(role="user", content="read app.py")])
    call = OutputParser.parse_tool_call(resp.text)
    assert call is not None, f"tool call dropped from text: {resp.text!r}"
    assert call.name.value == "read_file"
    assert call.args == {"path": "app.py"}


def test_openai_compat_tool_calls_serialization_is_parseable():
    """A tool-calls-only response (content: null) must produce text the
    parser can turn into a ToolCall."""
    prov = OpenAICompatProvider(_cfg("openai"))
    prov._loaded = True
    prov._api_key = "k"

    def fake_post(payload, *, stream):
        return {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search_project",
                                     "arguments": json.dumps({"query": "foo", "directory": "."})},
                    }],
                },
            }],
            "model": "m",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    prov._post = fake_post  # type: ignore[method-assign]
    resp = prov.generate([ProviderMessage(role="user", content="search")])
    call = OutputParser.parse_tool_call(resp.text)
    assert call is not None, f"tool call dropped from text: {resp.text!r}"
    assert call.name.value == "search_project"
    assert call.args == {"query": "foo", "directory": "."}
