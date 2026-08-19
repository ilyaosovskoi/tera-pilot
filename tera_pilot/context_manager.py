"""
Tera Pilot v1.1 — Context Manager.

Intelligently selects which files to include in the LLM context.
Ranks files by relevance using heuristics + optional embeddings.
Manages a token budget so only the most relevant files are sent.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Files/dirs to always skip
IGNORED_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    "node_modules", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".eggs", ".tox", ".cache", ".next", ".nuxt", "target", "out",
}

# Approximate chars per token (conservative for mixed content)
CHARS_PER_TOKEN = 4.0

# v2.3.4-fix: hard bounds for the project index walk. The OLD code walked
# the ENTIRE project synchronously inside set_root()/AgentRuntime.__init__
# with no cap — opening a large folder (e.g. the GUI's "Open project"
# picking ~ or ~/Documents) indexed 346k files and blocked the HTTP
# request for ~20s (the web GUI appeared frozen, and the picker dialog
# "opened but no directory and an error"). The index is only consumed at
# agent-run time (select_context / build_context_block), so we build it
# lazily on first use and stop early on pathological trees.
_MAX_INDEX_FILES = 50_000     # stop after this many files
_INDEX_TIME_BUDGET = 5.0      # stop after this many seconds

# Common file extensions and their "information density" (higher = more compact)
EXT_PRIORITY = {
    ".py": 1.0, ".js": 1.0, ".ts": 1.0, ".tsx": 1.0, ".jsx": 1.0,
    ".rs": 0.9, ".go": 0.9, ".java": 0.85, ".kt": 0.85,
    ".md": 0.6, ".rst": 0.6, ".txt": 0.5,
    ".json": 0.7, ".yaml": 0.7, ".yml": 0.7, ".toml": 0.7,
    ".css": 0.7, ".scss": 0.7, ".html": 0.7,
    ".sql": 0.8, ".sh": 0.8,
}


@dataclass
class FileScore:
    """A file with its relevance score."""
    path: str
    rel_path: str
    size_bytes: int
    approx_tokens: int
    score: float
    reason: str       # why this file was scored highly

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.rel_path,
            "size_bytes": self.size_bytes,
            "approx_tokens": self.approx_tokens,
            "score": round(self.score, 2),
            "reason": self.reason,
        }


class ContextManager:
    """
    Selects the most relevant files for the LLM context.
    Uses multiple heuristic signals to rank files.
    """

    def __init__(self, root: Optional[str] = None):
        self._root: Optional[Path] = None
        self._file_index: Dict[str, Dict[str, Any]] = {}
        self._pinned_files: Set[str] = set()  # user-pinned files always included
        self._recently_accessed: List[str] = []  # last N files accessed by agent
        self._max_recent = 20
        # v2.3.4-fix: index building is deferred until first use (see
        # _ensure_indexed). set_root() clears this flag so a workspace
        # switch re-indexes lazily instead of blocking the caller.
        self._index_dirty = False
        # v1.1.4-fix: 128K was the entire context window of most models,
        # which left no room for the system prompt, MCP catalog, skills,
        # or conversation history once this block is actually injected
        # into the prompt (see AgentRuntime.execute_task). 6K tokens is a
        # reasonable slice for "auto-attached relevant files" — pinned
        # files are still always included regardless of this budget, and
        # it remains overridable via set_token_budget().
        self._token_budget: int = 6_000

        if root:
            self.set_root(root)

    def set_root(self, root: str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._file_index = {}
        self._pinned_files.clear()
        self._recently_accessed.clear()
        # v2.3.4-fix: do NOT walk the tree here. set_root() is called from
        # AgentRuntime.__init__ and set_workspace() — i.e. on every
        # workspace switch and on the GUI's "Open project" request. The old
        # synchronous _index_project() walked every file under the new
        # root, which froze the request for ~20s on large folders. The
        # index is only read when the agent actually runs, so mark it
        # dirty and build it lazily in _ensure_indexed() (bounded).
        self._index_dirty = True

    def _ensure_indexed(self) -> None:
        """Build the file index on first use, at most once per set_root().

        v2.3.4-fix: set_root() used to index synchronously (a 20s+ block on
        large folders). Now the walk is deferred until the index is actually
        needed (score_files / stats) and is bounded by _MAX_INDEX_FILES and
        _INDEX_TIME_BUDGET so a pathological tree (e.g. a whole home
        directory) can never freeze an HTTP request or agent turn.
        """
        if not self._index_dirty:
            return
        self._index_dirty = False
        self._index_project()

    def set_token_budget(self, tokens: int) -> None:
        self._token_budget = max(1000, tokens)

    def pin_file(self, rel_path: str) -> None:
        self._pinned_files.add(rel_path)

    def unpin_file(self, rel_path: str) -> None:
        self._pinned_files.discard(rel_path)

    def mark_accessed(self, rel_path: str) -> None:
        """Mark a file as recently accessed by the agent (boosts its score).

        v1.1.5-fix (tera_pilot_bug_report.md bug #4): also add the file to
        ``_file_index`` if it isn't already there. ``_index_project``
        is a one-time disk snapshot taken in ``set_root()`` — files
        the agent creates mid-session never appear in that snapshot,
        so ``score_files()`` (which iterates only ``_file_index``)
        silently skips them. The "bug 4.2" fix that introduced
        ``mark_accessed`` was supposed to keep agent-active files
        auto-attached on later iterations, but for the main scenario
        (agent creates a new file then keeps working on it) the
        promise was broken: the file was added to
        ``_recently_accessed`` but never to ``_file_index``, so the
        recency boost in ``score_files`` never matched. We now
        reconcile the index lazily here, on every ``mark_accessed``
        call.
        """
        if rel_path in self._recently_accessed:
            self._recently_accessed.remove(rel_path)
        self._recently_accessed.insert(0, rel_path)
        if len(self._recently_accessed) > self._max_recent:
            self._recently_accessed.pop()
        # Bug #4 fix: keep _file_index in sync with files the agent
        # actually touches, so score_files() can see them. Best-effort
        # — silently skip files that no longer exist or are outside
        # the project root.
        if rel_path not in self._file_index:
            self._add_to_index(rel_path)

    def _add_to_index(self, rel_path: str) -> None:
        """Add a single file to ``_file_index`` if it exists on disk
        and lives inside ``self._root``. Used by ``mark_accessed`` to
        keep the index in sync with files the agent creates mid-session
        (bug #4). Silently no-ops for missing / out-of-root paths so
        callers can invoke it unconditionally.
        """
        if not self._root:
            return
        p = self._safe_resolve(rel_path)
        if p is None or not p.exists() or not p.is_file():
            return
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        ext = p.suffix.lower()
        self._file_index[rel_path] = {
            "size": size,
            "approx_tokens": int(size / CHARS_PER_TOKEN),
            "ext": ext,
            "priority": EXT_PRIORITY.get(ext, 0.5),
        }

    # ── Project indexing ───────────────────────────────────────────

    def _index_project(self) -> None:
        """Walk the project and build a lightweight file index (bounded).

        v2.3.4-fix: the walk is now capped by _MAX_INDEX_FILES and
        _INDEX_TIME_BUDGET. Previously this walked every file under the
        root with no limit — opening ~ or ~/Documents indexed 346k files
        and blocked the caller for ~20s. A partial index is fine: the
        agent can always read_file/grep for files the index missed.
        """
        if not self._root:
            return
        self._file_index = {}
        start = time.monotonic()
        indexed = 0
        try:
            for root, dirs, files in os.walk(self._root):
                dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
                for name in files:
                    if name.startswith("."):
                        continue
                    indexed += 1
                    if indexed > _MAX_INDEX_FILES:
                        logger.info(
                            "[context] index truncated at %d files (limit %d)",
                            len(self._file_index), _MAX_INDEX_FILES,
                        )
                        return
                    if time.monotonic() - start > _INDEX_TIME_BUDGET:
                        logger.info(
                            "[context] index stopped after %.1fs (%d files)",
                            time.monotonic() - start, len(self._file_index),
                        )
                        return
                    try:
                        size = os.stat(os.path.join(root, name)).st_size
                    except OSError:
                        size = 0
                    rel = os.path.relpath(os.path.join(root, name), self._root)
                    ext = Path(name).suffix.lower()
                    self._file_index[rel] = {
                        "size": size,
                        "approx_tokens": int(size / CHARS_PER_TOKEN),
                        "ext": ext,
                        "priority": EXT_PRIORITY.get(ext, 0.5),
                    }
        except PermissionError as e:
            logger.warning(f"[context] permission error indexing: {e}")
        logger.info(f"[context] indexed {len(self._file_index)} files")

    # ── Relevance scoring ──────────────────────────────────────────

    def score_files(
        self,
        query: str = "",
        mentioned_files: Optional[List[str]] = None,
    ) -> List[FileScore]:
        """
        Score all indexed files by relevance to the query.
        Returns sorted list (highest score first).
        """
        self._ensure_indexed()
        if not self._file_index:
            return []

        # Normalize query for matching
        query_lower = query.lower()
        query_words = set(re.findall(r"\w+", query_lower))
        mentioned_set = set(mentioned_files or [])

        scores: List[FileScore] = []
        for rel_path, info in self._file_index.items():
            score = 0.0
            reason = ""

            # 1. Explicitly mentioned in prompt — highest priority
            if rel_path in mentioned_set or Path(rel_path).name in mentioned_set:
                score += 100.0
                reason = "mentioned in prompt"

            # 2. Pinned by user
            if rel_path in self._pinned_files:
                score += 80.0
                if not reason:
                    reason = "pinned by user"

            # 3. Recently accessed by agent
            try:
                recent_idx = self._recently_accessed.index(rel_path)
                recency_boost = max(0, 20 - recent_idx)  # 20 for most recent, decays
                score += recency_boost
                if not reason:
                    reason = f"recently accessed (#{recent_idx + 1})"
            except ValueError:
                pass

            # 4. File name contains query words
            path_lower = rel_path.lower()
            for word in query_words:
                if word in path_lower:
                    score += 5.0
                    if not reason:
                        reason = f"path matches '{word}'"

            # 5. Config/entry files get a small boost
            name = Path(rel_path).name
            if name in ("README.md", "package.json", "pyproject.toml", "Cargo.toml",
                        "go.mod", "Makefile", "Dockerfile", ".env.example"):
                score += 3.0
                if not reason:
                    reason = "project config file"

            # 6. Source files over generated/docs
            score += info["priority"] * 2.0

            scores.append(FileScore(
                path=str(self._root / rel_path) if self._root else rel_path,
                rel_path=rel_path,
                size_bytes=info["size"],
                approx_tokens=info["approx_tokens"],
                score=score,
                reason=reason or "default",
            ))

        # Sort by score descending
        scores.sort(key=lambda s: s.score, reverse=True)
        return scores

    # ── Context selection ──────────────────────────────────────────

    def select_context(
        self,
        query: str = "",
        mentioned_files: Optional[List[str]] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Select files to fill the token budget.
        Returns {files: [...], total_tokens, budget, utilization_pct}.
        """
        budget = max_tokens or self._token_budget
        scored = self.score_files(query, mentioned_files)

        selected = []
        used_tokens = 0

        for fs in scored:
            # Always include pinned files
            if fs.rel_path in self._pinned_files:
                selected.append(fs)
                used_tokens += fs.approx_tokens
                continue

            if used_tokens + fs.approx_tokens > budget:
                continue

            selected.append(fs)
            used_tokens += fs.approx_tokens

        utilization = (used_tokens / budget * 100) if budget > 0 else 0

        return {
            "files": [s.to_dict() for s in selected],
            "total_tokens": used_tokens,
            "budget": budget,
            "utilization_pct": round(utilization, 1),
            "total_indexed": len(self._file_index),
        }

    # ── File content loading ───────────────────────────────────────

    def load_file_content(self, rel_path: str) -> Dict[str, Any]:
        """Load a file's content for inclusion in context."""
        if not self._root:
            return {"path": rel_path, "content": "", "error": "No root"}
        abs_path = self._safe_resolve(rel_path)
        if not abs_path or not abs_path.exists():
            return {"path": rel_path, "content": "", "error": "Not found"}
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {"path": rel_path, "content": content, "tokens": int(len(content) / CHARS_PER_TOKEN)}
        except OSError as e:
            return {"path": rel_path, "content": "", "error": str(e)}

    def build_context_block(
        self,
        query: str = "",
        mentioned_files: Optional[List[str]] = None,
    ) -> str:
        """
        Build a text block with selected file contents for injection into the prompt.
        Format: <file path="...">\n...\n</file>
        """
        selection = self.select_context(query, mentioned_files)
        parts = []
        for f in selection["files"]:
            loaded = self.load_file_content(f["path"])
            if loaded.get("content"):
                parts.append(f'<file path="{f["path"]}">\n{loaded["content"]}\n</file>')
        return "\n\n".join(parts)

    # ── Stats ──────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        self._ensure_indexed()
        return {
            "total_files": len(self._file_index),
            "pinned_files": list(self._pinned_files),
            "recently_accessed": self._recently_accessed[:10],
            "token_budget": self._token_budget,
        }

    # ── Helpers ────────────────────────────────────────────────────

    def _safe_resolve(self, rel_path: str) -> Optional[Path]:
        if not self._root:
            return None
        candidate = (self._root / rel_path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError:
            return None
        return candidate


# ── Singleton ──────────────────────────────────────────────────────
# v1.1.4-fix: this module was never wired in anywhere — ContextManager
# was fully implemented but dead code. Mirrors project_context.py's
# get_project_context() so agent_runtime.py and web_bridge.py can share
# one instance per process.
_context_manager: Optional["ContextManager"] = None


def get_context_manager() -> "ContextManager":
    """Get the global ContextManager singleton."""
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager