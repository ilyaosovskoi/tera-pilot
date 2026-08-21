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


def test_parse_lfm_native_tool_calls_single():
    text = (
        "<|tool_call_start|>[read_file(path='db.py')]<|tool_call_end|>"
    )
    calls = OutputParser.parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name.value == "read_file"
    assert calls[0].args == {"path": "db.py"}


def test_parse_lfm_native_multiple_calls_with_escapes():
    # Raw triple-quoted so the LFM escapes are spelled exactly as the
    # model emits them: \' (single quote), \" (double quote), \n.
    text = r'''Let me read the file first.
<|tool_call_start|>[read_file(path='db.py')]<|tool_call_end|>
Then I will write the review.
<|tool_call_start|>[write_file(path='REVIEW.md', content='# Review\n\nThe line is: query = f\"SELECT * FROM users WHERE username = \'{username}\'\"\n')]<|tool_call_end|>
Done.'''
    calls = OutputParser.parse_tool_calls(text)
    assert [c.name.value for c in calls] == ["read_file", "write_file"]
    content = calls[1].args["content"]
    # Escapes are unescaped: \' -> ', \" -> ", \n -> newline.
    assert "query = f\"SELECT * FROM users WHERE username = '{username}'\"" in content
    assert "\n" in content


def test_parse_lfm_positional_args_map_by_order():
    # The model sometimes emits positional args; they map to the tool's
    # documented parameter order (_TOOL_ARGS).
    text = "<|tool_call_start|>[write_file('x.py', 'content here')]<|tool_call_end|>"
    calls = OutputParser.parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].args == {"path": "x.py", "content": "content here"}


def test_parse_lfm_unknown_tool_name_skipped():
    text = "<|tool_call_start|>[read_tool(path='x')]<|tool_call_end|>"
    assert OutputParser.parse_tool_calls(text) == []


def test_parse_lfm_dedupes_consecutive_identical():
    text = (
        "<|tool_call_start|>[read_file(path='a.py')]<|tool_call_end|>\n"
        "<|tool_call_start|>[read_file(path='a.py')]<|tool_call_end|>"
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
    # P0.4: fail-closed default would block scripted write_file tool calls
    # in these runtime tests — opt in to headless auto-approve.
    rt.tools.headless_confirm = "allow"
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


def test_runtime_native_malformed_arguments_do_not_poison_history(reg, tmp_path):
    """v2.3.5-fix (LM Studio / LFM 2.5): a model sometimes emits a native
    tool call whose arguments string is NOT valid JSON — an unescaped
    quote inside ``content`` (e.g. ``query = f"SELECT ...``). LM Studio's
    engine re-validates the assistant tool_calls message on the NEXT
    request and hard-400s the whole run ("Invalid diff: '…' not found at
    start of '…'", server_error 500). The runtime must never echo the
    raw arguments back into the conversation: every tool_call appended to
    the native history is rebuilt from the PARSED dict, so the next
    request always carries well-formed JSON.
    """
    (tmp_path / "a.txt").write_text("AAA")
    malformed_args = '{"path": "b.txt", "content": "query = f"SELECT *"}'  # broken JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(malformed_args)
    fp = _NativeFake(
        script=["", FakeProvider.final_answer("done")],
        native_script=[
            [{"id": "call_bad", "type": "function",
              "function": {"name": "write_file", "arguments": malformed_args}}],
            None,
        ],
    )
    rt = _runtime(reg, tmp_path, fp)
    result = rt.run("write b.txt")
    # The run must NOT die with a provider 400 — the malformed call
    # becomes a normal tool error and the agent still finishes.
    assert result.success, result.output
    assert fp.call_count == 2
    # The assistant tool_calls message in the final request has VALID,
    # freshly-serialized JSON arguments (never the raw broken string).
    msgs = fp.recorded_messages[1]
    asst = next(m for m in msgs if m.role == "assistant" and m.tool_calls)
    for tc in asst.tool_calls:
        assert json.loads(tc["function"]["arguments"]) is not None
    # The tool_call_id still matches the follow-up tool message.
    tool_msg = next(m for m in msgs if m.role == "tool")
    assert tool_msg.tool_call_id == "call_bad"


# ── 7. Workspace/memory contamination (P1.7) ──────────────────────────

def test_workspace_switch_does_not_leak_previous_workspace(reg, tmp_path):
    """P1.7: the runtime is a process-wide singleton reused across HTTP
    requests. Switching to a DIFFERENT workspace must clear conversation
    memory + task history, so the next task never sees the previous
    workspace's tool observations (observed in eval: the fix-config-loader
    task tried to read discount.py from an earlier task's workspace)."""
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()
    (ws_a / "discount.py").write_text("def apply_discount(price, pct): return price", encoding="utf-8")
    (ws_b / "config_loader.py").write_text("def load(path): return {}", encoding="utf-8")

    fp = FakeProvider(script=[
        FakeProvider.tool_call("read_file", {"path": "discount.py"}),
        FakeProvider.final_answer("discount.py summary (workspace A)"),
    ])
    rt = _runtime(reg, ws_a, fp)
    res1 = rt.run("Summarize discount.py")
    assert res1.success, res1.output
    # The first run's observations live in memory, with workspace A paths.
    assert "discount.py" in rt.memory.to_prompt_history()

    # Switch to workspace B — memory + history must be cleared.
    rt.set_workspace(str(ws_b))
    assert len(rt.memory.messages) == 0, "memory must be cleared on workspace switch"
    assert len(rt.task_history) == 0, "task history must be cleared on workspace switch"

    fp._script = [
        FakeProvider.tool_call("read_file", {"path": "config_loader.py"}),
        FakeProvider.final_answer("config_loader.py summary (workspace B)"),
    ]
    # Only inspect the SECOND run's LLM messages (recorded_messages
    # accumulates across both runs; run 1 legitimately mentions A).
    run2_start = len(fp.recorded_messages)
    res2 = rt.run("Summarize config_loader.py")
    assert res2.success, res2.output
    run2_msgs = " ".join(
        m.content for call in fp.recorded_messages[run2_start:]
        for m in call if getattr(m, "content", None)
    )
    # NB: "discount.py" itself appears in the static system prompt as an
    # example path, so assert on workspace-A's UNIQUE content + dir name.
    assert "apply_discount(price, pct)" not in run2_msgs, "workspace A leaked into workspace B run"
    assert "ws_a" not in run2_msgs
    assert "config_loader.py" in run2_msgs


# ── v2.3.4: no retry over a live stream ──────────────────────────────
# A stream that already emitted chunks must NEVER be restarted — the
# partial text was already relayed to the UI sink, so a retry would
# re-deliver the same tokens (duplicated output, garbled response).

class _FlakyStreamProvider(FakeProvider):
    """FakeProvider whose ``stream()`` yields ``fail_after_chunks`` chunks
    and then raises a retryable transient error."""

    def __init__(self, script, fail_after_chunks=0, error=None):
        super().__init__(script=script)
        self.fail_after_chunks = fail_after_chunks
        self.error = error or RuntimeError("HTTP 503 Service Unavailable")
        self.stream_calls = 0

    def stream(self, messages, model=None):
        self.stream_calls += 1
        self._record(messages)
        text = self._next_text()
        chunks = [text[i:i + 5] for i in range(0, len(text), 5)] or [""]
        for idx, chunk in enumerate(chunks):
            if idx >= self.fail_after_chunks:
                raise self.error
            yield chunk


def test_stream_not_retried_after_partial_output(reg, tmp_path):
    """A stream that emitted chunks then hit a transient error must fail
    immediately — the retry loop must NOT restart it (no duplicated
    tokens in the UI)."""
    fp = _FlakyStreamProvider(
        script=["hello world partial output"],
        fail_after_chunks=2,
        error=RuntimeError("HTTP 503 Service Unavailable"),
    )
    rt = _runtime(reg, tmp_path, fp)
    deltas: list = []
    rt._on_token_delta = deltas.append
    with pytest.raises(RuntimeError):
        rt._generate_streaming_with_retry(fp, [])
    assert fp.stream_calls == 1, "stream must not be restarted after partial output"
    assert "".join(deltas) == "hello worl", "only the pre-failure deltas may reach the sink"


def test_stream_retried_when_nothing_emitted(reg, tmp_path):
    """A stream that fails BEFORE its first chunk is safe to retry —
    nothing was relayed, so a retry cannot duplicate output."""
    fp = _FlakyStreamProvider(
        script=["ok"],
        fail_after_chunks=0,
        error=RuntimeError("HTTP 503 Service Unavailable"),
    )
    rt = _runtime(reg, tmp_path, fp)
    rt._RETRY_MAX_ATTEMPTS = 2  # keep the backoff wait short
    rt._RETRY_QUOTA_MAX_ATTEMPTS = 2
    deltas: list = []
    rt._on_token_delta = deltas.append
    with pytest.raises(RuntimeError):
        rt._generate_streaming_with_retry(fp, [])
    assert fp.stream_calls > 1, "a pre-first-chunk failure should be retried"
    assert deltas == [], "no chunk may reach the sink before the retry"


def test_same_workspace_keeps_conversation_history(reg, tmp_path):
    """P1.7: calling set_workspace with the SAME workspace (as the HTTP
    path does before every request) must NOT clear the conversation — a
    same-workspace call is a continuing chat."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x", encoding="utf-8")
    fp = FakeProvider(script=[FakeProvider.final_answer("first answer")])
    rt = _runtime(reg, ws, fp)
    rt.run("first task")
    assert len(rt.memory.messages) > 0

    rt.set_workspace(str(ws))  # same workspace — continuing chat
    assert len(rt.memory.messages) > 0, "same-workspace set_workspace must not clear memory"
