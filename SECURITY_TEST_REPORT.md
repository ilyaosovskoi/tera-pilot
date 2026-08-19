# Tera Pilot Security Test Report

**Date:** 2026-08-17
**Version:** 2.3.4 (main, working tree)
**Method:** white-box testing against the threat model in `THREAT_MODEL.md` (§4–§5, threats T1–T8), at the code, unit, and HTTP levels — fully local, deterministic, no network and no LLM provider calls.

---

## 1. Methods

The assessment was performed in four layers:

1. **Static review of security controls** — reading the key enforcement code: the path sandbox (`ToolEngine._resolve_path` / `_validate_command_paths`), the command sanitizer (`_sanitize_command`), the command policy (`command_policy.py`), Guardian, the audit trail (Ed25519 + hash chain), local API auth/CORS, and `EncryptedPromptStore` (ChaCha20-Poly1305).
2. **Automated security suite** — `tests/test_security_suite.py` (84 tests, 6 groups, see §2).
3. **Manual offensive probes** — scenarios that don't fit into ready-made tests: git config command execution, `~` non-expansion, symlink escapes, path-prefix parent spoofing, CORS origin echoing, constant-time token comparison.
4. **Micro-benchmarks** of security hot paths — `benchmarks/bench_security.py` (cost of the controls per agent action).

Run:

```bash
python3 -m pytest tests/test_security_suite.py tests/test_tool_engine_sandbox.py -q
python3 benchmarks/bench_security.py
```

---

## 2. Test matrix mapped to the threat model

| Threat | Control | Tests | Result |
|---|---|---|---|
| T2 — malicious command execution | Sanitizer: metacharacters `; && \| \| \| > < backtick $ \n` | 9 (parametrized) | ✅ blocked |
| T2 | Sanitizer: disallowed binaries and dangerous flags (`curl`, `python3 -c`, `pip install`, `npm run`, `git clone/push`, …) | 11 (parametrized) | ✅ blocked |
| T2 | Legitimate commands (`git status`, `pytest -q`, `python3 script.py`, …) keep working | 8 (parametrized) | ✅ allowed |
| T2 | Unbalanced quotes / empty commands — no crash | 2 | ✅ |
| T3 | `_resolve_path`: escapes `../`, absolute paths, `sub/../../` | 6 (parametrized) | ✅ `PermissionError` |
| T3 | Path-prefix parent spoofing (`/tmp/ws` vs `/tmp/ws-evil`) | 1 | ✅ component-wise compare |
| T3 | Symlink escape (file and directory) | 2 | ✅ |
| T3 | File operations outside workspace (`_read/write/delete/rename/mkdir`) | 1 (5 calls) | ✅ `PermissionError` |
| T3 | Workspace-root deletion protection (`_delete_file(".")`, root rename) | 2 | ✅ `[REFUSED]` |
| T3 | `_validate_command_paths`: `cat /etc/passwd`, `grep -r /`, `rm -rf ../`, `find /`, `mv /etc/hosts`, … | 9 (parametrized) | ✅ `[SECURITY ERROR]` |
| T3 | git `-c`/`config` with exec-capable keys (`core.fsmonitor`, `core.editor`, `sshCommand`, `pager`, `askpass`, `hooksPath`, `credential.helper`, `diff.*.textconv`, `filter.*.clean/.smudge`) | 1 (15 commands) | ✅ `[SECURITY ERROR]` — see §3 |
| T3 | Diff/patch path traversal: multi-file diff with `+++ b/../../outside` and absolute paths in `_apply_diff` | 2 | ✅ `[SECURITY ERROR]` / `[DIFF ERROR]` |
| T3 | `~` is not expanded (shell=False) — keys don't leak | 1 | ✅ |
| T5 | web_fetch: only `http(s)` — `file://`/`ftp://`/`javascript:` rejected before network/filesystem | 1 | ✅ |
| T1/T7 | System prompt scaffold: external content = untrusted | 1 | ✅ |
| T4 | `EncryptedPromptStore`: round-trip, wrong key, tampered/truncated ciphertext, key length, fail-closed without `cryptography` | 6 | ✅ |
| Local | Bearer token on mutating endpoints (core + extended + DELETE) | 4 | ✅ 401 without token |
| Local | CORS: evil and lookalike origins not echoed; loopback origins (IPv4/IPv6/https) echoed; empty/null origin → `*`; preflight | 11 | ✅ |

**Totals: 96 tests in `test_security_suite.py`** (+ 36 in `tests/test_tool_engine_sandbox.py`), all passing.

Full test suite: **448 passed** (`python3 -m pytest tests/ -q --ignore=tests/test_npm_install.py`, ~3.5 min).

---

## 3. Vulnerabilities found and fixed: git config = sandbox escape (two rounds)

**Severity: high** (T3 — sandbox escape, T2 — arbitrary command execution).

### Round 1: git `!` alias

`git` executes any alias whose value starts with `!` through the shell:

```bash
git -c alias.x='!rm -rf ~/Documents' x     # ← arbitrary shell command
git config alias.x '!curl https://evil'    # ← write alias, then git x
```

The alias value is **not a path**, so it sailed through `_resolve_path` ("inside workspace") unchecked: the sandbox was fully bypassed regardless of which paths were mentioned. The agent (or a prompt injection from repository files — threat T1) could execute any command outside the project. Confirmed end-to-end through the engine (`PWNED-OUTSIDE-WORKSPACE`).

### Round 2: exec-capable config keys (the Round-1 fix was insufficient)

A second offensive pass showed `!` aliases are only the tip: **git executes the values of a whole family of config keys as commands**. End-to-end confirmation through the engine:

```python
e._execute_command("git -c core.fsmonitor='touch /tmp/x' status")   # /tmp/x created — executed!
e._execute_command("git -c core.editor='touch /tmp/x' commit --allow-empty")  # /tmp/x created — executed!
```

`git status` and `git commit` are routine agent commands; `core.fsmonitor` and `core.editor` execute arbitrary commands **outside** the workspace. Verified exec-capable keys: `core.fsmonitor`, `core.editor`, `core.sshCommand`, `core.pager`, `core.askpass`, `core.hooksPath`, `sequence.editor`, `credential.helper`, `interactive.diffFilter`, plus the driver keys `diff.*.textconv`, `filter.*.clean/.smudge`, `credential.*.helper`.

### Fix (final)

`tera_pilot/agent_runtime/tool_engine/_engine.py`, `git` branch of `_validate_command_paths`:

1. any argument starting with `!` → blocked (covers `git config alias.x '!…'`);
2. `git -c key=!value` → blocked;
3. **denylist of exec-capable keys**: `-c <exec-key>=...` and `git config <exec-key> ...` → blocked (exact match + patterns `diff.*.textconv`, `filter.*.clean/.smudge`, `credential.*.helper`), fail-closed, case-insensitive;
4. legitimate `-c`/`config` (`-c core.quotepath=false`, `config user.name …`) keep working — verified.

### Regression tests

`tests/test_tool_engine_sandbox.py`: 4 tests for `!` aliases + `test_git_exec_capable_config_keys_blocked` (15 commands including case-insensitivity) + a check that legitimate `-c` flags are not blocked.

## 3.1. Vulnerability found and fixed: CORS echo via `startswith` → token theft

**Severity: critical** (CSRF-to-localhost → bearer-token theft → arbitrary commands through the agent).

### The bug

The origin check used a string prefix:

```python
origin.startswith('http://localhost')   # ← http://localhost.evil.com passes!
```

A domain whose subdomain is literally named `localhost` (e.g. `localhost.evil.com`) can be registered by anyone. A page on it does `fetch('http://127.0.0.1:18732/api/status')` — the server **echoed** the attacker origin in `Access-Control-Allow-Origin`, the browser allowed reading the response, and the public `GET /api/status` returns `api_token`. From there — POST with the stolen token: run the agent with an arbitrary prompt (read files, execute commands). Confirmed end-to-end: all three lookalike domains were echoed and the token was returned.

### Fix

`tera_pilot/api_server.py`, `_allowed_origin()`: parse the origin with `urlparse` and compare the **exact hostname** against `{localhost, 127.0.0.1, ::1}` (any port), rejecting malformed ports. `localhost.evil.com` → hostname `localhost.evil.com` → not echoed.

### Regression tests

`tests/test_security_suite.py`: `test_cors_attacker_localhost_lookalikes_not_echoed` (5 lookalike domains) + `test_cors_loopback_origins_echoed` (IPv4/IPv6/https, various ports) + updated CORS tests.

---

## 3.2. Vulnerability found and fixed: repo-supplied git exec keys & hooks = sandbox escape (Round 3)

**Severity: high** (T1 → T3 — untrusted repo content escapes the workspace sandbox).

### The bug

The Round-1/Round-2 fixes blocked exec-capable git keys only when they were passed **on the command line** (`git -c core.fsmonitor=...`, `git config core.editor ...`). But git also executes the values of these keys read from the **repo's own `.git/config`** — no `-c` needed. Confirmed end-to-end through the engine on a clean repo:

```python
e._execute_command("git status")   # .git/config [core] fsmonitor = touch /tmp/pwned  → /tmp/pwned CREATED
```

A malicious repo (threat T1) — or a prompt injection that tells the agent to write `.git/config` — turns a routine, whitelisted `git status` into arbitrary command execution **outside** the workspace. Same family, additionally confirmed:

- `diff.<name>.textconv` (`.git/config` + `.gitattributes`) executes on `git diff`;
- `.git/hooks/pre-commit` (and any hook) executes on `git commit`;
- `core.editor` executes on `git commit`, `filter.*.clean/.smudge` on `git add`/`checkout`.

None of these are visible to `_validate_command_paths` (nothing is passed on the command line), so the arg-level checks could not see them.

### Fix (runtime neutralization, defense-in-depth)

Instead of trying to enumerate every malicious config value, every git invocation made by the agent now **injects `-c` overrides that empty out all exec-capable keys** and point `core.hooksPath` at a hook-free directory:

- `tera_pilot/git_service.py`: new `git_neutralization_args()` + `_git_repo_exec_config_keys()`; `GitService` neutralizes by default (`neutralize_exec=True`), covering the agent's git tools **and** the GUI status poll;
- `ToolEngine._execute_command`: raw `git ...` commands get the same flags injected before Popen;
- `tera_pilot/learning_loop.py::_run_git`: same flags for git reads.

Legitimate git usage is unaffected (verified: status/diff/log/stage/commit all still work); a repo-supplied command simply never runs.

### Regression tests

`tests/test_tool_engine_sandbox.py`: `test_git_repo_config_exec_keys_neutralized`, `test_git_repo_hooks_neutralized`, `test_git_textconv_driver_neutralized`, `test_git_legitimate_commands_work_with_neutralization`, `test_git_neutralization_helper_marks_exec_keys`.

## 3.3. Vulnerability found and fixed: `npm test`/`npm exec` executed arbitrary repo scripts (T2)

**Severity: medium-high** (T2 — malicious script from the repo). The dangerous-flags map blocked `npm run` but **not its aliases**: `npm test`, `npm t`, `npm start`, `npm exec`, `npm ci`, `npm run-script` all execute the repo's `package.json` scripts (or install from the registry) — `npm test` is literally an alias for `npm run test`. Confirmed live: a repo with `{"scripts": {"test": "touch /tmp/pwned"}}` executes on `npm test`. Also, the self-verify flow (`_run_test_command_sandboxed`) ran the auto-detected test/lint command **with no user-confirmation gate**, unlike `_execute_command`/`_run_code`.

**Fixes:** `npm` dangerous flags now include every script-executing subcommand (`run`, `run-script`, `test`, `t`, `start`, `restart`, `exec`, `ci`, `install-test`, `install-ci-test`, `link`, `rebuild`, `publish`) in both `command_policy.py` and the `_helpers.py` fallback; `_run_test_command_sandboxed` now requires the same `_request_confirmation` gate as `_execute_command`. Users can re-allow a subcommand for a trusted project via `commands.json` `extra_trusted_flags`.

**Regression tests:** `test_npm_script_execution_subcommands_blocked` (11 commands), `test_auto_test_command_requires_confirmation`, `test_auto_test_command_runs_when_approved`.

## 3.4. Hardening: local API request bodies, daemon auth, web_fetch loopback

- **Oversized request bodies**: `api_server._read_json` and `daemon._read_body` now refuse bodies above a cap (8 MiB / 2 MiB) instead of allocating arbitrarily large buffers.
- **Daemon auth**: `DaemonHandler._check_auth` now uses `secrets.compare_digest` (constant-time) instead of `==`, matching `api_server`.
- **web_fetch loopback**: `_web_fetch` now rejects `localhost`/`127.0.0.0/8`/`::1` targets — the local API returns `api_token` at `GET /api/status`, so a prompt-injected agent could otherwise read and exfiltrate it (T5).

## 4. What was checked and not found

Directions where **no vulnerabilities were found**:

- **Metacharacters and dangerous flags** — all probed vectors are blocked before execution.
- **Paths outside the workspace** — absolute, `..`, symlink files and directories, path-prefix spoofing — all raise `PermissionError` / `[SECURITY ERROR]`.
- **Diff/patches** — multi-file diffs with `+++` headers pointing outside and absolute paths in `_apply_diff` never write outside the workspace (every target is re-resolved through `_resolve_path`).
- **web_fetch** — `file://`/`ftp://`/`javascript:` rejected before network/filesystem access; long base64-like query params (exfiltration) are rejected before the request.
- **Keys** — `~` is not expanded with `shell=False` (verified against a fake `~/.ssh/id_rsa` with a marker); keys never appear in command results.
- **API auth** — mutating endpoints (including extended routes and DELETE) require the bearer token; comparison uses `secrets.compare_digest` (constant time, benchmark §5).
- **CORS** — after the §3.1 fix, evil and lookalike domains are not echoed (5 lookalike variants + IPv6 verified), legitimate loopback origins are echoed, preflight is covered.
- **Audit trail** — record tampering and reordering are detected (covered by `tests/test_audit_cli.py`, re-verified).
- **Encrypted prompts** — ChaCha20-Poly1305: wrong key and tampered ciphertext are detected; the store fails closed without `cryptography`.

---

## 5. Security-path benchmarks

Local, macOS, Python 3.12, pure Python (no native). `benchmarks/bench_security.py`:

| Operation | ops | µs/op |
|---|---|---|
| `_sanitize_command` (allowed command) | 50 000 | 23.0 |
| `_sanitize_command` (blocked: `python3 -c …`) | 50 000 | 0.45 |
| `_validate_command_paths` (path inside) | 20 000 | 19.7 |
| `_validate_command_paths` (path outside) | 20 000 | 23.5 |
| `_resolve_path` (sandbox check per file operation) | 20 000 | 19.8 |
| `command_policy.is_allowed` (2 lookups) | 50 000 | 0.10 |
| token compare: naive `==` | 50 000 | 0.01 |
| token compare: `secrets.compare_digest` | 50 000 | 0.03 |
| `EncryptedPromptStore.encrypt` | 5 000 | 3.30 |
| `EncryptedPromptStore.decrypt` | 5 000 | 2.73 |

**Conclusion:** the security checks cost ~20–40 µs per file operation and ~23 µs per command — fractions of a percent of the cost of the operation itself. Constant-time comparison (`compare_digest`) is ~3× more expensive than naive `==` on 50k ops — 0.02 µs difference, a negligible price for timing-attack resistance.

---

## 6. Residual risks and recommendations

1. **No OS-level sandbox** (documented, deliberate for now): `python3 script.py` or `pytest` can do anything *inside* the workspace; the sandbox is path-based, not process-based. The threat model lists this as future work (bubblewrap/firejail). The git command surface in particular remains a zone for continuous testing until then.
2. **GET extended-API endpoints with side effects** (low): `/api/context/pin` and `/api/context/unpin` were converted from GET to POST in v2.3.4 (now under the bearer token). Any future GET routes with side effects should follow the same rule.
3. **`Origin: null` → `*` in CORS** (accepted risk): required for `file://` loading in QWebEngineView; a sandboxed iframe from a third-party site also sends `null`. Mitigated by the mandatory bearer token on mutating POSTs.
4. **git hooks/helper paths** (`--exec-path`, `core.fsmonitor` beyond `-c`) — indirect vectors that require writing a file into the workspace first; not individually fixed, monitored by the exec-key denylist.

---

## 7. Conclusion

- **Four real vulnerabilities found and fixed (two rounds this cycle):** Round 3 added the repo-supplied git exec-key/hook escape (`core.fsmonitor`/`diff.*.textconv`/`.git/hooks/*` in a malicious repo's own `.git/config`, executed by a plain `git status`/`git diff`/`git commit` — neutralized at runtime) and the `npm test`/`npm exec` arbitrary-script gap (plus the missing confirmation gate on auto-detected test commands). These join the earlier git `!`-alias / exec-key family and the critical CORS `startswith` bug. All were confirmed with end-to-end exploits before the fix and are blocked/neutralized afterwards.
- **Fail-closed hardening:** the insecure XOR fallback in `EncryptedPromptStore` was removed (missing `cryptography` now raises instead of "encrypting"); `/api/context/pin|unpin` moved from GET to POST under the bearer token.
- Core claimed controls are confirmed by tests — 96 tests in `test_security_suite.py` + 36 in `test_tool_engine_sandbox.py`, all green; the full suite is 448 tests (incl. 2 new picker/index regression tests).
- The security checks cost tens of microseconds per operation — not a bottleneck.
- Residual risks are acknowledged and documented (§6); the main open item is the absence of an OS-level sandbox (roadmap).

Artifacts: `tests/test_security_suite.py`, `benchmarks/bench_security.py`, regressions in `tests/test_tool_engine_sandbox.py`, this report.
