"""Sandbox — thin Python wrapper around `tera_pilot_native.sandbox` (Rust) or pure-Python fallback.

The sandbox applies OS-level kernel restrictions (Landlock on Linux, Seatbelt
on macOS) to the entire Tera Pilot process at startup. Once applied, restrictions
are **irreversible** — the model cannot convince the agent to relax them
at runtime.

Profiles:
- "off"        — no restrictions (default in dev)
- "workspace"  — read/write only inside the workspace root; deny network egress except to LLM/MCP
- "read-only"  — read-only filesystem everywhere
- "strict"     — read-only filesystem everywhere (writes only to explicit
                 extra_readwrite_paths) + all network egress blocked. NOT
                 workspace-writable; identical fs policy to "read-only".

Usage:
    from tera_pilot.agent.sandbox import apply_sandbox, current_sandbox_profile

    apply_sandbox(
        profile="workspace",
        workspace_root="/path/to/project",
        extra_readwrite_paths=["~/.tera_pilot"],  # for memory log
        allowed_egress=["api.openai.com:443"],
    )
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

from .native import get_sandbox, NATIVE_AVAILABLE

logger = logging.getLogger(__name__)


class SandboxProfile:
    OFF = "off"
    WORKSPACE = "workspace"
    READ_ONLY = "read-only"
    STRICT = "strict"


def apply_sandbox(
    profile: str,
    workspace_root: Optional[str] = None,
    allowed_egress: Optional[List[str]] = None,
    extra_readonly_paths: Optional[List[str]] = None,
    extra_readwrite_paths: Optional[List[str]] = None,
) -> None:
    """Apply the sandbox profile. **Irreversible.**

    Args:
        profile: one of SandboxProfile constants.
        workspace_root: required for "workspace" profile.
        allowed_egress: list of "host:port" strings; empty = all blocked.
        extra_readonly_paths: extra read-only paths (e.g. system Python stdlib).
        extra_readwrite_paths: extra read-write paths (e.g. ~/.tera_pilot for memory log).

    Raises:
        RuntimeError: if already applied.
        OSError: on platform-specific failure.
    """
    # Expand ~ in paths.
    if workspace_root:
        workspace_root = os.path.expanduser(workspace_root)
    extra_ro = [os.path.expanduser(p) for p in (extra_readonly_paths or [])]
    extra_rw = [os.path.expanduser(p) for p in (extra_readwrite_paths or [])]

    sb = get_sandbox()
    if not NATIVE_AVAILABLE:
        logger.warning(
            "Pure-Python sandbox fallback active — no kernel-level enforcement. "
            "Build tera_pilot_native (maturin develop --manifest-path tera-pilot-native/pyo3/Cargo.toml) "
            "for real Landlock/Seatbelt protection."
        )
    sb.apply_profile(
        profile=profile,
        workspace_root=workspace_root,
        allowed_egress=allowed_egress,
        extra_readonly_paths=extra_ro,
        extra_readwrite_paths=extra_rw,
    )


def current_sandbox_profile() -> Optional[str]:
    """Return the applied profile name, or None if sandbox is not applied."""
    return get_sandbox().current_profile()


def describe_state() -> str:
    return get_sandbox().describe_state()


def path_would_be_writable(
    profile: str,
    workspace_root: Optional[str],
    path: str,
    extra_readwrite_paths: Optional[List[str]] = None,
) -> bool:
    """Advisory: would `path` be writable under the given profile?"""
    return bool(
        get_sandbox().path_would_be_writable(
            profile=profile,
            workspace_root=workspace_root,
            path=path,
            extra_readwrite_paths=extra_readwrite_paths,
        )
    )


def supported_platform() -> bool:
    """True iff this platform supports kernel-level sandbox enforcement."""
    return bool(get_sandbox().supported_platform())
