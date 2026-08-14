## What

<!-- One or two sentences: what changed and why. -->

## Verification

<!-- How was this verified? Check all that apply. -->
- [ ] `python3 -m compileall eval tera_pilot tera_pilot_tui tests`
- [ ] `python3 -m pytest tests/ -q` — all pass
- [ ] `python3 -m eval.runner check` — 0 problems
- [ ] `python3 -m eval.runner smoke` — all pass
- [ ] New regression test added for the fix

## Checklist

- [ ] No real API keys or live LLM calls in code/tests (fake provider only)
- [ ] Docs/comments in English; identifiers, CLI flags and JSON schemas unchanged
- [ ] Claims discipline respected (no unverified capability claims)
- [ ] Related issue(s): #<!-- number -->

## Notes for the reviewer

<!-- Anything non-obvious: design decisions, follow-up work, known limitations. -->
