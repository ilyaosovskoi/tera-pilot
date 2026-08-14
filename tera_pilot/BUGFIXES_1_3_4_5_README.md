# Tera Pilot — Bug Fixes for #1, #3, #4, #5

This archive contains corrected source files for the four bugs reported in
`tera_pilot_bug_report.md` that were still outstanding after the user's own fixes
for #2, #7, #14 and the previous session's fixes for #8–#13, #15.

## Files Changed

| File | Bug | What was fixed |
|------|-----|----------------|
| `tera_pilot/agent_runtime.py` | #1 | `_write_file` and `_str_replace` now call `_request_confirmation()` when `diff_review_enabled == False`, so the user's autonomy level (`never_ask` / `new_files_only` / `always_ask`) is honored even when the diff review popup is disabled. Mirrors the existing logic in `write_binary_file`. |
| `tera_pilot/quota.py` | #3 | `_ensure_loaded()` is now wrapped in `self._file_lock()` (including the destructive rotation rewrite). Previously only `record()` held the cross-process lock, but `_ensure_loaded` is also called from `today_counts()`, `stats()`, and `exhausted()` — all unlocked. Two parallel calls that both decided "time to rotate" would race on `open(self._path, "w")` and clobber each other. A per-thread reentrance counter was added to `_file_lock` so `record()` (which already holds the lock) can still call `_ensure_loaded_locked()` without deadlocking on `flock()`. Pattern mirrors `memory_service.py::_file_lock`. |
| `tera_pilot/context_manager.py` | #4 | `mark_accessed()` now adds the file to `_file_index` if it isn't already there, via a new `_add_to_index()` helper. `_index_project` is a one-time disk snapshot taken in `set_root()` — files the agent creates mid-session never appeared in that snapshot, so `score_files()` (which iterates only `_file_index`) silently skipped them. The "bug 4.2" fix that introduced `mark_accessed` was supposed to keep agent-active files auto-attached on later iterations, but for the main scenario (agent creates a new file then keeps working on it) the promise was broken. |
| `tera_pilot/web/app.js` | #5 | `handleSend()` now tracks a `streamStarted` flag that becomes true on the first successfully-parsed SSE event. If the HTTP stream breaks mid-flight (after tokens or `chat_info` were already processed), the code no longer blindly falls back to the Qt bridge — that would have re-sent the same message and produced a duplicate chat. Now: if `!streamStarted`, fall back to the bridge (as before); if `streamStarted == true`, show a "Stream interrupted — please retry manually" toast and reset UI state without re-sending. |

## Verification

All 35 automated checks pass:
- 10 source/AST checks for bug #1
- 6 functional ToolEngine tests for bug #1 (real `write_file` / `str_replace` calls with `autonomy=never_ask/always_ask`, accept/reject UI callbacks, and `diff_review_enabled=True/False` combinations)
- 4 quota locking tests for bug #3 (rotation, reentrance counter, no deadlock, concurrent threads)
- 6 context manager tests for bug #4 (file appears in `_file_index` / `score_files` / `select_context`, out-of-root/missing paths silently skipped)
- 4 source checks for bug #5 (streamStarted declared, set on first event, gates the bridge fallback, mid-flight break branch present)
- 4 syntax/compile checks (py_compile for the 3 Python files, `node --check` for app.js)

Previous session's 30 checks (`verify_bugfixes.py`) and the 11 `test_approach2.py` smoke tests continue to pass — no regressions.

## Installation

Drop the four files into your existing Tera Pilot source tree, preserving the
paths shown above. The changes are backward-compatible (no new public API,
no schema changes, no config flag required).

## See Also

The full set of fixes for the 15-bug report:
- User fixed: #2, #7, #14
- Previous session: #8, #9, #10, #11, #12, #13, #15
- This session: #1, #3, #4, #5
- Remaining: #6 (user states it's already resolved)
