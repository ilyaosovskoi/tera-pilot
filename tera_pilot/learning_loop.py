"""
Automatic Learning Loop (G17) — wire automation into the existing
loop-engineering process.

Builds on the *existing* project methodology described in
``Loop_Engineering_Guide.md``, ``Loops_Library.md``, and
``Learnings.md`` — this is NOT a code-only feature. It automates a
process that already exists as markdown convention.

Two trigger classes are detected:

1. **Git-based rollback signals**:
   - ``git reset --hard`` (HEAD moves backwards).
   - Force-pushes that discard commits (``git push --force`` /
     ``--force-with-lease`` where the remote HEAD moves backwards).
   - Reverted commits (``git revert <sha>`` creates a new commit that
     undoes a previous one).
   - Branches abandoned after N commits (a branch with N+ commits that
     hasn't been touched in D days, with no upstream merge).

2. **CI failure signals**:
   - Parse failed test output. We reuse the existing test-runner
     detection logic from ``ToolEngine._detect_project_command`` /
     ``_TEST_COMMAND_DETECTORS`` (see
     ``agent_runtime/tool_engine/_engine.py``) — no new detection
     scheme is invented here. The user runs their tests, we parse
     the failure summary.

On trigger, we auto-create a ``learnings/<date>-<slug>.md`` entry
following the EXACT structure already used in ``Learnings.md`` —
the entry template is read from the existing
``Learnings.md`` ``## Entry Template`` section so we never drift
from the project's convention.

Relevant learnings are injected into the system prompt **per
repository** (scoped by project path, not global) when a new agent
turn starts on that project. We reuse the existing
``agent/context_fragments.py`` infrastructure so injected learnings
participate in the same tombstone-compaction as everything else —
they don't permanently bloat every prompt.

Slash command ``/learnings`` makes the loop observable: list, show,
and dismiss auto-generated entries so the loop doesn't silently
pollute future prompts with something the user disagrees with.

Zero-telemetry: nothing here phones home. The learnings live on the
user's disk under ``<project>/learnings/`` (or ``~/.tera_pilot/learnings/``
for global fallback). Git history is read locally. Test output is
parsed from local runs.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Paths ──────────────────────────────────────────────────────────────
# Per-repo learnings live under <project>/learnings/ — the same
# convention Learnings.md documents. If the project dir isn't writable
# (e.g. read-only mount), we fall back to ~/.tera_pilot/learnings/<project_hash>/
# so the feature still works without forcing the user to chmod anything.

_LEARNINGS_DIR_NAME = "learnings"
_DISMISSED_FILE = ".dismissed.json"  # inside learnings dir
_GLOBAL_FALLBACK_DIR = Path.home() / ".tera_pilot" / "learnings"

# How many learnings to inject into a single system prompt. Capped so
# we don't blow the context window with stale entries.
_MAX_INJECTED = 5
# Max chars per injected learning body.
_MAX_LEARNING_CHARS = 1500


def _project_learnings_dir(project_path: str) -> Path:
    """Return the learnings directory for ``project_path``.

    Falls back to ``~/.tera_pilot/learnings/<hash>/`` if the project dir
    isn't writable (so the feature degrades gracefully on read-only
    mounts).
    """
    project = Path(project_path).resolve()
    candidate = project / _LEARNINGS_DIR_NAME
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        # Touch a sentinel file to confirm write access.
        sentinel = candidate / ".tera_pilot_writable"
        if not sentinel.exists():
            sentinel.write_text("ok", encoding="utf-8")
        return candidate
    except Exception:
        # Fallback: hash the project path so each project gets its own
        # namespace under ~/.tera_pilot/learnings/.
        h = hashlib.sha1(str(project).encode("utf-8")).hexdigest()[:12]
        fallback = _GLOBAL_FALLBACK_DIR / h
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


# ── Slug + filename helpers ────────────────────────────────────────────


def _slugify(text: str, max_len: int = 50) -> str:
    """Convert arbitrary text to a URL-safe slug (matching the
    Learnings.md convention: lowercase, hyphen-separated)."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not s:
        s = "untitled"
    return s[:max_len]


def _today_str() -> str:
    return datetime.date.today().isoformat()


def _learning_filename(date_str: str, slug: str) -> str:
    return f"{date_str}-{slug}.md"


# ── Entry template (read from Learnings.md so we never drift) ──────────

# Fallback template — used only if Learnings.md can't be read for any
# reason. Mirrors the structure documented in Learnings.md.
_FALLBACK_TEMPLATE = """---
id: LEARN-{id}
date: {date}
tags: [{tags}]
source: {source}
severity: {severity}
status: tentative
---

# Title: {title}

## Context
{context}

## What Happened
{what_happened}

## Root Cause / Insight
{root_cause}

## Evidence
{evidence}

## Actionable Rule
**DO**: {do}
**DON'T**: {dont}

## How to Apply Next Time
{how_to_apply}

## Related Learnings
- (none yet)
"""


def _extract_template_from_learnings_md(learnings_md_path: Path) -> Optional[str]:
    """Read Learnings.md and pull out the entry template block.

    The template is the fenced code block under the ``## Entry Template``
    heading. Returns None if not found.
    """
    try:
        text = learnings_md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    # Find the "## Entry Template" heading and the next fenced block.
    m = re.search(
        r"##\s*Entry Template\s*\n+(?:.*?\n)*?```markdown\n(.*?)\n```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # Looser: any fenced block right after the heading.
    m2 = re.search(
        r"##\s*Entry Template\s*\n+(?:.*?\n)*?```\n(.*?)\n```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m2:
        return m2.group(1)
    return None


# ── Trigger detection ──────────────────────────────────────────────────


@dataclass
class GitRollbackSignal:
    """One detected git rollback event."""
    kind: str  # "reset_hard" | "force_push" | "revert" | "abandoned_branch"
    sha: str = ""
    branch: str = ""
    description: str = ""
    raw_output: str = ""


@dataclass
class CIFailureSignal:
    """One detected CI/test failure event."""
    test_cmd: str
    exit_code: int
    failed_tests: List[str] = field(default_factory=list)
    error_summary: str = ""
    raw_output: str = ""


@dataclass
class LearningEntry:
    """A learning written to disk."""
    path: str
    title: str
    date: str
    source: str
    tags: List[str]
    severity: str
    body: str
    dismissed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "date": self.date,
            "source": self.source,
            "tags": self.tags,
            "severity": self.severity,
            "body_chars": len(self.body),
            "body_preview": self.body[:240],
            "dismissed": self.dismissed,
        }


# ── Git rollback detection ─────────────────────────────────────────────


def _run_git(project_path: str, args: List[str], timeout: float = 5.0) -> Tuple[int, str, str]:
    """Run git inside ``project_path`` with shell=False. Returns
    (exit_code, stdout, stderr). Never raises."""
    try:
        proc = subprocess.Popen(
            ["git"] + args,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_path,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def detect_git_rollbacks(project_path: str, since_hours: int = 24) -> List[GitRollbackSignal]:
    """Detect rollback events in the recent git history.

    We check the reflog for ``git reset --hard`` and force-pushes, the
    commit log for ``git revert`` (commits whose message starts with
    ``Revert "..."``), and branches that haven't been touched in a
    long time after accumulating N+ commits.

    This is best-effort — git reflog is local and may be pruned, and
    we don't try to detect every exotic form of history rewriting.
    The goal is to surface the COMMON cases where the user clearly
    threw away work, which is the signal that "we learned something
    here" applies.
    """
    out: List[GitRollbackSignal] = []
    if not Path(project_path).exists():
        return out

    # 1. reflog for "reset moving to" (reset --hard) and "update" with
    #    force-push (the reflog entry for a force-push is typically
    #    "update by push" with HEAD moving backwards).
    rc, stdout, _ = _run_git(project_path, ["reflog", "--since", f"{since_hours} hours ago", "--pretty=%H|%gs"])
    if rc == 0:
        for line in stdout.splitlines():
            if "|" not in line:
                continue
            sha, action = line.split("|", 1)
            action_lower = action.lower()
            if "reset moving to" in action_lower:
                out.append(GitRollbackSignal(
                    kind="reset_hard",
                    sha=sha[:12],
                    description=f"git reset --hard moved HEAD to {sha[:8]}",
                    raw_output=action,
                ))
            elif "forced" in action_lower or "force" in action_lower:
                out.append(GitRollbackSignal(
                    kind="force_push",
                    sha=sha[:12],
                    description=f"force-push detected: {action[:80]}",
                    raw_output=action,
                ))

    # 2. Recent commits whose message starts with "Revert ..." — these
    #    are created by `git revert <sha>`.
    rc, stdout, _ = _run_git(project_path, ["log", f"--since={since_hours} hours ago", "--pretty=%H|%s", "--grep=^Revert ", "--all"])
    if rc == 0:
        for line in stdout.splitlines():
            if "|" not in line:
                continue
            sha, subject = line.split("|", 1)
            out.append(GitRollbackSignal(
                kind="revert",
                sha=sha[:12],
                description=f"git revert: {subject[:100]}",
                raw_output=subject,
            ))

    # 3. Abandoned branches — branches with N+ commits that haven't
    #    been touched in D days. Default: 5+ commits, untouched 14+ days.
    rc, stdout, _ = _run_git(project_path, ["for-each-ref", "refs/heads/", "--format=%(refname:short)|%(committerdate:unix)|%(objectname:short)|%02d"])
    if rc == 0:
        now = datetime.datetime.now().timestamp()
        for line in stdout.splitlines():
            parts = line.split("|")
            if len(parts) < 4:
                continue
            branch, ts_str, sha, count_str = parts[0], parts[1], parts[2], parts[3]
            try:
                ts = float(ts_str)
                count = int(count_str)
            except ValueError:
                continue
            age_days = (now - ts) / 86400
            if count >= 5 and age_days >= 14:
                out.append(GitRollbackSignal(
                    kind="abandoned_branch",
                    sha=sha,
                    branch=branch,
                    description=f"branch '{branch}' has {count} commits, untouched {age_days:.0f} days",
                    raw_output=line,
                ))
    return out


# ── CI failure parsing ─────────────────────────────────────────────────
# Reuses ToolEngine._detect_project_command for test-command detection,
# as the spec demands (don't reinvent test-command detection).

def _detect_test_command(project_path: str) -> Tuple[Optional[str], str]:
    """Detect the project's test command by consulting the SAME
    detector list that ToolEngine uses for self_verify.

    Returns (command, description) or (None, ""). We inline the
    detector tuples here (rather than importing the private
    ToolEngine._TEST_COMMAND_DETECTORS) so the learning loop works
    even when the ToolEngine isn't instantiated yet (e.g. at agent
    startup, before the runtime is fully wired).
    """
    # Mirrors ToolEngine._TEST_COMMAND_DETECTORS — kept in sync
    # manually. If ToolEngine adds a new detector, this list should
    # grow too; the spec's intent is "don't reinvent test-command
    # detection", which we honour by mirroring the existing logic.
    detectors: Tuple[Tuple[str, str, str], ...] = (
        ("pytest.ini", "pytest", "pytest.ini found"),
        ("pyproject.toml", "pytest", "pyproject.toml [tool.pytest] found"),
        ("setup.cfg", "pytest", "setup.cfg [tool:pytest] found"),
        ("tox.ini", "pytest", "tox.ini found"),
        ("package.json", "npm test", "package.json found"),
        ("Cargo.toml", "cargo test", "Cargo.toml found"),
        ("go.mod", "go test ./...", "go.mod found"),
        ("Makefile", "make test", "Makefile found"),
    )
    for marker, command, desc in detectors:
        if (Path(project_path) / marker).exists():
            return command, desc
    return None, ""


def parse_test_failures(test_output: str, exit_code: int) -> List[str]:
    """Extract the names of failed tests from test runner output.

    Supports pytest's "FAILED test_file.py::test_name - reason" format
    and the more verbose "_____ test_name _____" format. Returns a
    list of test identifiers (capped at 20 to avoid blowing the
    learning entry with hundreds of failures).
    """
    if exit_code == 0:
        return []
    failed: List[str] = []
    seen: set = set()
    # pytest short summary line: "FAILED test_file.py::test_name - reason"
    for m in re.finditer(r"^FAILED\s+(\S+?)(?:\s+-|$)", test_output, re.MULTILINE):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            failed.append(name)
        if len(failed) >= 20:
            break
    # If no short-summary matches, try the verbose format:
    # "_____ TestClass.test_method _____"
    if not failed:
        for m in re.finditer(r"^_{5,}\s+(\S+)\s+_{5,}", test_output, re.MULTILINE):
            name = m.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                failed.append(name)
            if len(failed) >= 20:
                break
    return failed


def detect_ci_failure(project_path: str) -> Optional[CIFailureSignal]:
    """Run the project's test command and return a CIFailureSignal if
    it fails. Returns None if no test command is detected OR the
    tests pass.

    We deliberately run the tests here (rather than parsing a log file
    the user pasted) because:
    1. The spec says "parse failed test output (reuse whatever
       test-runner integration exists)" — ToolEngine._self_verify_run_tests
       ALSO runs the tests, so this is the same pattern.
    2. A log file would require the user to manually save+point us
       at it, which defeats the "automatic" goal.

    The test command is run with a 60s timeout (same as
    ToolEngine._run_test_command_sandboxed) and shell=False (the
    command comes from our hardcoded list, never from user input).
    """
    cmd, desc = _detect_test_command(project_path)
    if cmd is None:
        return None
    try:
        proc = subprocess.Popen(
            cmd.split() if isinstance(cmd, str) else cmd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_path,
        )
        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return CIFailureSignal(
                test_cmd=cmd,
                exit_code=124,
                failed_tests=[],
                error_summary="test command timed out after 60s",
                raw_output="",
            )
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        if proc.returncode == 0:
            return None
        combined = out_text + "\n" + err_text
        failed = parse_test_failures(combined, proc.returncode)
        summary = err_text[-500:] if err_text else out_text[-500:]
        return CIFailureSignal(
            test_cmd=cmd,
            exit_code=proc.returncode,
            failed_tests=failed,
            error_summary=summary,
            raw_output=combined[-3000:],
        )
    except Exception as e:
        return CIFailureSignal(
            test_cmd=cmd,
            exit_code=1,
            failed_tests=[],
            error_summary=f"failed to run test command: {e}",
            raw_output="",
        )


# ── Learning entry creation ────────────────────────────────────────────


def _next_learning_id(learnings_dir: Path, date_str: str) -> str:
    """Pick the next LEARN-YYYYMMDD-XXX id that isn't taken."""
    # Scan existing files for IDs on this date.
    existing: set = set()
    for f in learnings_dir.glob(f"*-{date_str}-*.md"):
        # Filenames look like "LOOP-...-LEARN-20260731-001.md" or just
        # "2026-07-31-slug.md". We only care about LEARN- IDs in body.
        pass
    # Simpler: just pick 001, 002, ... based on count of files today.
    today_files = list(learnings_dir.glob(f"{date_str}-*.md"))
    return f"LEARN-{date_str.replace('-', '')}-{len(today_files) + 1:03d}"


def create_learning_entry(
    *,
    project_path: str,
    title: str,
    context: str,
    what_happened: str,
    root_cause: str,
    evidence: str,
    do_rule: str,
    dont_rule: str,
    how_to_apply: str,
    source: str = "AUTO",
    tags: Optional[List[str]] = None,
    severity: str = "medium",
    slug: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a single ``learnings/<date>-<slug>.md`` file.

    The file follows the EXACT structure documented in Learnings.md's
    ``## Entry Template`` section — we read that template at runtime
    so we never drift from the project's convention.

    Returns ``{ok, path, id}`` or ``{ok: False, error}``.
    """
    learnings_dir = _project_learnings_dir(project_path)
    date_str = _today_str()
    # Try to read the template from Learnings.md (project root).
    template = _extract_template_from_learnings_md(Path(project_path) / "Learnings.md")
    if template is None:
        # Fall back to repo-level Learnings.md (no project root given).
        template = _FALLBACK_TEMPLATE
    learn_id = _next_learning_id(learnings_dir, date_str)
    tags_str = ", ".join(tags or ["auto", "process"])
    # Fill in the template. We use simple {field} placeholders so we
    # don't need a templating engine.
    body = template.format(
        id=learn_id.split("-", 1)[-1] if "-" in learn_id else learn_id,
        date=date_str,
        tags=tags_str,
        source=source,
        severity=severity,
        title=title,
        context=context,
        what_happened=what_happened,
        root_cause=root_cause,
        evidence=evidence,
        do=do_rule,
        dont=dont_rule,
        how_to_apply=how_to_apply,
    )
    if slug is None:
        slug = _slugify(title)
    filename = _learning_filename(date_str, slug)
    out_path = learnings_dir / filename
    # Avoid clobbering an existing entry with the same slug — append
    # a counter if needed.
    counter = 1
    while out_path.exists():
        out_path = learnings_dir / f"{date_str}-{slug}-{counter}.md"
        counter += 1
    try:
        out_path.write_text(body, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"failed to write learning file: {e}"}
    return {"ok": True, "path": str(out_path), "id": learn_id}


# ── Trigger → learning entry glue ──────────────────────────────────────


def create_learning_from_git_rollback(
    project_path: str, signal: GitRollbackSignal,
) -> Dict[str, Any]:
    """Auto-create a learning entry from a git rollback signal.

    The entry is marked ``source: AUTO-GIT-ROLLBACK`` and
    ``status: tentative`` — the user reviews it via ``/learnings``
    and can dismiss or promote it.
    """
    kind_label = {
        "reset_hard": "git reset --hard",
        "force_push": "force-push discarding commits",
        "revert": "git revert",
        "abandoned_branch": "abandoned branch with commits",
    }.get(signal.kind, signal.kind)
    return create_learning_entry(
        project_path=project_path,
        title=f"Rollback detected: {kind_label}",
        context=(
            f"A rollback signal was detected in the git history of "
            f"{project_path}. This usually means work was discarded — "
            f"the agent should understand WHY before repeating the "
            f"same approach on this project."
        ),
        what_happened=signal.description,
        root_cause=(
            f"Git history shows a {kind_label} event. The most common "
            f"causes are: (1) the previous approach introduced a bug "
            f"or regression, (2) the direction was wrong, (3) a merge "
            f"conflict was resolved by discarding one side. Without "
            f"more context the agent should ASK the user before "
            f"re-attempting the same strategy."
        ),
        evidence=(
            f"git signal: {signal.kind}\n"
            f"sha: {signal.sha}\n"
            f"branch: {signal.branch or '(current)'}\n"
            f"raw: {signal.raw_output[:200]}"
        ),
        do_rule=(
            "Before re-attempting the discarded approach, ask the user "
            "whether the rollback was intentional and what went wrong."
        ),
        dont_rule=(
            "Don't blindly redo the discarded work — the user spent "
            "time reverting it for a reason."
        ),
        how_to_apply=(
            "1. Detect this learning at agent turn start (it's injected "
            "automatically).\n"
            "2. If the current task looks similar to the discarded work, "
            "surface the rollback in the response before proceeding.\n"
            "3. Wait for user confirmation before redoing the approach."
        ),
        source="AUTO-GIT-ROLLBACK",
        tags=["auto", "git", "rollback", signal.kind],
        severity="medium" if signal.kind != "abandoned_branch" else "low",
        slug=f"rollback-{signal.kind}-{signal.sha[:6]}",
    )


def create_learning_from_ci_failure(
    project_path: str, signal: CIFailureSignal,
) -> Dict[str, Any]:
    """Auto-create a learning entry from a CI failure signal."""
    failed_list = "\n".join(f"  - {t}" for t in signal.failed_tests[:10]) or "  (no specific test names parsed)"
    return create_learning_entry(
        project_path=project_path,
        title=f"CI failure: {signal.test_cmd} exited {signal.exit_code}",
        context=(
            f"The project's test command ({signal.test_cmd}) failed "
            f"with exit code {signal.exit_code}. This is a signal that "
            f"a recent change broke something — the agent should "
            f"understand which test(s) failed and why before "
            f"continuing on this project."
        ),
        what_happened=(
            f"Test command: {signal.test_cmd}\n"
            f"Exit code: {signal.exit_code}\n"
            f"Failed tests:\n{failed_list}"
        ),
        root_cause=(
            "The test failure indicates the previous change introduced "
            "a regression. The agent should NOT assume the test itself "
            "is broken — start by assuming the code change is wrong "
            "and verify against the test's expectation."
        ),
        evidence=(
            f"command: {signal.test_cmd}\n"
            f"exit_code: {signal.exit_code}\n"
            f"failed_tests: {', '.join(signal.failed_tests[:5])}\n"
            f"summary:\n{signal.error_summary[:500]}"
        ),
        do_rule=(
            "When working in this project, run the test command BEFORE "
            "claiming a task is complete. If these specific tests "
            "fail, fix them before proceeding."
        ),
        dont_rule=(
            "Don't disable or skip the failing tests to make the "
            "build green — that hides the regression."
        ),
        how_to_apply=(
            "1. The failing test names are listed above.\n"
            "2. When the agent starts a new task on this project, this "
            "learning is injected into the system prompt.\n"
            "3. The agent should mention the failure if the new task "
            "touches the same code paths."
        ),
        source="AUTO-CI-FAILURE",
        tags=["auto", "ci", "tests", "regression"],
        severity="high",
        slug=f"ci-failure-{_today_str()}",
    )


# ── Missing verification detection (Loop 2) ───────────────────────────


@dataclass
class MissingVerificationSignal:
    """Detected pattern where a task completion lacked self_verify
    and was followed by a rollback or CI failure."""
    task_description: str = ""
    tools_used: List[str] = field(default_factory=list)
    had_self_verify: bool = False
    had_rollback: bool = False
    had_ci_failure: bool = False
    description: str = ""


def detect_missing_verification(project_path: str) -> Optional[MissingVerificationSignal]:
    """Check if recent task completion lacked self_verify call.

    Uses the activity log to find the last task completion event,
    then checks for self_verify tool calls in the same conversation
    window. If missing AND a rollback/CI-failure follows, triggers
    a learning entry.

    This is a best-effort detection — it relies on the activity log
    being present and populated. If the activity log is unavailable,
    it silently returns None.

    v2.1.0 (Loop 2 fix): the previous implementation called
    ``ActivityLog.query(limit=..., project_path=...)``, a method that
    does not exist on ``ActivityLog`` (real API: ``recent()``, ``get()``,
    ``stats()``). Because the call was wrapped in a broad ``except
    Exception``, this silently returned ``None`` on every invocation,
    so the "G17 detects missing verification" hook never actually
    fired. Fixed to use ``ActivityLog.recent()``. ``ActivityLog`` has
    no built-in project scoping, so ``project_path`` is applied as a
    best-effort filter on each entry's recorded ``path`` (entries with
    no path — e.g. ``self_verify`` itself — are always kept so they
    aren't dropped from the window).
    """
    try:
        from tera_pilot.activity_log import get_activity_log
        log = get_activity_log()
        all_entries = log.recent(n=200)
        entries = [
            e for e in all_entries
            if not e.get("path") or str(e.get("path", "")).startswith(str(project_path))
        ][-50:]
    except Exception:
        return None

    if not entries:
        return None

    # Walk the recent window and look for a self_verify call, plus any
    # sign of a rollback or CI failure among the entries in it. (There
    # is no "final_answer" / "done" activity entry to anchor on --
    # final_answer is parsed by OutputParser, not dispatched through
    # record_tool_call -- so task_description is best-effort from the
    # most recent entry's title instead.)
    had_self_verify = False
    had_rollback = False
    had_ci_failure = False
    tools_used: List[str] = []
    task_description = ""

    for entry in entries:
        entry_data = entry if isinstance(entry, dict) else {}
        tool_name = entry_data.get("tool", "") or entry_data.get("kind", "")

        if tool_name == "self_verify":
            had_self_verify = True
        if tool_name:
            tools_used.append(tool_name)
        title = entry_data.get("title", "")
        if title:
            task_description = title
        haystack = " ".join(
            str(entry_data.get(k, "") or "")
            for k in ("summary", "result_preview", "command")
        ).lower()
        if "rollback" in haystack or "git reset --hard" in haystack or "revert" in haystack:
            had_rollback = True
        if "ci_failure" in haystack or "test failed" in haystack or "tests failed" in haystack:
            had_ci_failure = True


    # Only trigger if: no self_verify AND (rollback OR CI failure)
    # This avoids false positives for tasks that completed successfully
    # without verification (which is fine for read-only tasks).
    if not had_self_verify and (had_rollback or had_ci_failure):
        return MissingVerificationSignal(
            task_description=task_description,
            tools_used=tools_used,
            had_self_verify=had_self_verify,
            had_rollback=had_rollback,
            had_ci_failure=had_ci_failure,
            description=(
                f"Task completed without self_verify, followed by "
                f"{'rollback' if had_rollback else 'CI failure'}. "
                f"Tools used: {', '.join(tools_used[:10]) or 'unknown'}"
            ),
        )
    return None


def create_learning_from_missing_verification(
    project_path: str, signal: MissingVerificationSignal,
) -> Dict[str, Any]:
    """Auto-create a learning entry from a missing verification signal."""
    return create_learning_entry(
        project_path=project_path,
        title="Task completed without self_verify, followed by failure",
        context=(
            "A task was marked as done without calling self_verify, "
            "and was subsequently followed by a rollback or CI failure. "
            "This suggests the agent should have verified its work "
            "before reporting completion."
        ),
        what_happened=signal.description,
        root_cause=(
            "The agent completed the task without verifying the result. "
            "Without self_verify, the agent cannot catch issues like "
            "incorrect edits, missing files, or broken tests before "
            "reporting success."
        ),
        evidence=(
            f"had_self_verify: {signal.had_self_verify}\n"
            f"had_rollback: {signal.had_rollback}\n"
            f"had_ci_failure: {signal.had_ci_failure}\n"
            f"tools_used: {', '.join(signal.tools_used[:10])}"
        ),
        do_rule=(
            "Always call self_verify before reporting task completion "
            "when the task involved writing or editing files."
        ),
        dont_rule=(
            "Don't report task completion without verification when "
            "files were modified — the changes may be incorrect."
        ),
        how_to_apply=(
            "1. When the agent writes or edits files, call self_verify "
            "before final_answer.\n"
            "2. If self_verify reveals issues, fix them before "
            "reporting completion.\n"
            "3. For read-only tasks, self_verify is optional."
        ),
        source="AUTO-MISSING-VERIFICATION",
        tags=["auto", "verification", "quality"],
        severity="medium",
        slug=f"missing-verification-{_today_str()}",
    )


# ── Scan + create on trigger ───────────────────────────────────────────


def scan_and_create_learnings(project_path: str) -> List[Dict[str, Any]]:
    """Run both trigger detectors and create learning entries for any
    new signals.

    Returns a list of ``{ok, path, id, source}`` dicts — one per
    learning created. Empty list if no triggers fired.

    This is the main entry point for the "automatic" part of the
    loop. It's safe to call repeatedly — duplicate detection is
    handled by filename collision (same slug on the same day
    appends a counter).
    """
    out: List[Dict[str, Any]] = []
    # Git rollbacks.
    for signal in detect_git_rollbacks(project_path):
        result = create_learning_from_git_rollback(project_path, signal)
        if result.get("ok"):
            result["source"] = "git_rollback"
            out.append(result)
    # CI failures.
    ci = detect_ci_failure(project_path)
    if ci is not None:
        result = create_learning_from_ci_failure(project_path, ci)
        if result.get("ok"):
            result["source"] = "ci_failure"
            out.append(result)
    # v2.1.0 (Loop 2): missing verification detection.
    mv = detect_missing_verification(project_path)
    if mv is not None:
        result = create_learning_from_missing_verification(project_path, mv)
        if result.get("ok"):
            result["source"] = "missing_verification"
            out.append(result)
    return out


# ── Loading + injection ────────────────────────────────────────────────


def _load_dismissed(learnings_dir: Path) -> set:
    """Load the set of dismissed learning file paths."""
    p = learnings_dir / _DISMISSED_FILE
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_dismissed(learnings_dir: Path, dismissed: set) -> None:
    p = learnings_dir / _DISMISSED_FILE
    try:
        p.write_text(json.dumps(sorted(dismissed), indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("[learning-loop] failed to save dismissed list: %s", e)


def list_learnings(project_path: str, include_body: bool = False) -> List[LearningEntry]:
    """List all learning entries for a project.

    Sorted by date descending (newest first). ``Dismissed`` entries
    are included but flagged — the caller can filter them out.
    """
    learnings_dir = _project_learnings_dir(project_path)
    dismissed = _load_dismissed(learnings_dir)
    out: List[LearningEntry] = []
    for f in sorted(learnings_dir.glob("*.md"), reverse=True):
        if f.name == _DISMISSED_FILE:
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Parse frontmatter (same minimal parser as skill_loader).
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", body, re.DOTALL)
        meta: Dict[str, str] = {}
        body_text = body
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip().lower()] = v.strip().strip('"').strip("'")
            body_text = m.group(2)
        title_match = re.search(r"^#\s+Title:\s*(.+)$", body_text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else f.stem
        out.append(LearningEntry(
            path=str(f),
            title=title,
            date=meta.get("date", ""),
            source=meta.get("source", ""),
            tags=[t.strip() for t in meta.get("tags", "").strip("[]").split(",") if t.strip()],
            severity=meta.get("severity", "medium"),
            body=body_text if include_body else "",
            dismissed=str(f) in dismissed or f.name in dismissed,
        ))
    return out


def dismiss_learning(project_path: str, identifier: str) -> Dict[str, Any]:
    """Dismiss a learning entry so it stops being injected into prompts.

    ``identifier`` can be a filename, a full path, or a date-slug prefix.
    Dismissal is reversible — the file is NOT deleted, just flagged in
    ``.dismissed.json`` next to the learnings.
    """
    learnings_dir = _project_learnings_dir(project_path)
    dismissed = _load_dismissed(learnings_dir)
    # Try to resolve the identifier to an actual file.
    target: Optional[Path] = None
    candidates = [
        learnings_dir / identifier,
        learnings_dir / f"{identifier}.md",
    ]
    for c in candidates:
        if c.exists():
            target = c
            break
    if target is None:
        # Loose match: any file containing the identifier in its name.
        matches = list(learnings_dir.glob(f"*{identifier}*.md"))
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            return {"ok": False, "error": f"ambiguous identifier {identifier!r}: {len(matches)} matches"}
    if target is None:
        return {"ok": False, "error": f"no learning entry matches {identifier!r}"}
    dismissed.add(target.name)
    _save_dismissed(learnings_dir, dismissed)
    return {"ok": True, "path": str(target)}


def restore_learning(project_path: str, identifier: str) -> Dict[str, Any]:
    """Un-dismiss a previously dismissed learning entry."""
    learnings_dir = _project_learnings_dir(project_path)
    dismissed = _load_dismissed(learnings_dir)
    removed: List[str] = []
    for name in list(dismissed):
        if identifier in name:
            dismissed.discard(name)
            removed.append(name)
    if not removed:
        return {"ok": False, "error": f"no dismissed entry matches {identifier!r}"}
    _save_dismissed(learnings_dir, dismissed)
    return {"ok": True, "restored": removed}


def build_learnings_fragment(project_path: str) -> str:
    """Build the system-prompt fragment that injects relevant learnings.

    Returns a ``<context_fragment type="project_learnings" id="<path>">``
    block (using the existing context_fragments.py format) so the
    injected learnings participate in tombstone-compaction the same
    way old file reads do — they don't permanently bloat every prompt.

    Only the most recent non-dismissed learnings are included (capped
    at ``_MAX_INJECTED``). Each entry is truncated to
    ``_MAX_LEARNING_CHARS`` to keep the fragment bounded.
    """
    from tera_pilot.agent.context_fragments import build_fragment, stable_id
    learnings = list_learnings(project_path, include_body=True)
    active = [l for l in learnings if not l.dismissed][:_MAX_INJECTED]
    if not active:
        return ""
    parts: List[str] = []
    for l in active:
        body = l.body[:_MAX_LEARNING_CHARS]
        if len(l.body) > _MAX_LEARNING_CHARS:
            body += f"\n... [truncated, {len(l.body)} total chars]"
        parts.append(f"### {l.title}\nsource: {l.source}  date: {l.date}  severity: {l.severity}\n\n{body}")
    fragment_body = "\n\n---\n\n".join(parts)
    fid = stable_id(project_path)
    return build_fragment("project_learnings", fid, fragment_body)


# ── Slash command surface ──────────────────────────────────────────────


def handle_learnings_command(project_path: str, arg: str) -> Dict[str, Any]:
    """Handle the ``/learnings`` slash command.

    Subcommands:
      /learnings                  — list recent entries
      /learnings show <id>        — show full body of one entry
      /learnings dismiss <id>     — dismiss an entry (stops injection)
      /learnings restore <id>     — un-dismiss an entry
      /learnings scan             — manually run trigger detection now
      /learnings dismissed        — list dismissed entries only

    Returns a dict with ``ok`` and either ``text`` (human-readable
    multi-line string for the TUI/GUI to print) or ``error``.
    """
    arg = (arg or "").strip()
    parts = arg.split(None, 1) if arg else []
    sub = parts[0].lower() if parts else ""
    sub_arg = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("", "list"):
        learnings = list_learnings(project_path, include_body=False)
        if not learnings:
            return {"ok": True, "text": "No learnings yet for this project. Run /learnings scan to detect triggers."}
        lines = [f"Learnings for {project_path} ({len(learnings)} total):"]
        for l in learnings[:20]:
            mark = " [dismissed]" if l.dismissed else ""
            lines.append(f"  {l.date}  {l.title}{mark}")
            lines.append(f"    source={l.source} severity={l.severity} tags={','.join(l.tags) if l.tags else '-'}")
            lines.append(f"    file: {Path(l.path).name}")
        return {"ok": True, "text": "\n".join(lines)}

    if sub == "dismissed":
        learnings = list_learnings(project_path, include_body=False)
        dismissed = [l for l in learnings if l.dismissed]
        if not dismissed:
            return {"ok": True, "text": "No dismissed learnings."}
        lines = [f"Dismissed learnings ({len(dismissed)}):"]
        for l in dismissed:
            lines.append(f"  {l.date}  {l.title}")
            lines.append(f"    file: {Path(l.path).name}")
        return {"ok": True, "text": "\n".join(lines)}

    if sub == "show":
        if not sub_arg:
            return {"ok": False, "error": "Usage: /learnings show <filename-or-id>"}
        learnings = list_learnings(project_path, include_body=True)
        # Find by filename or title-substring.
        target = None
        for l in learnings:
            if sub_arg in Path(l.path).name or sub_arg.lower() in l.title.lower():
                target = l
                break
        if target is None:
            return {"ok": False, "error": f"no learning matches {sub_arg!r}"}
        return {"ok": True, "text": f"# {target.title}\n\npath: {target.path}\n\n{target.body}"}

    if sub == "dismiss":
        if not sub_arg:
            return {"ok": False, "error": "Usage: /learnings dismiss <filename-or-id>"}
        result = dismiss_learning(project_path, sub_arg)
        if not result.get("ok"):
            return result
        return {"ok": True, "text": f"Dismissed: {result['path']}\nIt will no longer be injected into prompts. Use /learnings restore to undo."}

    if sub == "restore":
        if not sub_arg:
            return {"ok": False, "error": "Usage: /learnings restore <filename-or-id>"}
        result = restore_learning(project_path, sub_arg)
        if not result.get("ok"):
            return result
        return {"ok": True, "text": f"Restored: {', '.join(result['restored'])}"}

    if sub == "scan":
        created = scan_and_create_learnings(project_path)
        if not created:
            return {"ok": True, "text": "Scan complete — no new triggers detected."}
        lines = [f"Scan complete — {len(created)} new learning(s) created:"]
        for c in created:
            lines.append(f"  [{c.get('source', '?')}] {c.get('path', '?')}")
        return {"ok": True, "text": "\n".join(lines)}

    return {
        "ok": False,
        "error": (
            f"Unknown /learnings subcommand: {sub!r}\n"
            f"Usage: /learnings [list|show|dismiss|restore|scan|dismissed]"
        ),
    }