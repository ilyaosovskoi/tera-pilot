#!/usr/bin/env python3
"""Микро-бенчмарк native (Rust) vs fallback (pure Python).

Замеряет реальный выигрыш `tera_pilot_native` на горячих путях:

- circuit_breaker: record + try_claim на каждый LLM/MCP-вызов
  (Python — O(window) сканы под threading.Lock; Rust — O(1) amortized);
- sandbox.path_would_be_writable: на каждую запись файла агентом
  (Python — Path.resolve + is_relative_to; Rust — canonicalize + prefix);
- interjection: push + drain (буфер mid-turn сообщений).

Запуск:  python3 benchmarks/bench_native.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Скрипт лежит в benchmarks/, а пакет tera_pilot — в корне репозитория.
sys.path.insert(0, str(PROJECT_ROOT))


def _bench(label, fn, n):
    # прогрев
    fn(1)
    start = time.perf_counter()
    fn(n)
    elapsed = time.perf_counter() - start
    per_op = elapsed / n
    print(f"  {label:<46} {n:>9,} ops  {elapsed:>8.3f}s  {per_op*1e6:>9.1f} µs/op")
    return per_op


def bench_circuit_breaker():
    print("\nCircuit breaker (record + try_claim, window=60s):")
    n = 5_000

    # ── native ──
    import tera_pilot_native as native
    nreg = native.circuit_breaker.CircuitBreakerRegistry(window_secs=60.0)
    nb = nreg.get("bench/key")

    def native_run(count):
        for i in range(count):
            nb.record(ok=(i % 10 != 0), rate_limited=(i % 97 == 0))
            nb.try_claim()

    native_per_op = _bench("native (Rust)", native_run, n)

    # ── fallback ──
    from tera_pilot.agent import _fallback_circuit_breaker as fcb
    freg = fcb.CircuitBreakerRegistry(fcb.BreakerConfig(window_secs=60.0))
    fb = freg.get("bench/key")

    def fallback_run(count):
        for i in range(count):
            fb.record(ok=(i % 10 != 0), rate_limited=(i % 97 == 0))
            fb.try_claim()

    fallback_per_op = _bench("fallback (pure Python)", fallback_run, n)

    ratio = fallback_per_op / native_per_op
    print(f"  → Rust быстрее в {ratio:.1f}x")
    return ratio


def bench_sandbox_checks():
    print("\nSandbox path_would_be_writable (против реального workspace):")
    n = 100_000
    ws = str(PROJECT_ROOT)

    import tera_pilot_native as native
    sb = native.sandbox

    def native_run(count):
        for i in range(count):
            sb.path_would_be_writable("workspace", ws, f"{ws}/src/file_{i % 100}.py")
            sb.path_would_be_writable("read-only", None, "/etc/passwd", [ws])

    native_per_op = _bench("native (Rust)", native_run, n)

    from tera_pilot.agent import _fallback_sandbox as fsb

    def fallback_run(count):
        for i in range(count):
            fsb.path_would_be_writable("workspace", ws, f"{ws}/src/file_{i % 100}.py")
            fsb.path_would_be_writable("read-only", None, "/etc/passwd", [ws])

    fallback_per_op = _bench("fallback (pure Python)", fallback_run, n)

    ratio = fallback_per_op / native_per_op
    print(f"  → Rust быстрее в {ratio:.1f}x")
    return ratio


def bench_interjection():
    print("\nInterjection (push + drain):")
    n = 50_000

    import tera_pilot_native as native
    native_buf = native.interjection.InterjectionBuffer()

    def native_run(count):
        for i in range(count):
            native_buf.push(f"msg {i}")
        native_buf.drain()

    native_per_op = _bench("native (Rust)", native_run, n)

    from tera_pilot.agent import _fallback_interjection as fij
    fb_buf = fij.InterjectionBuffer()

    def fallback_run(count):
        for i in range(count):
            fb_buf.push(f"msg {i}")
        fb_buf.drain()

    fallback_per_op = _bench("fallback (pure Python)", fallback_run, n)

    ratio = fallback_per_op / native_per_op
    print(f"  → Rust быстрее в {ratio:.1f}x")
    return ratio


def main() -> None:
    print("Tera Pilot — native vs fallback benchmark")
    print("=" * 68)
    try:
        import tera_pilot_native  # noqa: F401
    except ImportError:
        print("tera_pilot_native не установлен. Соберите: cd tera-pilot-native/pyo3 && maturin build --release")
        return
    bench_circuit_breaker()
    bench_sandbox_checks()
    bench_interjection()


if __name__ == "__main__":
    main()
