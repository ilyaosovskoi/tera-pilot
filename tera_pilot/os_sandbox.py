"""OS-level sandbox for agent code/command execution (P1.10).

Wraps the argv that `ToolEngine` hands to ``subprocess.Popen`` for
``execute_command`` / ``run_code`` / detected test-lint commands so that
untrusted code the agent runs cannot:

- open network connections (exfiltration to an attacker's server);
- write outside the workspace (plus the OS temp dirs);
- read sensitive paths (~/.ssh, ~/.aws, ~/.gnupg, cloud SDK configs).

Backends (detected at runtime):

- **macOS** — ``/usr/bin/sandbox-exec`` with a generated Seatbelt profile.
  ``(deny default)`` + targeted allows: reads everywhere, writes only to
  the workspace + ``/tmp`` + ``/private/tmp``, ``(deny network*)``, and
  sensitive-path read denials.
- **Linux** — ``bwrap`` (bubblewrap): ``--ro-bind / /`` (root read-only),
  workspace bound read-write, ``--tmpfs /tmp``, ``--unshare-net`` for
  network isolation, sensitive dirs remapped to ``/dev/null`` when they
  exist.

This is an EXTRA layer on top of the path sandbox, command policy and
the confirmation gates — not a replacement for them. When the backend is
unavailable and the mode is ``on``, the caller must refuse to run
(fail closed); ``auto`` runs unwrapped with a loud log.

Configuration: ``ToolEngine.os_sandbox`` (``off`` | ``auto`` | ``on``),
set from ``~/.tera_pilot/config.json`` key ``agent_os_sandbox`` or the
``TERA_PILOT_OS_SANDBOX`` environment variable.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

VALID_MODES = ("off", "auto", "on")

#: Sensitive paths never readable from inside the sandbox (best-effort —
#: entries that do not exist are skipped so a missing dir doesn't break
#: the profile or bwrap invocation).
SENSITIVE_SUBPATHS = (
    ".ssh", ".aws", ".gnupg", ".config/gcloud", ".config/gh",
    ".kube", ".docker", ".netrc", ".git-credentials",
    "Library/Keychains", "Library/Application Support/Google/Chrome",
)

#: Candidate seatbelt deny targets (system-level).
_SYSTEM_SENSITIVE = (
    "/etc/ssh", "/root/.ssh", "/var/root/.ssh",
)


def sanitize_mode(mode: Optional[str]) -> str:
    """Normalize a mode string to one of off|auto|on (default: auto)."""
    if mode is None:
        return os.environ.get("TERA_PILOT_OS_SANDBOX", "auto")
    m = str(mode).strip().lower()
    if m in VALID_MODES:
        return m
    logger.warning("[os_sandbox] unknown mode %r — falling back to 'auto'", mode)
    return "auto"


def detect_backend() -> Optional[str]:
    """Return the sandbox backend available on this machine, or None.

    macOS: ``/usr/bin/sandbox-exec``. Linux: ``bwrap`` (bubblewrap) — the
    closest stdlib-adjacent tool for user-namespace isolation. Windows is
    not supported (the path sandbox + confirmations still apply).
    """
    if shutil.which("sandbox-exec"):
        return "macos_sandbox_exec"
    if shutil.which("bwrap"):
        return "linux_bwrap"
    return None


def _quote_sb_path(path: str) -> str:
    """Quote a path for embedding in a Seatbelt profile string."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_seatbelt_profile(workspace: str, home: Optional[str] = None) -> str:
    """Generate a macOS Seatbelt profile string for the given workspace.

    ``workspace`` is realpath()ed so symlinked paths (e.g. ``/tmp`` →
    ``/private/tmp``) resolve to the paths the kernel actually enforces.

    The profile:
    - denies everything by default;
    - allows process/sysctl/mach plumbing and file reads everywhere;
    - allows file WRITES only under the workspace, ``/tmp`` and
      ``/private/tmp``;
    - denies reads under sensitive paths (~/.ssh, ~/.aws, ...);
    - denies all network access.
    """
    ws = os.path.realpath(workspace)
    home = os.path.realpath(home or os.path.expanduser("~"))
    # The system temp dir — on macOS this is per-user
    # (/var/folders/.../T), NOT /tmp (a symlink to /private/tmp).
    # tempfile.TemporaryDirectory() lives there, so run_code writes to
    # its temp workspace would be denied without this allowance.
    sys_tmp = os.path.realpath(tempfile.gettempdir())
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow sysctl-read)",
        "(allow mach-lookup",
        '    (global-name "com.apple.system.opendirectoryd")',
        '    (global-name "com.apple.system.logger")',
        '    (global-name "com.apple.coresymbolicationd"))',
        "(allow file-read*)",
        # git needs to open /dev/null for reading AND writing during
        # commit/merge plumbing — without the write allow, `git commit`
        # fails with "could not open '/dev/null'... Operation not
        # permitted". The literal allow is scoped to /dev/null only, not
        # the rest of /dev.
        '(allow file-write* (literal "/dev/null"))',
        "(allow file-write*",
        f"    (subpath {_quote_sb_path(ws)})",
        '    (subpath "/tmp")',
        '    (subpath "/private/tmp")',
        f"    (subpath {_quote_sb_path(sys_tmp)}))",
    ]
    denied = list(_SYSTEM_SENSITIVE)
    for rel in SENSITIVE_SUBPATHS:
        denied.append(os.path.join(home, rel))
    existing = [p for p in denied if os.path.isdir(p) or os.path.isfile(p)]
    if existing:
        lines.append("(deny file-read*")
        for p in existing:
            lines.append(f"    (subpath {_quote_sb_path(p)})")
        lines.append(")")
    lines.append("(deny network*)")
    return "\n".join(lines)


def wrap_macos(args: List[str], workspace: str, home: Optional[str] = None) -> List[str]:
    """Wrap argv with ``sandbox-exec`` + a generated Seatbelt profile.

    The profile is written to a temp file (read by sandbox-exec in the
    host before the sandbox applies).
    """
    profile = build_seatbelt_profile(workspace, home=home)
    fd, prof_path = tempfile.mkstemp(prefix="tera_pilot_sb_", suffix=".sb")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(profile)
    except Exception:
        try:
            os.unlink(prof_path)
        except OSError:
            pass
        raise
    # The profile file must outlive this function (sandbox-exec reads it
    # at launch). Best-effort cleanup in a background thread is not worth
    # it — leave it for the OS temp dir to reap.
    return ["/usr/bin/sandbox-exec", "-f", prof_path, "--", *args]


def build_bwrap_args(args: List[str], workspace: str, home: Optional[str] = None) -> List[str]:
    """Build a bubblewrap argv for Linux (pure, unit-testable)."""
    ws = os.path.realpath(workspace)
    home = os.path.realpath(home or os.path.expanduser("~"))
    bwrap = shutil.which("bwrap") or "bwrap"
    wrapped = [
        bwrap,
        "--die-with-parent", "--new-session",
        "--unshare-net", "--unshare-ipc",
        "--ro-bind", "/", "/",
        "--dev", "/dev", "--proc", "/proc",
        "--bind", ws, ws,
        "--tmpfs", "/tmp",
    ]
    # Deny reads of sensitive dirs by remapping them to /dev/null
    # (only when they exist — bwrap requires existing mount points).
    for rel in SENSITIVE_SUBPATHS:
        target = os.path.join(home, rel)
        if os.path.isdir(target):
            wrapped += ["--bind", "/dev/null", target]
    for sys_path in _SYSTEM_SENSITIVE:
        if os.path.isdir(sys_path):
            wrapped += ["--bind", "/dev/null", sys_path]
    wrapped += ["--", *args]
    return wrapped


def wrap_command(
    args: List[str],
    workspace: Optional[str],
    mode: str = "auto",
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Wrap ``args`` in the OS-level sandbox per ``mode``.

    Returns ``(wrapped_argv, backend)`` where ``wrapped_argv`` is None
    when the sandbox was REQUIRED (mode ``on``) but no backend is
    available — the caller must then refuse to run (fail closed).

    ``auto`` with no backend runs unwrapped (a loud warning is logged);
    ``off`` never wraps.
    """
    mode = sanitize_mode(mode)
    if mode == "off":
        return list(args), None
    backend = detect_backend()
    if backend is None:
        if mode == "on":
            logger.error(
                "[os_sandbox] mode=on but no OS sandbox backend available "
                "(need sandbox-exec on macOS or bwrap on Linux) — refusing to run unsandboxed"
            )
            return None, None
        logger.warning(
            "[os_sandbox] mode=auto but no OS sandbox backend available — "
            "running unsandboxed (path sandbox + confirmations still apply)"
        )
        return list(args), None
    if not workspace:
        logger.warning("[os_sandbox] no workspace set — cannot build a write-restricted profile")
        return list(args), backend
    try:
        if backend == "macos_sandbox_exec":
            return wrap_macos(list(args), workspace), backend
        if backend == "linux_bwrap":
            return build_bwrap_args(list(args), workspace), backend
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("[os_sandbox] failed to build sandbox wrapper: %s", exc)
        if mode == "on":
            return None, backend
    return list(args), backend
