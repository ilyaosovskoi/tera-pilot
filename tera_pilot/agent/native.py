"""Loader for the Rust extension module `tera_pilot_native`.

Design:
- If `tera_pilot_native` is installed (via maturin build of `tera-pilot-native/pyo3`),
  use it. All hot paths (sandbox, compaction, circuit breaker, interjection)
  run in native Rust.
- If not installed, fall back to pure-Python implementations of the same
  interfaces (in `tera_pilot.agent._fallback_*`). Performance will be lower for
  high-throughput MCP / multi-provider scenarios, but everything still
  works.

This module is the *only* place that knows about the Rust extension. All
other modules import from here.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Set TERA_PILOT_DISABLE_NATIVE=1 to force pure-Python mode (for debugging / CI).
_FORCE_PURE = os.environ.get("TERA_PILOT_DISABLE_NATIVE", "") == "1"

_NATIVE: Optional[Any] = None
_NATIVE_LOAD_ATTEMPTED = False
_NATIVE_VERSION: Optional[str] = None


def _try_load_native() -> Optional[Any]:
    """Attempt to import the `tera_pilot_native` extension module.

    Returns the module object if successful, None otherwise.
    Never raises — callers rely on this to be total.
    """
    global _NATIVE_LOAD_ATTEMPTED, _NATIVE, _NATIVE_VERSION
    if _NATIVE_LOAD_ATTEMPTED:
        return _NATIVE
    _NATIVE_LOAD_ATTEMPTED = True

    if _FORCE_PURE:
        logger.info("TERA_PILOT_DISABLE_NATIVE=1 — using pure-Python fallback for all subsystems")
        return None

    try:
        mod = importlib.import_module("tera_pilot_native")
        _NATIVE = mod
        _NATIVE_VERSION = getattr(mod, "__version__", None)
        logger.info(
            "tera_pilot_native extension loaded (version=%s) — sandbox/circuit_breaker/interjection/compaction will use Rust",
            _NATIVE_VERSION,
        )
        return mod
    except ImportError as e:
        logger.info(
            "tera_pilot_native extension not available (%s); falling back to pure Python. "
            "To enable native acceleration, build with `maturin develop --manifest-path tera-pilot-native/pyo3/Cargo.toml`.",
            e,
        )
        return None
    except Exception as e:
        # Don't let a broken native module crash the entire agent.
        logger.warning(
            "tera_pilot_native extension failed to load (%s); using pure-Python fallback",
            e,
        )
        return None


def get_native_module() -> Optional[Any]:
    """Return the `tera_pilot_native` module, or None if not available."""
    if not _NATIVE_LOAD_ATTEMPTED:
        return _try_load_native()
    return _NATIVE


def native_version() -> Optional[str]:
    """Return the version string of the loaded native extension, or None."""
    get_native_module()
    return _NATIVE_VERSION


def _is_native_available() -> bool:
    return get_native_module() is not None


# Bind at module load so `from tera_pilot.agent.native import NATIVE_AVAILABLE`
# gets a bool, not a function descriptor. We do *not* support hot-reloading
# of the native module mid-session.
NATIVE_AVAILABLE = _is_native_available()


def get_sandbox():
    """Return the sandbox submodule of tera_pilot_native, or the pure-Python fallback."""
    native = get_native_module()
    if native is not None and hasattr(native, "sandbox"):
        return native.sandbox
    from . import _fallback_sandbox
    return _fallback_sandbox


def get_circuit_breaker():
    native = get_native_module()
    if native is not None and hasattr(native, "circuit_breaker"):
        return native.circuit_breaker
    from . import _fallback_circuit_breaker
    return _fallback_circuit_breaker


def get_interjection():
    native = get_native_module()
    if native is not None and hasattr(native, "interjection"):
        return native.interjection
    from . import _fallback_interjection
    return _fallback_interjection


def get_compaction():
    native = get_native_module()
    if native is not None and hasattr(native, "compaction"):
        return native.compaction
    from . import _fallback_compaction
    return _fallback_compaction


def get_actor():
    native = get_native_module()
    if native is not None and hasattr(native, "actor"):
        return native.actor
    from . import _fallback_actor
    return _fallback_actor


# Eagerly probe once at import time so the logger line fires early.
_ = NATIVE_AVAILABLE
