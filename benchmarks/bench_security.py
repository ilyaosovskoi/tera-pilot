#!/usr/bin/env python3
"""Security-path microbenchmarks — Tera Pilot (locally, no network).

Measures the cost of the security controls that run on EVERY agent
action / every command / every file operation:

  - _sanitize_command (safe and blocked) — every execute_command
  - _validate_command_paths (valid + escaping) — every path-taking command
  - _resolve_path — every file read/write/delete/rename/mkdir
  - CommandPolicy.is_allowed — every execute_command (policy lookup)
  - constant-time token compare (compare_digest) vs naive == — why the
    API auth uses compare_digest
  - EncryptedPromptStore encrypt/decrypt — prompt-at-rest confidentiality
  - git_neutralization_args — every agent `git ...` invocation (v2.3.4)

Run:  python3 benchmarks/bench_security.py
"""

from __future__ import annotations

import logging
import secrets
import sys
import time
from pathlib import Path

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ROWS: list[dict] = []


def _timeit(fn, n: int) -> float:
    fn(10)  # warm-up (lazy imports, caches)
    start = time.perf_counter()
    fn(n)
    elapsed = time.perf_counter() - start
    return (elapsed / n) * 1e6


def add_row(name: str, n: int, us: float) -> None:
    ROWS.append({"name": name, "n": n, "us": us})


def bench_sanitize_command() -> None:
    from tera_pilot.agent_runtime._helpers import _sanitize_command

    n = 50_000
    safe = "git status --short"

    def run_safe(count: int) -> None:
        for _ in range(count):
            _sanitize_command(safe)

    def run_blocked(count: int) -> None:
        for _ in range(count):
            _sanitize_command("python3 -c 'import os; os.system(\"x\")'")

    add_row("_sanitize_command (allowed)", n, _timeit(run_safe, n))
    add_row("_sanitize_command (blocked, dangerous flag)", n, _timeit(run_blocked, n))


def bench_command_paths() -> None:
    from tera_pilot.agent_runtime.tool_engine import ToolEngine

    n = 20_000
    e = ToolEngine(str(PROJECT_ROOT))

    def run_valid(count: int) -> None:
        for _ in range(count):
            e._validate_command_paths(["cat", "tera_pilot/__init__.py"])

    def run_escape(count: int) -> None:
        for _ in range(count):
            e._validate_command_paths(["cat", "/etc/passwd"])

    add_row("_validate_command_paths (inside)", n, _timeit(run_valid, n))
    add_row("_validate_command_paths (escaping)", n, _timeit(run_escape, n))


def bench_resolve_path() -> None:
    from tera_pilot.agent_runtime.tool_engine import ToolEngine

    n = 20_000
    e = ToolEngine(str(PROJECT_ROOT))

    def run(count: int) -> None:
        for _ in range(count):
            e._resolve_path("tera_pilot/__init__.py")

    add_row("_resolve_path (workspace sandbox)", n, _timeit(run, n))


def bench_policy_lookup() -> None:
    from tera_pilot.command_policy import get_global_policy

    n = 50_000
    policy = get_global_policy()

    def run(count: int) -> None:
        for _ in range(count):
            policy.is_allowed("git")
            policy.is_allowed("curl")

    add_row("command_policy.is_allowed (2 lookups)", n, _timeit(run, n))


def bench_token_compare() -> None:
    """Demonstrates WHY the API uses secrets.compare_digest: naive '=='
    short-circuits on the first differing byte (timing side-channel),
    compare_digest always compares everything."""
    n = 50_000
    a = secrets.token_urlsafe(32)
    b = a[:-1] + ("A" if a[-1] != "A" else "B")

    def run_naive(count: int) -> None:
        for _ in range(count):
            a == b  # noqa: B015

    def run_digest(count: int) -> None:
        for _ in range(count):
            secrets.compare_digest(a, b)

    add_row("token compare: naive '==' (wrong token)", n, _timeit(run_naive, n))
    add_row("token compare: compare_digest (wrong token)", n, _timeit(run_digest, n))


def bench_git_neutralization() -> None:
    """v2.3.4: cost of neutralizing repo-supplied git exec keys/hooks on
    every agent git invocation (config scan + -c flag list)."""
    from tera_pilot.git_service import git_neutralization_args

    n = 20_000
    # PROJECT_ROOT is a real git repo with a small .git/config.
    root = PROJECT_ROOT

    def run(count: int) -> None:
        for _ in range(count):
            git_neutralization_args(root)

    add_row("git_neutralization_args (repo config scan)", n, _timeit(run, n))


def bench_encrypted_prompt() -> None:
    from tera_pilot.agent.encrypted_prompt import EncryptedPromptStore

    n = 5_000
    store = EncryptedPromptStore(EncryptedPromptStore.generate_key())
    blob = store.encrypt("System prompt with secrets " * 20)

    def run_enc(count: int) -> None:
        for _ in range(count):
            store.encrypt("System prompt with secrets " * 20)

    def run_dec(count: int) -> None:
        for _ in range(count):
            store.decrypt(blob)

    add_row("EncryptedPromptStore.encrypt (ChaCha20-Poly1305)", n, _timeit(run_enc, n))
    add_row("EncryptedPromptStore.decrypt (ChaCha20-Poly1305)", n, _timeit(run_dec, n))


def render_table() -> None:
    print("Tera Pilot — security-path microbenchmarks")
    print("=" * 78)
    print(f"{'Operation':<52} {'ops':>9}  {'µs/op':>10}")
    print("-" * 78)
    for r in ROWS:
        print(f"{r['name']:<52} {r['n']:>9,}  {r['us']:>10.2f}")
    print("-" * 78)
    print("Local, no network. Lower = faster. See tests/test_security_suite.py")


def main() -> None:
    bench_sanitize_command()
    bench_command_paths()
    bench_resolve_path()
    bench_policy_lookup()
    bench_token_compare()
    bench_git_neutralization()
    bench_encrypted_prompt()
    render_table()


if __name__ == "__main__":
    main()
