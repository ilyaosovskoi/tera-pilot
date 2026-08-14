"""
Tera Pilot v1.0.1 — Code Viewer Service.

Backs the right-hand "Code" panel in the HTML frontend.
Reads files from disk, watches for changes, and exposes a small API
the web bridge can call:

    list_files(root) → [{path, name, section, status, lines}, ...]
    read_file(path)  → {path, content, language, lines}
    search(pattern)  → [{path, line, text}, ...]
    watch(root, cb)  → notifies on file changes

Designed to be safe: paths are sandboxed to the project root,
no symlinks above root, no writes from this module.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────

@dataclass
class FileEntry:
    path: str
    name: str
    section: str               # "App" | "Tests" | "Root" — derived from top dir
    status: str = ""           # "" | "created" | "modified" | "deleted"
    lines: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path":    self.path,
            "name":    self.name,
            "section": self.section,
            "status":  self.status,
            "lines":   self.lines,
        }


@dataclass
class FileContent:
    path: str
    content: str
    language: str
    lines: int
    exists: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path":     self.path,
            "content":  self.content,
            "language": self.language,
            "lines":    self.lines,
            "exists":   self.exists,
        }


@dataclass
class SearchResult:
    path: str
    line: int
    text: str
    match_start: int
    match_end: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path":        self.path,
            "line":        self.line,
            "text":        self.text,
            "match_start": self.match_start,
            "match_end":   self.match_end,
        }


# ── CodeViewer service ─────────────────────────────────────────────

# Files we never want to show in the tree (kept minimal — agent's job is to be transparent)
IGNORED_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    "node_modules", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".eggs", ".tox", ".cache",
}
IGNORED_FILES = {
    ".DS_Store", "Thumbs.db",
}
MAX_FILE_SIZE = 256 * 1024    # 256 KB — anything bigger is shown as "binary/large"


class CodeViewerService:
    """Read-only file browser for the project root."""

    def __init__(self, root: Optional[str] = None):
        self._root: Optional[Path] = Path(root).resolve() if root else None
        self._watcher = None
        self._watch_callback: Optional[Callable[[str, str], None]] = None

    # ── Root management ───────────────────────────────────────────

    def set_root(self, root: str) -> None:
        new_root = Path(root).expanduser().resolve()
        if not new_root.exists():
            raise FileNotFoundError(f"Project root does not exist: {new_root}")
        self._root = new_root
        logger.info(f"[code_viewer] root = {self._root}")
        self._start_watcher()

    @property
    def root(self) -> Optional[Path]:
        return self._root

    # ── Listing ───────────────────────────────────────────────────

    def list_files(self) -> List[Dict[str, Any]]:
        """Return a flat list of files in the project root, grouped by section."""
        if not self._root:
            return []

        entries: List[FileEntry] = []

        # Walk top-level dirs first, then root files
        try:
            for entry in sorted(self._root.iterdir()):
                if entry.name in IGNORED_DIRS or entry.name in IGNORED_FILES:
                    continue
                if entry.is_dir():
                    entries.extend(self._scan_dir(entry, entry.name.capitalize()))
                elif entry.is_file():
                    entries.append(self._make_entry(entry, "Root"))
        except PermissionError as e:
            logger.warning(f"[code_viewer] permission error scanning root: {e}")

        return [e.to_dict() for e in entries]

    def _scan_dir(self, dir_path: Path, section: str) -> List[FileEntry]:
        out: List[FileEntry] = []
        try:
            for entry in sorted(dir_path.iterdir()):
                if entry.name in IGNORED_DIRS or entry.name in IGNORED_FILES:
                    continue
                if entry.is_dir():
                    out.extend(self._scan_dir(entry, section))
                elif entry.is_file():
                    out.append(self._make_entry(entry, section))
        except (PermissionError, OSError) as e:
            logger.warning(f"[code_viewer] error scanning {dir_path}: {e}")
        return out

    def _make_entry(self, file_path: Path, section: str) -> FileEntry:
        rel = str(file_path.relative_to(self._root))
        try:
            size = file_path.stat().st_size
        except OSError:
            size = 0
        # Quick line count — capped so we don't read 50 MB files just to count
        lines = 0
        if size < MAX_FILE_SIZE:
            try:
                with open(file_path, "rb") as f:
                    lines = sum(1 for _ in f)
            except (OSError, UnicodeDecodeError):
                lines = 0
        return FileEntry(
            path=rel,
            name=file_path.name,
            section=section,
            status="",
            lines=lines,
        )

    # ── Reading ───────────────────────────────────────────────────

    def read_file(self, rel_path: str) -> Dict[str, Any]:
        if not self._root:
            return FileContent(rel_path, "", "text", 0, exists=False).to_dict()

        abs_path = self._resolve_safe(rel_path)
        if not abs_path or not abs_path.exists():
            return FileContent(rel_path, "", "text", 0, exists=False).to_dict()

        try:
            size = abs_path.stat().st_size
            if size > MAX_FILE_SIZE:
                return FileContent(
                    rel_path,
                    f"# File too large to preview ({size // 1024} KB).\n# Open in external editor.\n",
                    "text", 2, exists=True,
                ).to_dict()

            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            logger.warning(f"[code_viewer] read error: {e}")
            return FileContent(rel_path, f"# Error reading file: {e}\n", "text", 1, exists=True).to_dict()

        lines = content.count("\n") + (0 if content.endswith("\n") else 1)
        return FileContent(
            rel_path,
            content,
            self._detect_language(abs_path.name),
            lines,
            exists=True,
        ).to_dict()

    def _resolve_safe(self, rel_path: str) -> Optional[Path]:
        """Resolve a relative path under root, blocking path traversal."""
        candidate = (self._root / rel_path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError:
            logger.warning(f"[code_viewer] path traversal blocked: {rel_path}")
            return None
        return candidate

    @staticmethod
    def _detect_language(filename: str) -> str:
        ext = Path(filename).suffix.lower()
        return {
            ".py":   "python",
            ".js":   "javascript",
            ".ts":   "typescript",
            ".tsx":  "tsx",
            ".jsx":  "jsx",
            ".md":   "markdown",
            ".markdown": "markdown",
            ".json": "json",
            ".toml": "toml",
            ".yaml": "yaml",
            ".yml":  "yaml",
            ".html": "html",
            ".css":  "css",
            ".scss": "scss",
            ".rs":   "rust",
            ".go":   "go",
            ".java": "java",
            ".kt":   "kotlin",
            ".swift":"swift",
            ".c":    "c",
            ".cpp":  "cpp",
            ".h":    "c",
            ".sh":   "bash",
            ".bash": "bash",
            ".zsh":  "bash",
            ".sql":  "sql",
            ".txt":  "text",
            ".env":  "ini",
            ".ini":  "ini",
            ".cfg":  "ini",
        }.get(ext, "text")

    # ── Search ────────────────────────────────────────────────────

    def search(self, pattern: str, *, regex: bool = False, max_results: int = 200) -> List[Dict[str, Any]]:
        """Grep through project files for `pattern`."""
        if not self._root or not pattern:
            return []

        results: List[SearchResult] = []
        try:
            compiled = re.compile(pattern) if regex else None
            needle = pattern.lower() if not regex else None
        except re.error as e:
            logger.warning(f"[code_viewer] bad regex: {e}")
            return []

        for path in self._iter_files():
            if len(results) >= max_results:
                break
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, start=1):
                        if regex:
                            m = compiled.search(line)
                            if m:
                                results.append(SearchResult(
                                    path=str(path.relative_to(self._root)),
                                    line=i,
                                    text=line.rstrip()[:300],
                                    match_start=m.start(),
                                    match_end=m.end(),
                                ))
                                if len(results) >= max_results:
                                    break
                        else:
                            idx = line.lower().find(needle)
                            if idx >= 0:
                                results.append(SearchResult(
                                    path=str(path.relative_to(self._root)),
                                    line=i,
                                    text=line.rstrip()[:300],
                                    match_start=idx,
                                    match_end=idx + len(pattern),
                                ))
                                if len(results) >= max_results:
                                    break
            except OSError:
                continue

        return [r.to_dict() for r in results]

    def _iter_files(self):
        for root, dirs, files in os.walk(self._root):
            # prune ignored dirs in-place
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for name in files:
                if name in IGNORED_FILES:
                    continue
                p = Path(root) / name
                # v1.0.6: catch OSError on stat() — file may have been
                # deleted between os.walk and stat (M-AUTO-4).
                try:
                    if p.stat().st_size < MAX_FILE_SIZE:
                        yield p
                except OSError:
                    continue

    # ── File watching ─────────────────────────────────────────────
    #
    # v2.2.0: the legacy QFileSystemWatcher (PySide6) has been replaced
    # by a plain polling watcher. It scans the project tree every
    # ``POLL_INTERVAL_SECONDS`` seconds, compares mtimes/sizes against
    # the last snapshot, and fires the registered callback for every
    # changed path. No external deps — works on every platform Python
    # runs on.
    #
    # The polling approach trades a tiny bit of CPU + IO for a much
    # simpler deployment story (no inotify / kqueue / ReadDirectoryChanges
    # glue, no FD exhaustion on huge repos, no Qt event loop dependency).
    # On a 10k-file project a full scan takes ~50ms on a modern SSD, so
    # polling at 2s intervals adds <3% IO load.

    #: How often (seconds) the polling watcher re-scans the project root.
    POLL_INTERVAL_SECONDS: float = 2.0

    #: Cap on the number of directories the watcher will scan per tick.
    #: Picked to keep a single poll under ~100ms on a warm SSD. If a
    #: project is larger, deeper dirs are simply not watched — same
    #: behaviour as the legacy QFileSystemWatcher cap.
    MAX_WATCHED_DIRS = 4096

    def watch(self, callback: Callable[[str, str], None]) -> None:
        """Register ``callback(path, event_type)`` for file changes.

        ``event_type`` is either ``"file"`` or ``"directory"``. The
        callback fires from a background daemon thread — callers are
        responsible for thread-safety (typically by posting onto an
        event loop / queue).
        """
        self._watch_callback = callback
        self._start_watcher()

    def _start_watcher(self) -> None:
        if not self._root or not self._watch_callback:
            return
        # Stop any previous watcher first.
        self._stop_watcher_event = getattr(self, "_stop_watcher_event", None) or threading.Event()
        if self._watcher is not None and self._watcher.is_alive():
            # Already running — leave it alone.
            return
        self._stop_watcher_event.clear()
        self._snapshot: Dict[str, float] = self._scan_tree()
        self._watcher = threading.Thread(
            target=self._poll_loop,
            name="tera-pilot-code-viewer-watcher",
            daemon=True,
        )
        self._watcher.start()
        logger.info(
            "[code_viewer] polling watcher started (interval=%ss, dirs~%d) under %s",
            self.POLL_INTERVAL_SECONDS, len(self._snapshot), self._root,
        )

    def _scan_tree(self) -> Dict[str, float]:
        """Snapshot ``{path: mtime}`` for every file under ``_root``.

        Respects ``IGNORED_DIRS`` / ``IGNORED_FILES`` and the
        ``MAX_WATCHED_DIRS`` cap. Returns an empty dict if no root is
        set or the root doesn't exist.
        """
        if not self._root or not self._root.exists():
            return {}
        out: Dict[str, float] = {}
        dir_count = 0
        try:
            for root, dirs, files in os.walk(self._root):
                dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
                dir_count += 1
                if dir_count > self.MAX_WATCHED_DIRS:
                    break
                for name in files:
                    if name in IGNORED_FILES:
                        continue
                    p = os.path.join(root, name)
                    try:
                        out[p] = os.path.getmtime(p)
                    except OSError:
                        continue
        except (PermissionError, OSError) as e:
            logger.warning("[code_viewer] scan failed: %s", e)
        return out

    def _poll_loop(self) -> None:
        """Background loop — re-scans every POLL_INTERVAL_SECONDS and
        fires the callback for every changed path. Stops cleanly when
        ``stop_watcher()`` is called or the process exits.
        """
        while not self._stop_watcher_event.is_set():
            try:
                self._stop_watcher_event.wait(self.POLL_INTERVAL_SECONDS)
                if self._stop_watcher_event.is_set():
                    break
                new_snapshot = self._scan_tree()
                # Detect changes + additions.
                old = self._snapshot
                for path, mtime in new_snapshot.items():
                    prev = old.get(path)
                    if prev is None or prev != mtime:
                        try:
                            self._watch_callback(path, "file")
                        except Exception:
                            logger.exception("[code_viewer] watch callback failed")
                # Detect deletions.
                for path in old.keys() - new_snapshot.keys():
                    try:
                        self._watch_callback(path, "file")
                    except Exception:
                        logger.exception("[code_viewer] watch callback failed")
                self._snapshot = new_snapshot
            except Exception:
                logger.exception("[code_viewer] poll loop error")
                # Avoid a tight error loop.
                self._stop_watcher_event.wait(self.POLL_INTERVAL_SECONDS)

    def _on_watcher_directory_changed(self, path: str) -> None:
        """Legacy compat — kept so external callers don't break.

        The polling watcher doesn't differentiate directory vs file
        events at the source, so this is just a forwarder. The next
        poll will pick the change up and fire ``_watch_callback``.
        """
        try:
            if self._watch_callback:
                self._watch_callback(path, "directory")
        except Exception:
            pass

    def _collect_watched_dirs(self) -> List[str]:
        """v1.1.5 — recursively collect all directories under ``_root``
        that should be watched, respecting ``IGNORED_DIRS`` and the
        ``MAX_WATCHED_DIRS`` cap.

        Kept for backward compatibility with any external code that
        called it directly. The polling watcher no longer needs it —
        :meth:`_scan_tree` does its own walk.
        """
        if not self._root:
            return []
        out: List[str] = [str(self._root)]
        try:
            for root, dirs, _files in os.walk(self._root):
                dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
                for d in dirs:
                    out.append(os.path.join(root, d))
                    if len(out) >= self.MAX_WATCHED_DIRS:
                        return out
        except (PermissionError, OSError) as e:
            logger.warning(f"[code_viewer] partial walk failure during watch setup: {e}")
        return out

    def stop_watcher(self) -> None:
        ev = getattr(self, "_stop_watcher_event", None)
        if ev is not None:
            ev.set()
        w = getattr(self, "_watcher", None)
        if w is not None and w.is_alive():
            try:
                w.join(timeout=2.0)
            except Exception:
                pass
        self._watcher = None
