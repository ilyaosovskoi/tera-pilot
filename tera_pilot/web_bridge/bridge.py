"""
tera_pilot.web_bridge.bridge — Qt-free shim.

v2.2.0: the legacy ``TeraPilotBridge`` QObject (a 4400-line PySide6 /
QWebChannel adapter that exposed Python methods to the in-process
HTML frontend) has been removed. The browser now talks to the
backend exclusively via the HTTP REST API + SSE in
:mod:`tera_pilot.api_server` (served by :mod:`tera_pilot.web_server`).

This module is kept as a marker so any external code or docs that
still reference ``tera_pilot.web_bridge.bridge.TeraPilotBridge`` get a clear
error instead of a silent ImportError.

For the TUI bridge (plain Python, no Qt, still supported) see
:mod:`tera_pilot_tui.bridge`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TeraPilotBridgeRemovedError(RuntimeError):
    """Raised when the legacy Qt TeraPilotBridge is constructed."""


class TeraPilotBridge:  # noqa: N801 - keep the legacy name
    """Legacy Qt bridge — now a hard error.

    The PySide6 ``TeraPilotBridge`` QObject was removed in v2.2.0. Use
    one of these instead:

    * For an in-process Python bridge (e.g. tests, scripts, TUI):
      ``tera_pilot_tui.bridge.TeraPilotBridge`` (plain Python, no Qt).
    * For the browser GUI: the HTTP API at ``/api/*`` served by
      :class:`tera_pilot.api_server.TeraPilotAPIServer` /
      :class:`tera_pilot.web_server.TeraPilotWebServer`.
    """

    def __init__(self, *args, **kwargs):
        raise TeraPilotBridgeRemovedError(
            "tera_pilot.web_bridge.bridge.TeraPilotBridge was removed in v2.2.0 — "
            "the Qt / PySide6 GUI is no longer maintained. Use "
            "`tera_pilot_tui.bridge.TeraPilotBridge` for an in-process Python bridge, "
            "or `TeraPilotWebServer` / `TeraPilotAPIServer` for the HTTP API the "
            "browser frontend consumes."
        )


__all__ = ["TeraPilotBridge", "TeraPilotBridgeRemovedError"]
