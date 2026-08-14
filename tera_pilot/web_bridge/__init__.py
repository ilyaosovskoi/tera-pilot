"""
tera_pilot.web_bridge — Qt-free path/config helpers.

v2.2.0: the legacy ``TeraPilotBridge`` QObject (PySide6 / QWebChannel
adapter) has been removed. The browser frontend now talks to the
backend exclusively through the HTTP REST API + SSE in
:mod:`tera_pilot.api_server` (served by :mod:`tera_pilot.web_server`).

The path / config / chat-store helpers in ``_paths_config`` are kept
because ``tera_pilot.api_server`` and the TUI backend import them. The Qt
QThread workers (``workers.py``) are removed — :mod:`tera_pilot.api_server`
uses :mod:`threading` directly for streaming.

Public API kept for backward compatibility:

    from tera_pilot.web_bridge import (
        _tera_pilot_home, _config_path, _chats_dir,
        _load_templates_from_disk, _load_skills_from_disk,
        _classify_user_intent,
        _load_config, _save_config,
        _chat_path, _load_chat, _save_chat,
    )

Removed (was Qt-only):

    TeraPilotBridge, GenerationWorker, OneShotWorker, TitleWorker
"""

from ._paths_config import (
    _tera_pilot_home, _config_path, _chats_dir,
    _load_templates_from_disk, _load_skills_from_disk,
    _classify_user_intent,
    _load_config, _save_config,
    _chat_path, _load_chat, _save_chat,
)

__all__ = [
    "_tera_pilot_home", "_config_path", "_chats_dir",
    "_load_templates_from_disk", "_load_skills_from_disk",
    "_classify_user_intent",
    "_load_config", "_save_config",
    "_chat_path", "_load_chat", "_save_chat",
]
