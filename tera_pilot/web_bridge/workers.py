"""
tera_pilot.web_bridge.workers — Qt-free threading workers.

v2.2.0: the legacy PySide6 QThread workers (GenerationWorker,
OneShotWorker, TitleWorker) have been removed. The HTTP API server
in :mod:`tera_pilot.api_server` runs all generation in plain
``threading.Thread`` instances and streams results to the browser
via Server-Sent Events — there is no longer a Qt event loop to
drive QThreads against.

These thin shims are kept so any external code that still imports
the old names doesn't crash on ``ImportError``. They raise a clear
``RuntimeError`` if instantiated, pointing the user at the new
HTTP API path.

For new code, use the ``/api/chat/stream`` and ``/api/chat/oneshot``
endpoints on :class:`tera_pilot.api_server.TeraPilotAPIServer` directly.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _RemovedQtWorker:
    """Base class for the legacy QThread workers — now hard errors."""

    _legacy_name: str = "QtWorker"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            f"{self._legacy_name} was removed in Tera Pilot v2.2.0 — the Qt / "
            "PySide6 GUI is no longer maintained. Use the HTTP API at "
            "`/api/chat/stream` (or `/api/chat/oneshot`) on TeraPilotAPIServer "
            "instead. See tera_pilot/api_server.py and tera_pilot/web_server.py."
        )


class GenerationWorker(_RemovedQtWorker):
    """Legacy QThread that streamed tokens from a provider.

    Removed in v2.2.0 — use ``POST /api/chat/stream`` (SSE) instead.
    """
    _legacy_name = "GenerationWorker"


class OneShotWorker(_RemovedQtWorker):
    """Legacy QThread that ran a single non-streaming generation.

    Removed in v2.2.0 — use ``POST /api/chat/oneshot`` instead.
    """
    _legacy_name = "OneShotWorker"


class TitleWorker(_RemovedQtWorker):
    """Legacy QThread that generated a chat title.

    Removed in v2.2.0 — title generation is now triggered inline by
    the chat-stream handler in :mod:`tera_pilot.api_server`.
    """
    _legacy_name = "TitleWorker"


__all__ = ["GenerationWorker", "OneShotWorker", "TitleWorker"]
