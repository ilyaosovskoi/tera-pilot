"""Tests for the Rust native extension `tera_pilot_native`.

Skipped automatically when the extension is not built — the pure-Python
fallbacks then take over and the rest of the suite must stay green.
"""

import time

import pytest

native = pytest.importorskip("tera_pilot_native")


# ── Module surface ────────────────────────────────────────────────────


def test_module_exposes_submodules_and_version():
    assert native.__version__
    for name in ("sandbox", "circuit_breaker", "interjection", "compaction", "actor"):
        assert hasattr(native, name), f"missing submodule {name}"


# ── sandbox ───────────────────────────────────────────────────────────


def test_sandbox_profile_lifecycle(tmp_path):
    sb = native.sandbox
    assert sb.current_profile() is None
    sb.apply_profile(profile="workspace", workspace_root=str(tmp_path))
    assert sb.current_profile() == "workspace"
    assert "workspace" in sb.describe_state()
    # повторное применение (не-off) запрещено
    with pytest.raises(RuntimeError):
        sb.apply_profile(profile="workspace", workspace_root=str(tmp_path))
    with pytest.raises(ValueError):
        sb.apply_profile(profile="bogus")
    assert sb.supported_platform() is False  # kernel enforcement not wired yet


def test_sandbox_path_would_be_writable(tmp_path):
    sb = native.sandbox
    ws = str(tmp_path)
    # новый (несуществующий) файл внутри workspace — writable (resolve-семантика)
    assert sb.path_would_be_writable("workspace", ws, str(tmp_path / "new_file.py")) is True
    assert sb.path_would_be_writable("workspace", ws, str(tmp_path / "sub" / "deep.py")) is True
    # снаружи — нет
    assert sb.path_would_be_writable("workspace", ws, "/etc/passwd") is False
    # read-only: только явные extra_readwrite_paths
    assert sb.path_would_be_writable("read-only", None, str(tmp_path / "x.txt"), [ws]) is True
    assert sb.path_would_be_writable("read-only", None, "/etc/passwd", [ws]) is False
    # strict == read-only для fs
    assert sb.path_would_be_writable("strict", None, str(tmp_path / "x.txt"), [ws]) is True
    # ".." не должен позволять выйти за пределы workspace (normalize-семантика)
    assert sb.path_would_be_writable("workspace", ws, f"{ws}/sub/../../etc/passwd") is False
    # ".." внутри workspace остаётся внутри
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert sb.path_would_be_writable("workspace", ws, f"{ws}/a/b/../c.py") is True


def test_sandbox_symlink_dotdot_parity(tmp_path):
    """`..` в сочетании с симлинками обязан совпадать с Python `Path.resolve()`.

    Регрессия: первая версия Rust-порта лексически схлопывала `..` до
    резолва симлинков, и `<ws>/link/../x.py` с симлинком `link` наружу
    ошибочно считался writable (Python — нет).
    """
    from tera_pilot.agent import _fallback_sandbox as fsb

    sb = native.sandbox
    ws = str(tmp_path)
    sibling = tmp_path.parent / "sibling_outside"
    sibling.mkdir(exist_ok=True)
    (tmp_path / "sub").mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(sibling)  # симлинк наружу workspace
    (tmp_path / "real").mkdir(exist_ok=True)
    (tmp_path / "linkin").symlink_to(tmp_path / "real")  # симлинк внутрь

    cases = [
        f"{ws}/sub/../../etc/passwd",
        f"{ws}/../outside.txt",
        f"{ws}/link/../x.py",  # наружу + `..` — эскейп
        f"{ws}/link/../../etc/passwd",
        f"{ws}/linkin/../x.py",  # внутрь + `..` — внутри
        f"{ws}/linkin/deep/../x.py",
        "/etc/passwd",
    ]
    for c in cases:
        nv = sb.path_would_be_writable("workspace", ws, c)
        pv = fsb.path_would_be_writable("workspace", ws, c)
        assert nv == pv, f"parity mismatch for {c}: native={nv} python={pv}"

    # Явные security-инварианты
    assert sb.path_would_be_writable("workspace", ws, f"{ws}/link/../x.py") is False
    assert sb.path_would_be_writable("workspace", ws, f"{ws}/sub/../../etc/passwd") is False


# ── circuit_breaker ───────────────────────────────────────────────────


def test_circuit_breaker_transitions_and_metrics():
    cb = native.circuit_breaker
    reg = cb.CircuitBreakerRegistry(
        min_samples=3, error_rate_threshold=0.5, window_secs=60.0, open_duration_secs=0.05
    )
    b = reg.get("openai/gpt-4o")
    assert b.key == "openai/gpt-4o"
    assert b.state == "closed" and not b.is_open

    for _ in range(3):
        b.record(ok=False)
    assert b.state == "open" and b.is_open
    assert b.try_claim() is False

    m = dict(b.metrics())
    for key in (
        "key", "state", "window_samples", "window_successes", "window_failures",
        "window_rate_limited", "lifetime_success", "lifetime_failure",
        "lifetime_rate_limited", "generation",
    ):
        assert key in m
    assert m["lifetime_failure"] == 3

    # ошибок меньше порога — не открывается (rate 2/5 = 0.4 < 0.5)
    reg2 = cb.CircuitBreakerRegistry(min_samples=5, error_rate_threshold=0.5)
    b2 = reg2.get("k")
    for _ in range(3):
        b2.record(ok=True)
    for _ in range(2):
        b2.record(ok=False)
    assert b2.state == "closed"

    assert cb.retry_disposition_server(429) == "retryable"
    assert cb.retry_disposition_server(503) == "retryable"
    assert cb.retry_disposition_server(401) == "auth_refresh"
    assert cb.retry_disposition_server(200) == "terminal"
    assert cb.retry_disposition_client_storage(404) == "terminal"
    assert cb.retry_disposition_client_storage(500) == "retryable"


# ── interjection ──────────────────────────────────────────────────────


def test_interjection_buffer_roundtrip():
    ij = native.interjection.InterjectionBuffer()
    i1 = ij.push("hello")
    i2 = ij.push("x" * 30_000, "att")
    assert i2 == i1 + 1
    assert ij.pending_count() == 2

    entries = ij.drain()
    assert len(entries) == 2
    assert entries[0]["id"] == i1
    assert entries[0]["raw_text"] == "hello"
    assert entries[0]["truncated"] is False
    assert entries[1]["truncated"] is True
    assert entries[1]["attachment"] == "att"
    assert ij.pending_count() == 0

    ij.push("a")
    ij.push("b")
    formatted = ij.drain_formatted()
    assert formatted is not None
    assert "The user sent a message while you were working:" in formatted
    assert "a" in formatted and "b" in formatted
    assert ij.drain_formatted() is None  # пусто

    assert "[truncated" in native.interjection.render_entry("y", True)


# ── compaction ────────────────────────────────────────────────────────


def _sample_items(comp, n=20):
    return [comp.ConversationItem(role="user", content=f"msg {i}", tokens=10) for i in range(n)]


def test_compaction_engine_with_python_sampler():
    comp = native.compaction
    calls = []

    def sampler(prompt, items):
        calls.append((prompt, [it.role for it in items], [it.tokens for it in items]))
        return "SUMMARY"

    engine = comp.CompactionEngine(sampler)

    items = _sample_items(comp)
    s, fresh = engine.code_compact(items)
    assert s == "SUMMARY"
    assert fresh[0].role == "system" and "[CONVERSATION SUMMARY]" in fresh[0].content
    assert len(calls) == 1

    s, fresh = engine.intra_compact(items, keep_recent=4)
    assert len(fresh) == 5
    assert fresh[0].role == "system" and "[PREVIOUS TURN SUMMARY]" in fresh[0].content

    s, fresh = engine.inter_compact(items, chunk_size=5, keep_recent=4)
    assert len(fresh) == 5
    assert "---" in s
    assert "[CONVERSATION HISTORY SUMMARY]" in fresh[0].content
    assert len(calls) == 1 + 1 + 4  # code + intra + 16/5=4 чанка


def test_compaction_engine_errors():
    comp = native.compaction
    engine = comp.CompactionEngine(lambda prompt, items: "S")
    with pytest.raises(ValueError):
        engine.code_compact([])
    with pytest.raises(ValueError):
        engine.intra_compact(_sample_items(comp, 3), keep_recent=6)
    with pytest.raises(ValueError):
        engine.inter_compact(_sample_items(comp, 8), chunk_size=10, keep_recent=6)


# ── actor ─────────────────────────────────────────────────────────────


def test_cancel_token_semantics():
    act = native.actor
    t = act.CancelToken()
    assert not t.is_cancelled()
    assert t.reason is None
    t.cancel("stop")
    assert t.is_cancelled()
    assert t.reason == "stop"
    t.cancel("other")  # первый reason побеждает
    assert t.reason == "stop"


def test_cancel_token_parent_child_propagation():
    act = native.actor
    parent = act.CancelToken()
    child = parent.child()
    parent.cancel("parent cancelled")
    assert child.is_cancelled()

    # ребёнок уже-отменённого родителя сразу отменён
    dead_parent = act.CancelToken()
    dead_parent.cancel("x")
    assert dead_parent.child().is_cancelled()

    # независимая отмена ребёнка не трогает родителя
    p2 = act.CancelToken()
    c2 = p2.child()
    c2.cancel("child only")
    assert c2.is_cancelled() and not p2.is_cancelled()


# ── скорость (smoke) ─────────────────────────────────────────────────


def test_native_breaker_fast_smoke():
    """Санитарная проверка: 5k записей за разумное время (< 1 c)."""
    cb = native.circuit_breaker
    reg = cb.CircuitBreakerRegistry(window_secs=60.0)
    b = reg.get("bench")
    start = time.perf_counter()
    for i in range(5_000):
        b.record(ok=(i % 10 != 0))
        b.try_claim()
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"native breaker too slow: {elapsed:.3f}s"
