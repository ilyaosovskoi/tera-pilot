"""Regression tests for ToolEngine sandbox guards and Guardian command-policy checks.

Covers three security/correctness fixes:

1. ``git --git-dir=<path>`` / ``--work-tree=<path>`` (and their two-arg
   forms) bypassed the workspace sandbox in ``ToolEngine._validate_command_paths``
   because every argument starting with ``-`` was skipped as a "flag" —
   only ``git -C`` was validated. ``git --git-dir=/home/user/.git log``
   could read git history from anywhere on disk, and
   ``git --work-tree=/etc add -A`` could stage arbitrary files.

2. ``git_diff`` accepted an unvalidated ``path`` pathspec, so a relative
   ``../outside`` or absolute path could show diffs of files outside the
   workspace (e.g. when the workspace is a subdirectory of a larger repo).

3. Guardian's command-policy risk check called
   ``command_policy.is_dangerous_flag(binary, "")`` with an empty flag,
   which can never return True — dead code. It now flags binaries the
   resolved policy would refuse (deny list / not allowed).
"""

import os
from pathlib import Path

import pytest

from tera_pilot.agent.guardian import assess_risk
from tera_pilot.command_policy import CommandPolicy
from tera_pilot.agent_runtime.tool_engine import ToolEngine


def _engine(tmp_path) -> ToolEngine:
    return ToolEngine(str(tmp_path))


# ── git flag sandbox bypass (fix 1) ────────────────────────────────────


def test_git_c_escape_blocked(tmp_path):
    e = _engine(tmp_path)
    err = e._validate_command_paths(["git", "-C", "/etc", "status"])
    assert err is not None and "SECURITY ERROR" in err


def test_git_git_dir_inline_escape_blocked(tmp_path):
    e = _engine(tmp_path)
    err = e._validate_command_paths(["git", "--git-dir=/home/user/.git", "log"])
    assert err is not None and "SECURITY ERROR" in err
    assert "--git-dir" in err


def test_git_git_dir_two_arg_escape_blocked(tmp_path):
    e = _engine(tmp_path)
    err = e._validate_command_paths(["git", "--git-dir", "/home/user/.git", "log"])
    assert err is not None and "SECURITY ERROR" in err


def test_git_work_tree_inline_escape_blocked(tmp_path):
    e = _engine(tmp_path)
    err = e._validate_command_paths(["git", "--work-tree=/etc", "add", "-A"])
    assert err is not None and "SECURITY ERROR" in err


def test_git_work_tree_two_arg_escape_blocked(tmp_path):
    e = _engine(tmp_path)
    err = e._validate_command_paths(["git", "--work-tree", "/", "add", "-A"])
    assert err is not None and "SECURITY ERROR" in err


def test_git_relative_escape_blocked(tmp_path):
    e = _engine(tmp_path)
    err = e._validate_command_paths(["git", "--git-dir=../outside/.git", "log"])
    assert err is not None and "SECURITY ERROR" in err


def test_git_inside_workspace_not_blocked(tmp_path):
    e = _engine(tmp_path)
    (tmp_path / "sub").mkdir(exist_ok=True)
    assert e._validate_command_paths(["git", "status"]) is None
    assert e._validate_command_paths(["git", "-C", ".", "status"]) is None
    assert e._validate_command_paths(["git", "--git-dir=.git", "log"]) is None
    assert e._validate_command_paths(["git", "--work-tree=sub", "add", "-A"]) is None


def test_non_git_path_commands_still_validated(tmp_path):
    e = _engine(tmp_path)
    assert e._validate_command_paths(["rm", "-rf", "/etc"]) is not None
    assert e._validate_command_paths(["cat", "/etc/passwd"]) is not None
    assert e._validate_command_paths(["cat", "file.txt"]) is None


# ── _git_diff pathspec validation (fix 2) ──────────────────────────────


class _FakeGit:
    """Minimal stand-in so _git_diff can be exercised without a real repo."""

    def __init__(self):
        self.last_path = None

    def diff(self, *, staged=False, file_path=None):
        self.last_path = file_path
        return "diff"


def test_git_diff_outside_path_rejected(tmp_path):
    e = _engine(tmp_path)
    fake = _FakeGit()
    e._get_git_service = lambda: fake  # type: ignore[method-assign]
    res = e._git_diff(staged=False, path="../outside.py")
    assert "GIT ERROR" in res and "outside workspace" in res


def test_git_diff_absolute_outside_path_rejected(tmp_path):
    e = _engine(tmp_path)
    fake = _FakeGit()
    e._get_git_service = lambda: fake  # type: ignore[method-assign]
    res = e._git_diff(staged=False, path="/etc/passwd")
    assert "GIT ERROR" in res and "outside workspace" in res


def test_git_diff_inside_path_passed_as_relative(tmp_path):
    e = _engine(tmp_path)
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    fake = _FakeGit()
    e._get_git_service = lambda: fake  # type: ignore[method-assign]
    res = e._git_diff(staged=False, path="src/app.py")
    assert res == "diff"
    assert fake.last_path == os.path.join("src", "app.py")


def test_git_diff_no_path_allowed(tmp_path):
    e = _engine(tmp_path)
    fake = _FakeGit()
    e._get_git_service = lambda: fake  # type: ignore[method-assign]
    assert e._git_diff(staged=False, path="") == "diff"
    assert fake.last_path is None


# ── Guardian command-policy check (fix 3) ──────────────────────────────


def _policy():
    return CommandPolicy(
        allowed=frozenset({"git", "python3", "cat"}),
        dangerous_flags={"python3": frozenset({"-c", "-m"})},
        denied=frozenset({"rm"}),
    )


def test_guardian_flags_denied_binary_as_high():
    risk = assess_risk(
        "execute_command",
        {"command": "rm -rf /"},
        "/ws",
        _policy(),
    )
    assert risk.level == "high"
    assert any("not allowed" in r for r in risk.reasons)


def test_guardian_allowed_binary_not_policy_flagged():
    # `git status` is allowed by the policy — the command-policy branch
    # must NOT fire; the call stays at the default medium for shell exec.
    risk = assess_risk(
        "execute_command",
        {"command": "git status"},
        "/ws",
        _policy(),
    )
    assert risk.level == "medium"
    assert not any("not allowed" in r for r in risk.reasons)


def test_guardian_no_policy_is_safe():
    # Passing no policy must not raise and must not change risk level.
    risk = assess_risk(
        "execute_command",
        {"command": "git status"},
        "/ws",
        None,
    )
    assert risk.level == "medium"


# ── Subprocess output draining (pipe-buffer deadlock fix) ─────────────


def _auto_approve(engine):
    engine._request_confirmation = lambda *a, **k: True  # type: ignore[method-assign]


def test_run_code_large_output_no_timeout(tmp_path):
    """>64KB of stdout must not deadlock the pipe and spuriously time out.

    Old behaviour: stdout/stderr were only read AFTER the child exited, so
    a child writing more than the OS pipe buffer (~64KB) blocked forever
    on write and was killed at the deadline → "[TIMEOUT]" with no output.
    """
    e = _engine(tmp_path)
    _auto_approve(e)
    res = e._run_code('print("x" * 100000)', language="python", timeout=15)
    assert "[TIMEOUT]" not in res
    assert "x" * 20 in res  # output was actually captured


def test_run_code_large_stderr_no_timeout(tmp_path):
    e = _engine(tmp_path)
    _auto_approve(e)
    res = e._run_code('import sys; sys.stderr.write("e" * 100000)', language="python", timeout=15)
    assert "[TIMEOUT]" not in res
    assert "[STDERR]" in res


def test_execute_command_large_output_no_timeout(tmp_path):
    (tmp_path / "big.txt").write_text("y" * 150000, encoding="utf-8")
    e = _engine(tmp_path)
    _auto_approve(e)
    res = e._execute_command("cat big.txt", timeout=15)
    assert "[TIMEOUT]" not in res
    assert "y" * 20 in res


# ── Workspace symlink resolution (fix: __init__ must resolve) ──────────


def test_workspace_with_symlink_prefix_not_falsely_blocked(tmp_path):
    """A workspace whose path contains a symlink component (e.g. macOS
    /var → /private/var) must not reject every path as "outside the
    workspace". Old __init__ kept the workspace unresolved while
    _resolve_path resolved the candidate → is_relative_to always failed.
    """
    # Create a symlinked alias of tmp_path, then construct with it.
    link = tmp_path.parent / f"{tmp_path.name}_link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    try:
        e = ToolEngine(str(link))
        (tmp_path / "f.txt").write_text("hi", encoding="utf-8")
        # A file inside the (symlinked) workspace must resolve.
        p = e._resolve_path("f.txt")
        assert p.exists()
        # A path outside must still be blocked.
        with pytest.raises(PermissionError):
            e._resolve_path("/etc/passwd")
        # And a path-taking command must not be falsely rejected.
        _auto_approve(e)
        res = e._execute_command("cat f.txt", timeout=15)
        assert "SECURITY ERROR" not in res
    finally:
        try:
            link.unlink()
        except OSError:
            pass


# ── Guardian template path (fix: correct location) ─────────────────────


def test_guardian_template_exists_at_correct_path():
    """The engine's guardian prompt template must exist on disk.

    The old path (relative to agent_runtime/tool_engine/) never existed,
    so every Guardian LLM call silently used the generic fallback prompt.
    """
    from tera_pilot.agent_runtime.tool_engine import _engine as engine_module
    template_path = Path(engine_module.__file__).resolve().parents[2] / "agent" / "templates" / "guardian.md"
    assert template_path.is_file(), f"guardian template missing: {template_path}"
    content = template_path.read_text(encoding="utf-8")
    assert len(content) > 200, "guardian template looks like the degraded fallback"
