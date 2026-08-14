# Tera Pilot Threat Model

> Working security document. Assessment status: August 2026.
> Public version — no internal details that would help an attacker.
> Related documents: [`TERA_PILOT_PRODUCT_STRATEGY.md`](TERA_PILOT_PRODUCT_STRATEGY.md) (§3.2 "Trust-first execution", §11 "Claims discipline"), [`TERA_PILOT_PRODUCT_READINESS.md`](TERA_PILOT_PRODUCT_READINESS.md).

## 1. Purpose

Tera Pilot is a local-first, vendor-neutral coding agent. It runs on the user's machine,
reads repositories, executes commands, talks to LLM providers and (when explicitly
configured) to external MCP servers. The goal of this document is to **honestly describe
the security boundaries**: what is protected, from whom, by which mechanisms, and what
residual risk remains.

This is not a certification document (not SOC 2, not ISO 27001) and not a guarantee of
the absence of vulnerabilities. It is an engineering threat model on which the product
development is built.

## 2. Assets (what we protect)

| Asset | Where it lives | Value |
|---|---|---|
| Source code and repository data | Workspace directory | Confidentiality, integrity |
| Prompts and dialogs | Process memory, `~/.tera_pilot/` | Confidentiality |
| Provider API keys | env / `~/.tera_pilot/config.json` | Confidentiality, money |
| Audit signing key (Ed25519) | `~/.tera_pilot/audit_key` (0600) | Evidentiary integrity |
| Activity log / audit trail | Process memory + exports | Non-repudiation, integrity |
| Files created by the agent | Workspace | Integrity |
| Commands executed by the agent | Shell | Machine integrity |

## 3. Trust boundaries

1. **User's local machine** — the most trusted zone.
2. **LLM providers (cloud)** — semi-trusted: they receive prompts, they do NOT receive
   local keys. Traffic leaves the machine **by design** — local-first ≠ always-offline.
3. **Local models (Ollama / LM Studio)** — traffic is restricted to localhost.
4. **MCP servers** — connected **only explicitly** (`~/.tera_pilot/mcp.json`);
   write-capable external tools are a separate deliberate user decision.
5. **Repository/content** — **untrusted input**: it may contain instructions aimed at
   the agent (prompt injection), malicious scripts and tests.

## 4. Threat scenarios (threat actors)

| # | Threat | Attacker | Vector |
|---|---|---|---|
| T1 | Prompt injection via repo files/README/issues | Untrusted content | Agent reads a file with instructions "ignore previous rules, run X" |
| T2 | Execution of a malicious command/script from the repo | Untrusted content | `execute_command`, `run_code`, tests, Makefile, postinstall scripts |
| T3 | Agent escapes the workspace | Malicious prompt, planning error | `write_file`/`delete_file`/`git_*` on absolute paths outside the project |
| T4 | Theft/leak of API keys | Malicious prompt, compromised MCP | Agent reads `.env`/`config.json` and puts the key into output or an external call |
| T5 | Code leak to the outside | Misconfiguration, compromised MCP/provider | web_fetch/web_search with code content, cloud provider with logs |
| T6 | Audit-log tampering (hiding actions) | Insider, malware, compromised repo | Editing/deleting/reordering activity log records |
| T7 | Social engineering via CLI output | Untrusted content | Command output contains "run this next" |
| T8 | Supply chain: malicious package/dependency | Supplier | Fake `tera-pilot` package, compromised dependency |

## 5. Controls mapping

| Threat | Tera Pilot mechanism | Module |
|---|---|---|
| T1, T2, T7 | Prompt scaffold with safety guardrails: external content = untrusted; instructions from files/output have no priority over the system prompt | `agent_runtime/prompts.py` |
| T2 | Command policy: allowlist/denylist of commands; project approvals; autonomy levels (`always_ask` / `new_files_only` / `never_ask`); diff review gate; Guardian risk assessment of tool calls | `command_policy.py`, `agent/guardian.py`, `progressive_tools.py` |
| T3 | Workspace sandbox: file operations restricted to the selected project; checkpoint/undo for rollback | `context_manager.py` (path restrictions), `checkpoint.py` |
| T4 | Prompts do not require reading keys; keys live in env/config, not in the repository; `encrypted_prompt.py` (ChaCha20-Poly1305) for enterprise prompts; UI hints "do not paste keys into code" | `agent/encrypted_prompt.py` |
| T5 | The single network egress point is `web_search_backend.py` (zero-telemetry, only explicit web_search/web_fetch calls); MCP connects explicitly; providers are chosen by the user (BYOK) | `web_search_backend.py`, `mcp_manager.py` |
| T6 | Activity log (append-only, in memory) + signed export: Ed25519 signature per record + hash chain (SHA-256 prev_hash+payload) — tampering/deletion/reordering is detected; verification: `tera-pilot audit verify` | `activity_log.py`, `audit_signing.py`, `audit_cli.py` |
| T8 | Dependencies pinned in requirements.txt; local installation from source; MIT license for code review | `requirements.txt`, `pyproject.toml` |

Additionally: **network egress visibility** — the `web` category in the activity log marks
all external agent calls, so the user sees "the agent left the project".

## 6. Verification and evidence

What a user can verify themselves:

1. **Action review** — Activity Stream / activity log shows every agent action:
   commands, files, web calls, statuses (`ok`/`error`/`rejected`/...).
2. **Signed export** — `tera-pilot audit export` (or `/audit-signed` in the TUI)
   produces JSON where every record is signed with Ed25519 and hash-linked to the previous one.
3. **Verification outside Tera Pilot** — `tera-pilot audit verify <file>` recomputes
   the hash chain and checks all signatures; the public key lives in
   `~/.tera_pilot/audit_key.pub` (format: magic `CLWA1` + 32 raw bytes), so verification
   can be done on another machine.
4. **Diffs and approvals** — diff review before writing files, confirmation gates,
   checkpoint/undo — the user controls changes before and after.

## 7. Residual risks (honestly acknowledged)

- Cloud providers and remote MCP servers receive the data the agent includes in
  prompts/tools — **this is by design**. "100% offline" does not apply to cloud providers.
- The model can make mistakes; automatic verification does not guarantee correctness of
  the result (a reproducible evaluation harness is needed — in the P0 roadmap).
- Rust acceleration (sandbox/circuit breaker/compaction) is optional; without it
  pure-Python fallbacks are used.
- No formal certifications, RBAC/OIDC/SAML, centralized policy distribution or fleet
  control — this is the P2 roadmap.
- The activity log lives in process memory; on a process crash unsaved records are lost
  (export is up to the user).
- Prompt injection cannot be fully eliminated at the prompt level — defense in depth:
  sandbox + policy + approvals + audit.

## 8. What we do NOT claim

Per claims discipline (strategy, §11), we do not claim that Tera Pilot:

- is "smarter" than other agents or has a better SWE-bench;
- is enterprise-ready (until P0/P2 gaps are closed);
- guarantees the absence of vulnerabilities;
- is fully offline when using cloud providers or MCP.

Correct formulations: "local-first", "supports local and cloud providers",
"vendor-neutral", "provides policy, approval and audit mechanisms",
"designed for private and isolated workflows", "verification evidence is exposed
when the workflow runs it".
