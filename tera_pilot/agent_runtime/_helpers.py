"""
Low-level helpers for the agent runtime.

Currently contains:
- _sanitize_command(): shell=False + shlex.split() + whitelist
  validation for shell commands. Used by the tool engine's
  execute_command implementation.

Kept as a separate module so that the sanitisation rules can be
unit-tested in isolation without importing the full ToolEngine.
"""

import logging
import os
import shlex
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


ALLOWED_COMMANDS = {
    "python3", "python", "node", "npm", "pip", "git", "ls", "cat", "head",
    "tail", "find", "grep", "wc", "mkdir", "touch", "cp", "mv", "rm",
    "pytest", "black", "isort", "flake8", "mypy", "ruff",
}


def _sanitize_command(command: str, project_root: Optional[str] = None) -> Tuple[List[str], bool]:
    """
    Parse and validate a shell command.
    Returns (args_list, is_safe).
    Rejects shell=True, pipes, redirects, and disallowed binaries.

    v1.2.1-fix (review §4.6): the whitelist + dangerous-flags map are
    now resolved from BASE + ~/.tera_pilot/commands.json + <project>/.tera_pilot/
    commands.json via the CommandPolicy module, instead of the
    hardcoded constants the review flagged as too rigid. Out-of-the-box
    behaviour is unchanged (BASE_ALLOWED_COMMANDS matches the old set
    verbatim) — users can now EXTEND it without editing source.

    v1.2.2-fix: added ``project_root``. Every call site inside
    ToolEngine passes ``self.workspace`` now — previously all three
    call sites called ``get_global_policy()`` with no argument, which
    resolves with ``project_root=None`` and therefore never actually
    read ``<project>/.tera_pilot/commands.json`` in the GUI/agent path at
    all (only the headless CLI passed ``args.workspace`` through).
    That made project-scoped policy a CLI-only feature in practice.
    """
    # Reject dangerous metacharacters
    dangerous = {";", "&&", "||", "|", ">", "<", "`", "$", "\n"}
    if any(d in command for d in dangerous):
        logger.warning(f"[security] Dangerous metacharacters in command: {command}")
        return [], False

    try:
        args = shlex.split(command)
    except ValueError as e:
        logger.warning(f"[security] Failed to parse command: {e}")
        return [], False

    if not args:
        return [], False

    base_cmd = os.path.basename(args[0])

    # v1.2.1-fix (review §4.6): consult the resolved CommandPolicy
    # instead of the hardcoded ALLOWED_COMMANDS constant. Falls back
    # to the constant if the policy module isn't importable for any
    # reason (defensive — never break tool execution on policy
    # resolver failure).
    try:
        from .command_policy import get_global_policy
        policy = get_global_policy(project_root)
        is_allowed = policy.is_allowed(base_cmd)
    except Exception as policy_err:
        logger.debug("[security] CommandPolicy resolve failed (%s) — "
                     "falling back to BASE_ALLOWED_COMMANDS", policy_err)
        is_allowed = base_cmd in ALLOWED_COMMANDS
    if not is_allowed:
        logger.warning(f"[security] Command not in whitelist: {base_cmd}")
        return [], False

    # v1.0.6-security: block dangerous interpreter flags that allow
    # arbitrary code execution despite shell=False (C-RT-2).
    # python3 -c "..." / node -e "..." allow running arbitrary code
    # pip install / npm install download and execute arbitrary packages
    # git clone can exfiltrate data to remote repos
    # v1.2.1-fix (review §4.6): now sourced from CommandPolicy so the
    # user can EXTEND the dangerous-flag map per-project (e.g. block
    # ``docker rm``) without editing source.
    try:
        from .command_policy import get_global_policy as _gpf
        policy = _gpf(project_root)
        dangerous_flags = policy.dangerous_flags.get(base_cmd, frozenset())
    except Exception:
        dangerous_flags = _DANGEROUS_FLAGS_FALLBACK.get(base_cmd, frozenset())
    if dangerous_flags:
        for arg in args[1:]:
            if arg in dangerous_flags:
                logger.warning(
                    "[security] Dangerous flag %r for %r blocked: %s",
                    arg, base_cmd, command,
                )
                return [], False

    return args, True


# v1.2.1-fix (review §4.6): fallback used only if the CommandPolicy
# module fails to resolve. Identical to the original v1.2.0
# _DANGEROUS_FLAGS dict.
_DANGEROUS_FLAGS_FALLBACK = {
    "python3": frozenset({"-c", "-m"}),
    "python":  frozenset({"-c", "-m"}),
    "node":    frozenset({"-e", "--eval"}),
    "pip":     frozenset({"install", "uninstall"}),
    "npm":     frozenset({"install", "uninstall", "run"}),
    "git":     frozenset({"clone", "push", "pull", "fetch", "remote"}),
}
# Keep the original name as an alias for any external callers that
# imported it (smoke_tests.py etc.).
_DANGEROUS_FLAGS = {
    "python3": {"-c", "-m"},
    "python": {"-c", "-m"},
    "node": {"-e", "--eval"},
    "pip": {"install", "uninstall"},
    "npm": {"install", "uninstall", "run"},
    "git": {"clone", "push", "pull", "fetch", "remote"},
}


# ── Enums & Dataclasses ──────────────────────────────────────────────────

