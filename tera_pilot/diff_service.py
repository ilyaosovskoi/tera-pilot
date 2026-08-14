"""
Tera Pilot v1.1 — Diff Service.

Generates unified diffs between original and proposed file content.
Applies patches with hunk-level accept/reject.
Used by Inline Apply feature in chat.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DiffResult:
    """Result of computing a diff between two file versions."""
    file_path: str
    original: str
    proposed: str
    unified_diff: str
    hunks: List[Dict[str, Any]]
    has_changes: bool
    lines_added: int
    lines_removed: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "has_changes": self.has_changes,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "unified_diff": self.unified_diff,
            "hunks": self.hunks,
        }


class DiffService:
    """
    Computes diffs and applies patches.
    Works on strings — file I/O is the caller's responsibility.
    """

    def __init__(self, root: Optional[str] = None):
        self._root = Path(root).resolve() if root else None

    def set_root(self, root: str) -> None:
        self._root = Path(root).expanduser().resolve()

    # ── Diff computation ───────────────────────────────────────────

    def compute_diff(
        self,
        file_path: str,
        original: str,
        proposed: str,
        context_lines: int = 3,
    ) -> DiffResult:
        """Compute unified diff between original and proposed content."""
        original_lines = original.splitlines(keepends=True)
        proposed_lines = proposed.splitlines(keepends=True)

        diff_lines = list(difflib.unified_diff(
            original_lines,
            proposed_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=context_lines,
        ))

        unified_diff = "".join(diff_lines)
        hunks = self._parse_hunks(diff_lines)

        added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

        return DiffResult(
            file_path=file_path,
            original=original,
            proposed=proposed,
            unified_diff=unified_diff,
            hunks=hunks,
            has_changes=added > 0 or removed > 0,
            lines_added=added,
            lines_removed=removed,
        )

    def diff_file_against_disk(
        self,
        file_path: str,
        proposed: str,
        context_lines: int = 3,
    ) -> DiffResult:
        """Diff proposed content against what's on disk."""
        if not self._root:
            return self.compute_diff(file_path, "", proposed, context_lines)

        abs_path = self._safe_resolve(file_path)
        if not abs_path or not abs_path.exists():
            return self.compute_diff(file_path, "", proposed, context_lines)

        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                original = f.read()
        except OSError as e:
            logger.warning(f"[diff] read error: {e}")
            original = ""

        return self.compute_diff(file_path, original, proposed, context_lines)

    # ── Patch application ──────────────────────────────────────────

    def apply_proposed(
        self,
        file_path: str,
        proposed: str,
    ) -> Dict[str, Any]:
        """Write proposed content to disk. Returns {ok, diff, error}."""
        if not self._root:
            return {"ok": False, "error": "No project root set"}

        abs_path = self._safe_resolve(file_path)
        if not abs_path:
            return {"ok": False, "error": f"Path traversal blocked: {file_path}"}

        # Read original
        original = ""
        if abs_path.exists():
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    original = f.read()
            except OSError as e:
                return {"ok": False, "error": f"Read error: {e}"}

        # Compute diff before writing
        diff = self.compute_diff(file_path, original, proposed)
        if not diff.has_changes:
            return {"ok": True, "diff": diff.to_dict(), "message": "No changes"}

        # Write
        # v1.0.5-security: atomic write via tempfile + os.replace, so a crash
        # mid-write can't leave a half-truncated file (BUGS_REPORT M-DIFF-3).
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            import os as _os
            import tempfile as _tf
            data = proposed.encode('utf-8')
            fd, tmp_path = _tf.mkstemp(prefix='.diff_', suffix='.tmp', dir=str(abs_path.parent))
            try:
                with _os.fdopen(fd, 'wb') as f:
                    f.write(data)
                _os.replace(tmp_path, abs_path)
            except Exception:
                try: _os.unlink(tmp_path)
                except OSError: pass
                raise
        except OSError as e:
            return {"ok": False, "error": f"Write error: {e}"}

        return {"ok": True, "diff": diff.to_dict()}

    def apply_hunks(
        self,
        file_path: str,
        accepted_hunk_indices: List[int],
        hunks: List[Dict[str, Any]],
        original: str,
    ) -> Dict[str, Any]:
        """
        Apply only selected hunks from a diff to the original content.
        Returns the resulting content string (caller writes to disk).

        v1.0.5-security: bugfix (BUGS_REPORT H-DIFF-1, H-DIFF-2).
        Previously this method:
          1. Dropped context lines — `add_lines` contained only `add`-type
             lines, so any accepted hunk with context lines silently lost
             those lines from the file. Fix: rebuild the replacement as
             [context + add] lines (matching what the patch intends).
          2. Computed `idx = old_start + offset` without guarding against
             `old_start = 0` (new-file hunks `@@ -0,0 +1,N @@`), causing
             Python negative-index slicing to insert at the wrong position.
             Fix: clamp old_start to >= 0 and special-case new-file hunks.
        """
        if not hunks or not accepted_hunk_indices:
            return {"ok": True, "content": original, "message": "No hunks accepted"}

        original_lines = original.splitlines(keepends=True)
        # Ensure trailing newline
        if original_lines and not original_lines[-1].endswith("\n"):
            original_lines[-1] += "\n"

        # Build a map: for each line in original, track offset shifts from applied hunks
        result_lines = list(original_lines)
        offset = 0

        # Sort accepted hunks by their position in the file
        accepted = sorted(
            [h for i, h in enumerate(hunks) if i in accepted_hunk_indices],
            key=lambda h: h.get("old_start", 0),
        )

        for hunk in accepted:
            old_start_raw = hunk.get("old_start", 1)
            # v1.0.5-security: guard against new-file hunks (`@@ -0,0 +1,N @@`).
            # `old_start = 0` means "no original lines" — clamp to 0 (not -1).
            if old_start_raw is None or old_start_raw <= 0:
                old_start = 0
            else:
                old_start = old_start_raw - 1  # 0-indexed
            hunk_lines = hunk.get("lines", [])

            # v1.0.5-security: rebuild replacement as [context + add] lines.
            # Context lines must be preserved (they exist in both old and new);
            # add lines are new content; remove lines are dropped.
            # The slice we replace spans `len(remove+context)` original lines.
            remove_count = sum(1 for l in hunk_lines if l["type"] in ("remove", "context"))
            replacement_lines = [
                l["content"] + "\n"
                for l in hunk_lines
                if l["type"] in ("context", "add")
            ]

            # Replace the section
            idx = old_start + offset
            # v1.0.5-security: clamp idx to valid range to avoid negative-index slicing.
            if idx < 0:
                idx = 0
            end_idx = idx + remove_count
            # If idx is beyond end (new-file hunk on empty original), append.
            if idx >= len(result_lines):
                result_lines.extend(replacement_lines)
            else:
                result_lines[idx:end_idx] = replacement_lines
            offset += len(replacement_lines) - remove_count

        content = "".join(result_lines)
        return {"ok": True, "content": content}

    # ── Code block extraction from chat ────────────────────────────

    def extract_code_blocks(self, text: str) -> List[Dict[str, str]]:
        """
        Extract fenced code blocks from assistant messages.
        Returns [{language, code, file_hint}, ...]
        """
        blocks = []
        # Match ```lang\ncode\n```
        pattern = r"```(\w*)\s*\n(.*?)```"
        for m in re.finditer(pattern, text, re.DOTALL):
            lang = m.group(1) or "text"
            code = m.group(2)
            # Try to detect file path from preceding text
            # Common patterns: "file: path/to/file.py" or "# file.py" or just before the block
            file_hint = ""
            preceding = text[:m.start()].rstrip()
            # Check for file path pattern
            path_match = re.search(r"([\w./\-]+\.\w+)\s*$", preceding)
            if path_match:
                file_hint = path_match.group(1)
            # Check for "// file:" or "# file:" or "/* file:" patterns
            first_line = code.split("\n")[0] if code else ""
            comment_match = re.match(r"^\s*(?://|#|/\*)\s*(?:file|path|filename)\s*[:=]\s*(\S+)", first_line)
            if comment_match:
                file_hint = comment_match.group(1)

            blocks.append({
                "language": lang,
                "code": code,
                "file_hint": file_hint,
            })
        return blocks

    # ── Helpers ────────────────────────────────────────────────────

    def _safe_resolve(self, rel_path: str) -> Optional[Path]:
        if not self._root:
            return None
        candidate = (self._root / rel_path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError:
            logger.warning(f"[diff] path traversal blocked: {rel_path}")
            return None
        return candidate

    def _parse_hunks(self, diff_lines: List[str]) -> List[Dict[str, Any]]:
        """Parse diff lines into structured hunks."""
        hunks = []
        current_hunk = None
        old_ln = new_ln = 0

        for line in diff_lines:
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                if current_hunk:
                    hunks.append(current_hunk)
                old_start = int(m.group(1))
                new_start = int(m.group(3))
                old_ln = old_start
                new_ln = new_start
                current_hunk = {
                    "header": line,
                    "old_start": old_start,
                    "new_start": new_start,
                    "lines": [],
                }
                continue

            if current_hunk is None:
                continue

            if line.startswith("+") and not line.startswith("+++"):
                current_hunk["lines"].append({"type": "add", "content": line[1:], "new_lineno": new_ln})
                new_ln += 1
            elif line.startswith("-") and not line.startswith("---"):
                current_hunk["lines"].append({"type": "remove", "content": line[1:], "old_lineno": old_ln})
                old_ln += 1
            elif line.startswith(" "):
                current_hunk["lines"].append({"type": "context", "content": line[1:], "old_lineno": old_ln, "new_lineno": new_ln})
                old_ln += 1
                new_ln += 1

        if current_hunk:
            hunks.append(current_hunk)
        return hunks