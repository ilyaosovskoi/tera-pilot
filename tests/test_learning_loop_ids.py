"""Regression tests for ``_next_learning_id`` uniqueness (v2.3.4-fix).

The old implementation picked the next LEARN-YYYYMMDD-XXX id with
``len(today_files) + 1`` but never scanned for EXISTING ids (the loop
body was a ``pass``). If an entry file was deleted/dismissed, the count
dropped and a NEW entry created the same day could reuse an id that
still existed in another file — so a later ``dismiss LEARN-...-003``
would silently hit the wrong entry.

The id lives in the file BODY (``id: LEARN-YYYYMMDD-NNN`` frontmatter
line), not in the filename, so the fix scans the first 2 KB of every
``*.md`` entry file for the highest existing sequence number.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.learning_loop import _next_learning_id  # noqa: E402

DATE = "2026-08-17"
STAMP = "20260817"


def _write_entry(learnings: Path, seq: int, date: str = DATE) -> Path:
    """Write an entry file whose BODY carries ``id: LEARN-...-NNN``.

    The filename is deliberately arbitrary (slug-based, no id) — the id
    lives in the frontmatter line, exactly like create_learning_entry
    produces it.
    """
    stamp = date.replace("-", "")
    f = learnings / f"{date}-entry-{seq:03d}.md"
    f.write_text(
        f"---\nid: LEARN-{stamp}-{seq:03d}\ntitle: Entry {seq}\n---\n",
        encoding="utf-8",
    )
    return f


def test_first_id_is_001(tmp_path: Path):
    assert _next_learning_id(tmp_path, DATE) == f"LEARN-{STAMP}-001"


def test_sequential_ids(tmp_path: Path):
    _write_entry(tmp_path, 1)
    assert _next_learning_id(tmp_path, DATE) == f"LEARN-{STAMP}-002"
    _write_entry(tmp_path, 2)
    assert _next_learning_id(tmp_path, DATE) == f"LEARN-{STAMP}-003"


def test_no_reuse_after_gap(tmp_path: Path):
    """Regression: ids 001/002/003 exist, 002's file is dismissed, the
    next id must be 004 — the old code returned 003, colliding with the
    still-existing 003 file."""
    for seq in (1, 2, 3):
        _write_entry(tmp_path, seq)
    (tmp_path / f"{DATE}-entry-002.md").unlink()
    assert _next_learning_id(tmp_path, DATE) == f"LEARN-{STAMP}-004"


def test_no_reuse_when_oldest_dismissed(tmp_path: Path):
    for seq in (1, 2, 3):
        _write_entry(tmp_path, seq)
    (tmp_path / f"{DATE}-entry-001.md").unlink()
    assert _next_learning_id(tmp_path, DATE) == f"LEARN-{STAMP}-004"


def test_ignores_other_dates(tmp_path: Path):
    _write_entry(tmp_path, 7, date="2026-08-16")  # yesterday
    _write_entry(tmp_path, 99, date="2025-01-01")  # last year
    assert _next_learning_id(tmp_path, DATE) == f"LEARN-{STAMP}-001"


def test_handles_high_sequence_numbers(tmp_path: Path):
    _write_entry(tmp_path, 7)
    _write_entry(tmp_path, 9)
    assert _next_learning_id(tmp_path, DATE) == f"LEARN-{STAMP}-010"


def test_scans_body_not_filename(tmp_path: Path):
    # Filename carries no id; the id is only in the body.
    f = tmp_path / "totally-unrelated-name.md"
    f.write_text(f"id: LEARN-{STAMP}-042\n", encoding="utf-8")
    assert _next_learning_id(tmp_path, DATE) == f"LEARN-{STAMP}-043"


def test_ignores_malformed_and_binary_files(tmp_path: Path):
    (tmp_path / "notes.md").write_text("no ids here", encoding="utf-8")
    (tmp_path / "broken.bin").write_bytes(b"\x00\x01LEARN-\xff\xfe")
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    assert _next_learning_id(tmp_path, DATE) == f"LEARN-{STAMP}-001"
