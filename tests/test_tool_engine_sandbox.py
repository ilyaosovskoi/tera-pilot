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
import types
from pathlib import Path

import pytest

from tera_pilot.agent.guardian import assess_risk
from tera_pilot.command_policy import CommandPolicy
from tera_pilot.agent_runtime.tool_engine import ToolEngine


def _engine(tmp_path) -> ToolEngine:
    e = ToolEngine(str(tmp_path))
    # P0.4: ToolEngine fails CLOSED on confirmations without a UI
    # callback; these tests exercise sandbox guards, not the
    # confirmation policy — opt in to headless auto-approve.
    e.headless_confirm = "allow"
    return e


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


# ── git shell-alias sandbox escape (fix 4) ─────────────────────────────
# git executes any alias whose value starts with '!' through the shell:
# `git -c alias.x='!<cmd>' x` / `git config alias.x '!<cmd>'`. The value
# is never a path, so it bypassed _resolve_path entirely — a genuine
# workspace-sandbox escape to arbitrary shell execution.


def test_git_c_shell_alias_exec_blocked(tmp_path):
    e = _engine(tmp_path)
    res = e._execute_command("git -c alias.x='!echo pwned' x")
    assert "SECURITY ERROR" in res, res


def test_git_config_shell_alias_value_blocked(tmp_path):
    e = _engine(tmp_path)
    res = e._execute_command("git config alias.x '!touch /tmp/pwned'")
    assert "SECURITY ERROR" in res, res


def test_git_c_inline_shell_alias_blocked(tmp_path):
    e = _engine(tmp_path)
    res = e._execute_command("git -c alias.x='!cat /etc/passwd' x")
    assert "SECURITY ERROR" in res, res


def test_git_legit_c_flag_still_allowed(tmp_path):
    e = _engine(tmp_path)
    res = e._execute_command("git -c core.quotepath=false status")
    assert "SECURITY ERROR" not in res


def test_git_exec_capable_config_keys_blocked(tmp_path):
    """git executes the values of several config keys as commands — the
    path checks can't see them (not paths), so they must be blocked by
    key. Confirmed live: core.fsmonitor runs on `git status`, core.editor
    on `git commit`."""
    e = _engine(tmp_path)
    for cmd in (
        "git -c core.fsmonitor='touch /tmp/fsmonitor-pwned' status",
        "git -c core.editor='touch /tmp/editor-pwned' commit --allow-empty",
        "git -c core.sshCommand='touch /tmp/ssh-pwned' status",
        "git -c core.pager='touch /tmp/pager-pwned' status",
        "git -c core.askpass='touch /tmp/ask-pwned' status",
        "git -c core.hooksPath='.githooks' status",
        "git -c sequence.editor='touch /tmp/seq-pwned' status",
        "git -c credential.helper='touch /tmp/cred-pwned' status",
        "git -c diff.foo.textconv='touch /tmp/tc-pwned' status",
        "git -c filter.f.smudge='touch /tmp/sm-pwned' status",
        "git -c filter.f.clean='touch /tmp/cl-pwned' status",
        "git config core.editor 'touch /tmp/editor2-pwned'",
        "git config alias.x '!echo pwned'",
        # keys are case-insensitive in git
        "git -c CORE.FSMONITOR='touch /tmp/fm-pwned' status",
        "git -c Core.Editor='touch /tmp/ed-pwned' status",
    ):
        res = e._execute_command(cmd)
        assert "SECURITY ERROR" in res, f"{cmd!r} must be blocked, got: {res[:80]!r}"


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


# ── v2.3.4-security: repo-supplied git exec keys + hooks neutralized ──
# A malicious repository is UNTRUSTED input (threat model T1). Its OWN
# `.git/config` can set exec-capable keys (`core.fsmonitor` runs on plain
# `git status`, `diff.*.textconv` on `git diff`, `core.editor` on
# `git commit`) and its `.git/hooks/*` run on the matching operation —
# nothing is passed on the command line, so the arg-level checks in
# `_validate_command_paths` cannot see it. The engine now injects `-c`
# overrides that empty those keys and point `core.hooksPath` at a
# hook-free directory for every git invocation.

import subprocess
import tempfile


def _git_init(tmp_path):
    """Init a real repo in tmp_path with identity configured."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    return tmp_path


def test_git_repo_config_exec_keys_neutralized(tmp_path):
    """core.fsmonitor / core.editor from the repo's OWN .git/config must
    NOT execute on plain git status/commit (the arg checks can't see them)."""
    _git_init(tmp_path)
    marker = Path(tempfile.gettempdir()) / "PWNED_repo_config_exec"
    marker.unlink(missing_ok=True)
    cfg = (tmp_path / ".git" / "config")
    cfg.write_text(
        cfg.read_text(encoding="utf-8")
        + "\n[core]\n\tfsmonitor = touch " + str(marker)
        + "\n\teditor = touch " + str(marker) + "\n",
        encoding="utf-8",
    )
    e = _engine(tmp_path)
    _auto_approve(e)
    res = e._execute_command("git status --porcelain", timeout=30)
    assert "[SECURITY ERROR]" not in res, res
    assert not marker.exists(), "core.fsmonitor executed on git status!"
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    e._execute_command("git add f.txt", timeout=30)
    res = e._execute_command("git commit -m t", timeout=30)
    assert "[SECURITY ERROR]" not in res, res
    assert not marker.exists(), "core.editor executed on git commit!"


def test_git_repo_hooks_neutralized(tmp_path):
    """A malicious pre-commit hook in the repo's .git/hooks must NOT run
    on git commit."""
    _git_init(tmp_path)
    marker = Path(tempfile.gettempdir()) / "PWNED_repo_hook"
    marker.unlink(missing_ok=True)
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch " + str(marker) + "\n", encoding="utf-8")
    hook.chmod(0o755)
    e = _engine(tmp_path)
    _auto_approve(e)
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    e._execute_command("git add f.txt", timeout=30)
    res = e._execute_command("git commit -m t", timeout=30)
    assert "[SECURITY ERROR]" not in res, res
    assert not marker.exists(), "pre-commit hook executed on git commit!"


def test_git_textconv_driver_neutralized(tmp_path):
    """diff.*.textconv from the repo config + .gitattributes must NOT run
    on git diff."""
    _git_init(tmp_path)
    marker = Path(tempfile.gettempdir()) / "PWNED_textconv"
    marker.unlink(missing_ok=True)
    (tmp_path / "data.bin").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "data.bin"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True)
    (tmp_path / "data.bin").write_text("world\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("*.bin diff=evil\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "diff.evil.textconv", "touch " + str(marker)],
        cwd=str(tmp_path), check=True,
    )
    e = _engine(tmp_path)
    _auto_approve(e)
    res = e._execute_command("git diff", timeout=30)
    assert "[SECURITY ERROR]" not in res, res
    assert not marker.exists(), "diff.*.textconv executed on git diff!"


def test_git_legitimate_commands_work_with_neutralization(tmp_path):
    """Neutralization must not break ordinary git usage."""
    _git_init(tmp_path)
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    e = _engine(tmp_path)
    _auto_approve(e)
    res = e._execute_command("git add a.txt", timeout=30)
    assert "[SECURITY ERROR]" not in res
    res = e._execute_command("git commit -m init", timeout=30)
    assert "init" in res
    res = e._execute_command("git log --oneline", timeout=30)
    assert "init" in res
    res = e._execute_command("git status --porcelain", timeout=30)
    assert "[SECURITY ERROR]" not in res


def test_agent_git_tools_work_import_fixed(tmp_path):
    """Regression: the agent's git tools (_git_status etc.) used
    `from .git_service import GitService` inside tool_engine/, which
    resolves to a NONEXISTENT module — so every git tool call failed
    with "not a git repository (or git not installed)" on a valid repo."""
    _git_init(tmp_path)
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    e = _engine(tmp_path)
    status = e._git_status()
    assert "Branch:" in status, f"git tools broken: {status[:100]!r}"
    assert "not a git repository" not in status


def test_git_neutralization_helper_marks_exec_keys():
    """git_neutralization_args must include overrides for every known
    exec-capable key family (and driver keys parsed from a repo config)."""
    from tera_pilot.git_service import git_neutralization_args, _git_key_is_exec_capable
    assert _git_key_is_exec_capable("core.fsmonitor")
    assert _git_key_is_exec_capable("CORE.EDITOR")  # case-insensitive
    assert _git_key_is_exec_capable("diff.evil.textconv")
    assert _git_key_is_exec_capable("filter.f.clean")
    assert _git_key_is_exec_capable("filter.f.smudge")
    assert _git_key_is_exec_capable("credential.https://x.helper")
    assert not _git_key_is_exec_capable("core.quotepath")
    assert not _git_key_is_exec_capable("user.name")
    flags = git_neutralization_args(None)
    joined = " ".join(flags)
    assert "core.fsmonitor=" in joined
    assert "core.editor=true" in joined
    assert "core.hookspath=" in joined


# ── v2.3.4-security: auto-detected test/lint commands need approval ────
# The self-verify flow detects the repo's test command (pytest/npm test/…)
# and runs it. A malicious repo can ship a package.json whose "test"
# script is arbitrary code, or test files that execute on import — so the
# auto-run path must go through the same user-confirmation gate as
# execute_command/run_code (T2).


def test_auto_test_command_requires_confirmation(tmp_path):
    e = _engine(tmp_path)
    e._request_confirmation = lambda *a, **k: False  # type: ignore[method-assign]
    res = e._run_test_command_sandboxed(["pytest", "-q"])
    assert "REJECTED BY USER" in res, res


def test_auto_test_command_runs_when_approved(tmp_path):
    e = _engine(tmp_path)
    e._request_confirmation = lambda *a, **k: True  # type: ignore[method-assign]
    (tmp_path / "test_x.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    res = e._run_test_command_sandboxed(["pytest", "-q", "test_x.py"])
    assert "REJECTED BY USER" not in res
    assert "[EXIT CODE]" in res


# ── v2.3.4: Guardian MODIFY-verdict UI flow ───────────────────────────
# The engine now asks the user (via _guardian_callback) before applying a
# guardian-suggested fix, and ToolEngine._emit exists so GUARDIAN_REVIEW
# events actually propagate instead of crashing with AttributeError.


class _GuardianFakeProvider:
    """Minimal provider whose generate() returns a fixed LLM verdict text."""

    provider_id = "fake"
    model = "fake-model"

    def __init__(self, text):
        self._text = text

    def generate(self, messages):
        import types
        return types.SimpleNamespace(text=self._text)


_MODIFY_VERDICT_JSON = (
    '{"verdict": "MODIFY", "rationale": "use a safer command", '
    '"suggested_args": {"command": "ls -la /tmp"}}'
)


def _guardian_engine(tmp_path) -> ToolEngine:
    from tera_pilot.agent.guardian import GuardianConfig
    e = _engine(tmp_path)
    e._guardian_config = GuardianConfig(level="all")
    return e


def _wire_fake_provider(e, text):
    import types
    p = _GuardianFakeProvider(text)
    e._provider = p
    e._registry = types.SimpleNamespace(active=p, active_id="fake")
    return p


def test_guardian_modify_applies_fix_when_ui_approves_use_fix(tmp_path):
    """With a UI callback wired, 'use_fix' applies the suggested args."""
    import threading
    e = _guardian_engine(tmp_path)
    _wire_fake_provider(e, _MODIFY_VERDICT_JSON)
    received = {}

    def cb(info):
        received.update(info)
        threading.Timer(0.05, lambda: e.respond_guardian("use_fix")).start()

    e._guardian_callback = cb
    args = {"command": "rm -rf /"}
    e._guardian_review("execute_command", args)
    assert received.get("guardian_verdict") == "MODIFY"
    assert received.get("suggested_args") == {"command": "ls -la /tmp"}
    # The suggested fix was applied to the tool args.
    assert args == {"command": "ls -la /tmp"}


def test_guardian_modify_approve_keeps_original_args(tmp_path):
    """'approve' runs the ORIGINAL call, not the suggested fix."""
    import threading
    e = _guardian_engine(tmp_path)
    _wire_fake_provider(e, _MODIFY_VERDICT_JSON)

    def cb(info):
        threading.Timer(0.05, lambda: e.respond_guardian("approve")).start()

    e._guardian_callback = cb
    args = {"command": "rm -rf /"}
    e._guardian_review("execute_command", args)
    assert args == {"command": "rm -rf /"}


def test_guardian_modify_reject_raises(tmp_path):
    """'reject' aborts the tool call with a Guardian-rejected error."""
    import threading
    e = _guardian_engine(tmp_path)
    _wire_fake_provider(e, _MODIFY_VERDICT_JSON)

    def cb(info):
        threading.Timer(0.05, lambda: e.respond_guardian("reject")).start()

    e._guardian_callback = cb
    with pytest.raises(RuntimeError, match="Guardian rejected"):
        e._guardian_review("execute_command", {"command": "rm -rf /"})


def test_guardian_modify_headless_auto_applies_fix(tmp_path):
    """Headless (no UI callback) keeps the previous auto-apply behavior:
    the suggested fix is applied without asking."""
    e = _guardian_engine(tmp_path)
    _wire_fake_provider(e, _MODIFY_VERDICT_JSON)
    args = {"command": "rm -rf /"}
    e._guardian_review("execute_command", args)
    assert args == {"command": "ls -la /tmp"}


def test_guardian_review_emits_event_without_crashing(tmp_path):
    """GUARDIAN_REVIEW events must propagate through ToolEngine._emit
    (previously _emit did not exist on ToolEngine — AttributeError was
    swallowed and Guardian never actually acted)."""
    events = []
    e = _guardian_engine(tmp_path)
    _wire_fake_provider(e, _MODIFY_VERDICT_JSON)
    e.on_event = lambda ev, data: events.append((getattr(ev, "value", str(ev)), dict(data)))
    e._guardian_review("execute_command", {"command": "rm -rf /"})
    kinds = [k for k, _ in events]
    assert "guardian_review" in kinds, f"expected guardian_review event, got {kinds}"
    pending = [d for k, d in events if k == "guardian_review" and d.get("guardian_verdict") == "PENDING"]
    assert pending, "expected a PENDING guardian event before the verdict"


def test_guardian_modify_reject_through_execute(tmp_path):
    """A rejected MODIFY surfaces as [GUARDIAN REJECT] through execute()."""
    import threading
    from tera_pilot.agent_runtime.types import ToolCall, ToolName
    e = _guardian_engine(tmp_path)
    _wire_fake_provider(e, _MODIFY_VERDICT_JSON)

    def cb(info):
        threading.Timer(0.05, lambda: e.respond_guardian("reject")).start()

    e._guardian_callback = cb
    call = ToolCall(name=ToolName.EXECUTE_COMMAND, args={"command": "rm -rf /"})
    res = e.execute(call)
    assert "[GUARDIAN REJECT]" in res


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


# ═══════════════════════════════════════════════════════════════════
# P0.4 — uniform always_ask coverage + headless fail-closed
# ═══════════════════════════════════════════════════════════════════

import base64  # noqa: E402
import subprocess  # noqa: E402


def _reject_all(e: ToolEngine) -> None:
    """Wire a confirmation callback that always denies."""
    e._confirm_callback = lambda info: e.respond_confirmation(False)


def test_headless_fail_closed_blocks_side_effects(tmp_path):
    """P0.4: with NO confirmation callback wired, side-effecting tools
    are BLOCKED (fail-closed) — never silently run. Previously the
    engine failed OPEN and executed the action."""
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    e = ToolEngine(str(tmp_path))  # headless_confirm defaults to fail_closed
    res = e._execute_command("cat f.txt", timeout=30)
    assert "[REJECTED BY USER]" in res, res
    res2 = e._write_file("newfile.txt", "x")
    assert "[REJECTED BY USER]" in res2, res2
    assert not (tmp_path / "newfile.txt").exists()


def test_headless_allow_opt_in_runs_commands(tmp_path):
    """P0.4: the explicit opt-in (daemon/ACP --no-confirm) restores
    headless auto-approve — actions run without a UI."""
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    e = ToolEngine(str(tmp_path))
    e.headless_confirm = "allow"
    res = e._execute_command("cat f.txt", timeout=30)
    assert "[REJECTED BY USER]" not in res
    assert "hello" in res


def test_mkdir_requires_confirmation(tmp_path):
    """P0.4: mkdir is gated under always_ask (previously unconditional)."""
    e = ToolEngine(str(tmp_path))
    _reject_all(e)
    res = e._mkdir("subdir")
    assert "[REJECTED BY USER]" in res, res
    assert not (tmp_path / "subdir").exists()


def test_write_binary_new_file_requires_confirmation(tmp_path):
    """P0.4: write_binary_file confirms brand-new files too (previously
    only overwrites were gated, so new binary files were written silently)."""
    e = ToolEngine(str(tmp_path))
    _reject_all(e)
    res = e._write_binary_file("img.bin", base64.b64encode(b"data").decode())
    assert "[REJECTED BY USER]" in res, res
    assert not (tmp_path / "img.bin").exists()


def test_write_file_new_with_diff_review_requires_confirmation(tmp_path):
    """P0.4: a NEW file written while diff review is enabled had NO gate
    at all (no diff to review, so nothing asked) — now it honors autonomy."""
    e = ToolEngine(str(tmp_path))
    e.diff_review_enabled = True
    _reject_all(e)
    res = e._write_file("new.txt", "content")
    assert "[REJECTED BY USER]" in res, res
    assert not (tmp_path / "new.txt").exists()


def test_write_file_new_with_diff_review_confirmed_creates_file(tmp_path):
    """P0.4 positive: accepting the confirmation creates the new file."""
    e = ToolEngine(str(tmp_path))
    e.diff_review_enabled = True
    e._confirm_callback = lambda info: e.respond_confirmation(True)
    res = e._write_file("new.txt", "content")
    assert "[WRITTEN]" in res, res
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "content"


def test_write_file_overwrite_headless_diff_review_fails_closed(tmp_path):
    """P0.4: overwriting an EXISTING file with diff review enabled but NO
    UI callback wired must fail CLOSED — previously it applied the write
    with no gate at all (silent fail-open in daemon/ACP/headless runs)."""
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    e = ToolEngine(str(tmp_path))
    e.diff_review_enabled = True
    # No _diff_review_callback wired, headless_confirm defaults to fail_closed.
    res = e._write_file("f.txt", "new content")
    assert "[REJECTED BY USER]" in res, res
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "old\n"


def test_write_file_overwrite_headless_diff_review_opt_in_applies(tmp_path):
    """P0.4 positive: with the explicit --no-confirm opt-in, the same
    headless overwrite is allowed (mirrors _request_confirmation)."""
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    e = ToolEngine(str(tmp_path))
    e.diff_review_enabled = True
    e.headless_confirm = "allow"
    res = e._write_file("f.txt", "new content")
    assert "[WRITTEN]" in res, res
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new content"


def test_write_file_overwrite_diff_review_wired_still_reviews(tmp_path):
    """With a UI callback wired, an existing-file overwrite still goes
    through the diff-review dialog (not the generic confirmation)."""
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    e = ToolEngine(str(tmp_path))
    e.diff_review_enabled = True
    seen = {}

    def cb(info):
        seen.update(info)
        e.respond_diff_review(True)

    e._diff_review_callback = cb
    res = e._write_file("f.txt", "new content")
    assert "[WRITTEN]" in res, res
    assert "diff" in seen, "diff-review callback must be invoked, got %s" % seen


def test_str_replace_headless_diff_review_fails_closed(tmp_path):
    """P0.4: str_replace on an existing file with diff review enabled but
    no UI callback must fail CLOSED — previously it applied the edit with
    no gate at all."""
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    e = ToolEngine(str(tmp_path))
    e.diff_review_enabled = True
    res = e._str_replace("f.txt", "old", "new")
    assert "[REJECTED BY USER]" in res, res
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "old\n"


def test_str_replace_headless_diff_review_opt_in_applies(tmp_path):
    """P0.4 positive: --no-confirm opt-in allows the headless edit."""
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    e = ToolEngine(str(tmp_path))
    e.diff_review_enabled = True
    e.headless_confirm = "allow"
    res = e._str_replace("f.txt", "old", "new")
    assert "[STR_REPLACE]" in res, res
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new\n"


def test_guardian_llm_error_fails_closed(tmp_path):
    """P0.4-security: a provider/LLM error during Guardian review must
    REJECT the call (fail closed), never silently APPROVE it."""
    e = _guardian_engine(tmp_path)

    class _RaisingProvider:
        provider_id = "fake"
        model = "fake-model"

        def generate(self, messages):
            raise RuntimeError("provider exploded")

    e._provider = _RaisingProvider()
    e._registry = types.SimpleNamespace(active=e._provider, active_id="fake")
    with pytest.raises(RuntimeError, match="Guardian rejected|defaulting to reject|LLM error"):
        e._guardian_review("execute_command", {"command": "rm -rf /"})


def test_guardian_unparseable_verdict_fails_closed(tmp_path):
    """P0.4-security: an unparseable LLM review must REJECT, not APPROVE."""
    e = _guardian_engine(tmp_path)
    _wire_fake_provider(e, "this is not json at all")
    with pytest.raises(RuntimeError, match="Guardian rejected|unparseable"):
        e._guardian_review("execute_command", {"command": "rm -rf /"})


def test_guardian_no_provider_fails_closed(tmp_path):
    """P0.4-security: no reviewer provider available must REJECT, not
    silently APPROVE the risky call."""
    e = _guardian_engine(tmp_path)
    e._provider = None
    e._registry = None
    with pytest.raises(RuntimeError, match="Guardian rejected|No LLM provider"):
        e._guardian_review("execute_command", {"command": "rm -rf /"})


def test_git_stage_requires_confirmation(tmp_path):
    """P0.4: git_stage is gated under always_ask like git_commit."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    e = ToolEngine(str(tmp_path))
    _reject_all(e)
    res = e._git_stage(["a.txt"])
    assert "[REJECTED BY USER]" in res, res


def test_apply_diff_multifile_summary_mentions_file_count(tmp_path):
    """P0.4: a multi-file patch is confirmed ONCE, and the summary
    surfaces how many files the single confirmation covers."""
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("two\n", encoding="utf-8")
    multi = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+ONE\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-two\n+TWO\n"
    )
    e = ToolEngine(str(tmp_path))
    seen = []
    e._confirm_callback = lambda info: (seen.append(info), e.respond_confirmation(True))
    res = e._apply_diff("a.txt", multi)
    assert "[PATCHED]" in res or "[CREATED]" in res or "[DIFF ERROR]" not in res, res
    assert len(seen) == 1, "multi-file patch must be confirmed exactly once"
    assert "2 files" in seen[0]["summary"], seen[0]["summary"]
