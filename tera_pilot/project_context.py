"""
Tera Pilot v1.0.9 — Project Context (CLAUDE.md support).

Implements the long-lived instruction-file model of project-level persistent instructions:

  CLAUDE.md    — read automatically when a project is opened. Contains
                 project rules, conventions, architecture notes that
                 should apply to EVERY conversation in that project.
                 Lives at the project root (or ~/.tera_pilot/CLAUDE.md for
                 global rules).

  .claude/     — optional directory with supplementary context files
                 (coding standards, architecture diagrams in markdown,
                 etc.). All *.md files inside are loaded and concatenated.

The ProjectContext class loads these files once (cached per project root)
and exposes:
  - instructions() → string to inject into the agent's system prompt
  - reload()       → force re-read from disk (after CLAUDE.md is edited)
  - status()       → dict for the /context command

This mirrors the "long-lived instructions
that reduce repetitive explanations in chat and save context tokens".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# File names that are treated as project instructions, in priority order.
# The first match wins for the "main" instructions; supplementary files
# in .tera_pilot/ (or .claude/ for backward compat) are concatenated after.
#
# v1.0.10: TERA_PILOT.md is now the PRIMARY name. CLAUDE.md is kept as a
# secondary fallback for users migrating from other tools, but TERA_PILOT.md
# takes priority — if both exist, TERA_PILOT.md wins.
_PROJECT_INSTRUCTION_FILES = (
    "TERA_PILOT.md",          # ← PRIMARY: Tera Pilot-native name (v1.0.10+)
    "tera_pilot.md",
    ".tera_pilot.md",
    "CLAUDE.md",        # ← secondary: Claude Code compat (legacy)
    "claude.md",
    ".claude.md",
    "AGENTS.md",        # openclaude convention
)

# v1.0.10: .tera_pilot/ is primary supplementary dir, .claude/ is fallback
_SUPPLEMENTARY_DIRS = (".tera_pilot", ".claude")
# v1.0.10: global instructions file — TERA_PILOT.md primary, CLAUDE.md fallback
_GLOBAL_INSTRUCTION_FILES = ("TERA_PILOT.md", "CLAUDE.md")
_MAX_INSTRUCTION_CHARS = 32_000  # cap so a huge TERA_PILOT.md doesn't eat the context window


@dataclass
class ProjectContext:
    """Loads and caches TERA_PILOT.md + supplementary project instructions.

    v1.0.10: TERA_PILOT.md is the primary project instructions file. CLAUDE.md
    is kept as a backward-compat fallback for users migrating from
    other tools. If both exist, TERA_PILOT.md takes priority.

    The cache is keyed by project root — opening a different project
    re-reads its TERA_PILOT.md. Call reload() after editing TERA_PILOT.md to
    pick up changes without restarting Tera Pilot.
    """
    project_root: Optional[Path] = None
    _cached_instructions: str = ""
    _cached_sources: List[str] = field(default_factory=list)
    _cached_at: float = 0.0  # mtime of newest source file

    def set_root(self, root: Optional[str]) -> None:
        """Set the project root and clear the cache (forces re-read)."""
        new_root = Path(root).expanduser().resolve() if root else None
        if new_root != self.project_root:
            self.project_root = new_root
            self._cached_instructions = ""
            self._cached_sources = []
            self._cached_at = 0.0
            logger.debug("[project_context] root changed → %s", new_root)

    def reload(self) -> None:
        """Force re-read of CLAUDE.md on next instructions() call."""
        self._cached_instructions = ""
        self._cached_sources = []
        self._cached_at = 0.0

    def instructions(self) -> str:
        """Return the concatenated project instructions string.

        Returns "" if no CLAUDE.md / supplementary files are found.
        The result is cached until reload() or set_root() is called,
        or until a source file's mtime changes.
        """
        if self._cache_is_fresh():
            return self._cached_instructions

        if not self.project_root or not self.project_root.is_dir():
            self._cached_instructions = ""
            self._cached_sources = []
            return ""

        sources: List[tuple[str, str, float]] = []  # (path, content, mtime)

        # 1. Global instructions (~/.tera_pilot/TERA_PILOT.md) — applies to ALL projects
        # v1.0.10: try TERA_PILOT.md first, fall back to CLAUDE.md for legacy users
        for global_name in _GLOBAL_INSTRUCTION_FILES:
            global_path = Path.home() / ".tera_pilot" / global_name
            content = self._read_file(global_path)
            if content:
                sources.append((str(global_path), content, global_path.stat().st_mtime))
                break  # only one global file

        # 2. Project-root TERA_PILOT.md (or equivalent fallback)
        for name in _PROJECT_INSTRUCTION_FILES:
            p = self.project_root / name
            content = self._read_file(p)
            if content:
                sources.append((str(p), content, p.stat().st_mtime))
                break  # only one main instruction file

        # 3. Supplementary files in .tera_pilot/ (primary) or .claude/ (fallback)
        # v1.0.10: load from BOTH directories if they exist, .tera_pilot/ first
        # for priority ordering. This lets users keep legacy .claude/ files
        # while adding new ones to .tera_pilot/.
        seen_files: set[str] = set()
        for dir_name in _SUPPLEMENTARY_DIRS:
            supp_dir = self.project_root / dir_name
            if not supp_dir.is_dir():
                continue
            for md_file in sorted(supp_dir.glob("*.md")):
                # Deduplicate by filename — if coding-standards.md exists in
                # both .tera_pilot/ and .claude/, the .tera_pilot/ version wins (it was
                # added first).
                if md_file.name in seen_files:
                    continue
                content = self._read_file(md_file)
                if content:
                    sources.append((str(md_file), content, md_file.stat().st_mtime))
                    seen_files.add(md_file.name)

        if not sources:
            self._cached_instructions = ""
            self._cached_sources = []
            self._cached_at = 0.0
            return ""

        # Concatenate with clear section headers so the model knows
        # where each chunk came from.
        parts: List[str] = []
        total_chars = 0
        newest_mtime = 0.0
        source_paths: List[str] = []
        for path, content, mtime in sources:
            newest_mtime = max(newest_mtime, mtime)
            source_paths.append(path)
            # Truncate individual files that are too long
            if len(content) > _MAX_INSTRUCTION_CHARS // 2:
                content = content[:_MAX_INSTRUCTION_CHARS // 2] + "\n... (truncated)\n"
            header = f"# Project instructions from {path}\n"
            parts.append(header + content)
            total_chars += len(content) + len(header)
            if total_chars >= _MAX_INSTRUCTION_CHARS:
                parts.append(f"\n... (remaining project instructions truncated at {_MAX_INSTRUCTION_CHARS} chars)")
                break

        self._cached_instructions = "\n\n".join(parts)
        self._cached_sources = source_paths
        self._cached_at = newest_mtime
        logger.info(
            "[project_context] loaded %d source(s), %d chars total",
            len(source_paths), len(self._cached_instructions),
        )
        return self._cached_instructions

    def status(self) -> Dict[str, Any]:
        """Return a status dict for the /context command."""
        return {
            "project_root": str(self.project_root) if self.project_root else None,
            "sources": list(self._cached_sources),
            "total_chars": len(self._cached_instructions),
            "loaded": bool(self._cached_sources),
        }

    # ── Internal helpers ──────────────────────────────────────────

    def _cache_is_fresh(self) -> bool:
        """Check if the cached instructions are still valid.

        v1.0.6: also detect NEW files that appeared after the last load
        (M-AUTO-3). If a new .md file appears in .tera_pilot/ or a new
        TERA_PILOT.md appears at the project root, we re-read.
        """
        if not self._cached_instructions and not self._cached_sources:
            return False
        if not self.project_root or not self.project_root.is_dir():
            return False

        # Check if any source file was modified after _cached_at
        for path_str in self._cached_sources:
            p = Path(path_str)
            try:
                if p.stat().st_mtime > self._cached_at:
                    return False
            except OSError:
                # File was deleted — cache is stale
                return False

        # v1.0.6: detect NEW instruction files (M-AUTO-3).
        # Check if any TERA_PILOT.md/CLAUDE.md exists at the project root
        # that wasn't in our source list.
        has_root_instruction = any(
            Path(path_str).parent == self.project_root
            for path_str in self._cached_sources
        )
        if not has_root_instruction:
            for name in _PROJECT_INSTRUCTION_FILES:
                if (self.project_root / name).exists():
                    return False  # New root instruction file appeared

        # Check for new supplementary files in .tera_pilot/ / .claude/
        for dir_name in _SUPPLEMENTARY_DIRS:
            supp_dir = self.project_root / dir_name
            if not supp_dir.is_dir():
                continue
            existing_names = {
                Path(p).name for p in self._cached_sources
                if str(supp_dir) in p
            }
            for md_file in supp_dir.glob("*.md"):
                if md_file.name not in existing_names:
                    # New supplementary file detected
                    try:
                        if md_file.stat().st_mtime > self._cached_at:
                            return False
                    except OSError:
                        continue

        return True

    @staticmethod
    def _read_file(p: Path) -> Optional[str]:
        """Read a markdown file, returning None if missing or unreadable."""
        if not p.exists() or not p.is_file():
            return None
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("[project_context] failed to read %s: %s", p, e)
            return None


# Module-level singleton — shared by the agent runtime and the bridge
_project_context: Optional[ProjectContext] = None


def get_project_context() -> ProjectContext:
    """Get the global ProjectContext singleton."""
    global _project_context
    if _project_context is None:
        _project_context = ProjectContext()
    return _project_context
