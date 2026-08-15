# P0 — Implementation Journal

> Working document: what has been done within the P0 priorities from
> [`TERA_PILOT_PRODUCT_STRATEGY.md`](TERA_PILOT_PRODUCT_STRATEGY.md) and how.
> Status: August 2026. Update as implementation proceeds.

## 0. Context: Clew → Tera Pilot rename

The project was fully renamed (user-facing name, packages, classes,
env variables, config paths, CLI):

| Was | Became |
|---|---|
| package `clew/` | `tera_pilot/` |
| package `clew_tui/` | `tera_pilot_tui/` |
| `bin/clew.js`, `bin/clew-tui.js` | `bin/tera-pilot.js`, `bin/tera-pilot-tui.js` (+ `bin/tera-pilot-daemon.js`, `bin/tera-pilot-acp.js`) |
| `clew` (CLI), `clew_tui`, `clew-acp`, `clew-daemon` | `tera-pilot`, `tera-pilot-tui`, `tera-pilot-acp`, `tera-pilot-daemon` |
| `ClewAPIServer`, `ClewBridge`, `ClewDaemon`, `ClewMainWindow` | `TeraPilotAPIServer`, `TeraPilotBridge`, `TeraPilotDaemon`, `TeraPilotMainWindow` |
| `CLEW_*` env vars, `~/.clew`, `CLEW.md` | `TERA_PILOT_*`, `~/.tera_pilot`, `TERA_PILOT.md` |
| GitHub `zai-shop/clew` | `ilyaosovskoi/tera-pilot` |

The README gained a "Migrating from Clew" section with config migration
(`mv ~/.clew ~/.tera_pilot`, `mv CLEW.md TERA_PILOT.md`); the banner was replaced
with `tera_pilot.png`.

---

## 1. Environment Doctor — `tera-pilot doctor` (P0 onboarding)

**What:** one command answers "is this machine ready to run Tera Pilot?".

**Files:** `tera_pilot/environment_doctor.py`, `tera_pilot/cli.py`,
`tera_pilot/__main__.py`, entry point `tera-pilot = "tera_pilot.cli:main"`.

**Checks** (each with `ok`/`warn`/`fail` status):
- Python ≥ 3.11, package import and version;
- `~/.tera_pilot` exists and is writable;
- critical dependencies (pydantic, textual, requests, aiohttp, toml, yaml, rich);
- optional ones (cryptography, office: docx/openpyxl/pptx);
- provider keys: env variables from the provider registry + `config.json`;
- local models: probes of Ollama (`127.0.0.1:11434`) and LM Studio (`127.0.0.1:1234`);
- Rust acceleration (`NATIVE_AVAILABLE`);
- web-search backend (status from `web_search_backend`);
- working directory (`--project` or cwd).

**Usage:**
```bash
tera-pilot doctor                 # human-readable report (rich)
tera-pilot doctor --json          # machine-readable (schema v1) for CI
tera-pilot doctor --project DIR   # check a specific directory
```
Exit code: 0 — no blocking issues, 1 — a `fail` exists (warn does not fail:
a local setup without cloud keys is valid).

**Verification:** `tests/test_environment_doctor.py` (4 tests). Network probes
are limited to localhost; nothing is sent externally.

---

## 2. Audit export / verification — `tera-pilot audit` (P0)

**What:** documented export and verification of the signed audit trail.

**Files:** `tera_pilot/audit_cli.py`, `tera_pilot/cli.py`.

**How it works:** every activity log record is signed with a local Ed25519 key
(`~/.tera_pilot/audit_key`, chmod 0600) and chained to the previous record by
a SHA-256 hash (`prev_hash + payload`). Tampering with content, deletion and
reordering of records are detected on verification.

```bash
tera-pilot audit export --out audit.json   # signed (Ed25519 + hash chain)
tera-pilot audit verify audit.json         # 0 = intact, 1 = tampering detected
```

The public key `~/.tera_pilot/audit_key.pub` allows verifying an export on
another machine. Inside a running TUI/Web, the slash commands `/audit` and
`/audit-signed` are available.

**Verification:** `tests/test_audit_cli.py` — export→verify roundtrip, record
tamper detection, reordering detection, invalid subcommand handling.

---

## 3. Threat Model — `THREAT_MODEL.md` (P0)

**What:** the public threat model: assets, trust boundaries, 8 threat scenarios
(prompt injection, malicious commands, workspace escape, key theft,
code leak, audit tampering, social engineering, supply chain), a controls
mapping with module references, user verification procedures, residual risks
and claims discipline (what we do NOT claim).

Linked from the README via the "Trust and Control" section.

---

## 4. Rust acceleration — `tera_pilot_native` (P0 readiness / performance)

### 4.1. Why this is a P0-relevant area

The project is designed around native acceleration (`tera_pilot/agent/native.py`),
but the Rust part did not exist — everything ran on pure-Python fallbacks.
Hot-path analysis (what is slowest under real load):

| Path | When it runs | Why it is slow in Python |
|---|---|---|
| **circuit_breaker** | on EVERY LLM/MCP call | `threading.Lock` + O(window) window scans (`sum(...)` over deque) on every `record()` |
| **sandbox.path_would_be_writable** | on every agent file write | `Path.resolve()` + `is_relative_to` — object overhead and repeated syscalls |
| **compaction** (code/intra/inter) | on context overflow | orchestration and prompt building in Python (the LLM call itself dominates; the win is moderate) |
| interjection, actor | mid-turn messages, cancellation | cheap, but trivially portable |

### 4.2. What was implemented

Crate **`tera-pilot-native/`** (Cargo workspace + PyO3 extension
`tera_pilot_native`, 5 submodules mirroring the fallback APIs 1-to-1):

- **`sandbox`** — profile state machine + fast path checks:
  canonicalization (symlinks + `..`, like `Path.resolve()`) and component-wise
  path comparison instead of string comparison. Non-existent files inside the
  workspace are correctly treated as writable (important for creating new files).
- **`circuit_breaker`** — sliding window with incremental counters:
  O(1) amortized instead of O(window); `VecDeque` + `Mutex` (parking_lot-style),
  metrics are returned in the same dict format the Python wrappers expect.
- **`interjection`** — thread-safe FIFO buffer, UTF-8-safe truncation
  by code points (parity with Python `s[:max]`), `drain()`/`drain_formatted()`.
- **`compaction`** — code/intra/inter engine with a callback into the Python
  sampler (`_NativeSamplerShim`); prompts and message formats are byte-for-byte
  identical to the fallback.
- **`actor`** — `CancelToken` with parent→child cancellation propagation
  through atomic flags (no daemon threads, as in the fallback).

Loading goes through the existing `tera_pilot/agent/native.py`; without a build
the project continues to run on fallbacks.

### 4.3. Benchmark results (Mac arm64, Python 3.12, release build)

`python3 benchmarks/bench_native.py`:

| Operation | Native (Rust) | Fallback (Python) | Speedup |
|---|---|---|---|
| circuit_breaker: record + try_claim (window 60s, 5k ops) | 0.4 µs/op | 19.3 µs/op | **~43x** |
| sandbox path_would_be_writable (100k ops, against a real workspace) | 19.8 µs/op | 66.2 µs/op | **~3.3x** |
| interjection push + drain (50k ops) | 0.4 µs/op | 0.7 µs/op | ~1.8x |

Circuit breaker is the main win: it runs on every provider/MCP call, and the
Python version degrades quadratically with long windows.

### 4.6. Security bugs found and fixed in the sandbox (`..` escape)

**Bug 1 — `..` was silently dropped.** The first `resolve_path` version built
the path "tail" via `Path::file_name()`, which returns `None` in Rust for a
trailing `..`. The walk-up loop silently skipped such components, and the path
`<workspace>/sub/../../etc/passwd` (where `sub` does not exist) normalized to
`<workspace>/sub/etc/passwd` — i.e. it was treated as **writable**, although it
really leads outside the workspace. Additionally verified:
`std::path::absolute` does not normalize `..` at all, and naive
"normalize against the filesystem" breaks symlink semantics.

**Bug 2 — symlink + `..` (found in the second review).** Lexically collapsing
`..` before symlink resolution diverged from Python: for `<ws>/link/../x.py`
where `link` is a symlink to a directory outside the workspace, Python first
resolves the symlink and then applies `..` (→ outside, not writable), while the
lexical variant dropped `link` and treated the path as inside (→ writable).

**Final fix (parity with Python `Path.resolve()`):**

1. existing path — canonicalize immediately;
2. otherwise — walk up from the original path to the deepest existing
   ancestor: `Path::exists()` lets the OS resolve `..` and symlinks against
   the existing prefix, so the stopping point matches where Python `realpath`
   would land;
3. the remainder is built **with** `..` components via
   `components().next_back()` (not `file_name()`!);
4. the remainder is attached to the canonicalized ancestor, and finally
   `normalize_lexically` collapses `.`/`..`.

Verification: 13 live native-vs-Python cases (including symlink-outside and
inside + `..`) — all match; the `tests/test_native.py` regression covers both
dotdot escapes and symlink+`..`; the benchmark did not regress after the fix
(3.3x vs 3.2x before). This is exactly the class of bug the sandbox checks are
moved to Rust for: Python `Path.resolve()` does not surprise, a naive Rust port
does.

### 4.4. Build

```bash
cd tera-pilot-native/pyo3
maturin build --release                    # wheel → ../target/wheels/
python3 -m pip install --force-reinstall ../target/wheels/*.whl
# or in a venv: maturin develop --release
```

### 4.5. Verification

- `tests/test_native.py` (11 tests) — behavior parity of all 5 submodules
  with the fallbacks: breaker transitions, metrics format, retry dispositions,
  sandbox checks (including non-existent files, `..` escapes and
  symlink+`..` parity), interjection roundtrip, compaction with a real Python
  callback, CancelToken semantics (first reason wins, propagation), breaker
  speed smoke.
- `tera-pilot doctor` now shows `native: ok` when the extension is built.
- Live parity run: 13 native-vs-Python `Path.resolve()` cases — all match
  (`..` escapes, symlink-outside/inside + `..`, double slashes, `.`, root `..`).

---

## 5. Evaluation Harness — `eval/` (P0.1)

**What:** a reproducible evaluation contour — tasks run in a clean copy of the
repository, results are written against a machine-readable schema v1.

**Files:** `eval/runner.py` (CLI `run`/`check`/`smoke`/`report`,
`--version`), `eval/schema.py` (manual validator, mirror of the schema),
`eval/results/schema.json` (JSON Schema v1), `eval/smoke.json` (CI smoke set),
`eval/tasks/<id>/` (manifest `task.json` + fixture `repo/` +
reference `gold/`), `eval/README.md`.

**Task set: 43** in 6 categories (bug_fix ×12, test_repair ×6, refactor ×5,
feature ×9, code_review ×5, documentation ×6). Each task: a realistic
mini-fixture (stdlib + pytest), `test_command`, a declared
`baseline_status`, and a reference `gold/`. The `doc-changelog-from-git`
fixture is a real git repository with tags v1.0.0/v1.1.0 to exercise
`workspace.commit`.

**How it works:**

- the runner copies the fixture into a temporary directory (the source is
  untouched), records `repo_hash` (SHA-256 snapshot without `.git`/`__pycache__`)
  and `workspace.commit` (git HEAD, if the fixture is a git repo);
- baseline mode: `test_command` on the **pristine** repo is recorded in
  `workspace.baseline` (always for `fake`, with `--baseline` for `api`);
- drivers: `fake` (deterministic, no network) and `api` (real run via SSE
  `/api/agent/stream`; tokens/cost from the server usage tracker
  `GET /api/usage/get` (delta before/after), falling back to counting
  token events when unavailable);
- after the agent, the runner re-runs `test_command`, collects metrics and
  validates the result against schema v1 **before** writing (a bad result is
  not written); the temporary workspace is removed (`--keep-workspace` for
  debugging).

**CI commands:**

```bash
python3 -m eval.runner check    # structural check of all tasks (fast)
python3 -m eval.runner smoke    # fake driver over eval/smoke.json + baseline_status check
python3 -m pytest tests/test_evaluation_schema.py tests/test_eval_tasks.py -q
```

**Schema v1 extended backward-compatibly:** `workspace.baseline`
(test results on the pristine repo) and optional `metrics.tokens_in` /
`tokens_out` / `request_count` / `cancelled`. The required field set is
unchanged.

**Claims discipline:** `status` (about the run), `metrics.test_passed`
(about tests), `metrics.verification_status` (about verification) and
`workspace.baseline` (about the pristine repo) are not conflated;
`test_output` and `final_output` are marked as potentially sensitive.

**Verification:**

- `tests/test_evaluation_schema.py` — schema integrity, manual-validator /
  schema.json synchronization, rejection of bad results (incl. new optional
  fields), end-to-end fake run, `report` aggregation;
- `tests/test_eval_tasks.py` — quality gate over all 43 tasks: manifest,
  `baseline_status` matches the real pristine-repo state ("tests fail before
  the agent"), `test_command` passes with `gold/` ("the task is solvable and
  verifiable"), `workspace.commit` recorded for the git task.

**Status:** implemented (43-task set, baseline, commit, usage-cost,
check/smoke/report, quality-gate tests). Remaining: baseline runs on real
providers and publishing measured metrics (see `eval/README.md`).

---

## 6. GUI v2.3.0 — Noir theme + Basic/Advanced modes

**What:** the web interface moved to black "launch-pad" minimalism and
 gained a **Basic/Advanced** switcher. (Theme ids are neutral; see
 v2.3.1 for the brand-name cleanup.)

**`noir` theme:**

- clean black background (`#050506`), layered surfaces, hairline borders
  (alpha 0.07/0.14), a single white accent;
- sharp corners (4–8px instead of 20px+), mono family for statuses/versions
  (JetBrains Mono), uppercase labels with wide tracking;
- red `#E82127` for dangerous states, signal green/blue;
- component polish in `design-polish.css` (the v2.2.2 stub became a layer of
  Noir overrides).
- **Default theme** for new installs (in settings, the "Noir" card is first
  in the list; previously saved themes are untouched).

**Basic/Advanced modes:**

- switcher in the top bar (always visible, NOT advanced-only): a pill with a
  slider; state stored in `tera_pilot:uiMode`;
- **Basic** (default): a clean surface — hides agent/plan toggles, RAG,
  enhance, swarm, git bar, Heavy Code/Office/Tools panels, token
  optimizations, router indicator and advanced settings tabs
  (Tools/MCP/Agent/Project/Snippets); keeps provider, model, composer,
  chats, catalog, usage, files, settings (basic tabs);
- **Advanced**: the full control surface;
- implementation: `data-ui-mode` on `<html>` + `.advanced-only` + `data-advanced`
  on tabs; a guard in `openSettings` prevents opening a hidden tab in
  basic mode (programmatic calls also fall back to `appearance`).

**Verification:** `node --check app.js` and `tools_panels.js` — clean;
HTML parser — no unclosed tags; a live web server serves index.html (200)
with all markers (`uiModeToggle`, `data-ui-mode`, 16× `advanced-only`),
CSS/JS — 200. Visual browser check happens on a machine with Chrome / a
built-in window (Chrome is not installed in this environment).

---

## 7. ToolEngine / Guardian security fixes (regression-tested)

Four sandbox/security bugs found and fixed in the tool layer, with
regression tests in `tests/test_tool_engine_sandbox.py`:

1. **`git --git-dir` / `--work-tree` sandbox bypass.** Every argument
   starting with `-` was skipped as a "flag", so only `git -C` was validated.
   `git --git-dir=/home/user/.git log` could read git history from anywhere
   on disk, and `git --work-tree=/etc add -A` could stage arbitrary files.
   Now both the inline (`--git-dir=<path>`) and two-arg (`--git-dir <path>`)
   forms are validated against the workspace, including relative escapes.
2. **`git_diff` unvalidated pathspec.** A relative `../outside` or absolute
   path could show diffs of files outside the workspace. The pathspec is now
   validated against the workspace before the git call.
3. **Guardian dead code.** The command-policy risk check called
   `command_policy.is_dangerous_flag(binary, "")` with an empty flag, which
   can never return True. It now flags binaries the resolved policy would
   refuse (deny list / not allowed).
4. **Guardian template path.** The engine's guardian prompt template was
   looked up at a path that never existed, so every Guardian LLM call
   silently used the generic fallback prompt. The template is now resolved
   to the real location (`tera_pilot/agent/templates/guardian.md`).

---

## 8. TUI bridge reliability fixes + integration tests

### 8.1. Cancel hang (TOCTOU) fix

`request_stop()` and `answer_confirmation()` previously set the cancel flag
first and then the confirmation event, leaving a window where the agent
checked `_confirm_accepted` before it was set — a pending approval could hang
the turn even after the user pressed Stop. Both paths now set the event
FIRST and the flag second, so a blocked approval unwinds immediately.

### 8.2. TUI integration test suite

`tests/test_tui_integration.py` (19 tests) drives the REAL `AgentRuntime` +
`ToolEngine` through the REAL `TeraPilotBridge` with the deterministic
`FakeProvider` (tests/fake_provider.py) — no network, no API keys. Covers
the failure modes a user can actually hit:

- streaming and non-streaming turns (incl. exact token-delta delivery);
- cancel (Stop) mid-stream returns promptly;
- approval flow: Allow runs the command, Deny skips it, headless mode
  fails open (no deadlock);
- provider errors: invalid key (no retry), timeout (retried), rate limit
  (retried);
- workspace / provider / model switching;
- long output not truncated; tool errors are partial failures, not crashes;
- checkpoint rewind + undo restore files;
- recovery after a provider error on the same bridge;
- the CI adapter (`backend_runner.run_task`) reports provider errors cleanly.

---

## 9. One-command install (npm) — `npm install -g tera-pilot`

**What:** a single command installs the full product — no `git clone`, no
manual `pip install`.

**How it works:**

- `scripts/postinstall.js` (npm `postinstall`) resolves a Python interpreter,
  creates an isolated virtualenv (default `~/.tera_pilot/venv`, overridable
  with `TERA_PILOT_VENV`), installs the bundled Python package + dependencies
  into it, and writes a `.npm-managed.json` marker so `preuninstall` can
  safely remove it on uninstall.
- `scripts/preuninstall.js` removes the npm-managed venv only when the marker
  matches this exact package version — a venv that predates the package (or
  belongs to another version) is left untouched.
- `bin/*.js` launchers resolve Python in the order
  `TERA_PILOT_PYTHON` → npm venv → system python, check the module, and
  forward all args. Missing Python / missing module produce a friendly
  recovery message (reinstall with `npm install -g tera-pilot`) instead of a
  raw traceback.
- npm bin exposes: `tera-pilot`, `tera-pilot-tui`, `tera-pilot-daemon`
  (→ `python -m tera_pilot.daemon`), `tera-pilot-acp`
  (→ `python -m tera_pilot.agent.acp_server`; `--mcp-server` dispatches to
  the MCP server mode), plus `tera-pilot doctor` / `tera-pilot audit`
  subcommands.
- Env knobs (all optional): `TERA_PILOT_PYTHON`, `TERA_PILOT_VENV`,
  `TERA_PILOT_SKIP_PIP=1`, `TERA_PILOT_PIP_EXTRA_ARGS`, `TERA_PILOT_FORCE=1`.

**Verification:** `tests/test_npm_install.py` (9 tests) exercises the real
postinstall/preuninstall/launchers against a *fake* Python interpreter —
hermetic, no network, no real venv/pip. Covers venv bootstrap + marker,
idempotent fast path, force rewrite, clean failure without Python, arg
forwarding for all four launchers, friendly missing-module error, and
uninstall cleanup (managed venv removed, unmanaged venv kept).

---

## 10. Backend report contract — schema v1

**What:** a stable machine-readable contract for one backend task run
(TUI-backed automation adapter, CI, GitHub workflows, daemon).

**Files:** `tera_pilot_tui/backend_report_schema.json` (JSON Schema v1),
`tera_pilot_tui/backend_report.py` (manual validator mirroring the schema,
no jsonschema dependency), `tera_pilot_tui/backend_runner.py` (producer —
validates every report before returning).

**Fields:** `schema_version` (1), `ok` / `status` (`success|failed|error`),
actual `provider`/`model`, `workspace` identity (absolute path),
`iterations`, `duration_sec`, `tokens`, `cost_usd`, `tools` (names + error +
duration only — **raw tool arguments are never exported**), `verification`
(`self_verify_called` + `status` in
`not_requested|ran|passed|failed|unknown`), optional `test_result`
(`command`/`passed`/`exit_code`/`output`), `final_output` (potentially
sensitive), optional `audit_ref`, JSON-safe `metadata`.

**Claims discipline:** an unrun verification is never called `passed`;
tool arguments never leak into the report; `test_output`/`final_output` are
marked sensitive. The producer sanitizes runtime metadata recursively so a
report is always `json.dumps`-able.

**Verification:** `tests/test_tui_backend.py` (9 tests) — JSON Schema and
manual validator agree on required fields and vocabulary; sample report is
schema-valid and JSON-serializable; raw tool args are rejected; missing
required fields are rejected; `passed` without `self_verify_called` is
rejected; non-JSON metadata serializes safely.

---

## 11b. v2.3.1 — streaming fix + minimal motion language + brand cleanup

### 11b.1. ChatLog streaming duplication (real, user-visible bug)

The v2.2.4 streaming implementation updated `RichLog._children[-1]`, but
`RichLog` on Textual 8.x has no `_children` — every `append_token_delta`
chunk fell through to a fresh `self.write()`, so a stream of N chunks
produced N progressively-longer duplicate log entries. Worse,
`_on_turn_done` skipped `add_final()` on streamed turns, so the final
answer was never rendered as Markdown (it stayed as raw plain text).

**Fix** (`tera_pilot_tui/widgets/chat_log.py`, `app.py`): the streaming
entry is now tracked by a baseline line count — each chunk rolls the
entry back and re-writes it in place (one growing entry, never N), and
`add_final()` replaces it with the Markdown render. New
`abort_streaming()` discards partial text on errors/interrupts.
Regression tests: `tests/test_chat_log_streaming.py` (3 tests).

### 11b.2. Minimal design + motion

- **Brand cleanup**: all third-party brand references removed from the UI
  (theme ids `spacex`→`noir`, `cursor`→`flat` with a localStorage
  migration for saved themes; `apple-design.css` renamed to
  `design-polish.css`; "Tesla/SpaceX/Apple/Claude Code" wording removed
  from user-facing strings, CSS comments and docs).

- **TUI**: near-black (`#0a0a0c`) surfaces, hairline borders, single warm
  accent; `GuardianModal` finally got its own styles (it rendered
  unstyled before); entrance animations for all modals (fade + rise via
  the `animate()` API — Textual 8's CSS `@keyframes`/`transition` are not
  supported at app level), suggestion-bar fade-in, a warm "working"
  border on the input while the agent runs, and smooth scroll on chat
  writes. Shared helpers in `tera_pilot_tui/widgets/motion.py`.
- **GUI**: micro-interaction press feedback on every clickable control,
  agent-step glide-in, message hover lift, staggered empty-state chips,
  modal-body fade, generating-glow on the brand dot, provider-menu item
  slide, all gated by `prefers-reduced-motion`.
- Version bumped to 2.3.1 (package.json, `__version__`, web server,
  TUI InfoBox, web UI chrome).

### 11b.3. Dead HTTP endpoints in the browser GUI (real, user-visible bug)

The browser GUI (served by `tera_pilot.web_server`, the current primary
interface) dispatched every backend call through `bridge_shim.js` HTTP
routes, but a large cluster of those routes had no handler on the Python
backend — the GUI's `/context`, `/clear`, `/compact`, `/pin`, `/unpin`,
`/reload-context` slash commands, the Collective Memory editor, the
Apply/Copy file buttons, the file-tree panel, Settings save, Stop
generation, diff-review responses and ~20 more controls all silently
failed with HTTP 404 (`{"error": "not found"}`).

**Fix** (`tera_pilot/api_extended.py`, `api_server.py`, `bridge_shim.js`):
implemented the missing endpoints, reusing the shared `AgentRuntime`
(via `handler.ctx.get_agent_runtime`) so context ops affect the exact
conversation the GUI streams, `CodeViewerService` for safe
workspace-scoped file read/list, the checkpoint system for agent undo,
and `AutoRouter.classify_explain` for prompt classification. The memory
editor routes live under neutral `/api/memory/*` (the old
`/api/claude_md/*` never existed, so there is no compat break) and the
file write path blocks traversal outside the workspace. Also fixed a
latent bug: the chat stream polled `ServerContext._stop_event`, which is
*also* the server-shutdown event — a GUI Stop would have killed the whole
server; chat streaming now has its own cancel event
(`_chat_cancel_event`). `bridge_shim.js` gained a `POST_ARG_MAP` for the
positional-argument methods so payloads reach the new handlers intact.
Regression tests: `tests/test_api_extended_endpoints.py` (24 tests).

---

## 11. What remains (outside this P0 slice)

- **Kernel-level sandbox** (Landlock/Seatbelt): `supported_platform()`
  honestly returns False — the pattern/checks are in Rust; real enforcement
  is a separate task (platform-specific, risky, needs CI coverage on
  Linux/macOS).
- **Evaluation harness**: baseline runs on real providers and publishing
  measured metrics (infrastructure — see section 5 — is done).
- **Network egress visibility** — the `web` category in the activity log
  already marks external calls; a full egress report/log is a separate task.

---

## Final verification

```
python3 -m pytest tests/ -q        # 209 passed (incl. TUI integration + quality gate on 43 eval tasks)
python3 -m compileall eval tera_pilot tera_pilot_tui tests  # OK
python3 -m eval.runner check       # 43 tasks OK (structure + gold/)
python3 -m eval.runner smoke       # 10/10 tasks OK (fake driver, baseline verified)
python3 -m tera_pilot doctor --json                    # ready=true, counts ok=16/warn=4/fail=0, native=ok
python3 benchmarks/bench_native.py  # breaker ~43x, sandbox ~3.3x, interjection ~1.8x
```
