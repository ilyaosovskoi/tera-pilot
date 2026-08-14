# Tera Pilot Product Readiness Plan

> Working plan for making Tera Pilot product-ready. Status snapshot: August 2026.
>
> This document answers not "what features can we add next", but: **what must be proven, packaged and stabilized so that Tera Pilot can be safely handed to the first users and small teams**.

---

## 1. Executive Summary

Tera Pilot already has a strong technical foundation for a TUI-first, local-first coding agent:

- the full-screen Textual TUI is the primary interactive interface;
- there is a ReAct runtime, planning, read/edit/command-run/verify tools;
- cloud, BYOK and local providers are supported via Ollama / LM Studio;
- there are workspace restrictions, command policy, approvals, autonomy levels, Guardian, checkpoints/undo;
- there is MCP/ACP, a daemon, Web UI, GitHub automation and a TUI-backed backend adapter;
- there is an activity/audit trail, signed audit export, token/cost tracking and provider routing;
- the public command-oriented `tera-pilot-cli` was removed; CI/GitHub use the same backend as the TUI.

But this is not yet a proven product contour. The main gap is operational reliability and evidence:

1. there is no reproducible evaluation harness on real repository tasks;
2. the test suite is too narrow relative to the size of the runtime;
3. there is no full clean-install/release pipeline;
4. there is no single environment doctor and no clear onboarding from installation to the first task;
5. security/local-first claims are not yet backed by a threat model, egress visibility and independent checks;
6. the GitHub Action is a good template but not yet proven on real repositories;
7. quality, latency, cost, rollback and human-acceptance rates are not measured;
8. enterprise features — identity, RBAC, retention, deployment and support — are absent or roadmap items.

**Bottom line:** Tera Pilot is a technically serious alpha base approaching a controlled private beta, but the current smoke tests do not yet prove readiness for an external beta. For a public product-ready v1, close the P0 items below first rather than expanding the agent capability list.

---

## 2. What product-ready means for Tera Pilot

For Tera Pilot, product-ready does not mean "the agent never makes mistakes" and does not mean parity with Cursor, Claude Code, or GitHub Copilot.

Product-ready means a new user can:

1. install Tera Pilot on a supported OS from a clean environment;
2. choose a local model or a provider/BYOK without reading the source;
3. complete a first safe task in the TUI within minutes;
4. understand what actions the agent took and which permissions it used;
5. see the diff, test results, errors, and a recovery path;
6. get a predictable error on key/model/network/sandbox problems;
7. upgrade to a new version and recover from a failed upgrade;
8. use the CI integration with a machine-readable evidence report;
9. not rely on marketing claims that are not backed by measurements.

### Target readiness levels

| Level | What must be proven | Outcome |
|---|---|---|
| **Private beta** | Reliable TUI, basic security, understandable installation, manual regression checks | 5–20 external users |
| **Product-ready v1** | Reproducible evaluation, clean install, CI evidence, threat model, release process, support docs | Public launch for individual developers and small teams |
| **Team-ready** | Shared policies, roles, retention, deployment guidance, predictable spend and CI workflows | First team pilots |
| **Enterprise-ready** | Identity, RBAC, centralized governance, formal security/compliance and SLA | Sales into regulated/enterprise segments |

Tera Pilot is currently at the **technical alpha base** stage. A controlled private beta can start only after the basic reliability, onboarding and security gates are closed. The label "enterprise-ready" must not be used yet.

### Proposed minimum launch thresholds

These are working internal thresholds for a public v1 decision, not current measured numbers:

- at least 30–50 reproducible repository tasks in the evaluation set;
- task success and test pass rates published with a baseline and a counting methodology;
- at least 80% of new beta users complete a first safe task without author help;
- at least 5 users repeat the core workflow after the first session;
- clean install/upgrade smoke passes on 100% of officially supported OS/Python combinations;
- 100% of evidence reports pass schema validation;
- 0 known unaddressed critical security issues and 0 silent secret leaks in verified paths;
- flaky test rate below 5% in the mandatory CI suite;
- provider failure, cancellation and recovery paths have automated regression tests.

Thresholds can be refined after the first baseline sample, but the metrics themselves and the rules for changing thresholds must be fixed in advance.

---

## 3. P0 — launch blockers for product-ready v1

P0 is the set of tasks without which a stable, trusted product cannot be promised.

### P0.1. Reproducible evaluation harness

**Problem:** the strategy already correctly forbids claiming superiority without reproducible data, but the evaluation contour does not exist yet.

**What to do:**

- create a catalog of benchmark/repository tasks with fixed inputs;
- support task types: bug fix, test repair, refactor, feature addition, code review and documentation;
- run each task in a clean copy of the repository;
- save prompt, base commit, provider/model, duration, cost, tool errors, final diff and test result;
- separate `task_success`, `tests_passed`, `human_accepted`, `rollback_required` and `verification_status`;
- add a baseline: manual, a simple single-agent run, and selected alternative workflows if legally and technically possible;
- run a smoke set on every runtime/provider change.

**Definition of done:**

- a minimum of 30–50 representative tasks for the first baseline sample;
- re-running produces a reproducible result format;
- the report shows success/failure, test pass rate, cost and latency;
- the benchmark is not substituted by tool-call count or answer length;
- only measured claims are published in the README.

**Proposed artifacts:**

- `eval/README.md`;
- `eval/tasks/`;
- `eval/runner.py`;
- `eval/results/schema.json`;
- `tests/test_evaluation_schema.py`.

---

### P0.2. Base TUI scenario reliability

**What to verify on a clean process:**

- starting the TUI without an API key;
- selecting Ollama / LM Studio;
- selecting a cloud provider and a wrong key;
- streaming responses;
- cancelling a task;
- a stuck approval modal;
- provider/network timeout errors;
- workspace/provider/model switching;
- chat load, create and restore;
- diff review, checkpoint and undo;
- long answers, tool errors and partial failures;
- re-running after a runtime crash.

**Definition of done:**

- no expected user failure leads to a traceback or a hung TUI;
- every error has a human explanation and a recovery action;
- streaming, non-streaming and provider retry have equally understandable UX;
- there are integration tests at least for the main happy path and the key failure paths.

The current 33 smoke checks are useful for TUI structure but do not replace behavioral integration tests with a fake provider.

---

### P0.3. Clean install, release and upgrade path

**What to do:**

- choose officially supported Python and OS versions;
- verify installation from source and from a built wheel/sdist;
- verify the npm launcher on macOS/Linux/Windows, if those platforms remain in metadata;
- decide whether the package is published on PyPI, npm, or only via GitHub Releases;
- pin dependency versions and check conflicting SDKs;
- add SBOM, dependency audit and reproducible-build metadata;
- sign release artifacts or publish provenance/attestation;
- add `--version` and unified diagnostics output;
- prepare changelog, migration notes, rollback and vulnerable-release withdrawal instructions;
- add CI for build, install and import smoke in a clean environment;
- verify that secrets, local configs and test data do not leak into the package artifact.

**Definition of done:**

```text
clean environment
  -> install
  -> launch tera-pilot-tui
  -> configure local/BYOK provider
  -> run safe sample task
  -> upgrade to next patch version
  -> reopen session
```

This scenario must be automated and pass on every release candidate version.

---

### P0.4. Onboarding and environment doctor

Today the README explains the architecture, but a product-ready user should not have to diagnose the environment by hand.

**A single doctor/onboarding flow is needed that checks:**

- Python and OS version;
- presence of Textual and provider SDK dependencies;
- availability of the selected provider;
- presence and validity of the API key without printing the key itself;
- availability of the Ollama / LM Studio endpoint;
- the selected workspace and read/write permissions;
- Git and test runner;
- sandbox/native acceleration status;
- MCP configuration and potentially dangerous env values;
- storage paths for chats, logs, audit keys and permissions files.

**Definition of done:**

- a new user gets a clear `ready / warning / blocked` result;
- every problem includes a concrete fix action;
- the doctor does not send code or secrets to an external network;
- the TUI can open the doctor from the start screen or settings.

Proposed interface: `tera-pilot-tui --doctor` or a built-in `/doctor` command. This does not bring back the old user-facing CLI: it is a diagnostic mode of the TUI/launcher, not a separate command-oriented agent product.

---

### P0.5. Threat model and security boundaries

Local-first is an architectural direction, not proof of no leaks. Documents and checks are needed.

**Document:**

- trust boundaries: TUI, runtime, provider SDK, MCP servers, subprocesses, GitHub, daemon and Web UI;
- what data can leave the machine;
- what happens on cloud provider, web search, MCP and GitHub calls;
- which secrets are available to parent and child processes;
- workspace sandbox limits and known escape paths;
- command policy, autonomy levels and Guardian: what exactly they guarantee and what they do not;
- behavior when the Rust/native sandbox is disabled;
- local storage of chats, logs, audit keys and provider configuration;
- the threat model for prompt injection from repositories, web pages and MCP responses;
- the threat model for CI runners and pull request content.

**Definition of done:**

- `THREAT_MODEL.md` and `SECURITY_BOUNDARIES.md` are published;
- a dependency/security scan has been run;
- there are tests for path traversal, command policy bypass, secret leakage and prompt injection boundaries;
- permissions and redaction are separately verified for `~/.tera_pilot`, API keys, chats, logs, MCP env and provider SDK logging;
- documented data-retention/deletion behavior exists for chats, logs, audit keys and reports;
- known limitations are explicitly listed in the README;
- no "zero telemetry", "air-gapped" or "secure" claim exceeds its proven scope.

---

### P0.6. Machine-readable evidence contract

The TUI backend can already produce a report, but the contract must become a stable product API.

**Fix schema v1:**

- `schema_version`;
- `ok`, `error`, `status`;
- actual provider/model;
- workspace identity without unnecessary secrets;
- iterations, duration and cost/token metadata;
- tool names without raw tool arguments by default;
- verification status, where `ran`, `passed`, `failed`, `unknown` are not conflated;
- test result and exit code, if tests were run;
- final output as a potentially sensitive field;
- optional signed audit reference.

**Definition of done:**

- the schema is published and versioned;
- backward compatibility is checked by a test;
- a malformed provider failure still produces valid JSON;
- the report can be processed without importing Tera Pilot internals;
- secrets do not leak into reports and logs by default.

---

### P0.7. Basic quality gate

The current test contour is too small for the number of runtime modules.

**Add:**

- unit tests for providers, config, policy, sandbox, diff, persistence and report schema;
- integration tests with a deterministic fake provider;
- regression tests for known bugs;
- TUI bridge tests: prompt, cancel, approval, provider switch, workspace switch;
- API/daemon response contract tests;
- lint/type checks for mutable modules;
- dependency audit and secret scanning;
- coverage report with an explanation of exclusions.

**Definition of done:**

- CI is mandatory for merge;
- all P0 regression tests pass on a clean environment;
- critical security paths are covered by tests;
- flaky tests are not masked by retry without a separate issue.

---

## 4. P1 — what is needed for the first paying/team users

P1 does not necessarily block private beta, but without it adoption will be weak.

### P1.1. Real CI/CD scenarios

- GitHub Action tested on at least 3–5 different repositories;
- a stable install artifact published (PyPI or an explicitly chosen GitHub Release/private package path) compatible with team workflows;
- GitLab CI template;
- JSON + JUnit/SARIF output where it is genuinely useful;
- PR review mode with read-only default;
- a separate explicit mode for write operations;
- artifact retention and redaction guidance;
- clear configuration of `TERA_PILOT_PROVIDER`, `TERA_PILOT_MODEL` and secret variables;
- timeout, budget cap and cancellation for CI;
- isolated runner documentation.

**Definition of done:** an external user can enable the workflow from the docs without reading Python code manually.

### P1.2. Product UX TUI

The TUI must be not just a pretty window, but a reliable workspace.

Check and improve:

- welcome screen with one clear next step;
- provider/model status without hidden config assumptions;
- visible workspace and policy mode;
- clear thought/tool/result separation without extra noise;
- fast diff review;
- stop/retry/continue after an error;
- chat history and session search;
- copy/export of results;
- keyboard accessibility and terminal resize;
- large outputs, unicode, color themes and screen-reader-friendly fallback;
- no "dead end" screens.

**Metric:** a user opening the TUI for the first time completes a safe read-only task without developer help.

### P1.3. Provider onboarding

- one recommended local path: Ollama or LM Studio;
- one recommended BYOK path;
- model check before the first task;
- a clear message about where code is sent;
- provider capability matrix: streaming, tools, vision, structured output, local/remote;
- graceful fallback if the model lacks required capabilities;
- timeout, retry and budget configuration without manually editing config JSON.

### P1.4. Real user workflows

Focus on 3–4 scenarios, not dozens of feature labels:

1. fix failing tests;
2. safely make a small refactor with diff review;
3. do a read-only code review;
4. run a local model on a private repository.

For each scenario:

- short documentation;
- a demo repository;
- expected output;
- failure recovery;
- an evaluation task;
- success criteria.

### P1.5. Support and feedback loop

- issue templates for bug/security/feature;
- a public compatibility matrix;
- a minimal troubleshooting guide;
- `SECURITY.md` and a security contact;
- release notes;
- a version support policy;
- telemetry opt-in only if ever needed, with a transparent description;
- a feedback form or `/feedback` command that does not send repository content without consent.

---

## 5. P2 — team and enterprise readiness

P2 must not be built until demand is confirmed through private beta and the first team pilots.

### P2.1. Identity and access

- OIDC/SAML;
- SSO;
- RBAC;
- team/project membership;
- service accounts for CI;
- model/tool allowlists;
- centralized autonomy policy management.

### P2.2. Centralized policies and audit retention

- policy distribution with versioning;
- immutable audit export;
- retention rules;
- export to SIEM/log platforms;
- audit of user, agent and subagent actions;
- verification of signed records outside Tera Pilot;
- redaction and data classification.

### P2.3. Deployment and isolated contours

- reproducible Docker/VM deployment;
- offline/air-gapped bundle;
- private package mirror guidance;
- network allowlist/egress proxy;
- backup/restore for sessions and audit keys;
- OS-specific sandbox documentation;
- disaster recovery and upgrade rollback.

### P2.4. Economics and operations

- budgets per user/team/project;
- provider cost attribution;
- quotas and rate limits;
- concurrency controls;
- health/readiness endpoints for the daemon;
- dashboards for latency, errors and provider availability;
- SLA only after operational data has accumulated.

### P2.5. Formal assurance processes

- independent security review;
- penetration test within product scope;
- dependency/SBOM process;
- vulnerability disclosure program;
- SOC 2 / ISO 27001 roadmap only with real enterprise demand;
- legal documents for code handling and third-party providers.

---

## 6. What should not be done yet

To avoid spreading thin, do not prioritize before P0/P1:

- a Cursor-level inline autocomplete of our own;
- a full new IDE instead of TUI + a thin IDE/ACP bridge;
- dozens of new provider adapters without capability tests;
- complex multi-agent orchestration for feature count;
- automatic merge/commit without explicit policy;
- enterprise SSO before the first team pilots;
- public benchmark claims without an evaluation harness;
- expanding the marketing positioning before onboarding and reliability gaps are closed.

Tera Pilot's main product moat is not the number of tools. It is **predictable execution, local control, vendor independence and verifiable evidence of results**.

---

## 7. Priority 90-day roadmap

### Days 0–30 — evidence base and P0 reliability

- fix the supported OS/Python/provider matrix;
- create the threat model and security boundaries;
- build a fake-provider integration suite;
- add the evaluation harness skeleton and 30 baseline tasks;
- freeze evidence schema v1;
- implement the doctor/onboarding flow;
- write the release checklist;
- add clean-install smoke to CI;
- close the known TUI failure paths.

**Result:** Tera Pilot can be safely tested with external private-beta users.

### Days 31–60 — private beta and CI workflows

- run 5–10 external user sessions;
- collect task success, test pass, recovery and UX feedback;
- fix the top-10 failure modes;
- test the GitHub Action on real repositories;
- add a GitLab template or explicitly defer it;
- build a provider setup wizard;
- add JSON/JUnit/SARIF output if users actually require it;
- publish troubleshooting and support processes.

**Result:** evidence that the product can be used without the author nearby.

### Days 61–90 — product-ready v1 decision

- re-run evaluation after fixes;
- compare baseline and new version;
- run a security review of critical paths;
- assemble release candidate wheel/sdist/npm artifacts;
- perform an upgrade/rollback test;
- freeze evidence schema v1;
- prepare changelog, docs, demos and known limitations;
- make a go/no-go decision on public v1.

**Go:** P0 criteria are closed, no critical security issues, 5–10 users repeat the workflow.

**No-go:** metrics are missing, clean install is not reproducible, provider failures are not explainable, or users cannot recover after an error.

---

## 8. Release checklist

### Product

- [ ] Primary users and 3–4 core workflows are clearly defined.
- [ ] The TUI is the main interactive surface.
- [ ] The README does not promise unsupported IDE/enterprise/benchmark capabilities.
- [ ] Current limitations are published.

### Reliability

- [ ] Full TUI happy path passes on a clean install.
- [ ] Provider errors, timeout, cancellation and approval recovery are tested.
- [ ] Chats, workspace, diff, checkpoint and undo work after restart.
- [ ] No known critical/high regression issues.

### Security

- [ ] Threat model is published.
- [ ] Workspace/path/command policy are verified with adversarial tests.
- [ ] Prompt injection boundaries are documented.
- [ ] Secrets do not leak into logs/reports by default.
- [ ] Network egress behavior is transparent.
- [ ] `local-first` is not used as a synonym for `always offline`.

### Evidence

- [ ] Report schema v1 is published.
- [ ] Provider/model are actual, not just CLI/config hints.
- [ ] Verification status does not call an unrun check successful.
- [ ] Tool arguments are redacted by default.
- [ ] Final output is explicitly marked as potentially sensitive.

### Release engineering

- [ ] Build wheel/sdist/npm artifact passes.
- [ ] Install/import smoke passes in a clean environment.
- [ ] Version and changelog are synchronized.
- [ ] Upgrade and rollback are verified.
- [ ] CI is required for merge and release.
- [ ] Security contact and issue templates are published.

### User readiness

- [ ] The first task is completed in under 10 minutes after installation.
- [ ] There is a local provider path and a BYOK path.
- [ ] `/doctor` or equivalent explains typical problems.
- [ ] There is a demo repository and sample tasks.
- [ ] There is a troubleshooting guide.
- [ ] There is a way to report a bug without sending private code.

---

## 9. Metrics to start collecting

### Quality

- task success rate;
- test pass rate;
- human acceptance rate of diffs;
- rollback rate;
- unverified completion rate;
- mean time to recovery;
- provider failure rate.

### UX

- time to first successful task;
- setup abandonment rate;
- share of tasks completed without manual help;
- number of retries/cancels per task;
- approval comprehension feedback;
- repeat usage after the first session.

### Trust and safety

- policy violation attempts;
- unexpected network calls;
- redaction failures;
- audit verification success;
- percentage of runs with visible evidence;
- security issues by severity and time to fix.

### Operations

- p50/p95 latency;
- cost per successful task;
- token usage;
- provider uptime/error rate;
- CI workflow completion rate;
- artifact/report parse success.

This data does not need to be collected via hidden telemetry. Local evaluation reports, opt-in beta feedback and CI artifacts are enough to start.

---

## 10. Main decision gate

Tera Pilot can be called **product-ready v1 for individual developers and small teams** only when all of the following hold:

1. reproducible evaluation shows measurable task success and test pass rates;
2. clean install and upgrade pass on officially supported environments;
3. a new user completes the core workflow without author help;
4. provider/network/sandbox failures are explainable and recoverable;
5. the threat model and security boundaries are published;
6. the evidence report is stable and usable in CI;
7. the GitHub workflow is verified on real repositories;
8. there are no critical security findings;
9. at least a few external users return to the product after the first session;
10. the README honestly describes limitations and does not present a roadmap as existing functionality.

Until these conditions are met, say:

> Tera Pilot is a strong TUI-first technical alpha base for private, local-first and vendor-neutral coding-agent workflows.

After they are met:

> Tera Pilot is a product-ready self-hosted coding agent for controlled work with private repositories, local/BYOK models and CI evidence.

But even after v1, do not claim that Tera Pilot replaces Cursor autocomplete, guarantees security, or is enterprise-ready without the corresponding security and compliance scope.
