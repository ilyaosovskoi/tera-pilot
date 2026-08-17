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

These mechanisms provide control and evidence; they are not a claim of formal SOC 2, ISO 27001, or vulnerability-free code. See [`TERA_PILOT_PRODUCT_STRATEGY.md`](TERA_PILOT_PRODUCT_STRATEGY.md) for the security and product roadmap, and [`THREAT_MODEL.md`](THREAT_MODEL.md) for the public threat model and trust boundaries.

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

## Reproducible Evaluation

The `eval/` harness (P0.1) runs real repository tasks against the agent and records schema-valid results in `eval/results/`. It ships **43 tasks** across bug fixes, test repair, refactoring, features, code review and documentation (`eval/tasks/`), each with a clean-copy fixture repo and a baseline-verified test command:

```bash
python3 -m eval.runner check                       # structural check of all tasks
python3 -m eval.runner smoke                       # fake-driver smoke set (CI)
python3 -m eval.runner run eval/tasks/<task_id> --driver api \
    --api-base http://127.0.0.1:18734 --api-token <token>   # live agent run
python3 -m eval.runner report --dir eval/results   # summary
```

**Current results:** the first live run of `fix-missing-return` (a bug-fix task whose fixture tests fail at baseline) produced a working fix — the task's `pytest` passes (`2 passed`) and verification is `passed` — but the api driver recorded the run as `error` because the SSE stream did not deliver its final `done` event within the 300 s task timeout. Driven directly (SSE probe), the same agent completes the task in ~16 s. This driver limitation is tracked for the next release.

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
- Benchmark claims are limited to the reproducible evaluation harness (`eval/`, 43 repository tasks). Known harness limitation: the api driver records a run as `error` if the 300 s SSE stream times out even when the agent's fix already passes the task's tests.

## License

MIT — free to use, modify and integrate.
