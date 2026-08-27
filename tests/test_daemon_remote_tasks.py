"""Tests for the daemon remote-task-mode integration.

``tera-pilot-daemon serve --inbound telegram`` reads
~/.tera_pilot/inbound.json and wires the inbound messenger listener to
the task queue: an accepted message becomes a task, a reply of STOP
cancels the running task. These tests cover the config plumbing in
``tera_pilot.daemon`` (load/save + the listener wiring) without hitting
the real Telegram API.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.daemon import (  # noqa: E402
    _inbound_config_path,
    load_inbound_config,
    save_inbound_config,
    TeraPilotDaemon,
)


def test_inbound_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = {
        "backend": "telegram",
        "telegram_token": "123456:ABC",
        "allowed_chat_ids": ["111", "222"],
        "workspace": "/tmp/proj",
    }
    save_inbound_config(cfg)
    assert load_inbound_config() == cfg
    assert os.path.exists(_inbound_config_path())


def test_load_inbound_config_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_inbound_config() == {}


def test_enable_inbound_wires_listener_to_task_queue(tmp_path, monkeypatch):
    """--inbound telegram must start a listener whose messages land in
    the daemon's task queue and whose STOP cancels the running task.

    We exercise the callback plumbing directly (same functions the
    daemon wires up) to avoid real network / Telegram calls.
    """
    from tera_pilot.inbound_listener import (
        InboundMessage,
        make_daemon_callback,
        make_daemon_stop_callback,
    )
    from tera_pilot.daemon import TaskQueue, TaskState

    monkeypatch.setenv("HOME", str(tmp_path))
    q = TaskQueue()

    on_message = make_daemon_callback(q, workspace=str(tmp_path))
    on_stop = make_daemon_stop_callback(q)

    msg = InboundMessage(
        backend="telegram", chat_id="1", sender_id="2",
        sender_name="bob", text="fix the auth bug",
    )
    on_message(msg)
    tasks = q.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["prompt"] == "fix the auth bug"
    assert tasks[0]["state"] == "pending"

    # STOP cancels the running task
    task = q.get_task(tasks[0]["id"])
    task.state = TaskState.RUNNING
    on_stop(InboundMessage(
        backend="telegram", chat_id="1", sender_id="2",
        sender_name="bob", text="STOP",
    ))
    assert q.get_task(tasks[0]["id"]).state == TaskState.CANCELLED


def test_enable_inbound_missing_config_disables_gracefully(tmp_path, monkeypatch, capsys):
    """With no ~/.tera_pilot/inbound.json, --inbound must print a hint
    and keep the daemon running (no crash)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    daemon = TeraPilotDaemon(host="127.0.0.1", port=0, auth_token="t", inbound="telegram")
    daemon._enable_inbound("telegram")
    out = capsys.readouterr().out
    assert "inbound.json" in out
    assert daemon._inbound_listener is None


def test_enable_inbound_uses_saved_config(tmp_path, monkeypatch):
    """With a valid config, _enable_inbound must start a listener."""
    monkeypatch.setenv("HOME", str(tmp_path))
    save_inbound_config({
        "backend": "telegram",
        "telegram_token": "123456:ABC",
        "allowed_chat_ids": ["111"],
        "workspace": str(tmp_path),
    })
    daemon = TeraPilotDaemon(host="127.0.0.1", port=0, auth_token="t", inbound="telegram")
    daemon._enable_inbound("telegram")
    try:
        assert daemon._inbound_listener is not None
        assert daemon._inbound_listener.is_running()
    finally:
        if daemon._inbound_listener is not None:
            daemon._inbound_listener.stop()
