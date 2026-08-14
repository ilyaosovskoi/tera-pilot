"""Pure-Python fallback for the sandbox module.

This is a *very* weak substitute for the Rust sandbox — it cannot enforce
kernel-level restrictions. It only records the requested profile and
provides `path_would_be_writable` for advisory checks.

For real safety, install the Rust extension: `maturin develop` from
`tera-pilot-native/pyo3`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

PROFILE_OFF = "off"
PROFILE_WORKSPACE = "workspace"
PROFILE_READ_ONLY = "read-only"
PROFILE_STRICT = "strict"

_VALID_PROFILES = {PROFILE_OFF, PROFILE_WORKSPACE, PROFILE_READ_ONLY, PROFILE_STRICT}

_APPLIED_PROFILE: Optional[str] = None
_APPLIED_CONFIG: Optional[dict] = None


def apply_profile(
    profile: str,
    workspace_root: Optional[str] = None,
    allowed_egress: Optional[List[str]] = None,
    extra_readonly_paths: Optional[List[str]] = None,
    extra_readwrite_paths: Optional[List[str]] = None,
) -> None:
    """Record the requested profile. Does NOT enforce anything at the kernel level."""
    global _APPLIED_PROFILE, _APPLIED_CONFIG
    if profile not in _VALID_PROFILES:
        raise ValueError(f"invalid sandbox profile: {profile!r}")
    if _APPLIED_PROFILE is not None and _APPLIED_PROFILE != PROFILE_OFF:
        raise RuntimeError(
            f"sandbox already applied (profile={_APPLIED_PROFILE!r}); restrictions are irreversible"
        )
    _APPLIED_PROFILE = profile
    _APPLIED_CONFIG = {
        "workspace_root": workspace_root,
        "allowed_egress": allowed_egress or [],
        "extra_readonly_paths": extra_readonly_paths or [],
        "extra_readwrite_paths": extra_readwrite_paths or [],
    }
    if profile == PROFILE_OFF:
        logger.info("sandbox profile is 'off'; not enforcing")
    else:
        logger.warning(
            "Pure-Python sandbox fallback: profile=%r recorded but NOT kernel-enforced. "
            "Install tera_pilot_native for real Landlock/Seatbelt protection.",
            profile,
        )


def current_profile() -> Optional[str]:
    return _APPLIED_PROFILE


def describe_state() -> str:
    if _APPLIED_PROFILE is None:
        return "not applied"
    return f"applied (profile={_APPLIED_PROFILE})"


def path_would_be_writable(
    profile: str,
    workspace_root: Optional[str],
    path: str,
    extra_readwrite_paths: Optional[List[str]] = None,
) -> bool:
    if profile in (PROFILE_READ_ONLY, PROFILE_STRICT):
        # Only paths explicitly listed as read-write are writable.
        for p in (extra_readwrite_paths or []):
            try:
                if Path(path).resolve().is_relative_to(Path(p).resolve()):
                    return True
            except (ValueError, OSError):
                continue
        return False
    if workspace_root:
        try:
            if Path(path).resolve().is_relative_to(Path(workspace_root).resolve()):
                return True
        except (ValueError, OSError):
            pass
    for p in (extra_readwrite_paths or []):
        try:
            if Path(path).resolve().is_relative_to(Path(p).resolve()):
                return True
        except (ValueError, OSError):
            continue
    return False


def supported_platform() -> bool:
    """Fallback provides NO kernel-level enforcement — it is a pure no-op.

    Return False so callers do not mistake the fallback for a platform that
    actually enforces sandboxing. Real enforcement requires the native
    (Landlock/Seatbelt) backend.
    """
    return False
