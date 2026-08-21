<div align="center">

<img src="./tera_pilot.png" alt="Tera Pilot Logo" width="180"/>

<br/>

# Tera Pilot — Private, Vendor-Neutral Coding Agents

### A self-hosted coding agent for private repositories, local models, CI, and verifiable automation.

**Textual TUI first · Web UI · TUI backend · HTTP daemon · MCP/ACP · 16 providers · Ollama/LM Studio · Guardian safety**

<br/>

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-blue.svg)]()
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?style=for-the-badge&logo=python)](https://www.python.org/)
[![Textual](https://img.shields.io/badge/TUI-Textual-purple?style=for-the-badge)](https://textual.textualize.io/)
[![Privacy](https://img.shields.io/badge/Privacy-Local--First-orange?style=for-the-badge)]()

</div>

<br/>

## Why Tera Pilot?

AI coding agents are becoming capable of editing repositories, running commands, calling external tools, and completing multi-step engineering tasks. The hard part is no longer only generation quality — it is **trust, control, and evidence**.

Tera Pilot is built for developers and teams that need to:

- keep code local when using Ollama or LM Studio;
- bring their own API keys and choose among multiple providers;
- run an agent from a terminal, TUI, browser, daemon, or CI job;
- review and restrict file, command, Git, and MCP actions;
- preserve an activity and audit trail of what the agent did;
- verify the result instead of accepting an opaque final answer;
- avoid lock-in to a single model vendor or hosted IDE.

Tera Pilot is **not** positioned as a replacement for Cursor autocomplete or GitHub Copilot distribution. Its focus is controlled, private, vendor-neutral agent execution.

## Quick Start

### One-command install (npm) — recommended

No `git clone`, no manual `pip install`. Python 3.11+ is the only system
prerequisite:

```bash
npm install -g tera-pilot
```

The npm `postinstall` step creates an isolated Python virtualenv at
`~/.tera_pilot/venv` and installs the bundled Python package plus its
dependencies into it, so all launchers work out of the box:

```bash
tera-pilot                              # Web UI
tera-pilot-tui                          # Full-screen terminal UI (primary interactive app)
tera-pilot-daemon --help                # REST API + SSE daemon
tera-pilot-acp --help                   # ACP (Agent Client Protocol) server
tera-pilot doctor                       # environment doctor
tera-pilot audit                        # signed audit export/verification
```

Environment knobs (all optional):

| Variable | Effect |
|---|---|
| `TERA_PILOT_PYTHON` | which Python interpreter to use (default `python3`) |
| `TERA_PILOT_VENV` | where the virtualenv lives (default `~/.tera_pilot/venv`) |
| `TERA_PILOT_SKIP_PIP=1` | install the npm package without running `pip install` (offline / custom setups) |

On `npm uninstall -g tera-pilot` the npm-managed venv is removed with it
(only if this package version created it — user data is never deleted
speculatively).

### Install from source

```bash
git clone https://github.com/ilyaosovskoi/tera-pilot.git
cd tera-pilot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

The Python package provides the same commands: `tera-pilot`, `tera-pilot-tui`,
`tera-pilot-daemon`, `tera-pilot-acp`, plus `tera-pilot doctor` and
`tera-pilot audit` subcommands.

### Environment Doctor

Not sure your machine is ready? One command checks Python version, dependencies, config directory, provider keys, local model servers (Ollama / LM Studio), optional Rust acceleration, web-search backend, and the workspace:

```bash
tera-pilot doctor          # human-readable report
tera-pilot doctor --json   # machine-readable report (CI / scripts)
```

Exit code is 0 when there are no blocking issues; warnings alone (e.g. no cloud API keys on a fully local setup) do not fail the check.

## Choose a Model

Tera Pilot is provider-neutral. Configure a cloud provider with your own key, or use a local model without sending repository content to a cloud provider.

### Built-in provider families

- Anthropic, OpenAI, Google Gemini, DeepSeek, Groq, xAI, z.ai, Mistral
- Cerebras, Together, Fireworks, SambaNova, Nvidia NIM
- OpenRouter for access to many additional models
- Ollama and LM Studio for local inference

The TUI exposes provider selection, model overrides, workspace selection and autonomy settings through its visual controls and command palette. For a local-first workflow, choose Ollama or LM Studio in the provider selector and keep the workspace inside the intended project root.

## Core Agent Runtime

Tera Pilot runs a ReAct-style agent loop:

1. **Plan** — understand the task and define a path forward.
2. **Explore** — search the workspace, read files, inspect Git state.
3. **Act** — edit files, apply diffs, run commands, call MCP tools.
4. **Verify** — re-read touched files, run tests, or request a reviewer.
5. **Report** — return the result and the evidence available from the run.

The runtime includes tools for:

- files: `read_file`, `write_file`, `str_replace`, `apply_diff`, `delete_file`, `rename_file`;
- search: `search_project`, `grep`, `glob`, `list_files`, `get_project_structure`;
- execution: `execute_command`, `run_code`;
- Git: status, diff, stage, commit, checkpoints and undo;
- web: `web_search`, `web_fetch`;
- MCP: external tools and typed MCP tool discovery;
- agents: subagents, parallel tasks, watchdog and task decomposition;
- verification: `self_verify`, test execution and reviewer subagents;
- office workflows: `.docx`, `.xlsx` and `.pptx` operations.

## Trust and Control

Tera Pilot treats autonomy as a policy decision, not a binary marketing label.

- **Workspace sandbox** limits file operations to the selected project.
- **Command policy** controls allowed and denied commands.
- **Project approvals** prevent a repository from silently widening its own permissions.
- **Autonomy levels**: `always_ask`, `new_files_only`, `never_ask`.
- **Diff review** can pause writes for human approval.
- **Guardian** assesses risky tool calls and can approve, reject, or modify them.
- **Checkpoints and undo** provide recovery paths for file changes.
- **Activity and audit logs** record tool execution and agent identity.
- **Signed audit support** uses Ed25519 signatures and hash chaining for exported records.
- **Offline licensing** (Pro gating) uses Ed25519-signed keys verified entirely offline — zero telemetry, no phone-home (see [`LICENSING.md`](LICENSING.md)).

These mechanisms provide control and evidence; they are not a claim of formal SOC 2, ISO 27001, or vulnerability-free code. See [`TERA_PILOT_PRODUCT_STRATEGY.md`](TERA_PILOT_PRODUCT_STRATEGY.md) for the security and product roadmap, and [`THREAT_MODEL.md`](THREAT_MODEL.md) for the public threat model and trust boundaries.

## Security Posture & Verification

Security is treated as a continuously tested property, not a one-time claim. Every control below is covered by automated tests (`tests/test_security_suite.py` — 96 tests mapped to `THREAT_MODEL.md` T1–T8 — plus 36 sandbox/command tests in `tests/test_tool_engine_sandbox.py`; the full suite is 448 tests).

**Verified controls:**

| Control | What is tested |
|---|---|
| Command sanitization | shell metacharacters (`;`, `&&`, `|`, backtick, `$`, …), disallowed binaries, dangerous flags (`python3 -c`, `pip install`, `git clone`, …) — all blocked before execution; legitimate commands unaffected |
| Workspace sandbox | absolute paths, `..`, symlink files/dirs, path-prefix lookalikes (`/tmp/ws` vs `/tmp/ws-evil`) — all rejected with `PermissionError`; workspace-root deletion refused |
| git sandbox | `-C`/`--git-dir`/`--work-tree` escapes, `!`-alias shell execution, exec-capable config keys (`core.fsmonitor`, `core.editor`, `sshCommand`, `pager`, `askpass`, `hooksPath`, `credential.helper`, `diff.*.textconv`, `filter.*.clean/.smudge`) — blocked on the command line AND **neutralized at runtime** when read from a malicious repo's own `.git/config` or `.git/hooks/*` (a plain `git status`/`commit`/`diff` on a hostile repo can no longer execute its commands) |
| npm scripts | `npm run` **and its aliases** (`test`, `t`, `start`, `exec`, `ci`, `run-script`, `install-test`, `link`, …) — all execute arbitrary `package.json` scripts, all blocked; auto-detected test/lint commands need user approval like any other command |
| web_fetch (SSRF, P0.2) | only `http(s)` schemes; **loopback, private, link-local and cloud-metadata targets rejected** (IPv4 + IPv6: `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254.0.0/16` incl. `169.254.169.254`, `::1`, `fc00::/7`, `fe80::/10`, `::ffff:`-mapped); the hostname is **DNS-resolved and every resolved address is checked** (DNS-rebinding defense) and **every redirect hop is re-validated** — a 302 to localhost/private IP is blocked; exfiltration-style URLs blocked before the request |
| Local API | bearer-token required on every mutating endpoint (including extended routes and DELETE); constant-time token comparison (API **and** daemon); request bodies capped (8 MiB / 2 MiB); CORS echoes only exact loopback hosts (`localhost`, `127.0.0.1`, `::1`, any port) — `localhost.evil.com` is NOT echoed; **the token is no longer returned by the public `GET /api/status`** (P0.3) — it reaches the frontend only through the same-origin HTML page the server itself serves, and that page never carries a CORS header, so a cross-origin reader (incl. a null-origin sandboxed iframe) cannot steal it |
| Autonomy / confirmations (P0.4) | `always_ask` now covers **every** side effect uniformly — `execute_command`, `run_code`, `write_file`, `write_binary_file` (new AND overwrite), `str_replace`, `apply_diff` (multi-file patches show the file count), `mkdir`, `delete_file`, `rename_file`, `git_stage`, `git_commit`, `call_mcp_tool`, auto-detected test/lint commands. Headless runs (daemon/ACP) **fail closed**: without a UI callback a side-effecting action is BLOCKED, never silently run; explicit opt-in via `--no-confirm` / `TERA_PILOT_ACP_NO_CONFIRM=1` |
| OS sandbox (P1.10) | real end-to-end tests on macOS `sandbox-exec`: network is **denied** (`urllib`/`curl` fail), writes **outside** the workspace are blocked, `~/.ssh`-style secrets are unreadable, while workspace writes, `git`, `node`, `bash` and `python3` still work; `on` fails closed without a backend; Linux `bwrap` arg-builder unit-tested |
| Diff/patches | multi-file diffs with `+++` headers pointing outside the workspace — rejected per-file |
| Audit trail | record tampering and reordering detected via Ed25519 signatures + SHA-256 hash chain |
| Encrypted prompts | ChaCha20-Poly1305 round-trip, wrong key, tampered/truncated ciphertext — all detected; fails closed without `cryptography` (insecure XOR fallback removed in v2.3.4) |

**Cost of the controls** (macOS, Python 3.12, `benchmarks/bench_security.py`):

| Operation | µs/op |
|---|---:|
| `_sanitize_command` (allowed command) | ~23 |
| `_validate_command_paths` / `_resolve_path` (sandbox check) | ~20 |
| `secrets.compare_digest` (token) | 0.03 |
| `EncryptedPromptStore` encrypt / decrypt | ~3 / ~2.7 |

Security checks add tens of microseconds per operation — well under a percent of the cost of the command or file operation itself.

**Honest findings this cycle (v2.3.4):** five real vulnerabilities were found by offensive testing and fixed: (1) `git -c alias.x='!<cmd>' x` executed arbitrary shell commands outside the sandbox; (2) the same class via exec-capable git config keys (`core.fsmonitor` runs on `git status`, `core.editor` on `git commit`, …); (3) the CORS check used `startswith('http://localhost')`, so an attacker-controlled domain `localhost.evil.com` was echoed as an allowed origin — combined with the public `/api/status` returning `api_token`, a malicious page could steal the token and drive the agent; (4) a malicious repo's OWN `.git/config`/`.git/hooks` executed commands on a plain `git status`/`commit`/`diff` (now neutralized at runtime for every agent git call); (5) `npm test`/`npm exec` executed arbitrary `package.json` scripts while only `npm run` was blocked (all script-executing npm subcommands are now blocked, and auto-detected test/lint commands require approval). All are closed, regression-tested, and covered above.

**What we are deliberately NOT claiming — the documented security boundary:**

- **The OS sandbox (P1.10) is a defense layer, not a full VM.** `execute_command` / `run_code` / auto-detected test-lint commands run inside an OS-level sandbox when a backend is available (macOS `sandbox-exec` / Seatbelt profile; Linux `bubblewrap`): **network is denied**, **writes are restricted to the workspace + OS temp**, and **sensitive paths (~/.ssh, ~/.aws, ~/.gnupg, cloud SDK configs) are unreadable**. Mode is configurable (`agent_os_sandbox`: `auto` [default] / `on` / `off`; `on` fails closed without a backend; `off` is needed for commands that legitimately need network, e.g. `git push`/`npm install`). It still is NOT a hardened multi-tenant container: no user-namespace escape containment beyond the seatbelt/bwrap policy, and processes inside the sandbox can still read system libraries and spawn children.
- **Guardian is advisory, not a barrier.** Its LLM review falls back to APPROVE on provider/LLM errors or unparseable verdicts; it is one layer on top of the rule-based risk scorer, command policy and the confirmation gates — not a substitute for them.
- **web_fetch SSRF checks are application-level**, applied immediately before connecting and on every redirect hop; urllib re-resolves the host when it connects, so a tiny TOCTOU window remains (closed in practice by the per-hop checks).
- Headless auto-approve (`--no-confirm`) explicitly trusts the operator to run without confirmations.

See [`SECURITY_TEST_REPORT.md`](SECURITY_TEST_REPORT.md) for the full methodology, and [`THREAT_MODEL.md`](THREAT_MODEL.md) §7 for acknowledged residual risks.

```bash
# Reproduce the security checks
python3 -m pytest tests/test_security_suite.py tests/test_tool_engine_sandbox.py -q
python3 benchmarks/bench_security.py
```

## TUI-First Workflow and Backend Integrations

The **TUI is the primary interactive product**. It is a full-screen Textual application with a chat surface, activity stream, task canvas, provider controls, command palette, approval dialogs and verification feedback. Users type normal requests into the composer; slash commands are available only for advanced controls and settings.

The TUI uses `TeraPilotBridge` as its backend. The same backend can be embedded by integrations such as the daemon and GitHub automation without exposing a separate command-oriented `tera-pilot-cli` product. Backend reports intentionally omit tool arguments by default, while final output may still contain repository code and must be treated as sensitive.

For CI and GitHub workflows, configure `TERA_PILOT_PROVIDER`, `TERA_PILOT_MODEL`, and the matching provider API key as repository secrets/variables. Use an isolated runner and review all generated changes before merging. The generated workflow uploads an evidence report; it does not automatically publish a PR comment.

## MCP and ACP

Tera Pilot can both consume external MCP tools and expose Tera Pilot tools through an MCP server. MCP servers are configured explicitly; write-capable external tools should be trusted and approved deliberately.

```bash
# Expose read-only Tera Pilot tools from a workspace
tera-pilot-acp --mcp-server --workspace /path/to/project

# Enable writes only when you explicitly need them
tera-pilot-acp --mcp-server --workspace /path/to/project --allow-writes
```

ACP/MCP surfaces are intended to connect Tera Pilot to other agents and editor integrations. A first-party native VS Code/JetBrains experience is part of the roadmap, not a current claim.

## Interfaces

| Interface | Best for |
|---|---|
| Textual TUI | Primary interactive app: full-screen chat, activity, approvals, task canvas and provider controls |
| `tera-pilot` | Web UI: browser chat, project browsing, provider settings and activity (since v2.3.1 every control is wired to the backend — slash commands, memory file editor, file tree, snippets, stop/undo, diff review) |
| `tera-pilot-daemon` | Backend service for REST/SSE task execution, queues and notifications |
| `tera-pilot-acp` | Backend integration for MCP/ACP-compatible editors and agents |
| `tera-pilot doctor` | Environment doctor: one-command onboarding and readiness check |
| `tera-pilot audit` | Export and verify the signed audit trail (Ed25519 + hash chain) |
| `eval/runner.py` | Reproducible evaluation harness: clean-copy repository tasks → schema-valid results (P0.1) |

## Target Users

Tera Pilot is designed first for:

1. **Privacy-first developers** who want local models or BYOK.
2. **Senior engineers and DevOps users** who prefer a full-screen TUI, Git and automation.
3. **Teams with sensitive or regulated repositories** that need self-hosting and policy control.
4. **Small engineering teams** that want CI-based maintenance, review and test workflows.
5. **Internal AI/platform teams** building controlled agent infrastructure.
6. **Open-source and self-hosting users** who want an MIT-licensed, vendor-neutral runtime.

Tera Pilot is not primarily an autocomplete product, and it is not yet an enterprise SaaS replacement for GitHub Copilot.

## Architecture

```text
tera_pilot/
├── agent_runtime/       ReAct runtime, tools, memory, parser and verification
├── agent/               Guardian, sandbox, checkpoints and agent support
├── providers/           Provider registry, cloud/local adapters and routing
├── web/                 Browser UI (index.html + app.js + bridge_shim.js)
├── web_server.py        Static file server + API delegation for the Web UI
├── api_server.py        REST/SSE API server (chat, agent stream, diff review)
├── api_extended.py      Extended REST endpoints mirroring the TUI bridge
├── web_bridge/          UI/runtime bridge and persistence helpers
├── session/             Subagent hosting and SQLite persistence
├── swarm/               Multi-agent swarm collaboration
├── mcp_client.py        External MCP client
├── mcp_server.py        Tera Pilot-as-MCP server mode
├── audit_signing.py     Signed audit export and verification
├── github_automation.py GitHub API helpers and Action template

tera_pilot_tui/
├── app.py               Textual application
├── bridge.py            TUI bridge to the runtime
├── backend_runner.py    TUI-backed automation adapter
├── styles_dark.tcss     Minimal dark theme (Noir)
├── styles_light.tcss    Minimal light theme
└── widgets/             Chat, tool, approval and activity widgets
```

## Product Direction

The strategic focus is not feature count. It is measurable, trustworthy execution:

- reproducible evaluation on real repository tasks;
- machine-readable reports and CI evidence;
- transparent sandbox and network boundaries;
- a smooth local/BYOK onboarding path;
- thin VS Code/ACP integration rather than a new IDE at any cost;
- GitHub/GitLab CI workflows;
- stronger team policies, identity and audit retention over time.

Read the complete goals, audience segmentation, ICP, competitive framing and roadmap in [`TERA_PILOT_PRODUCT_STRATEGY.md`](TERA_PILOT_PRODUCT_STRATEGY.md).

What has already been implemented from the P0 roadmap — environment doctor, signed audit export/verification, threat model, Rust native acceleration (circuit breaker ~43x faster, sandbox checks ~3.3x faster), the evaluation harness skeleton (`eval/`), and the v2.3.0 GUI (Noir minimal theme + Basic/Advanced UI modes) — is documented in [`P0_IMPLEMENTATION.md`](P0_IMPLEMENTATION.md).

The **v2.3.1** release completes the browser GUI: chat streaming renders a single live entry (no more duplicated lines), every control talks to the backend (no more silent 404s), all themes are brand-neutral (Noir/Flat), and the TUI gained smooth modal/scroll motion in a minimal dark theme.

The **v2.3.2** release is a reliability and version-consistency release: the version is now in sync everywhere (npm, pip, Web UI, TUI, auto-updater); the daemon builds a config-backed provider registry (previously every daemon task crashed with `TypeError`); providers accept a per-call `model` override and honour provider retry-delay hints on quota errors; `AgentRuntime.get_token_stats()` exists so the daemon and e2e harness report real token usage; a keyless `local` OpenAI-compatible provider is registered (no more `Unknown provider: local` warnings for local-endpoint configs); and the evaluation harness ships 43 baseline-verified repository tasks (see below).

The **v2.3.3** release improves reliability and usability: quota/rate-limit errors now surface actionable messages with retry/switch guidance instead of raw JSON; the agent runtime gives saturated free-tier pools more time to recover (8 attempts vs 5); health probes no longer cripple subsequent LLM calls (provider config is restored after testing); the "Open Project" action now updates the file tree immediately; provider model lists can be fetched live from the API; chat titles are generated via a short LLM round-trip for better summaries; terminal command output keeps the tail (useful error details) instead of the head; and the eval harness correctly times out HTTP reads independent of the agent's own timeout.

The **v2.3.4** release fixes three silent data-integrity bugs and polishes the GUI: test/lint commands that emit more than the OS pipe buffer (~64 KB) no longer spuriously hit the 60 s timeout with zero captured output (the pipes are now drained while the child runs); **Undo** in the GUI actually works (it now uses the shared process checkpoint manager instead of a fresh empty one) and rewind restores the latest backup at-or-before the target checkpoint instead of only the target's own manifest — files modified before the target but not at it are no longer deleted; and learning-loop entries get a unique `LEARN-YYYYMMDD-NNN` id even after older entries are dismissed. The web GUI gains smart auto-scroll with a "Jump to latest" pill (reading history while the agent streams no longer yanks the viewport), the Settings button always opens the full modal (which hides advanced tabs in Basic mode), and the About tab now shows the real version from the backend instead of a stale hardcoded one.

v2.3.4 also ships a security hardening pass driven by offensive testing (see [Security Posture & Verification](#security-posture--verification) and [`SECURITY_TEST_REPORT.md`](SECURITY_TEST_REPORT.md)): `git` commands can no longer execute arbitrary shell code through `!`-aliases or exec-capable config keys (`core.fsmonitor`, `core.editor`, …) — including when a malicious repo ships those keys in its own `.git/config`/`.git/hooks` (now neutralized at runtime for every agent git call); the CORS check now matches exact loopback hosts instead of a string prefix (an attacker-controlled `localhost.evil.com` used to be echoed as an allowed origin, exposing `api_token`); the encrypted-prompt store fails closed without `cryptography` instead of degrading to an insecure XOR scheme; `/api/context/pin|unpin` moved from GET to POST under the bearer token; `npm test`/`npm exec` and the other `npm run` aliases are blocked (they executed arbitrary `package.json` scripts), and auto-detected test/lint commands require user approval; `web_fetch` refuses loopback targets so the local API token can't be exfiltrated; the daemon's token check is constant-time and request bodies are size-capped on both servers; and the daemon closes SSE streams immediately for already-finished tasks.

v2.3.4 also fixes the GUI's **Open project** flow end-to-end: the directory picker no longer crashes the backend on macOS (it used to create a tkinter dialog from the HTTP daemon thread, which AppKit forbids — the whole process aborted), real picker failures (automation-permission denied, missing dialog binary) are now reported to the GUI so it falls back to a manual path-entry modal **with a working Browse… button** instead of silently doing nothing, and opening a large folder no longer freezes the request for ~20 s — the project context index is now built lazily on first agent use and bounded (50k files / 5 s), not synchronously during `set_root`. The sidebar file-tree panel also refreshes after a project switch, `⌘O`/`Ctrl+O` actually opens the picker (the command palette advertised it but no binding existed), `/cd ~` expands the home directory in the TUI, and the provider wizard's model examples were refreshed to current 2026 models (GPT-5.5, Claude Sonnet 5, Gemini 3.1 Pro, DeepSeek-V4, GLM-5.1, Grok 4.5, Llama 4, …).

## Reproducible Evaluation

The `eval/` harness (P0.1) runs real repository tasks against the agent and records schema-valid results in `eval/results/`. It ships **43 tasks** across bug fixes, test repair, refactoring, features, code review and documentation (`eval/tasks/`), each with a clean-copy fixture repo and a baseline-verified test command:

```bash
python3 -m eval.runner check                       # structural check of all tasks
python3 -m eval.runner smoke                       # fake-driver smoke set (CI)
python3 -m eval.runner run eval/tasks/<task_id> --driver api \
    --api-base http://127.0.0.1:18734 --api-token <token>   # live agent run
python3 -m eval.runner report --dir eval/results   # summary
```

**Current results:** the first analyzed live batch (2026-08-19, 30 runs across 9 tasks) is documented in [`eval/REPORT_2026-08-19.md`](eval/REPORT_2026-08-19.md), with recorded vs. corrected numbers: 12 of 27 meaningful runs passed their verification tests, and 8 of 9 tasks were solved at least once. Two harness corrections shipped in v2.3.4: parallel launches that collide on the single-agent server are retried with backoff instead of failing, and a run whose tests **passed** (agent actually ran) is no longer masked by a terminal driver `error`. Raw per-run logs are development artifacts and are not versioned; the report is the versioned summary. Earlier provider-specific runs (Groq) are in [`GROQ_EVAL_REPORT.md`](GROQ_EVAL_REPORT.md).

**Local-model batch (2026-08-21):** a first smoke batch against a fully local setup — `lfm2.5-2.6b-heretic-abliterated` (2.6B) in LM Studio — is documented in [`eval/REPORT_2026-08-21.md`](eval/REPORT_2026-08-21.md): **4 of 5 coding tasks solved** through the Tera Pilot agent (`add-clamp-function`, `fix-missing-return`, `fix-off-by-one-range`, `add-multiply-function`; `review-sql-injection` failed on the final write and was not re-run). This batch also surfaced and fixed a real LM Studio integration bug — the engine rejects generated native tool calls whose content contains quotes (`400 Invalid diff`), which is now worked around by advertising no `tools` schema to LM Studio and parsing the model's native `<|tool_call_start|>[name(arg='…')]` text format instead (see the report's bug list). This is a smoke batch, not a benchmark: 5 tasks, one run each, no repeats, no security tasks, no direct (no-agent) comparison runs yet.

## Audit Export & Verification

Every tool call is recorded in the process-scoped activity log. For tamper-evident evidence you can export the log with Ed25519 signatures and a SHA-256 hash chain, then verify it — even on a different machine using the public key from `~/.tera_pilot/audit_key.pub`:

```bash
tera-pilot audit export --out audit.json   # signed + hash-chained export
tera-pilot audit verify audit.json         # exit 0 = chain intact, 1 = tampering detected
```

> Note: the activity log is process-scoped. In a fresh CLI process it is empty — export from inside a running TUI/Web session (`/audit`, `/audit-signed` slash commands) to capture real activity. The CLI `verify` works on any exported file.

## Migrating from Clew (v2.2.x)

This project was renamed from **Clew** to **Tera Pilot**. Configuration paths changed accordingly. If you used Clew v2.2.x, migrate once:

```bash
mv ~/.clew ~/.tera_pilot
# and, per project:
mv CLEW.md TERA_PILOT.md
```

Environment variables are now `TERA_PILOT_*` (e.g. `TERA_PILOT_PROVIDER`, `TERA_PILOT_MODEL`). The GitHub Action templates generated by `github_automation.py` use the new names automatically.

## Current Limitations

- Tera Pilot does not currently provide Cursor-level inline completion or a native full IDE.
- The old command-oriented `tera-pilot-cli` product is intentionally not distributed; interactive work belongs in the TUI.
- Quality depends on the selected model, provider configuration and repository tests.
- Cloud providers and remote MCP servers still send data outside the machine by design; local-first is not the same as always-offline.
- Enterprise features such as SSO/SCIM, centralized RBAC, formal compliance certifications and managed fleet control are roadmap work.
- The OS sandbox for `execute_command`/`run_code` (macOS `sandbox-exec`, Linux `bubblewrap`) denies network, restricts writes to the workspace and hides sensitive paths, but it is **not** a hardened multi-tenant container/VM. Tera Pilot is safe for **trusted local workflows** (its claims are documented in [Security Posture & Verification](#security-posture--verification)); it is **not** an environment for running fully untrusted code, and it does not promise enterprise security, air-gap, or protection from untrusted code without stronger isolation.
- Benchmark claims are limited to the reproducible evaluation harness (`eval/`, 43 repository tasks; batches in `eval/REPORT_2026-08-19.md` and `eval/REPORT_2026-08-21.md`); only measured claims are published. The harness's own known caveats are recorded in the reports, not the README.

## License

MIT — free to use, modify and integrate.
