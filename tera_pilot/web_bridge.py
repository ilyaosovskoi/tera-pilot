"""
Legacy shim for tera_pilot.web_bridge.

v2.2.0: the original 4126-line monolith (and the later refactored
``tera_pilot/web_bridge/`` package) exposed a PySide6 ``TeraPilotBridge``
QObject to the in-process HTML frontend via QWebChannel. The Qt
GUI has been removed; the browser now talks to the backend via
the HTTP REST API + SSE in :mod:`tera_pilot.api_server`.

This file re-exports only the path/config helpers that
:mod:`tera_pilot.api_server` and the TUI backend still depend on::

    from tera_pilot.web_bridge import _load_config
    from tera_pilot.web_bridge import _chat_path, _load_chat, _save_chat

The Qt-only names (``TeraPilotBridge``, ``GenerationWorker``,
``OneShotWorker``, ``TitleWorker``) are kept as hard-error shims
so old code fails loudly instead of silently.
"""

from tera_pilot.web_bridge import (
    _tera_pilot_home, _config_path, _chats_dir,
    _load_templates_from_disk, _load_skills_from_disk,
    _classify_user_intent,
    _load_config, _save_config,
    _chat_path, _load_chat, _save_chat,
)
from tera_pilot.web_bridge.bridge import TeraPilotBridge, TeraPilotBridgeRemovedError

__all__ = [
    "TeraPilotBridge", "TeraPilotBridgeRemovedError",
    "_tera_pilot_home", "_config_path", "_chats_dir",
    "_load_templates_from_disk", "_load_skills_from_disk",
    "_classify_user_intent",
    "_load_config", "_save_config",
    "_chat_path", "_load_chat", "_save_chat",
]
