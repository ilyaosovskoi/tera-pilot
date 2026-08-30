"""Tests for the activity/audit-log status classification fixes (v2.3.10).

Three related bugs are covered:

1. ``parse_status`` misclassified many error/rejected tool results as
   ``ok``. The tool engine emits tags like ``[TOOL ERROR]``, ``[GIT ERROR]``,
   ``[MCP ERROR]``, ``[BLOCKED]`` and ``[REFUSED]``, but the status-prefix
   table had no entry for them — and ``parse_status`` defaults *unknown*
   prefixes to ``STATUS_OK``. So the activity / signed audit trail recorded
   every failed tool call as a green "ok" (contradicting the engine's own
   comment that ``[TOOL ERROR]`` is an error status).

2. ``sanitise_args`` recursed into nested dicts using the *leaf* key name,
   so a nested field only *named* ``content``/``diff``/``code`` — e.g.
   ``{"meta": {"diff": ...}}`` — was collapsed to a summary even though it
   is ordinary metadata, dropping data the log should keep.

3. ``detect_missing_verification`` scoped its activity-log window by string
   *prefix* with no path boundary, so paths in a lookalike sibling project
   (``/ws/proj2/...`` when the project is ``/ws/proj``) bled into this
   project's window and could cause false rollback/CI learning triggers.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot import activity_log  # noqa: E402


# ── 1. parse_status ____________________________________________________

@pytest.mark.parametrize(
    "result",
    [
        "[TOOL ERROR] boom",
        "[GIT ERROR] not a git repository",
        "[MCP ERROR] tool failed",
        "[WEB_FETCH ERROR] blocked",
        "[WEB_SEARCH ERROR] backend down",
        "[SUBAGENT ERROR] worker crashed",
        "[MULTI-AGENTS ERROR] parallel job failed",
        "[SKILL ERROR] missing skill",
        "[GREP ERROR] no output",
        "[GLOB ERROR] bad pattern",
        "[SELF-VERIFY ERROR] unknown mode",
        "[SELF-VERIFY FAILED] tests red",
        "[RUNTIME NOT FOUND] nothing",
    ],
)
def test_error_prefixes_classify_as_error(result: str):
    """v2.3.10-fix: tool-defined error tags must not record as ``ok``."""
    assert activity_log.parse_status(result) == activity_log.STATUS_ERROR


@pytest.mark.parametrize(
    "result",
    [
        "[BLOCKED] command not in whitelist — skipped",
        "[REFUSED] Refusing to delete the workspace root itself.",
    ],
)
def test_blocked_refused_classify_as_rejected(result: str):
    assert activity_log.parse_status(result) == activity_log.STATUS_REJECTED


def test_wave_timeout_classifies_as_timeout():
    assert (
        activity_log.parse_status("[WAVE TIMEOUT] hung parallel run")
        == activity_log.STATUS_TIMEOUT
    )


def test_ok_prefixes_still_classify_as_ok():
    """The positive tags must keep classifying as ok after the additions."""
    for result in ("[WRITTEN] foo.py", "[NO OUTPUT]", "[CREATED] bar.py", "[OK]"):
        assert activity_log.parse_status(result) == activity_log.STATUS_OK


def test_end_to_end_with_record_tool_call():
    """A failing tool call must land in the activity log flagged as error,
    not ok — this is the audit-trail correctness property."""
    log = activity_log.ActivityLog()
    log.record_tool_call(
        tool="git_commit",
        args={"message": "x"},
        result="[GIT ERROR] not a git repository",
    )
    (entry,) = log.recent()
    assert entry["status"] == activity_log.STATUS_ERROR
    assert entry["tool"] == "git_commit"


def test_happy_tool_call_is_ok_in_log():
    log = activity_log.ActivityLog()
    log.record_tool_call(
        tool="write_file", args={"path": "a.py"}, result="[WRITTEN] a.py",
    )
    (entry,) = log.recent()
    assert entry["status"] == activity_log.STATUS_OK


# ── 2. sanitise_args nested-key handling ________________________________

def test_nested_metadata_named_like_large_arg_is_kept():
    """v2.3.10-fix: a nested field *named* ``diff`` under a different
    top-level key is ordinary metadata and must NOT be collapsed."""
    args = {"meta": {"diff": "x" * 50}}
    out = activity_log.sanitise_args(args)
    assert out["meta"]["diff"] == "x" * 50


def test_top_level_large_content_is_summarised():
    out = activity_log.sanitise_args({"content": "y" * 5000})
    assert out["content"] == {
        "_summary": True,
        "len": 5000,
        "preview": "y" * 240,
    }


def test_long_nested_string_still_summarised_by_generic_rule():
    """Even nested strings must be capped by the generic >800 rule — the
    fix only stops key-name-based summarisation, not runaway lengths."""
    out = activity_log.sanitise_args({"meta": {"note": "z" * 5000}})
    assert out["meta"]["note"]["_summary"] is True
    assert out["meta"]["note"]["len"] == 5000


def test_long_list_is_capped():
    out = activity_log.sanitise_args({"command": ["a" for _ in range(25)]})
    # first 20 items + the truncation marker entry.
    assert len(out["command"]) == 21
    assert out["command"][-1] == {"_truncated": True, "hidden": 5}
    assert all(v == "a" for v in out["command"][:-1])


# ── 3. detect_missing_verification path scoping _________________________

class _FakeLog:
    def __init__(self, entries):
        self._entries = entries

    def recent(self, n=200):  # noqa: N802
        return list(self._entries)


def _entry(*, path=None, tool="", title="", command="", summary=""):
    return {
        "path": path,
        "tool": tool,
        "title": title,
        "command": command,
        "summary": summary,
        "result_preview": "",
    }


def _detect(project_path, entries, monkeypatch):
    import tera_pilot.learning_loop as gen
    # get_activity_log is imported *inside* the function from
    # tera_pilot.activity_log, so patch it on that module.
    monkeypatch.setattr(activity_log, "get_activity_log", lambda: _FakeLog(entries))
    return gen.detect_missing_verification(str(project_path))


def test_lookalike_sibling_path_does_not_trigger(tmp_path, monkeypatch):
    """v2.3.10-fix regression: a rollback-looking entry whose path lives
    in ``/ws/proj2`` must not count as belonging to ``/ws/proj``."""
    proj = tmp_path / "proj"
    proj.mkdir()
    sibling = tmp_path / "proj2"  # startswith("proj") but NOT inside proj
    assert str(sibling).startswith(str(proj))

    # The rollback signal is on a sibling path; the project's own entry is
    # a harmless write (no self_verify), so there is no rollback to repo.
    entries = [
        _entry(path=str(sibling / "x.py"), tool="execute_command",
               command="git reset --hard"),
        _entry(path=str(proj / "a.py"), tool="write_file"),
    ]
    assert _detect(proj, entries, monkeypatch) is None


def test_project_entry_triggers_when_relevant(tmp_path, monkeypatch):
    """When the rollback genuinely lives inside the project and there was
    no self_verify, the signal must fire."""
    proj = tmp_path / "proj"
    proj.mkdir()
    entries = [
        _entry(path=str(proj / "x.py"), tool="execute_command",
               command="git reset --hard"),
        _entry(path=str(proj / "a.py"), tool="write_file"),
    ]
    sig = _detect(proj, entries, monkeypatch)
    assert sig is not None
    assert sig.had_rollback is True
    assert sig.had_self_verify is False


def test_self_verify_suppresses_signal(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    entries = [
        _entry(path=str(proj / "a.py"), tool="write_file"),
        _entry(tool="self_verify"),
    ]
    assert _detect(proj, entries, monkeypatch) is None