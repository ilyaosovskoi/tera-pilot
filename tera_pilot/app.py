"""
Tera Pilot v2.2.0 — Application Entry Point (Web UI).

The legacy PySide6 / QWebEngineView desktop GUI has been removed
in favour of a plain HTTP server that serves the same HTML/CSS/JS
frontend directly to the user's browser.

This module is kept as a thin shim for backward compatibility —
existing scripts / entry points that called ``tera_pilot.app.main()``
still work, they just boot the web server instead of a Qt window.

Run::

    python -m tera_pilot                  # http://127.0.0.1:18732
    python -m tera_pilot --port 8000
    python -m tera_pilot --no-browser

All UI logic lives in ``tera_pilot/web/index.html`` + ``tera_pilot/web/app.js``;
the backend lives in ``tera_pilot/api_server.py`` and ``tera_pilot/providers/``.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def main() -> None:
    """Tera Pilot v2.2.0 entry point — boots the Web UI server."""
    # Late import so ``import tera_pilot.app`` doesn't drag the whole HTTP
    # stack into modules that just want the package metadata.
    from .web_server import main as _web_main
    _web_main()


if __name__ == "__main__":
    main()
