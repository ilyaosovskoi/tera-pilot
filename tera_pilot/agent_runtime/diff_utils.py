"""
Diff utilities for the agent runtime.

Contains:
- _split_multi_file_diff(): split a single unified diff that
  touches multiple files into per-file chunks.
- _apply_unified_diff(): apply a unified diff to original text.
- _str_replace_hint(): produce a human-readable hint when
  str_replace cannot find old_str (helps the model self-correct).
- _compute_diff_text(): unified-diff string between original and
  proposed file content.
- _backup_file(): create a `.bak` snapshot of a file before
  overwriting it (used by write_file / str_replace / apply_diff).

Pure-Python, no I/O side effects except _backup_file.
"""

import difflib
import hashlib
import re
import shutil
import time
from pathlib import Path
from typing import List, Tuple

# ── multi-file diff splitting ─────────────────────────────────


def _split_multi_file_diff(diff: str) -> List[Tuple[str, str]]:
    """Split a multi-file unified diff into per-file (path, diff_text) tuples.

    v1.0.6: if the diff contains --- a/ / +++ b/ headers for multiple
    files, each file's hunks are separated and returned independently.
    Single-file diffs (or diffs without file headers) return a list
    with one entry using an empty path (M-RT-3).

    v1.1.3-fix (bug 1.11): the regex captured the entire remainder of
    the +++ line, including optional timestamp suffixes that some
    ``git diff`` modes and GUIs append (``+++ b/path.py\t2024-01-01
    12:34:56.000000000 +0000``). The captured target_path then included
    the timestamp, causing the write to go to the wrong file. We now
    strip anything after a tab or whitespace.
    """
    file_header_re = re.compile(r"^---\s+a/(.+)\s*\n\+\+\+\s+b/(.+)", re.MULTILINE)
    matches = list(file_header_re.finditer(diff))
    if len(matches) < 2:
        # Not a multi-file diff — return as-is
        return [("", diff)]
    splits: List[Tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        # Use the "new" path (+++ b/path) as the target.
        # v1.1.3-fix (bug 1.11): strip any timestamp suffix after a tab
        # or whitespace. ``git diff`` with --abbrev or some GUIs append
        # ``\t2024-01-01 12:34:56.000000000 +0000`` to the +++ line.
        raw_path = m.group(2).strip()
        target_path = raw_path.split("\t")[0].split()[0].strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
        file_diff = diff[start:end]
        splits.append((target_path, file_diff))
    return splits


def _apply_unified_diff(original: str, diff: str) -> str:
    """Apply a unified diff to *original*, returning the new content.

    v1.0.5-correctness: the old applicator never verified that the
    context lines in the diff actually matched the corresponding lines
    in *original*. If the diff was generated against a stale version of
    the file (line numbers shifted by even one line), the slice
    assignment would silently overwrite the wrong lines, and the running
    ``offset`` would accumulate the wrong correction for subsequent
    hunks — corrupting the file with no error (BUGS_REPORT H-RT-8).

    The new implementation:
      1. Verifies each hunk's context lines match the file at the
         expected position. If they don't, it raises ``ValueError``
         instead of silently corrupting the file.
      2. Handles new-file hunks (``@@ -0,0 +1,N @@``) by appending
         instead of slicing at index -1.
      3. Clamps ``orig_start`` to a valid range.
    """
    orig_lines = original.splitlines(keepends=True)
    result = list(orig_lines)
    offset = 0

    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    diff_lines = diff.splitlines(keepends=True)

    i = 0
    while i < len(diff_lines):
        m = hunk_re.match(diff_lines[i])
        if m:
            orig_start_raw = int(m.group(1))
            # v1.0.5-correctness: handle new-file hunks (`@@ -0,0 +1,N @@`).
            if orig_start_raw <= 0:
                orig_start = 0
            else:
                orig_start = orig_start_raw - 1  # 0-indexed
            i += 1
            hunk_orig, hunk_new = [], []
            while i < len(diff_lines) and not hunk_re.match(diff_lines[i]):
                line = diff_lines[i]
                if line.startswith("-"):
                    hunk_orig.append(line[1:])
                elif line.startswith("+"):
                    hunk_new.append(line[1:])
                elif line.startswith(" "):
                    hunk_orig.append(line[1:])
                    hunk_new.append(line[1:])
                # Lines not starting with -, +, or space (e.g. "\ No newline
                # at end of file") are ignored — they're meta-markers.
                i += 1

            start = orig_start + offset
            # v1.0.5-correctness: verify context+remove lines match the
            # file at the expected position. If they don't, refuse to
            # apply rather than corrupting the file silently.
            if hunk_orig:
                if start < 0:
                    raise ValueError(
                        f"diff apply failed: hunk starts before line 0 "
                        f"(orig_start={orig_start_raw}, offset={offset})"
                    )
                if start + len(hunk_orig) > len(result):
                    raise ValueError(
                        f"diff apply failed: hunk extends past end of file "
                        f"(need lines {start+1}..{start+len(hunk_orig)}, "
                        f"file has {len(result)} lines)"
                    )
                actual = result[start:start + len(hunk_orig)]
                if actual != hunk_orig:
                    # Show a short diagnostic so the caller (and the agent)
                    # can re-read the file and regenerate the diff.
                    preview_expected = "".join(hunk_orig[:3]).rstrip()
                    preview_actual = "".join(actual[:3]).rstrip()
                    raise ValueError(
                        f"diff apply failed: context mismatch at line {start+1}. "
                        f"Diff expected:\n  {preview_expected!r}\n"
                        f"File has:\n  {preview_actual!r}\n"
                        f"The diff was likely generated against a stale "
                        f"version of the file — re-read it and regenerate."
                    )
            # Apply the hunk.
            if start >= len(result) and not hunk_orig:
                # New-file hunk on empty original — append.
                result.extend(hunk_new)
            else:
                result[start:start + len(hunk_orig)] = hunk_new
            offset += len(hunk_new) - len(hunk_orig)
        else:
            i += 1

    return "".join(result)


# ── str_replace helper ─────────────────────────────────────────


def _str_replace_hint(original: str, old_str: str) -> str:
    """Best-effort hint: if the model was close (whitespace-only diff),
    point that out so it can self-correct."""
    # Normalize whitespace and try again
    norm = lambda s: " ".join(s.split())
    if norm(old_str) and norm(old_str) in norm(original):
        return ("Hint: the text matches up to whitespace — "
                "copy the exact indentation/newlines from the file.")
    # Check if a unique fragment of old_str appears in original
    words = [w for w in old_str.split() if len(w) >= 4]
    if words:
        found = [w for w in words if w in original]
        if found:
            return (f"Hint: these tokens DO appear in the file: "
                    f"{', '.join(found[:5])}. Re-read the file around them.")
    return ""


def _compute_diff_text(path: str, original: str, proposed: str) -> str:
    """Compute unified diff string."""
    diff = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    ))
    return "".join(diff)


def _backup_file(backup_dir: Path, max_backups: int, p: Path) -> Path:
    """Create a timestamped backup of *path* in the backup directory.

    v1.0.6: enforces a maximum backup count (M-RT-4).
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    # v2.3.8-fix: monotonic-nanosecond timestamp. The old code used
    # ``int(time.time())`` (seconds), so two writes to the same file within
    # one second produced the SAME backup filename and the second write
    # silently overwrote the first — undo_write could only ever reach the
    # latest pre-write state and the prune cap was defeated for fast edits.
    # ``time.monotonic_ns()`` is strictly increasing within the process, so
    # rapid successive backups get unique names AND sort correctly
    # (newest = largest) — which ``_undo_write`` relies on via
    # ``sorted(..., reverse=True)`` + ``candidates[0]``.
    ts = str(time.monotonic_ns())
    h = hashlib.md5(str(p).encode()).hexdigest()[:8]
    backup_name = f"{h}_{ts}_{p.name}"
    backup_path = backup_dir / backup_name
    backup_path.write_bytes(p.read_bytes())
    # v1.0.6: prune old backups if over the cap (M-RT-4). v2.3.8-fix: the
    # prune ran BEFORE writing the new backup, so the directory settled at
    # ``max_backups + 1`` files (write pushes the count over the cap, the
    # NEXT call prunes one, write pushes it over again). Prune AFTER
    # writing so the steady state is exactly ``max_backups``.
    try:
        existing = sorted(backup_dir.iterdir(),
                           key=lambda f: f.stat().st_mtime)
        while len(existing) > max_backups:
            oldest = existing.pop(0)
            try:
                oldest.unlink()
            except OSError:
                pass
    except OSError:
        pass
    return backup_path