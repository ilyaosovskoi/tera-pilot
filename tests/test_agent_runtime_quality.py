"""Tests for v2.3.5 agent-quality improvements (informed by Hermes Agent):

- repetition guard (ported from Nous Research's hermes-agent, MIT)
- streaming think scrubber (ported from hermes-agent, MIT)
- LM Studio ``reasoning_effort`` resolution
- multi tool-call parsing + native tool_calls conversion
- compact prompt mode + auto-selection heuristic
- the runtime executes ALL tool calls from one response
- the native-history loop (assistant tool_calls + role="tool" results)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_provider import FakeProvider  # noqa: E402

from tera_pilot.agent_runtime.parser import OutputParser  # noqa: E402
from tera_pilot.agent_runtime.prompts import PromptBuilder, build_native_tools_schema  # noqa: E402
from tera_pilot.agent_runtime.repetition_guard import is_repetition_dominated  # noqa: E402
from tera_pilot.providers.base import ProviderConfig, ProviderResponse  # noqa: E402
from tera_pilot.providers.lmstudio import LMStudioProvider  # noqa: E402
from tera_pilot.providers.think_scrubber import StreamingThinkScrubber  # noqa: E402


# ── 1. Repetition guard ──────────────────────────────────────────────

def test_repetition_guard_detects_dominated_text():
    rep = "This is the same fragment the model keeps echoing endlessly. " * 20
    assert is_repetition_dominated(rep)


def test_repetition_guard_fail_open():
    ok = "The agent read the file, applied the fix with str_replace, ran pytest, and all tests passed. " * 3
    assert not is_repetition_dominated(ok)
    assert not is_repetition_dominated("hi")
    assert not is_repetition_dominated("")
    assert not is_repetition_dominated(None)
    assert not is_repetition_dominated(123)


# ── 2. Streaming think scrubber ───────────────────────────────────────

def test_think_scrubber_strips_split_reasoning_block():
    s = StreamingThinkScrubber()
    parts = []
    for delta in ["<thi", "nk>Let me reason carefully</", "think>", "The answer is 42"]:
        v = s.feed(delta)
        if v:
            parts.append(v)
    out = "".join(parts) + s.flush()
    assert "Let me reason" not in out
    assert "<think>" not in out
    assert "42" in out


def test_think_scrubber_keeps_prose_mentioning_tag():
    s = StreamingThinkScrubber()
    out = ""
    for delta in ["Please use <think", "> tags per the docs.", " OK"]:
        out += s.feed(delta)
    out += s.flush()
    assert "<think>" in out


def test_think_scrubber_variants_after_newline():
    s = StreamingThinkScrubber()
    out = ""
    for delta in ["First, I'll check.\n<think", "ing>hmm</think", "ing>", "Done."]:
        out += s.feed(delta)
    out += s.flush()
    assert "hmm" not in out
    assert "Done" in out


# ── 3. LM Studio reasoning_effort ─────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("off", "none"),
    ("on", "medium"),
    ("max", "xhigh"),
    ("ultra", "xhigh"),
    ("low", "low"),
    ("medium", "medium"),
    ("bogus", None),
    ("", None),
    (None, None),
])
def test_lmstudio_reasoning_effort(raw, expected):
    extra = {"reasoning_effort": raw} if raw else {}
    cfg = ProviderConfig(provider_id="lmstudio", model="x", extra=extra)
    p = LMStudioProvider(cfg)
    assert p._resolved_reasoning_effort() == expected


def test_openai_compat_omits_effort_by_default():
    cfg = ProviderConfig(provider_id="lmstudio", model="x")
    p = LMStudioProvider(cfg)
    payload = p._build_payload([], None, None, stream=False)
    assert "reasoning_effort" not in payload


# ── 4. Multi tool-call parsing ────────────────────────────────────────

def test_parse_tool_calls_multiple():
    text = (
        '{"tool": "read_file", "args": {"path": "a.py"}}\n'
        '{"tool": "read_file", "args": {"path": "b.py"}}\n'
        '{"tool": "write_file", "args": {"path": "c.py", "content": "x"}}'
    )
    calls = OutputParser.parse_tool_calls(text)
    assert [c.name.value for c in calls] == ["read_file", "read_file", "write_file"]
    assert calls[2].args == {"path": "c.py", "content": "x"}


def test_parse_tool_calls_dedupes_consecutive_identical():
    text = (
        '{"tool": "read_file", "args": {"path": "a.py"}}\n'
        '{"tool": "read_file", "args": {"path": "a.py"}}\n'
        '{"tool": "read_file", "args": {"path": "a.py"}}'
    )
    calls = OutputParser.parse_tool_calls(text)
    assert len(calls) == 1


def test_parse_tool_calls_mcp_not_split():
    text = '{"tool": "call_mcp_tool", "args": {"server": "fs", "tool": "read_file", "args": {"path": "/tmp/x"}}}'
    calls = OutputParser.parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name.value == "call_mcp_tool"


def test_parse_tool_calls_final_answer_only():
    assert OutputParser.parse_tool_calls('{"final_answer": "done"}') == []


def test_tool_calls_from_native():
    calls = OutputParser.tool_calls_from_native([
        {"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
        {"id": "c2", "function": {"name": "write_file", "arguments": '{"path": "b.py", "content": "x"}'}},
        {"id": "c3", "function": {"name": "read_file", "arguments": "not json"}},
    ])
    assert [c.id for c in calls] == ["c1", "c2", "c3"]
    assert calls[0].name.value == "read_file" and calls[0].args == {"path": "a.py"}
    assert calls[2].args == {}  # malformed arguments degrade to {}


# ── 5. Compact mode ───────────────────────────────────────────────────

def test_compact_prompt_is_much_smaller():
    full = PromptBuilder.system()
    compact = PromptBuilder.system(compact=True)
    assert len(compact) < len(full) // 2
    assert "tool_list" not in compact  # placeholders resolved


def test_compact_schema_has_fewer_tools():
    full = build_native_tools_schema()
    compact = build_native_tools_schema(compact=True)
    assert len(compact) < len(full)
    names = {t["function"]["name"] for t in compact}
    assert {"read_file", "write_file", "str_replace"} <= names
    assert "call_mcp_tool" not in names


def test_compact_heuristic_by_model_size():
    from tera_pilot.agent_runtime.runtime import AgentRuntime

    class _Cfg:
        model = "lfm2.5-2.6b-heretic-abliterated"

    class _Prov:
        config = _Cfg()

    class _Reg:
        active = _Prov()

    rt = object.__new__(AgentRuntime)
    rt._compact_override = None
    rt._registry = _Reg()
    assert rt._use_compact_prompt() is True

    _Cfg.model = "qwen/qwen3.6-27b"
    assert rt._use_compact_prompt() is False

    _Cfg.model = "gpt-4o"
    assert rt._use_compact_prompt() is False


# ── 6. Runtime integration: multi-call + native history ───────────────

@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tera_pilot.providers import get_registry
    r = get_registry()
    r.register(FakeProvider)  # idempotent
    yield r
    r.set_active("ollama")
    r._instances.pop("fake", None)


def _runtime(reg, tmp_path, fp):
    from tera_pilot.agent_runtime.runtime import AgentRuntime
    reg._instances["fake"] = fp
    reg.set_active("fake")
    rt = AgentRuntime(registry=reg, workspace=str(tmp_path),
                      max_iterations=6, enable_planning=False)
    rt.tools.diff_review_enabled = False
    return rt


def test_runtime_executes_all_tool_calls_from_one_response(reg, tmp_path):
    (tmp_path / "a.txt").write_text("AAA")
    (tmp_path / "b.txt").write_text("BBB")
    fp = FakeProvider(script=[
        FakeProvider.tool_call("read_file", {"path": "a.txt"}) + "\n"
        + FakeProvider.tool_call("read_file", {"path": "b.txt"}),
        FakeProvider.final_answer("done"),
    ])
    rt = _runtime(reg, tmp_path, fp)
    result = rt.run("read both files", max_iterations=4)
    assert result.success, result.output
    # Two LLM calls: the plan (both reads) + the final answer.
    assert fp.call_count == 2
    # The second call's history must contain BOTH observations.
    second_user = fp.recorded_messages[1][1].content
    assert "AAA" in second_user
    assert "BBB" in second_user


class _NativeFake(FakeProvider):
    """FakeProvider that returns API-level tool_calls alongside text.

    ``generate`` accepts the ``tools`` kwarg (like real OpenAI-compatible
    providers) so the runtime's native-tool detection activates.
    """

    def __init__(self, script, native_script):
        super().__init__(script=script)
        self._native = list(native_script)

    def generate(self, messages, model=None, tools=None):
        self._record(messages)
        text = self._next_text()
        tc = self._native.pop(0) if self._native else None
        return ProviderResponse(
            text=text,
            model=model or self.config.model,
            provider=self.provider_id,
            tokens_in=10,
            tokens_out=max(1, len(text) // 4),
            tool_calls=tc,
        )


def test_runtime_native_history_loop(reg, tmp_path):
    (tmp_path / "a.txt").write_text("AAA")
    fp = _NativeFake(
        script=[
            "",
            "",
            FakeProvider.final_answer("created b.txt"),
        ],
        native_script=[
            [{"id": "call_a", "type": "function",
              "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}],
            [{"id": "call_b", "type": "function",
              "function": {"name": "write_file",
                           "arguments": json.dumps({"path": "b.txt", "content": "BBB"})}}],
            None,
        ],
    )
    rt = _runtime(reg, tmp_path, fp)
    result = rt.run("create b.txt with BBB")
    assert result.success, result.output
    assert (tmp_path / "b.txt").read_text() == "BBB"
    # Native history: call 2 must include the tool result as role="tool"
    # with the matching tool_call_id.
    msgs = fp.recorded_messages[1]
    roles = [m.role for m in msgs]
    assert "tool" in roles
    tool_msg = next(m for m in msgs if m.role == "tool")
    assert tool_msg.tool_call_id == "call_a"
    assert "AAA" in tool_msg.content
