"""
Tera Pilot v1.1 — Git Service.

Wraps git CLI for status, diff, commit, log, branch, stage/unstage.
No gitpython dependency — pure subprocess.
Provides data to the web bridge for UI rendering.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# v1.0.5-security: unquote git's C-style quoted paths.
# When `core.quotePath=true` (git's default), git wraps paths containing
# spaces, tabs, newlines, or non-ASCII bytes in double quotes and octal-
# escapes the non-ASCII bytes: `"r\303\251sum\303\251.txt"` for `résumé.txt`.
# Without unquoting, the path returned by `_parse_status` would not match
# what's actually on disk, and `git add <path>` would silently fail
# (BUGS_REPORT H-GIT-1).
_OCTAL_ESCAPE_RE = re.compile(rb'\\([0-7]{3})')


# ── v2.3.4-security: neutralize exec-capable git config keys + hooks ──
# A repository is UNTRUSTED input (threat model T1). git will execute
# the values of a whole family of config keys read from the REPO's own
# ``.git/config`` (no ``-c`` flag needed): ``core.fsmonitor`` runs on
# plain ``git status``, ``core.editor`` on ``git commit``, ``diff.*.textconv``
# on ``git diff``, ``filter.*.clean/.smudge`` on ``git add``/``checkout``,
# ``credential.*.helper`` on auth, and every ``.git/hooks/*`` script runs
# on the matching git operation. Confirmed live: a malicious repo whose
# ``.git/config`` contains ``[core] fsmonitor = touch /tmp/pwned`` executes
# that command on a routine ``git status`` — a workspace-sandbox escape
# that the command-line arg checks in ``ToolEngine._validate_command_paths``
# cannot see, because nothing is passed on the command line.
#
# Fix: for every git invocation made by the agent, inject ``-c`` overrides
# that (a) empty out every exec-capable key and (b) point ``core.hooksPath``
# at a directory that cannot contain hooks — so repo-supplied commands and
# hooks are neutralized regardless of what the repo's config contains.
_GIT_NEUTRALIZE_FIXED = [
    "-c", "core.fsmonitor=",          # runs on `git status`
    "-c", "core.editor=true",         # runs on `git commit` (true = no-op)
    "-c", "core.sshcommand=",         # runs on fetch/push
    "-c", "core.pager=",              # runs when paging
    "-c", "core.askpass=",            # runs on credential prompts
    "-c", "core.hookspath=" + str(Path(tempfile.gettempdir()) / "tera_pilot_no_hooks"),
    "-c", "sequence.editor=true",     # runs on rebase
    "-c", "credential.helper=",       # runs on credential prompts
    "-c", "interactive.difffilter=",  # runs on interactive diff
]

# Driver keys are per-name (``diff.<name>.textconv`` etc.) so they can't
# be enumerated in a fixed list — scan the repo's own config for them.
_GIT_EXEC_KEY_EXACT = frozenset({
    "core.fsmonitor", "core.editor", "core.sshcommand", "core.pager",
    "core.askpass", "core.hookspath", "sequence.editor",
    "credential.helper", "interactive.difffilter",
})
_GIT_EXEC_KEY_PATTERNS = (
    ("diff.", ".textconv"),
    ("filter.", ".clean"),
    ("filter.", ".smudge"),
    ("credential.", ".helper"),
)


def _git_key_is_exec_capable(key: str) -> bool:
    k = key.lower()
    if k in _GIT_EXEC_KEY_EXACT:
        return True
    return any(k.startswith(pre) and k.endswith(suf)
                for pre, suf in _GIT_EXEC_KEY_PATTERNS)


def _git_repo_exec_config_keys(root: Path) -> List[str]:
    """Return exec-capable config keys that git would read from the repo's
    own ``.git/config`` (driver keys with arbitrary names).

    Handles both a ``.git`` directory and a ``.git`` file (worktrees).
    Returns an empty list on any parse problem — the fixed neutralization
    set above still applies, so a malformed config cannot weaken it.
    """
    git_dir = root / ".git"
    config_path: Optional[Path] = None
    try:
        if git_dir.is_dir():
            config_path = git_dir / "config"
        elif git_dir.is_file():
            # Worktree: `.git` is a file containing `gitdir: <path>`.
            text = git_dir.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("gitdir:"):
                    target = Path(line[len("gitdir:"):].strip())
                    if not target.is_absolute():
                        target = (root / target).resolve()
                    config_path = target / "config"
                    break
    except OSError:
        return []
    if config_path is None or not config_path.is_file():
        return []
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    keys: List[str] = []
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            section = m.group(1).strip().lower()
            continue
        if "=" not in line:
            continue
        opt = line.split("=", 1)[0].strip().lower()
        # section like `diff "evil"` → key `diff.evil.textconv`
        sm = re.match(r"^([a-z0-9.-]+)\s+\"([^\"]+)\"$", section)
        if sm:
            full = f"{sm.group(1)}.{sm.group(2)}.{opt}"
        elif section:
            full = f"{section}.{opt}"
        else:
            continue
        if _git_key_is_exec_capable(full):
            keys.append(full)
    return keys


def git_neutralization_args(root: Optional[Path]) -> List[str]:
    """``-c`` flags that neutralize exec-capable git config keys and repo
    hooks for a git invocation rooted at *root* (may be None).

    These flags must be placed BEFORE the git subcommand
    (``git <flags> status``), which is exactly how git parses ``-c``.
    """
    flags = list(_GIT_NEUTRALIZE_FIXED)
    if root is not None:
        try:
            for key in _git_repo_exec_config_keys(root):
                flags += ["-c", f"{key}="]
        except Exception:
            pass  # fixed set still applies
    return flags


def _git_unquote_path(path: str) -> str:
    """Reverse git's C-style path quoting.

    - If `path` starts and ends with `"`, strip them and decode the
      octal escapes inside.
    - Otherwise return `path` unchanged.
    """
    if not path or len(path) < 2:
        return path
    if path[0] == '"' and path[-1] == '"':
        inner = path[1:-1].encode('utf-8')
        # Decode `\NNN` octal escapes back to raw bytes, then re-decode
        # the whole thing as UTF-8 (which is what git was quoting from).
        def _octal_sub(m):
            try:
                return int(m.group(1), 8).to_bytes(1, 'little')
            except (ValueError, OverflowError):
                return m.group(0)
        inner = _OCTAL_ESCAPE_RE.sub(_octal_sub, inner)
        # Also handle `\"` `\\` `\t` `\n` etc.
        try:
            return inner.decode('unicode_escape').encode('latin-1').decode('utf-8')
        except (UnicodeDecodeError, UnicodeEncodeError):
            try:
                return inner.decode('utf-8', errors='replace')
            except Exception:
                return path
    return path


@dataclass
class GitFileStatus:
    """One file's status in git."""
    path: str
    status: str       # "M" | "A" | "D" | "??" | "R" | "C"
    staged: bool      # True if change is in index
    old_path: str = ""  # for renames

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "staged": self.staged,
            "old_path": self.old_path,
        }


@dataclass
class GitCommitEntry:
    """One commit in the log."""
    hash: str
    short_hash: str
    author: str
    date: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hash": self.hash,
            "short_hash": self.short_hash,
            "author": self.author,
            "date": self.date,
            "message": self.message,
        }


@dataclass
class GitDiffHunk:
    """One hunk in a unified diff."""
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: List[Dict[str, Any]]  # [{"type": "context"|"add"|"remove", "content": str, "old_lineno": int|None, "new_lineno": int|None}]


@dataclass
class GitDiff:
    """Complete diff for a file or all files."""
    files: List[Dict[str, Any]]
    raw: str


class GitService:
    """
    Thin wrapper around the git CLI.
    All operations are relative to the project root.

    v1.0.5-hotfix: cache the "not a git repo" result per path so we
    don't spam the log with the same WARNING on every status check
    (BUGS_REPORT — the user's log showed 5+ identical "not a git repo"
    warnings because ``GitService(root=...)`` is re-created on every
    ``get_status()`` call).

    v1.1.5-fix (tera_pilot_bug_report.md bug #9): the class-level cache used
    to retain *both* positive and negative results forever. That meant
    a folder checked while it was NOT a git repo would stay "not a git
    repo" forever — even after the user ran ``git init`` — until the
    application was restarted. We now:

    * Give **negative** cache entries a TTL (default 30 s). A positive
      entry is still cached indefinitely for the lifetime of the
      process (a repo doesn't usually "un-become" a repo), but the
      negative entry is short-lived so ``git init`` is picked up on
      the next status poll.
    * Add a cheap pre-check: if ``<root>/.git`` exists on disk, we
      skip the cache entirely and re-run ``git rev-parse`` (so a
      freshly-initialised repo is detected immediately, without
      waiting for the TTL to expire).
    * Expose ``invalidate_cache(path=None)`` so callers (e.g. the
      agent's ``execute_command`` after running ``git init``) can
      force a re-check.
    """

    # Class-level cache: {resolved_path_str: (is_git_repo_bool, cached_at_ts)}
    # Prevents repeated `git rev-parse` calls for the same path.
    _git_repo_cache: Dict[str, Tuple[bool, float]] = {}

    # TTL for NEGATIVE cache entries, in seconds. Positive entries are
    # cached indefinitely (a folder doesn't usually stop being a repo).
    # 30 s is short enough that `git init` is picked up on the next UI
    # status poll (the code_viewer watcher fires on every directory
    # change, which is more frequent than that), but long enough that
    # we don't re-spawn `git rev-parse` on every keystroke-triggered
    # status refresh.
    NEGATIVE_CACHE_TTL_SECONDS = 30.0

    def __init__(self, root: Optional[str] = None, *, neutralize_exec: bool = True):
        self._root: Optional[Path] = None
        self._neutralize_exec = neutralize_exec
        if root:
            self.set_root(root)

    @classmethod
    def invalidate_cache(cls, path: Optional[str] = None) -> None:
        """v1.1.5 — drop one or all cached git-repo lookups.

        Useful when the caller *knows* the repo state changed (e.g.
        the agent just ran ``git init`` or ``git clone``) and wants
        the next ``set_root`` / ``status`` to re-check immediately
        instead of waiting for the negative-cache TTL.

        Parameters
        ----------
        path:
            If given, drop only the entry for this path (resolved
            absolute). If *None*, drop every cached entry — useful
            as a "reset" button.
        """
        if path is None:
            cls._git_repo_cache.clear()
            return
        try:
            key = str(Path(path).expanduser().resolve())
        except Exception:
            key = str(path)
        cls._git_repo_cache.pop(key, None)

    @classmethod
    def _cache_get(cls, path_str: str) -> Optional[bool]:
        """Return the cached bool for `path_str`, or *None* if not cached /
        expired. Expired NEGATIVE entries are evicted on read."""
        entry = cls._git_repo_cache.get(path_str)
        if entry is None:
            return None
        is_repo, cached_at = entry
        if is_repo:
            # Positive entries are cached indefinitely.
            return True
        # Negative entries expire after NEGATIVE_CACHE_TTL_SECONDS.
        if (time.time() - cached_at) > cls.NEGATIVE_CACHE_TTL_SECONDS:
            cls._git_repo_cache.pop(path_str, None)
            return None
        return False

    @classmethod
    def _cache_set(cls, path_str: str, is_repo: bool) -> None:
        cls._git_repo_cache[path_str] = (bool(is_repo), time.time())

    def set_root(self, root: str) -> bool:
        path = Path(root).expanduser().resolve()
        path_str = str(path)

        # v1.1.5-fix (bug #9): cheap disk pre-check. If `<root>/.git`
        # exists, this is almost certainly a git repo (or just became
        # one via `git init`). Bypass the cache so a freshly-initialised
        # repo is detected on the very next status check, instead of
        # being masked by a stale "not a repo" entry from before the
        # `git init`.
        git_dir = path / ".git"
        if git_dir.exists():
            # Don't trust the cache — re-run `git rev-parse` to confirm.
            result = self._run_git(path, ["rev-parse", "--is-inside-work-tree"])
            if result.returncode == 0:
                self._cache_set(path_str, True)
                self._root = path
                logger.info(f"[git] root = {self._root} (re-confirmed after .git appeared)")
                return True
            # .git exists but rev-parse failed — unusual (corrupted
            # repo?). Fall through to the normal cache logic so we
            # don't cache a misleading result.

        # v1.0.5-hotfix: check the cache first to avoid re-running
        # `git rev-parse` and re-logging the same warning.
        cached = self._cache_get(path_str)
        if cached is not None:
            if cached:
                self._root = path
                return True
            return False

        # Verify it's a git repo
        result = self._run_git(path, ["rev-parse", "--is-inside-work-tree"])
        if result.returncode != 0:
            # Cache the negative result with a TTL so a later `git init`
            # is picked up automatically (bug #9).
            self._cache_set(path_str, False)
            logger.debug(f"[git] not a git repo: {path}")  # debug, not warning
            return False
        # Cache the positive result.
        self._cache_set(path_str, True)
        self._root = path
        logger.info(f"[git] root = {self._root}")
        return True

    @property
    def root(self) -> Optional[Path]:
        return self._root

    @property
    def is_available(self) -> bool:
        return self._root is not None

    # ── Status ─────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        if not self._root:
            return {"branch": "", "files": [], "ahead": 0, "behind": 0}

        # Branch name
        branch_result = self._run_git(self._root, ["branch", "--show-current"])
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "HEAD"

        # Ahead/behind
        ahead, behind = 0, 0
        ab_result = self._run_git(self._root, ["rev-list", "--count", "--left-right", "@{upstream}...HEAD"],
                                   check=False)
        if ab_result.returncode == 0:
            parts = ab_result.stdout.strip().split("\t")
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])

        # File statuses
        files = self._parse_status()

        return {
            "branch": branch,
            "files": [f.to_dict() for f in files],
            "ahead": ahead,
            "behind": behind,
            "staged_count": sum(1 for f in files if f.staged),
            "unstaged_count": sum(1 for f in files if not f.staged),
            "total_changed": len(files),
        }

    def _parse_status(self) -> List[GitFileStatus]:
        # v1.0.5-security: use `-z` (NUL-separated) so paths containing
        # spaces, tabs, newlines, or non-ASCII bytes don't break parsing.
        # Also unquote git's default `core.quotePath=true` octal escapes
        # (e.g. `"r\303\251sum\303\251.txt"` → `résumé.txt`) so the
        # returned path matches what's actually on disk (BUGS_REPORT H-GIT-1).
        result = self._run_git(
            self._root,
            ["status", "--porcelain=v1", "-z", "--no-renames"],
        )
        if result.returncode != 0:
            return []
        files = []
        # `-z` separates records with NUL. Each record is "XY path\0"
        # (or "XY old_path\0new_path\0" for renames, but --no-renames
        # disables that, so we get a single path per record).
        records = result.stdout.split("\0")
        # The last element is an empty string after the final NUL — drop it.
        if records and records[-1] == "":
            records = records[:-1]
        i = 0
        while i < len(records):
            line = records[i]
            if len(line) < 4:
                i += 1
                continue
            x, y = line[0], line[1]
            # path starts after "XY " (3 chars)
            path = line[3:]
            # Unquote git's C-style escapes (e.g. "\"file with spaces.py\""
            # → "file with spaces.py"; "r\303\251sum\303\251.txt" → "résumé.txt").
            path = _git_unquote_path(path)
            old_path = ""

            status = y if y != " " else x
            staged = x != " " and x != "?"
            files.append(GitFileStatus(
                path=path, status=status, staged=staged, old_path=old_path,
            ))
            i += 1
        return files

    # ── Diff ───────────────────────────────────────────────────────

    def diff(self, *, staged: bool = False, file_path: Optional[str] = None) -> str:
        if not self._root:
            return ""
        args = ["diff"]
        if staged:
            args.append("--cached")
        if file_path:
            args.extend(["--", file_path])
        result = self._run_git(self._root, args, check=False)
        return result.stdout if result.returncode == 0 else ""

    def diff_parsed(self, *, staged: bool = False, file_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Parse diff into structured hunks for inline apply UI."""
        raw = self.diff(staged=staged, file_path=file_path)
        if not raw:
            return []
        return self._parse_diff(raw)

    def _parse_diff(self, raw: str) -> List[Dict[str, Any]]:
        """Parse unified diff into file-level hunks."""
        file_diffs = []
        current_file = None
        current_hunks = []
        current_lines = []
        # Initialise hunk-tracking state up front so every code path can
        # reference these names safely. Previously `current_hunk_header`
        # was only assigned inside the `@@` branch, which caused a
        # NameError if a `+`/`-` line appeared before the first hunk
        # header (rare but possible with malformed/synthetic diffs).
        current_hunk_header = ""
        old_ln = 0
        new_ln = 0

        for line in raw.split("\n"):
            # File header
            m = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
            if m:
                # Flush any in-flight hunk for the previous file
                if current_file is not None and current_lines:
                    current_hunks.append({
                        "header": current_hunk_header,
                        "lines": current_lines,
                    })
                    current_lines = []
                if current_file and current_hunks:
                    file_diffs.append({"file": current_file, "hunks": current_hunks})
                current_file = m.group(2)
                current_hunks = []
                current_lines = []
                current_hunk_header = ""
                continue

            # Hunk header
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                if current_lines:
                    # Finish previous hunk
                    current_hunks.append({
                        "header": current_hunk_header,
                        "lines": current_lines,
                    })
                    current_lines = []
                old_start = int(m.group(1))
                new_start = int(m.group(3))
                current_hunk_header = line
                old_ln, new_ln = old_start, new_start
                continue

            if current_file is not None:
                if line.startswith("+") and not line.startswith("+++"):
                    current_lines.append({"type": "add", "content": line[1:], "new_lineno": new_ln})
                    new_ln += 1
                elif line.startswith("-") and not line.startswith("---"):
                    current_lines.append({"type": "remove", "content": line[1:], "old_lineno": old_ln})
                    old_ln += 1
                elif line.startswith(" "):
                    current_lines.append({"type": "context", "content": line[1:], "old_lineno": old_ln, "new_lineno": new_ln})
                    old_ln += 1
                    new_ln += 1

        # Flush last hunk
        if current_lines:
            current_hunks.append({
                "header": current_hunk_header,
                "lines": current_lines,
            })
        if current_file and current_hunks:
            file_diffs.append({"file": current_file, "hunks": current_hunks})

        return file_diffs

    # ── Commit ─────────────────────────────────────────────────────

    def commit(self, message: str, *, files: Optional[List[str]] = None) -> Dict[str, Any]:
        """Stage files (if provided) and commit. Returns {ok, hash, error}."""
        if not self._root:
            return {"ok": False, "error": "No git repo"}
        if files:
            self.stage(files)

        result = self._run_git(self._root, ["commit", "-m", message])
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()}

        # Get the commit hash
        hash_result = self._run_git(self._root, ["rev-parse", "HEAD"])
        short_hash = hash_result.stdout.strip()[:8] if hash_result.returncode == 0 else "?"

        return {"ok": True, "hash": short_hash, "message": message}

    # ── Stage / Unstage ────────────────────────────────────────────

    def stage(self, paths: List[str]) -> bool:
        if not self._root:
            return False
        result = self._run_git(self._root, ["add", "--"] + paths)
        return result.returncode == 0

    def unstage(self, paths: List[str]) -> bool:
        if not self._root:
            return False
        result = self._run_git(self._root, ["reset", "HEAD", "--"] + paths)
        return result.returncode == 0

    def stage_all(self) -> bool:
        if not self._root:
            return False
        result = self._run_git(self._root, ["add", "-A"])
        return result.returncode == 0

    # ── Log ────────────────────────────────────────────────────────

    def log(self, n: int = 20) -> List[Dict[str, Any]]:
        if not self._root:
            return []
        # Format: hash|short|author|date|subject
        fmt = "%H|%h|%an|%ai|%s"
        result = self._run_git(self._root, ["log", f"-{n}", f"--format={fmt}"])
        if result.returncode != 0:
            return []

        commits = []
        for line in result.stdout.strip().split("\n"):
            if "|" not in line:
                continue
            parts = line.split("|", 4)
            if len(parts) < 5:
                continue
            commits.append(GitCommitEntry(
                hash=parts[0], short_hash=parts[1], author=parts[2],
                date=parts[3], message=parts[4],
            ).to_dict())
        return commits

    # ── Branch ─────────────────────────────────────────────────────

    def branch(self) -> Dict[str, Any]:
        if not self._root:
            return {"current": "", "branches": []}
        result = self._run_git(self._root, ["branch", "-a"])
        if result.returncode != 0:
            return {"current": "", "branches": []}

        current = ""
        branches = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("* "):
                current = line[2:]
                branches.append({"name": current, "current": True})
            else:
                name = line.lstrip("* ")
                branches.append({"name": name, "current": False})

        return {"current": current, "branches": branches}

    # ── Generate commit message (helper for AI) ────────────────────

    def staged_diff_for_ai(self) -> str:
        """Return staged diff text suitable for feeding to an LLM for commit message generation."""
        return self.diff(staged=True)

    # ── Internal ───────────────────────────────────────────────────

    def _run_git(self, cwd: Path, args: List[str], *, check: bool = True) -> subprocess.CompletedProcess:
        try:
            cmd: List[str] = ["git"]
            # v2.3.4-security: neutralize repo-supplied exec config keys and
            # hooks for every git invocation (agent tools AND GUI status).
            # A malicious .git/config (core.fsmonitor on status, diff.*.textconv
            # on diff) or .git/hooks/* (pre-commit on commit) would otherwise
            # execute arbitrary commands. See git_neutralization_args().
            if self._neutralize_exec:
                cmd += git_neutralization_args(cwd)
            cmd += args
            return subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            logger.warning("[git] git binary not found")
            return subprocess.CompletedProcess("git", 1, "", "git not found")
        except subprocess.TimeoutExpired:
            logger.warning(f"[git] command timed out: {args}")
            return subprocess.CompletedProcess("git", 1, "", "git command timed out")