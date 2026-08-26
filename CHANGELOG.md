# Changelog

All notable changes to Tera Pilot are documented here, newest first. The
README tracks the current product state (Why / Quick Start / Demo / security
summary); this file keeps the per-version history. Every release keeps the
version in sync everywhere: npm, pip, the Web UI, the TUI, the auto-updater
and the tests.

## [2.3.9] — Repository hygiene & docs

The v2.3.9 release is a repository-hygiene and documentation release: dated
internal reports and planning documents (eval/security reports, product
strategy and readiness plans, market-research notes) were removed from version
control — they stay on disk as working documents, and the public repo now
carries only docs that reflect the current product state. Measured eval and
security results remain in the README; `CONTRIBUTING.md`, `DEVELOPING.md`,
`THREAT_MODEL.md` and `eval/README.md` were updated to reference the public
sections instead of the removed files. No runtime code changed.

## [2.3.8] — Correctness & integration

The v2.3.8 release is a correctness-and-integration release. The ACP server
(IDE integration via the Agent Client Protocol) is fixed end-to-end:
`prompt/send` previously crashed on **every** turn — the streaming path
iterated the runtime's *sync* generator with `async for` (`TypeError: 'async
for' requires an object with __aiter__`), so no events ever reached the
editor; it now streams text chunks as `session/update` events and terminates
with `turn_end`. The `--no-confirm` flag no longer crashes with `NameError`
(the env var was set before `import os`), and the ToolEngine's `OfficeWorker`
forward reference is now a proper `TYPE_CHECKING` import (no accidental
import cost, clean mypy). File-backup integrity is fixed in the diff engine:
backups use a monotonic-nanosecond timestamp (two writes in the same second
used to overwrite each other's backup, silently narrowing undo history) and
the prune cap now holds exactly `max_backups` files instead of
`max_backups + 1`. The test suite grew by 66 tests — new suites for the ACP
server protocol surface, the diff utilities, the signed audit trail
(tamper/reorder/deletion detection) and the MCP client (env sandboxing,
argument validation, result handling) — bringing the total to **774 tests
(754 passing)**.

## [2.3.7] — Reliability & persistence

The v2.3.7 release is a reliability-and-persistence release: the TUI now
reads provider config (API keys / models / api_base) from
`~/.tera_pilot/config.json` on startup instead of silently falling back to
defaults; Quick Settings and `/model <pid> <model>` persist model/API-key
changes (they used to be lost on restart), and switching providers with a
model no longer wipes the saved API key/base. The agent loop's iteration
budget is now a **soft cap**: while the agent keeps executing tools
successfully it auto-extends (up to 3× the soft cap, 40–200), so large
multi-file tasks run to completion instead of dying with "Max iterations
reached" — while genuinely stuck loops (repeated errors, no tools) still stop
at the cap. A stale `active_provider` in config no longer crashes the TUI at
startup. Model-generated ANSI/control sequences are stripped before rendering,
the status line animates while a turn runs, `/checkpoint save` records the
files the agent actually touched, and runs that hit the cap surface their
partial output instead of discarding it. The eval harness reports real
provider/model/usage from the agent path and adds 5 new tasks
(median-mutation bug fix, binary search, currency-symbol refactor, test-format
repair, path-traversal review).

## [2.3.6] — Security & sandbox

The v2.3.6 release is a security-and-sandbox release, detailed in the
README's [Security Posture & Verification](README.md#security-posture--verification)
section: `execute_command` / `run_code` / auto-detected test-lint commands
now run inside an **OS-level sandbox** when a backend is available (P1.10 —
macOS `sandbox-exec`/Seatbelt, Linux `bubblewrap`; network denied, writes
restricted to workspace + OS temp, sensitive paths unreadable;
`agent_os_sandbox: auto|on|off`, `on` fails closed without a backend);
`web_fetch` blocks loopback/private/link-local/cloud-metadata targets with
DNS-rebinding defense and per-redirect-hop re-validation (P0.2); the local
API token is no longer returned by `GET /api/status` (P0.3); and headless
daemon/ACP runs now **fail closed** on side-effecting actions unless
explicitly opted in via `--no-confirm` / `TERA_PILOT_ACP_NO_CONFIRM=1`, with
the Guardian also failing closed on LLM/unparseable verdicts (P0.4). It fixes
the LM Studio integration for models that 400 generated native tool calls
containing quotes — the runtime stops advertising a `tools` schema there and
parses the model's native `<|tool_call_start|>` text format instead. The eval
harness gains the direct (no-agent) driver, `--repeat`, the `security` task
category with `security_expectation`, per-run evidence (actual diff,
provider/tool error counters, `self_verify`) and 10 adversarial `sec-*`
tasks. New tasks (added 2026-08-22): `fix-median-mutates-input` (bug_fix —
`median()` must not mutate its input), `add-binary-search` (feature — binary
search with edge cases), `refactor-extract-currency-symbol` (refactor —
extract a helper without changing behavior),
`repair-test-wrong-expected-value-format` (test_repair — fix the stale test
assertion, not the module), `review-path-traversal` (code_review — read-only
review identifying a path-traversal flaw in `REVIEW.md`). Misc:
timezone-aware UTC datetimes, request-queue stream serialization, quota
breakdown with token-optimization tips API.

## [2.3.5] — Security & sandbox (internal)

The v2.3.5 release was the security-and-sandbox release (OS sandbox P1.10,
SSRF hardening P0.2, fail-closed autonomy P0.4, LM Studio native tool-call
parsing). Its release notes were folded into the v2.3.6 entry above.

## [2.3.4] — Security hardening, data integrity & GUI polish

The v2.3.4 release fixes three silent data-integrity bugs and polishes the
GUI: test/lint commands that emit more than the OS pipe buffer (~64 KB) no
longer spuriously hit the 60 s timeout with zero captured output (the pipes
are now drained while the child runs); **Undo** in the GUI actually works (it
now uses the shared process checkpoint manager instead of a fresh empty one)
and rewind restores the latest backup at-or-before the target checkpoint
instead of only the target's own manifest — files modified before the target
but not at it are no longer deleted; and learning-loop entries get a unique
`LEARN-YYYYMMDD-NNN` id even after older entries are dismissed. The web GUI
gains smart auto-scroll with a "Jump to latest" pill (reading history while
the agent streams no longer yanks the viewport), the Settings button always
opens the full modal (which hides advanced tabs in Basic mode), and the About
tab now shows the real version from the backend instead of a stale hardcoded
one.

v2.3.4 also ships a security hardening pass driven by offensive testing (see
[Security Posture & Verification](README.md#security-posture--verification)):
`git` commands can no longer execute arbitrary shell code through `!`-aliases
or exec-capable config keys (`core.fsmonitor`, `core.editor`, …) — including
when a malicious repo ships those keys in its own `.git/config`/`.git/hooks`
(now neutralized at runtime for every agent git call); the CORS check now
matches exact loopback hosts instead of a string prefix (an attacker-controlled
`localhost.evil.com` used to be echoed as an allowed origin, exposing
`api_token`); the encrypted-prompt store fails closed without `cryptography`
instead of degrading to an insecure XOR scheme; `/api/context/pin|unpin` moved
from GET to POST under the bearer token; `npm test`/`npm exec` and the other
`npm run` aliases are blocked (they executed arbitrary `package.json`
scripts), and auto-detected test/lint commands require user approval;
`web_fetch` refuses loopback targets so the local API token can't be
exfiltrated; the daemon's token check is constant-time and request bodies are
size-capped on both servers; and the daemon closes SSE streams immediately
for already-finished tasks.

v2.3.4 also fixes the GUI's **Open project** flow end-to-end: the directory
picker no longer crashes the backend on macOS (it used to create a tkinter
dialog from the HTTP daemon thread, which AppKit forbids — the whole process
aborted), real picker failures (automation-permission denied, missing dialog
binary) are now reported to the GUI so it falls back to a manual path-entry
modal **with a working Browse… button** instead of silently doing nothing, and
opening a large folder no longer freezes the request for ~20 s — the project
context index is now built lazily on first agent use and bounded (50k files /
5 s), not synchronously during `set_root`. The sidebar file-tree panel also
refreshes after a project switch, `⌘O`/`Ctrl+O` actually opens the picker (the
command palette advertised it but no binding existed), `/cd ~` expands the
home directory in the TUI, and the provider wizard's model examples were
refreshed to current 2026 models (GPT-5.5, Claude Sonnet 5, Gemini 3.1 Pro,
DeepSeek-V4, GLM-5.1, Grok 4.5, Llama 4, …).

The eval harness was corrected too: parallel launches that collided on the
single-agent server now retry with backoff instead of failing, and a run whose
verification tests actually **passed** is no longer masked by a terminal
driver `error`. The earliest analyzed batch (2026-08-19, 12 of 27 meaningful
runs) ran while these harness/reporting bugs suppressed legitimate successes;
every batch since reflects the corrected harness.

## [2.3.3] — Reliability & usability

The v2.3.3 release improves reliability and usability: quota/rate-limit
errors now surface actionable messages with retry/switch guidance instead of
raw JSON; the agent runtime gives saturated free-tier pools more time to
recover (8 attempts vs 5); health probes no longer cripple subsequent LLM
calls (provider config is restored after testing); the "Open Project" action
now updates the file tree immediately; provider model lists can be fetched
live from the API; chat titles are generated via a short LLM round-trip for
better summaries; terminal command output keeps the tail (useful error
details) instead of the head; and the eval harness correctly times out HTTP
reads independent of the agent's own timeout.

## [2.3.2] — Version consistency & eval suite

The v2.3.2 release is a reliability and version-consistency release: the
version is now in sync everywhere (npm, pip, Web UI, TUI, auto-updater); the
daemon builds a config-backed provider registry (previously every daemon task
crashed with `TypeError`); providers accept a per-call `model` override and
honour provider retry-delay hints on quota errors;
`AgentRuntime.get_token_stats()` exists so the daemon and e2e harness report
real token usage; a keyless `local` OpenAI-compatible provider is registered
(no more `Unknown provider: local` warnings for local-endpoint configs); and
the evaluation harness ships 43 baseline-verified repository tasks.

## [2.3.1] — Browser GUI completion

The v2.3.1 release completes the browser GUI: chat streaming renders a single
live entry (no more duplicated lines), every control talks to the backend (no
more silent 404s), all themes are brand-neutral (Noir/Flat), and the TUI
gained smooth modal/scroll motion in a minimal dark theme.

## [2.3.0] — GUI (Noir) & P0 groundwork

The P0 roadmap work shipped up to this point — environment doctor, signed
audit export/verification, threat model, Rust native acceleration (circuit
breaker ~43x faster, sandbox checks ~3.3x faster), the evaluation harness
skeleton (`eval/`), and the v2.3.0 GUI (Noir minimal theme + Basic/Advanced
UI modes) — is documented in the README and in `eval/README.md`.
