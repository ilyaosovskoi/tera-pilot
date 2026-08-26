"""Unit tests for tera_pilot.agent_runtime.diff_utils.

These are pure functions with no I/O except ``_backup_file``, so they
are easy to test exhaustively. No LLM, no network, no subprocesses.

Covers:
- ``_apply_unified_diff``: basic apply, multi-hunk offset bookkeeping,
  new-file hunks, context-mismatch rejection (stale diff), hunk-past-EOF
  rejection, ``\\ No newline at end of file`` meta-lines.
- ``_split_multi_file_diff``: single-file passthrough, multi-file split,
  timestamp-suffix stripping from ``+++ b/path`` lines.
- ``_str_replace_hint``: whitespace-only mismatch and token-match hints.
- ``_compute_diff_text`` + ``_apply_unified_diff`` round-trip.
- ``_backup_file``: creates a timestamped backup and prunes the oldest
  past the cap.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.agent_runtime.diff_utils import (  # noqa: E402
    _apply_unified_diff,
    _backup_file,
    _compute_diff_text,
    _split_multi_file_diff,
    _str_replace_hint,
)


# ═══════════════════════════════════════════════════════════════════
# _apply_unified_diff
# ═══════════════════════════════════════════════════════════════════

def test_apply_basic_hunk():
    original = "line1\nline2\nline3\n"
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n line1\n-line2\n+CHANGED\n line3\n"
    assert _apply_unified_diff(original, diff) == "line1\nCHANGED\nline3\n"


def test_apply_multiple_hunks_tracks_offset():
    """Offset bookkeeping: two hunks where the first changes line count."""
    original = "a\nb\nc\nd\ne\n"
    diff = (
        "--- a/f\n+++ b/f\n"
        "@@ -1,2 +1,3 @@\n a\n-b\n+X\n+Y\n"
        "@@ -4,2 +5,2 @@\n d\n-e\n+Z\n"
    )
    assert _apply_unified_diff(original, diff) == "a\nX\nY\nc\nd\nZ\n"


def test_apply_new_file_hunk_on_empty_original():
    """@@ -0,0 +1,N @@ against an empty file appends."""
    diff = "--- a/new.py\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
    assert _apply_unified_diff("", diff) == "hello\nworld\n"


def test_apply_new_file_hunk_on_empty_original_no_trailing_newline():
    diff = "--- a/n.py\n+++ b/n.py\n@@ -0,0 +1,1 @@\n+x"
    assert _apply_unified_diff("", diff) == "x"


def test_apply_context_mismatch_raises():
    """A stale diff (line numbers shifted) must raise, not corrupt."""
    original = "AAA\nBBB\nCCC\n"
    diff = (
        "--- a/f\n+++ b/f\n"
        "@@ -2,2 +2,2 @@\n ZZZ\n-BBB\n+XXX\n"
    )
    with pytest.raises(ValueError, match="context mismatch"):
        _apply_unified_diff(original, diff)


def test_apply_hunk_past_eof_raises():
    original = "one line\n"
    diff = (
        "--- a/f\n+++ b/f\n"
        "@@ -1,5 +1,5 @@\n one line\n two\n three\n four\n five\n"
    )
    with pytest.raises(ValueError, match="past end of file"):
        _apply_unified_diff(original, diff)


def test_apply_ignores_no_newline_marker():
    original = "a\nb\n"
    diff = (
        "--- a/f\n+++ b/f\n"
        "@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
        "\\ No newline at end of file\n"
    )
    assert _apply_unified_diff(original, diff) == "a\nc\n"


def test_apply_unchanged_hunk_returns_same_content():
    original = "x\ny\nz\n"
    diff = "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n x\n y\n z\n"
    assert _apply_unified_diff(original, diff) == original


def test_apply_insertion_only_hunk():
    original = "a\nc\n"
    diff = "--- a/f\n+++ b/f\n@@ -1,2 +1,3 @@\n a\n+b\n c\n"
    assert _apply_unified_diff(original, diff) == "a\nb\nc\n"


def test_apply_deletion_only_hunk():
    original = "a\nb\nc\n"
    diff = "--- a/f\n+++ b/f\n@@ -1,3 +1,2 @@\n a\n-b\n c\n"
    assert _apply_unified_diff(original, diff) == "a\nc\n"


# ═══════════════════════════════════════════════════════════════════
# _split_multi_file_diff
# ═══════════════════════════════════════════════════════════════════

def test_split_single_file_diff_passthrough():
    diff = "--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert _split_multi_file_diff(diff) == [("", diff)]


def test_split_multi_file_diff():
    diff = (
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-p\n+q\n"
    )
    parts = _split_multi_file_diff(diff)
    assert len(parts) == 2
    assert parts[0][0] == "a.py"
    assert parts[1][0] == "b.py"
    assert "a.py" in parts[0][1] and "b.py" not in parts[0][1]
    assert "b.py" in parts[1][1]


def test_split_multi_file_strips_timestamp_suffix():
    """git diff --abbrev / GUIs append a tab+timestamp to the +++ line;
    the captured target path must NOT include it."""
    diff = (
        "--- a/a.py\n+++ b/a.py\t2024-01-01 12:34:56.000000000 +0000\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "--- a/b.py\n+++ b/b.py\t2024-02-02 00:00:00.000000000 +0000\n"
        "@@ -1 +1 @@\n-p\n+q\n"
    )
    parts = _split_multi_file_diff(diff)
    assert parts[0][0] == "a.py"
    assert parts[1][0] == "b.py"


def test_split_diff_without_headers_single_entry():
    assert _split_multi_file_diff("@@ -1 +1 @@\n-x\n+y\n") == [("", "@@ -1 +1 @@\n-x\n+y\n")]


# ═══════════════════════════════════════════════════════════════════
# _str_replace_hint
# ═══════════════════════════════════════════════════════════════════

def test_hint_whitespace_only_mismatch():
    original = "def foo():\n    return 1\n"
    hint = _str_replace_hint(original, "def foo(): return 1")
    assert "whitespace" in hint


def test_hint_token_match():
    original = "def foo():\n    return 42\n"
    hint = _str_replace_hint(original, "return 999")
    assert "tokens DO appear" in hint


def test_hint_empty_when_no_similarity():
    assert _str_replace_hint("abc def ghi\n", "zzz yyy xxx") == ""


# ═══════════════════════════════════════════════════════════════════
# Round-trip: _compute_diff_text -> _apply_unified_diff
# ═══════════════════════════════════════════════════════════════════

def test_compute_then_apply_roundtrip():
    original = "one\ntwo\nthree\nfour\nfive\n"
    proposed = "one\nTWO\nthree\nfour\nfive extra\nsix\n"
    diff = _compute_diff_text("f.txt", original, proposed)
    assert diff.startswith("--- a/f.txt")
    assert "+++ b/f.txt" in diff
    assert _apply_unified_diff(original, diff) == proposed


def test_compute_diff_empty_for_no_change():
    assert _compute_diff_text("f.txt", "a\nb\n", "a\nb\n") == ""


# ═══════════════════════════════════════════════════════════════════
# _backup_file
# ═══════════════════════════════════════════════════════════════════

def test_backup_file_creates_backup(tmp_path):
    backup_dir = tmp_path / "backups"
    src = tmp_path / "file.txt"
    src.write_text("original content", encoding="utf-8")
    created = _backup_file(backup_dir, max_backups=10, p=src)
    assert created.exists()
    assert created.read_text(encoding="utf-8") == "original content"
    # Name embeds the md5 of the path so undo_write can find it.
    import hashlib
    h = hashlib.md5(str(src).encode()).hexdigest()[:8]
    assert h in created.name


def test_backup_file_prunes_oldest(tmp_path):
    backup_dir = tmp_path / "backups"
    src = tmp_path / "file.txt"
    src.write_text("v1", encoding="utf-8")
    # Create 5 backups with a cap of 3.
    for _ in range(5):
        _backup_file(backup_dir, max_backups=3, p=src)
        src.write_text(src.read_text(encoding="utf-8") + "x", encoding="utf-8")
    remaining = sorted(backup_dir.iterdir())
    assert len(remaining) == 3
