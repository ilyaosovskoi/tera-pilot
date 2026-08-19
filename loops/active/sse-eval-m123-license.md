# Loop: SSE reliability fix + M1/M2/M3 completion + offline license system

Status: **in progress** (opened 2026-08-17)

Goal (from the task brief): fix the SSE "success reported as timeout" bug,
finish Second Opinion (M1) / Cost Router (M2) / Spend Dashboard (M3) "for
real" across every entry point, and ship an offline, zero-telemetry
Ed25519-signed license-key system for Pro gating.

## Success criteria

- [ ] Part 1: 3/3 reruns of `eval/tasks/fix-missing-return` (with `--driver
      api`) report `status: "success"` matching the actual test outcome —
      no false `"error"` on a real success. Old results in `eval/results/`
      are left untouched (they are the before-state).
- [ ] Part 2: M1/M2/M3 gating verified identical across TUI / Web UI /
      HTTP daemon / CLI, with a passing integration test per surface.
- [ ] Part 3: `is_feature_licensed()` fully replaces ad-hoc
      `is_pro_enabled()` calls in the Pro-gated modules; zero network
      calls during license checks (test-enforced via monkeypatched
      `socket.socket`/`urllib`); CLI `activate`/`status`/`deactivate`
      works end-to-end with a locally-signed test key.

## Definition of done (task-level)

1. `api_server.py`: `_handle_agent_stream` / `_handle_chat_stream` (and the
   same-pattern `_handle_oneshot` / `_handle_test_provider`) block on
   `stream_done.wait()` before returning, so `close_connection` is read by
   the server request loop and the socket closes right after `done`.
   Reasoning (ThreadingMixIn per-connection threads, `daemon_threads=True`)
   documented in a comment.
2. `eval/runner.py`: `except TimeoutError` only overwrites `status`/`final`
   when no terminal (`done`/`error`) event was already parsed.
3. Regression test: open `/api/agent/stream`, drain the SSE response,
   assert the connection closes within a few seconds of `done`.
4. Eval rerun: 3 fresh results saved into `eval/results/`, old ones kept.
5. M1/M2/M3: audit docstrings updated with honest behavior + limitations;
   HTTP-API integration tests per feature (config read/write, run/route/
   report, gating).
6. `tera_pilot/licensing.py`: `activate_license` / `get_license_status` /
   `is_feature_licensed` / `deactivate_license`, offline Ed25519 verify
   against embedded public key, persist to `~/.tera_pilot/license.json`,
   fail closed, `TERA_PILOT_PRO=1` kept as dev-only override.
7. CLI: `tera-pilot license activate|status|deactivate`.
8. No-telemetry test (monkeypatch network) + signed-key end-to-end test.
9. `LICENSING.md` in the tone of `THREAT_MODEL.md` / `SECURITY_TEST_REPORT.md`.

## Close-out notes (2026-08-17)

- **Part 1 done.** The four SSE handlers block on `stream_done.wait()`;
  `eval/runner.py` only overwrites terminal state on a timeout when no
  terminal SSE event was parsed. Regression tests added
  (`tests/test_sse_connection_close.py`, `tests/test_eval_api_driver.py`).
  Eval reruns: 3 fresh results in `eval/results/` (20260817_2341xx). All 3
  report `failed` with `test_passed: false`, matching the real outcome;
  run duration dropped from 314 s (of which ~300 s was the false-timeout
  hang) to ~25 s. The before-state files (20260816_18/19) are untouched.
- **Honest caveat on the reruns:** the free-tier openrouter pool model
  (poolside/laguna-s-2.1:free) today emits its native `<tool_call>` XML
  instead of the JSON tool calls the output parser expects, so the agent
  degraded to prose on all 3 runs (no tool executed) — `failed` is the
  CORRECT report (previously this same situation plus a real success were
  both reported as `error` after a 300 s hang). The success-reporting path
  itself is proven by the mock-server driver test and the SSE close test;
  a genuine success run was not achievable today because the model never
  emitted a parseable tool call. This is a model-behavior finding, not an
  SSE/code one.
- **Part 2 done.** M1/M2/M3 gating now enforced identically on every
  surface (TUI bridge, HTTP API, module entry points); integration tests
  in `tests/test_m123_api_integration.py` (10 tests). The audit found and
  FIXED real wiring bugs: `/api/second_opinion/run` passed wrong args
  (always errored), `/api/cost/route` and `/api/cost/apply` passed
  unexpected kwargs (always errored), and the web GUI's Second Opinion
  Configure posted wrong config keys. M1 auto-trigger and M2 runtime
  routing are documented as present-but-not-wired (see docstrings).
- **Part 3 done.** `tera_pilot/licensing.py` (offline Ed25519 verify,
  fail-closed), embedded `tera_pilot/license_pubkey.pem` (private key
  stored outside the repo at `~/.tera_pilot/license_dev_signing_key.pem`),
  `tera-pilot license activate|status|deactivate`, `is_feature_licensed()`
  replacing ad-hoc `is_pro_enabled()` in second_opinion/cost_router/
  spend_dashboard, `TERA_PILOT_PRO=1` kept as dev-only override,
  `LICENSING.md`, no-telemetry test + end-to-end CLI tests (16 tests).
- **Deliberately NOT claimed:** OS-level process sandbox around
  `execute_command` (path-based only); license revocation list (offline
  model has none); M1 auto-trigger / M2 runtime routing are not wired into
  the live agent loop yet.
