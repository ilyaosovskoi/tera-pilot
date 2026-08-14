# tera-pilot-native

Rust acceleration for Tera Pilot (PyO3 extension module `tera_pilot_native`).

Subsystems moved to Rust (mirroring the pure-Python fallbacks from
`tera_pilot/agent/_fallback_*`, but without GIL overhead):

- `sandbox` — profile state machine + fast advisory path checks
  (`path_would_be_writable`, component-wise comparison of canonical paths);
- `circuit_breaker` — sliding error window for every LLM/MCP call,
  O(1) amortized instead of O(window) in Python;
- `interjection` — thread-safe FIFO buffer for mid-turn messages;
- `compaction` — three-tier compaction engine (code/intra/inter) with a
  callback into the Python sampler;
- `actor` — `CancelToken` with parent-to-child cancellation propagation
  through atomic flags.

## Build

In a virtual environment:

```bash
cd tera-pilot-native/pyo3
maturin develop --release
```

Without a venv (as in this repository):

```bash
cd tera-pilot-native
maturin build --release --manifest-path pyo3/Cargo.toml
python3 -m pip install --force-reinstall target/wheels/*.whl
```

Without building, Tera Pilot runs on pure-Python fallbacks (slower).

## Test

```bash
cd /path/to/repo
python3 -m pytest tests/test_native.py -q
python3 benchmarks/bench_native.py
```
