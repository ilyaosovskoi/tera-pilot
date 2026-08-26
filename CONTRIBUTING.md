# Contributing to Tera Pilot

Thanks for wanting to help! Tera Pilot is a TUI-first, local-first coding agent
with a strong focus on **trust, control and verifiable evidence**. This guide
explains how to contribute code, docs, and issues without tripping over the
project's conventions.

> **New here?** Start with [`DEVELOPING.md`](DEVELOPING.md) — a codebase map,
> how the agent loop works, recipes for common tasks (new tool, provider, eval
> task, slash command, endpoint) and where the project needs help. This file
> covers the *process*; that one covers the *code*.

## TL;DR

- Repository: <https://github.com/ilyaosovskoi/tera-pilot>
- License: **MIT**
- Issues / PRs: English preferred (the project targets an international audience)
- No API keys, no real LLM calls in tests — use `tests/fake_provider.py`
- Before a PR: `python3 -m compileall` + `python3 -m pytest tests/ -q` pass,
  and `python3 -m eval.runner check` reports 0 problems

## Code of conduct

Be respectful, constructive and evidence-based. This project explicitly avoids
unverified claims — that applies to PR descriptions and reviews too.

## Reporting issues

- **Security vulnerabilities:** do **not** open a public issue. Follow
  [`SECURITY.md`](SECURITY.md).
- **Bugs:** include the Tera Pilot version (`tera-pilot --version` or
  `pip show tera-pilot`), your OS/Python version, the exact command or TUI steps,
  expected vs. actual behavior, and any error output. Run
  `tera-pilot doctor --json` and paste the summary if relevant.
- **Feature requests:** describe the user problem you are solving, not just the
  feature name. The project is deliberately focused; PRs that add unrequested
  surface area may be declined.

## Development setup

### Python package (required for everything)

```bash
git clone https://github.com/ilyaosovskoi/tera-pilot.git
cd tera-pilot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `tera-pilot`, `tera-pilot-tui`, `tera-pilot-daemon` and
`tera-pilot-acp` commands plus the `tera_pilot` / `tera_pilot_tui` packages.

### npm distribution (optional, only for packaging work)

```bash
npm install -g .   # runs scripts/postinstall.js — creates ~/.tera_pilot/venv
npm uninstall -g tera-pilot   # removes only the npm-managed venv
```

When changing `scripts/postinstall.js`, `scripts/preuninstall.js` or `bin/*.js`,
run `python3 -m pytest tests/test_npm_install.py -q` (hermetic — no network,
no real venv/pip).

### Rust native acceleration (optional)

The Rust extension is optional; without it the project runs on pure-Python
fallbacks (slower, but functionally identical). To build it:

```bash
cd tera-pilot-native/pyo3
maturin develop --release
```

When changing Rust code, run `python3 -m pytest tests/test_native.py -q` and
`python3 benchmarks/bench_native.py` (make sure the ~43x / ~3.3x speedups
don't regress).

## Running the checks

```bash
python3 -m compileall eval tera_pilot tera_pilot_tui tests   # syntax check
python3 -m pytest tests/ -q                                  # full suite
python3 -m eval.runner check                                 # eval task structure
python3 -m eval.runner smoke                                 # fake-driver smoke set
```

The full test suite must pass before a PR is merged. The CI gate is:
`tests/test_evaluation_schema.py`, `tests/test_eval_tasks.py`, `eval.runner check`,
`eval.runner smoke`.

## Writing tests

- **Never** use real API keys or live LLM providers in tests.
- Use `tests/fake_provider.py` (`FakeProvider`) for anything that touches the
  agent runtime or provider layer — see `tests/test_tui_integration.py` for the
  established pattern (real runtime + real bridge + fake provider).
- Keep tests hermetic: no network, no writes outside `tmp_path`, no dependence
  on the developer's `~/.tera_pilot` (isolate `HOME` in fixtures).
- A bug fix should ship with a regression test that fails on the old behavior.

## Style

- Python: follow the existing code style; no formatter is enforced, but keep
  changes consistent with the surrounding module.
- Type hints are used throughout — add them to new code.
- Docs and code comments are in **English**. Product docs live at the repo root
  (`README.md`, `THREAT_MODEL.md`, `LICENSING.md`, `SECURITY.md`,
  `eval/README.md`) and must keep "claims discipline": never state measured
  capabilities that are not backed by the evaluation harness.
- Slash commands, CLI flags, env vars and JSON schemas are identifiers — do not
  translate or rename them casually.

## Making a pull request

1. Fork the repository and create a branch: `git checkout -b fix/your-change`.
2. Make the change with a regression test where applicable.
3. Run the checks above locally.
4. Open a PR against `main`. Use the PR template; keep the description focused:
   what changed, why, and how it was verified.
5. Keep PRs small and reviewable. Large architectural changes should be
   discussed in an issue first.

### Commit style

Concise imperative subject line (`Fix sandbox path check`, `Add eval task`),
then a short body explaining the *why* when it is not obvious.

## Claims discipline (important)

Per the product strategy, do not claim in code, docs, or PRs that Tera Pilot:

- is "smarter" than other agents or has a better SWE-bench score;
- is enterprise-ready (until the P0/P2 gaps are closed);
- guarantees the absence of vulnerabilities;
- is fully offline when using cloud providers or MCP.

Correct formulations: "local-first", "supports local and cloud providers",
"vendor-neutral", "provides policy, approval and audit mechanisms".

## Getting help

- Open an issue for questions about the codebase.
- Check `tera-pilot doctor` if your environment misbehaves.
- For anything security-related, see [`SECURITY.md`](SECURITY.md).
