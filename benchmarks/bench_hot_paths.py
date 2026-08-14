#!/usr/bin/env python3
"""Микро-бенчмарки горячих путей Tera Pilot (локально, без сети, без API).

Покрывает пути, которые исполняются на КАЖДОМ вызове провайдера / MCP /
на каждой файловой операции агента:

  native vs python fallback (где применимо):
    - sandbox.path_would_be_writable    — на каждую запись файла агентом
    - circuit_breaker record+try_claim  — на каждый LLM/MCP-вызов
    - interjection push+drain           — mid-turn сообщения (TUI)

  pure Python (нет native-аналога — замер абсолютной стоимости):
    - command_policy.is_allowed         — на каждый execute_command
    - _sanitize_command                 — парсинг + whitelist + dangerous flags
    - diff apply (_apply_unified_diff)  — на каждый apply_diff
    - activity_log record_tool_call     — на каждый tool-вызов (audit trail)
    - sanitise_args                     — очистка args перед записью в лог
    - compaction chunking (inter, без LLM) — оркестрация без вызова модели

Запуск:
    python3 benchmarks/bench_hot_paths.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

# Замеряем код, а не логирование: блокируем warning-спам из
# _sanitize_command и др. на время прогона.
logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Наличие native-расширения определяем один раз; при отсутствии колонка
# native показывается как "n/a" (fallback'и — легитимный режим работы).
try:
    import tera_pilot_native as native  # type: ignore
    NATIVE = True
except ImportError:
    native = None
    NATIVE = False

ROWS: list[dict] = []


def _timeit(fn, n: int) -> float:
    """Прогрев + замер; возвращает µs на операцию."""
    fn(10)  # прогрев (JIT-кэши, ленивые импорты)
    start = time.perf_counter()
    fn(n)
    elapsed = time.perf_counter() - start
    return (elapsed / n) * 1e6


def add_row(name: str, n: int, native_us, python_us) -> None:
    ratio = (python_us / native_us) if (native_us and python_us) else None
    ROWS.append({
        "name": name,
        "n": n,
        "native_us": native_us,
        "python_us": python_us,
        "ratio": ratio,
    })


# ── native vs python fallback ─────────────────────────────────────────

def bench_sandbox() -> None:
    n = 100_000
    ws = str(PROJECT_ROOT)
    if NATIVE:
        sb = native.sandbox

        def native_run(count: int) -> None:
            for i in range(count):
                sb.path_would_be_writable("workspace", ws, f"{ws}/src/file_{i % 100}.py")
                sb.path_would_be_writable("read-only", None, "/etc/passwd", [ws])

    from tera_pilot.agent import _fallback_sandbox as fsb

    def fallback_run(count: int) -> None:
        for i in range(count):
            fsb.path_would_be_writable("workspace", ws, f"{ws}/src/file_{i % 100}.py")
            fsb.path_would_be_writable("read-only", None, "/etc/passwd", [ws])

    n_us = _timeit(native_run, n) if NATIVE else None
    p_us = _timeit(fallback_run, n)
    add_row("sandbox.path_would_be_writable", n, n_us, p_us)


def bench_circuit_breaker() -> None:
    n = 5_000
    if NATIVE:
        cb = native.circuit_breaker
        nreg = cb.CircuitBreakerRegistry(window_secs=60.0)
        nb = nreg.get("bench/key")

        def native_run(count: int) -> None:
            for i in range(count):
                nb.record(ok=(i % 10 != 0), rate_limited=(i % 97 == 0))
                nb.try_claim()

    from tera_pilot.agent import _fallback_circuit_breaker as fcb
    freg = fcb.CircuitBreakerRegistry(fcb.BreakerConfig(window_secs=60.0))
    fb = freg.get("bench/key")

    def fallback_run(count: int) -> None:
        for i in range(count):
            fb.record(ok=(i % 10 != 0), rate_limited=(i % 97 == 0))
            fb.try_claim()

    n_us = _timeit(native_run, n) if NATIVE else None
    p_us = _timeit(fallback_run, n)
    add_row("circuit_breaker record+try_claim", n, n_us, p_us)


def bench_interjection() -> None:
    n = 50_000
    if NATIVE:
        ij = native.interjection
        nb = ij.InterjectionBuffer()

        def native_run(count: int) -> None:
            for i in range(count):
                nb.push(f"msg {i}")
            nb.drain()

    from tera_pilot.agent import _fallback_interjection as fij
    fb = fij.InterjectionBuffer()

    def fallback_run(count: int) -> None:
        for i in range(count):
            fb.push(f"msg {i}")
        fb.drain()

    n_us = _timeit(native_run, n) if NATIVE else None
    p_us = _timeit(fallback_run, n)
    add_row("interjection push+drain", n, n_us, p_us)


# ── pure-Python hot paths (без native-аналога) ────────────────────────

def bench_command_policy() -> None:
    from tera_pilot.command_policy import CommandPolicy, resolve

    n = 200_000
    policy = resolve(project_root=None)  # base + user config (кэшруется в get_global_policy)

    def is_allowed_run(count: int) -> None:
        for i in range(count):
            policy.is_allowed("git")
            policy.is_allowed("docker")
            policy.is_dangerous_flag("python3", "-c")

    us = _timeit(is_allowed_run, n)
    add_row("command_policy is_allowed/is_dangerous_flag", n, None, us)

    # Отдельно: повторный resolve() (ре-резолв после invalidate) —
    # читает конфиг с диска, это реальная цена смены проекта.
    n_resolve = 2_000
    us = _timeit(lambda c: [resolve(project_root=None) for _ in range(c)], n_resolve)
    add_row("command_policy resolve() (с чтением конфига)", n_resolve, None, us)


def bench_sanitize_command() -> None:
    from tera_pilot.agent_runtime._helpers import _sanitize_command

    n = 20_000
    safe = "pytest tests/ -x -q"
    blocked = "python3 -c 'import os; os.system(\"id\")'"

    def run(count: int) -> None:
        for _ in range(count):
            _sanitize_command(safe, project_root=str(PROJECT_ROOT))
            _sanitize_command(blocked, project_root=str(PROJECT_ROOT))

    us = _timeit(run, n)
    add_row("_sanitize_command (safe+blocked)", n, None, us)


def bench_diff_apply() -> None:
    from tera_pilot.agent_runtime.diff_utils import _apply_unified_diff, _compute_diff_text

    n = 10_000
    original = "\n".join(f"line {i}" for i in range(200))
    proposed = original.replace("line 100", "line 100 CHANGED").replace("line 42", "line 42 CHANGED")
    diff = _compute_diff_text("f.py", original, proposed)

    def run(count: int) -> None:
        for _ in range(count):
            _apply_unified_diff(original, diff)

    us = _timeit(run, n)
    add_row("diff apply (_apply_unified_diff, 2 хунка)", n, None, us)


def bench_activity_log() -> None:
    from tera_pilot.activity_log import ActivityLog, sanitise_args

    log = ActivityLog(max_entries=2000)
    n = 20_000

    def record_run(count: int) -> None:
        for i in range(count):
            log.record_tool_call(
                tool="write_file",
                args={"path": f"src/file_{i % 100}.py",
                      "content": "x" * 500},
                result=f"[WRITTEN] src/file_{i % 100}.py (500 chars)",
            )

    us = _timeit(record_run, n)
    add_row("activity_log.record_tool_call (audit)", n, None, us)

    n_args = 50_000
    big_args = {
        "path": "src/app.py",
        "content": "y" * 2000,
        "command": "pytest -x tests/ -q",
        "nested": {"a": "b", "list": ["x", "y"] * 5},
    }

    def sanitise_run(count: int) -> None:
        for _ in range(count):
            sanitise_args(big_args)

    us = _timeit(sanitise_run, n_args)
    add_row("activity_log.sanitise_args", n_args, None, us)


def bench_compaction_chunking() -> None:
    """Оркестрация inter-компакции БЕЗ вызова LLM (дешёвый sampler)."""
    from tera_pilot.agent._fallback_compaction import CompactionEngine, ConversationItem

    n = 2_000
    items = [
        ConversationItem(role="user" if i % 2 else "assistant", content=f"message {i}", tokens=10)
        for i in range(200)
    ]
    engine = CompactionEngine(lambda prompt, chunk: "summary")

    def run(count: int) -> None:
        for _ in range(count):
            engine.inter_compact(items, chunk_size=10, keep_recent=6)

    us = _timeit(run, n)
    add_row("compaction inter_compact (chunking, без LLM)", n, None, us)


# ── вывод ──────────────────────────────────────────────────────────────

def render_table() -> None:
    print("Tera Pilot — горячие пути (hot-path microbenchmarks)")
    print("=" * 92)
    hdr = f"{'Операция':<44} {'ops':>8}  {'native, µs/op':>14} {'python, µs/op':>14} {'выигрыш':>9}"
    print(hdr)
    print("-" * 92)
    for r in ROWS:
        native_cell = f"{r['native_us']:>12.2f}" if r["native_us"] is not None else f"{'n/a':>12}"
        ratio_cell = (
            f"{r['ratio']:>7.1f}x" if r["ratio"] is not None
            else (f"{'—':>9}" if r["native_us"] is None else "")
        )
        print(
            f"{r['name']:<44} {r['n']:>8,} {native_cell:>14} {r['python_us']:>14.2f} {ratio_cell:>9}"
        )
    print("-" * 92)
    print(
        "native = Rust (tera_pilot_native), python = pure-Python fallback/реализация.\n"
        "pure-Python строки (n/a) — нет native-аналога, это абсолютная стоимость пути."
    )
    if not NATIVE:
        print("\n⚠ tera_pilot_native не установлен — колонка native пустая (n/a).")


def main() -> None:
    bench_sandbox()
    bench_circuit_breaker()
    bench_interjection()
    bench_command_policy()
    bench_sanitize_command()
    bench_diff_apply()
    bench_activity_log()
    bench_compaction_chunking()
    render_table()


if __name__ == "__main__":
    main()
