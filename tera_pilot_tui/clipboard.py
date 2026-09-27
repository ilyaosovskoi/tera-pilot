"""clipboard.py — system clipboard copy for the TUI.

Textual's built-in ``App.copy_to_clipboard()`` only emits an OSC52
escape sequence, which macOS Terminal.app ignores (and many remote
SSH sessions swallow). So pasting INTO the composer works (bracketed
paste / Ctrl+V are handled by TextArea), but copying the assistant's
answer OUT left users with nothing.

``copy_text()`` tries the platform tool first and falls back to
OSC52 through the running app:

  macOS        → pbcopy
  Windows/WSL  → clip.exe
  Wayland      → wl-copy
  X11          → xclip, then xsel

Never raises — returns ``(ok, method)`` so the caller can confirm
("Copied 1.2k chars via pbcopy") or say why it failed. Never logs
or prints the text itself (it may contain secrets).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Optional, Tuple


def _run_tool(cmd: list[str], text: str, timeout: float = 5.0) -> bool:
    """Pipe *text* into *cmd*. False on any failure (missing tool included)."""
    try:
        if not shutil.which(cmd[0]):
            return False
        proc = subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return proc.returncode == 0
    except Exception:
        return False


def copy_text(text: str, app: Optional[Any] = None) -> Tuple[bool, str]:
    """Copy *text* to the system clipboard.

    Returns ``(True, method)`` on success where method is one of
    ``pbcopy/clip/wl-copy/xclip/xsel/osc52``, else ``(False, "unavailable")``.
    Empty text is a no-op returning ``(False, "empty")``.
    """
    if not text:
        return False, "empty"
    import sys
    if sys.platform == "darwin":
        if _run_tool(["pbcopy"], text):
            return True, "pbcopy"
    elif sys.platform in ("win32", "cygwin"):
        if _run_tool(["clip"], text):
            return True, "clip"
    else:
        for tool in ("wl-copy", "xclip", "xsel"):
            if tool == "xclip":
                if _run_tool(["xclip", "-selection", "clipboard"], text):
                    return True, "xclip"
            elif _run_tool([tool], text):
                return True, tool
    # Last resort: OSC52 through the running Textual app. Works in
    # iTerm2/WezTerm/foot (with clipboard-write allowed), silently
    # ignored elsewhere — still better than nothing.
    if app is not None:
        try:
            app.copy_to_clipboard(text)
            return True, "osc52"
        except Exception:
            pass
    return False, "unavailable"
