# Developing Tera Pilot

This guide is for developers who want to **understand the codebase and improve
it** — not just contribute a one-off fix. If you are looking for the process
(how to open a PR, code of conduct, commit style), read
[`CONTRIBUTING.md`](CONTRIBUTING.md) first; this document assumes you have read
it.

Everything below is verified against the current tree (v2.3.8). The project is
in **testing phase** — see the banner in the README — so this guide, like the
code, is a living document: if a section drifts out of date, a PR fixing it is
a welcome first contribution.

---

## 1. Codebase map — where things live and why

Tera Pilot is deliberately split into **runtime** (headless, no UI), **TUI**
(Textual frontend + bridge), **servers** (REST/SSE + daemon), and **eval**
(harness + task fixtures). The one rule that ties them together: *the UI never
talks to providers directly — it always goes through a bridge or an HTTP
endpoint, which drives the same `AgentRuntime`.*

```text
tera_pilot/
├── agent_runtime/           THE core: ReAct loop, ToolEngine, memory, parser
│   ├── runtime.py           AgentRuntime.run() — the agent loop (start here)
│   ├── tool_engine/_engine.py  ToolEngine — every tool the agent can call
│   ├── types.py             ToolName enum, Task, AgentStep, messages
│   ├── prompts.py           System prompt + tool schema builders
│   ├── repetition_guard.py  Detects models stuck in a repetition loop
│   └── output_parser.py     Parses model output: thoughts, tool calls, final
├── providers/               Provider registry + adapters (OpenAI, Anthropic…)
│   ├── registry.py          Register/configure/activate providers
│   └── base.py              Provider abstract base + ProviderConfig
├── agent/                   Guardian (risk scoring), sandbox, checkpoints
├── api_server.py            REST/SSE server — the Web UI + eval talk to this
├── api_extended.py          More endpoints mirroring the TUI bridge
├── daemon.py                Long-running task daemon with its own SSE
├── web_server.py            Static file server + API delegation for Web UI
├── web_bridge/              HTTP-side bridge used by the browser GUI
├── audit_signing.py         Ed25519 signed audit export/verification
├── licensing.py             Offline Pro licensing (Ed25519, no phone-home)
├── token_budget.py          Cost caps + per-turn limits (incl. max_iterations)
├── quota.py                 Per-section daily request limits
└── utils.py                 Config load/save (atomic), paths, system info

tera_pilot_tui/
├── app.py                   Textual app: screens, slash commands, event UI
├── bridge.py                TeraPilotBridge — THE TUI↔runtime seam
├── backend_runner.py        Headless automation adapter over the bridge
└── widgets/                 ChatLog, InfoBox, modals, pickers, status bar

eval/
├── runner.py                Task runner: api driver, direct driver, report
├── run_all.py               Boots an in-process server, runs tasks, aggregates
└── tasks/<id>/              Each task = task.json + repo/ fixture + gold/
tests/                       Hermetic tests — no network, no real API keys
```

### Where to start reading

1. **`tera_pilot/agent_runtime/runtime.py` → `run()`** — the agent loop. This
   is the heart of the product: think → emit a tool call or final answer →
   execute → repeat, bounded by the iteration budget.
2. **`tera_pilot_tui/bridge.py` → `TeraPilotBridge`** — everything the TUI
   does to the runtime goes through here (`run_prompt`, `ensure_agent`,
   `set_provider`, `configure_provider`…). If you change runtime behavior,
   check whether the bridge needs a matching change.
3. **`tera_pilot_tui/app.py` → `_handle_slash_input` / `_exec_*`** — the
   command surface users actually type.
4. **`tests/test_tui_integration.py`** — the closest thing to an executable
   spec: it drives the *real* runtime + bridge with a fake provider and asserts
   the exact behaviors users rely on.

---

## 2. How the agent loop works

`AgentRuntime.run(description, task_type)` runs a bounded ReAct loop:

```
iteration n:
  1. build prompt (system + task + previous steps / native message history)
  2. call the active provider → raw text (or native tool_calls)
  3. parse: is it a final_answer? a tool call? prose?
  4. if tool call → ToolEngine.execute() → observation appended to history
  5. repeat until final_answer, an error, or the iteration budget runs out
```

Key behaviors you will encounter (and must preserve):

- **Iteration budget is a SOFT cap** — while the agent keeps executing tools
  successfully, the loop auto-extends up to `hard_max_iterations` (3× soft,
  at least 40, at most 200). Genuinely stuck loops (repeated errors, no tools)
  still stop at the cap. `max_iterations` is a property precisely so the
  hard ceiling stays in sync when callers mutate it.
- **Prose without a tool call** is retried twice, then accepted as a final
  answer (marked `degraded` if no tool ever ran).
- **Repetition-dominated responses** are refused (repetition guard) instead of
  being surfaced as garbage.
- **Every tool call goes through the same guards**: workspace sandbox, command
  policy, Guardian risk review, diff review / confirmation callbacks.
- Events (`THOUGHT`, `TOOL_CALLED`, `TOOL_RESULT`, `DONE`, …) are emitted via
  `on_event` — the bridge forwards them to the UI, the API server forwards
  them as SSE. **Adding a new event type means touching runtime.py AND the
  bridge AND the API server's event mapping.**

---

## 3. Common tasks — recipes

### 3.1 Add a new tool (agent capability)

A tool touches three places — do not skip any:

1. **`tera_pilot/agent_runtime/types.py`** — add a member to `ToolName`.
2. **`tera_pilot/agent_runtime/tool_engine/_engine.py`** — implement a
   `_your_tool(...)` method and add an entry to the `dispatch_map` inside
   `_dispatch()`. Follow the existing guard conventions (path sandboxing for
   file tools, `[TOOL ERROR]` / `[TOOL DENIED]` return conventions, activity
   log recording happens automatically in `execute()`).
3. **`tera_pilot/agent_runtime/prompts.py`** — advertise the tool in the
   `TOOL_SCHEMA` (and `build_native_tools_schema()` if it should work for
   native-tool-call providers) so the model knows it exists.

Then add a test: drive it through `FakeProvider` in `tests/test_tui_integration.py`
or directly in a `tests/test_*` unit test. If the tool can be abused (command
execution, file writes, network), also add a case to `tests/test_tool_engine_sandbox.py`
and note it in `THREAT_MODEL.md`.

### 3.2 Add a new provider

1. Look at `tera_pilot/providers/openai_compat.py` — most cloud providers are
   OpenAI-compatible and only override `provider_id`, `label`, `default_model`,
   `context_window`, and maybe headers/auth. Anthropic has its own adapter.
2. Subclass the right base in a new `providers/<name>.py`.
3. Register it in `providers/registry.py` → `register_default()`.
4. Add it to the TUI's quick-pick list in
   `tera_pilot_tui/widgets/settings_modal.py` (`QUICK_PROVIDERS`) if it is
   popular enough.
5. Test with the fake provider pattern; for a real adapter, at minimum test
   that config parsing works (see `tests/test_providers_tool_calls.py`).

### 3.3 Add an eval task (reproducible benchmark)

This is the project's evidence mechanism — every claim about capability must
be backed by a task.

1. Create `eval/tasks/<your-task>/` with:
   - `task.json` (see `eval/README.md` → "Task format"): `prompt`,
     `category` (`bug_fix`, `test_repair`, `refactor`, `feature`,
     `code_review`, `documentation`, `security`), `repo`, `test_command`,
     `baseline_status`, `timeout_secs`.
   - `repo/` — the fixture the agent works on.
   - `gold/` — reference solution; `tests/test_eval_tasks.py` applies it to a
     clean copy and asserts `test_command` passes.
2. Verify with `python3 -m eval.runner check` (structure + baseline) and the
   quality-gate tests.
3. Run it live with `python3 -m eval.run_all --tasks <id>` and paste the
   measured result into the report docs — never claim a task passes without a
   recorded run.

### 3.4 Add a slash command (TUI)

1. Add the entry to `BUILTIN_COMMANDS` in
   `tera_pilot_tui/widgets/command_palette.py` (this powers `/help` and the
   palette).
2. Add the dispatch branch in `tera_pilot_tui/app.py` → `_handle_slash_input`.
3. Implement `_exec_<command>(self, arg)` in the same file, and follow the
   convention: use `self.query_one(ChatLog)` for output and end with
   `self.query_one(InputBox).focus()`.
4. If it calls the runtime, add the method on `TeraPilotBridge` and cover it
   in `tests/test_tui_integration.py`.

### 3.5 Add an API endpoint

1. For Web-UI/SSE-facing endpoints: `tera_pilot/api_server.py` (register the
   path in `do_GET`/`do_POST`, add a `_handle_*` method, keep CORS + bearer
   token consistent with the rest).
2. For bridge-mirroring endpoints (used by the browser GUI): add to
   `tera_pilot/api_extended.py` → the `_bridge()` pattern.
3. Test in `tests/test_api_extended_endpoints.py` or
   `tests/test_tui_backend.py`; if it mutates config, follow the atomic-write
   pattern from `utils.save_config()`.

---

## 4. Testing — what to know

- **Never use real API keys or live providers in tests.** Use
  `tests/fake_provider.py` (`FakeProvider`) — it scripts responses
  (`tool_call(...)`, `final_answer(...)`, errors, timeouts) and lets you
  assert the runtime's exact behavior deterministically.
- The canonical integration pattern is in `tests/test_tui_integration.py`:
  real `AgentRuntime` + real `TeraPilotBridge` + fake provider, run in a
  thread. If you are changing runtime behavior, add the scenario there.
- Isolate `HOME` (fixtures set `TERA_PILOT_HOME`/`HOME` to `tmp_path`) so a
  developer's `~/.tera_pilot` never leaks into tests.
- Regression tests should fail on the old behavior: reproduce the bug first,
  then fix, then keep the test.
- **The full suite must pass before a PR**: `python3 -m pytest tests/ -q`
  (currently 689 passed / 20 skipped). The eval quality gate:
  `python3 -m eval.runner check` + `eval.runner smoke`.

---

## 5. Where to help — how to improve the project

The roadmap is maintained as internal planning notes (P0/P1 checklist, goals,
ICP, competitive framing); the public `README.md` tracks shipped capabilities
and measured results. The highest-leverage contributions right now:

1. **CI/CD (P1.1).** There is no automated pipeline yet — every release is
   gated by a human running the suite. Adding a GitHub Actions workflow that
   runs `pytest`, `eval.runner check`/`smoke`, and a clean-install smoke would
   unblock external contributors immediately.
2. **Type hygiene.** `mypy` is not clean (100+ pre-existing errors). Fixing
   them in small batches (one module per PR) makes the codebase reviewable
   and would let typecheck gate merges.
3. **Real user workflows (P1.4).** Dogfood the TUI on real tasks and turn
   whatever breaks into a regression test. `tests/test_tui_integration.py`
   is the place.
4. **More eval tasks** across all 7 categories — especially `security`
   (adversarial) and `documentation`. Every task strengthens the evidence
   base for the README's claims.
5. **Provider onboarding (P1.3).** New OpenAI-compatible providers are cheap
   to add (recipe 3.2) and expand the "17 providers" story.
6. **Docs.** `CONTRIBUTING.md`, `DEVELOPING.md` and the README must stay
   truthful and current; fixing a stale claim is a valuable, low-risk PR.

**Before writing a large feature:** open an issue first and reference the
roadmap item it serves. The project deliberately rejects unrequested surface
area — see "Claims discipline" in `CONTRIBUTING.md`.
