"""
Tera Pilot v2.2.0 — legacy Qt main window stub.

The PySide6 / QWebEngineView desktop window was removed in v2.2.0.
The GUI is now served by :mod:`tera_pilot.web_server` as a plain HTTP
server — point a browser at the printed URL.

This file is kept ONLY as a marker so that any external code or
docs that still reference ``TeraPilotMainWindow`` get a clear, actionable
error message instead of a silent ImportError.

If you actually need the legacy Qt window, you can find it in the
v2.1.0 git history — but it is no longer maintained.
"""

from __future__ import annotations


class TeraPilotMainWindowRemovedError(RuntimeError):
    """Raised when legacy Qt window code is invoked."""


def TeraPilotMainWindow(*args, **kwargs):  # noqa: N802 - keep the legacy name
    """Legacy constructor — now a hard error.

    The PySide6 / QWebEngineView desktop GUI was removed in v2.2.0.
    Use ``tera_pilot.web_server.main()`` (or ``python -m tera_pilot``) to start
    the Web UI server instead.
    """
    raise TeraPilotMainWindowRemovedError(
        "TeraPilotMainWindow was removed in v2.2.0 — the Qt / PySide6 GUI is no "
        "longer maintained. Run `python -m tera_pilot` to start the Web UI server, "
        "then open the printed URL in your browser."
    )


__all__ = ["TeraPilotMainWindow", "TeraPilotMainWindowRemovedError"]
