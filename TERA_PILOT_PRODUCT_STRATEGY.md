# Tera Pilot Product Strategy

> Working strategy document. Assessment status: August 2026.

## 1. Executive Summary

Tera Pilot must not try to be another Cursor or a free clone of GitHub Copilot. In the mass market it loses to them on distribution, native IDE UX, onboarding, and enterprise infrastructure.

The realistic entry point for Tera Pilot is a **private, multi-model, verifiable coding agent for sensitive repositories, local models, CI, and isolated environments**.

Working positioning:

> **Tera Pilot — self-hosted, vendor-neutral coding agent with verifiable actions for private and air-gapped codebases.**

## 2. The Market Problem

The coding-agent market has moved from simple autocomplete to agents that can:

- read and index the repository;
- plan a multi-step task;
- edit multiple files;
- run commands and tests;
- call external tools via MCP;
- create changes, branches, and pull requests.

Basic agent capabilities are quickly becoming table stakes. Competition is shifting to four areas:

1. **Result quality** — the task is actually done and tests pass.
2. **Trust** — the user understands what the agent did and why.
3. **Control** — sandbox, approvals, policies, network boundary.
4. **Integration** — the agent lives in the IDE, Git, CI/CD, and the team process.

The core market pain is the gap between usage and trust. According to the Stack Overflow Developer Survey 2025, 46% of developers rather do not trust the accuracy of AI tools, while 33% trust them.

## 3. Product Goals

### 3.1. Primary Goal

Make Tera Pilot the most understandable and verifiable way to run coding agents locally, with any chosen model, without losing control over the code and the agent's actions.

### 3.2. Product Objectives

1. **Provable quality**
   - Introduce a reproducible evaluation harness.
   - Measure task success, test pass rate, rollback rate, cost, and latency.
   - Never claim superiority without our own reproducible data.

2. **Trust-first execution**
   - Make every agent action explainable and observable.
   - Surface permissions, changes, commands, external calls, and verification results.
   - Keep an exportable, verifiable audit trail.

3. **Local-first and vendor neutrality**
   - Support local models via Ollama / LM Studio.
   - Support BYOK and multiple cloud providers.
   - Do not hold the user hostage to a single model or cloud.

4. **TUI-first and backend-ready workflow**
   - Make the full-screen TUI the primary user product.
   - Separate the polished interactive experience from backend integrations.
   - Produce machine-readable results through a TUI-backed adapter for CI, GitHub, and the daemon — without a public command-oriented CLI.

5. **Low onboarding friction**
   - Install Tera Pilot with one understandable command.
   - Quickly verify environment, model, keys, and sandbox.
   - Provide a working path from installation to the first successful task in minutes.

6. **IDE bridge, not a new IDE at any cost**
   - First ship a solid VS Code / ACP bridge.
   - Pass workspace, selection, diagnostics, and diffs into the familiar editor.
   - Do not build a separate IDE until the need is proven.

7. **Enterprise readiness without premature bureaucracy**
   - First: threat model, policy docs, reproducible deployment, audit export, and network controls.
   - Then: RBAC, OIDC/SAML, retention policies, support, and formal certifications.

## 4. Audience Segments

### Segment A — Privacy-first individual developers

**Problem:** the developer wants AI help but does not want to send code to an unknown cloud.

**Need:** local models, BYOK, transparent network calls, no telemetry, workspace sandbox.

**Why Tera Pilot fits:** Ollama / LM Studio, multiple providers, TUI/Web, policy and audit capabilities.

**Priority:** high.

### Segment B — Senior engineers and DevOps power users

**Problem:** ordinary IDE plugins are too limited for long multi-step tasks and automation.

**Need:** full-screen TUI, daemon/backend workflows, Git, MCP, subagents, cost/model routing.

**Why Tera Pilot fits:** the runtime and tools are already built for agentic workflows, TUI bridge, and daemon.

**Priority:** high.

### Segment C — Sensitive repositories and regulated teams

**Examples:** fintech, healthcare, government, defense, internal infrastructure, contractors under NDA.

**Problem:** cloud coding agents are prohibited or require complex approvals.

**Need:** self-hosting, air-gapped deployment, local models, granular approvals, signed audit, policy enforcement.

**Why Tera Pilot fits:** the local-first architecture and action control are a strong foundation.

**Limitation:** currently missing enterprise identity, deployment packaging, evidenced documentation, and customer support.

**Priority:** high as a strategic beachhead, but do not promise enterprise-readiness until the gaps are closed.

### Segment D — Small engineering teams

**Problem:** the team wants to automate maintenance, tests, refactoring, and review without an expensive platform.

**Need:** shared workflow, CI, pull request reports, predictable cost, simple installation.

**Why Tera Pilot fits:** daemon, TUI-backed GitHub automation, multi-provider routing.

**Priority:** medium-high.

### Segment E — AI/agent builders and internal platform teams

**Problem:** the team wants to embed a coding agent into its own system while keeping model choice and policies.

**Need:** runtime, MCP/ACP, API, event stream, audit, sandbox, extensibility.

**Why Tera Pilot fits:** Tera Pilot can be a control plane and runtime, not just a user-facing chat.

**Priority:** medium-high.

### Segment F — Open-source and self-hosting community

**Problem:** desire to study and modify the agent runtime without vendor lock-in.

**Need:** Apache-2.0, local run, understandable architecture, extensible providers/tools.

**Why Tera Pilot fits:** open Python runtime and a wide set of integrations.

**Priority:** medium; this is an adoption and feedback channel, not necessarily immediate revenue.

## 5. Non-Target Segments at the First Stage

1. **A user who needs the best inline autocomplete.**
   Tera Pilot must not try to beat Cursor Tab or GitHub Copilot completion.

2. **A team fully committed to GitHub Enterprise + Copilot.**
   Their switching cost is too high without a concrete privacy/air-gap reason.

3. **An enterprise that only needs SOC 2, SSO, and a procurement checkbox.**
   Tera Pilot must first build a provable security/deployment baseline.

4. **A user expecting a fully autonomous agent without review.**
   Tera Pilot is built around controlled autonomy, not unconditional trust.

## 6. What Tera Pilot Already Does

### Agent runtime

- ReAct plan/read/write/run/verify loop.
- 30+ built-in tools.
- File, Git, command, search, web, MCP, and office tools.
- Self-verification and recovery paths.
- Task decomposition, subagents, and watchdog.

### Model layer

- Multiple cloud providers.
- OpenRouter.
- Ollama and LM Studio.
- BYOK and provider/model override.
- Auto-routing and multi-provider consensus.

### Trust and control

- Workspace path restrictions.
- Command policy and project approvals.
- Tool autonomy levels.
- Diff review and confirmation gates.
- Guardian risk assessment.
- Checkpoints/undo.
- Activity/audit logging.
- Ed25519 signed audit support.

### Surfaces and integrations

- Web UI.
- Textual TUI.
- TUI-backed automation adapter.
- HTTP daemon + SSE.
- MCP client/server.
- ACP server surface.
- GitHub API automation and Action template.

## 7. Feature Parity and Sustainable Advantage

### This is already hygiene-level minimum

- ReAct agent loop.
- File editing.
- Command execution.
- Git integration.
- MCP.
- BYOK.
- Local models.
- Plan/Act and approvals.
- TUI/Web interface.

Having these features is not a moat by itself.

### Potential sustainable advantage of Tera Pilot

1. Vendor-neutral model control plane.
2. Local/air-gapped execution.
3. Signed, exportable, verifiable action history.
4. Guardian + policy + sandbox as one workflow.
5. Multi-provider consensus for high-risk changes.
6. The same runtime for TUI, Web UI, daemon, CI, and MCP.
7. The ability to explain not only the answer, but the chain of actions and evidence.

## 8. Competitive Frame

| Competitive category | Competitor strength | How Tera Pilot responds |
|---|---|---|
| Cursor/Windsurf | Native IDE UX, speed, polished multi-file editing | Do not copy the IDE; deliver a trust-first runtime and a thin IDE bridge |
| GitHub Copilot | Distribution, GitHub, PR/Issue/Actions, enterprise controls | Privacy, self-hosting, local models, vendor neutrality |
| Claude Code | Model quality and a deep terminal agent | Multi-model support, local run, audit/policy layer |
| Cline | VS Code, BYOK, MCP, approvals | TUI/daemon, multi-provider orchestration, audit and governance |
| OpenHands | Open agent platform, web/containers, MCP | A lighter local-first runtime and unified CLI/TUI/Web/daemon |
| Aider and CLI tools | Simplicity, Git workflow, power users | A richer control plane and an extensible agent architecture |

## 9. Roadmap by Priority

### P0 — evidence and a reliable foundation

- Machine-readable backend result/report.
- Reproducible evaluation harness.
- Task success/test/rollback/cost/latency metrics.
- Public threat model and security boundaries.
- Network egress visibility.
- Documented audit export/verification.
- One-command onboarding and environment doctor.

### P1 — working CI/CD scenario

- GitHub Action with a report artifact.
- GitLab CI template.
- PR review mode.
- JUnit/SARIF/JSON output.
- Explicit provider/key configuration.
- Isolated runner guidance.

### P1 — IDE bridge

- VS Code extension or ACP adapter.
- Workspace/selection/diagnostics context.
- Native diff/approval handoff.
- JetBrains integration after demand is verified.

### P2 — team/enterprise controls

- OIDC/SAML.
- RBAC and model/tool allowlists.
- Central policy distribution.
- Audit retention and export.
- Air-gapped installation bundle.
- Support and a formal compliance program.

### P2 — durable intelligence layer

- Consensus as a confidence/evidence workflow.
- Benchmark-driven routing.
- Regression evaluation after runtime/provider changes.
- Replayable sessions.
- Cost/quality optimization based on real task outcomes.

## 10. Success Metrics

### Product quality

- Task success rate.
- Test pass rate after agent changes.
- Human acceptance rate of diffs.
- Rollback rate.
- Unverified completion rate.
- Mean time to recover from failure.

### Trust

- Percentage of actions with visible evidence.
- Approval override rate.
- Policy violation rate.
- Audit verification success rate.
- Number of unexpected network calls.

### Adoption

- Time to first successful task.
- Weekly active TUI users.
- Percentage of users on local/BYOK models.
- Repeat usage after the first session.
- Number of repositories running Tera Pilot in CI.

### Business / ecosystem

- Active open-source contributors.
- Repositories using the GitHub Action.
- MCP/ACP integrations.
- Paid support or enterprise pilots.
- Conversion from individual usage to team usage.

## 11. Claims Discipline

Until reproducible data exists, do not claim:

- that Tera Pilot is smarter than Claude Code;
- that it has better SWE-bench performance;
- that it is enterprise-ready;
- that it guarantees the absence of vulnerabilities;
- that "100% offline" applies to cloud providers or MCP endpoints.

Correct formulations:

- "local-first";
- "supports local and cloud providers";
- "vendor-neutral";
- "provides policy, approval and audit mechanisms";
- "designed for private and isolated workflows";
- "verification evidence is exposed when the workflow runs it".

## 12. Decision Rule

Continuing to develop Tera Pilot as a standalone product makes sense if the team is ready to:

1. choose a trust-first/local-first beachhead;
2. measure real outcomes, not just feature count;
3. improve onboarding and CI/IDE integration;
4. prove security and reproducibility;
5. not enter a direct race with Cursor on autocomplete and polish.

If the goal is a mass-market IDE for all developers, the current architecture and distribution are insufficient. If the goal is **controlled, private, vendor-independent coding-agent infrastructure**, Tera Pilot has a real and defensible market opportunity.
