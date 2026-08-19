"""Regression tests for the GUI undo endpoint (v2.3.4-fix).

``_agent_undo`` (/api/agent/undo) used to construct a brand-new
``CheckpointManager(session_id="gui")`` per request. That manager starts
with an EMPTY in-memory checkpoint list and never loads from disk, so
``rewind(1)`` always returned "No checkpoints available" — the Undo
button in the GUI could never undo anything. It now uses the same
process-wide singleton the checkpoint API endpoints use
(``get_checkpoint_manager(session_id="default")``) and re-syncs its
workspace to the current project root.

These tests exercise the endpoint over a real HTTP server with a real
checkpoint created through the same singleton, and assert files are
actually restored (and files created after the checkpoint are deleted).
"""

import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tera_pilot.checkpoint as cp  # noqa: E402
from tera_pilot.api_server import TeraPilotAPIServer  # noqa: E402
from tera_pilot.checkpoint import get_checkpoint_manager, reset_checkpoint_manager  # noqa: E402


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    # Isolate ~/.tera_pilot (config + checkpoint backups) so the test
    # never touches the developer's real state.
    home = tmp_path_factory.mktemp("tera_pilot_home")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        server = TeraPilotAPIServer(port=0)
        token = server.auth_token
        server.start()
        ws = tempfile.mkdtemp(prefix="tera_pilot_undo_test_")
        server.ctx.config["project_root"] = ws
        yield {"server": server, "port": server.port, "token": token, "ws": ws}
        server.stop()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


@pytest.fixture(autouse=True)
def fresh_checkpoint_manager():
    """Each test starts from a clean, empty process singleton."""
    reset_checkpoint_manager()
    yield
    reset_checkpoint_manager()


def _undo(api):
    url = f"http://127.0.0.1:{api['port']}/api/agent/undo"
    req = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={
            "Authorization": "Bearer " + api["token"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read().decode())


def _seed_checkpoint(api, touched, message_count=1):
    """Create a checkpoint through the SAME singleton the undo endpoint
    uses, so rewind(1) can find it."""
    cm = get_checkpoint_manager(session_id="default", workspace=api["ws"])
    cm.create_checkpoint(message_count=message_count, touched_files=touched)


def test_undo_restores_modified_file(api):
    """rewind(1) targets the checkpoint at index len-2 (the current state
    is always AFTER the last checkpoint), so undo needs >= 3 checkpoints
    to step back one."""
    ws = Path(api["ws"])
    f = ws / "notes.txt"
    f.write_text("v1", encoding="utf-8")
    _seed_checkpoint(api, ["notes.txt"], message_count=5)   # cp1
    f.write_text("v2", encoding="utf-8")
    _seed_checkpoint(api, ["notes.txt"], message_count=10)  # cp2
    f.write_text("v3", encoding="utf-8")
    _seed_checkpoint(api, ["notes.txt"], message_count=15)  # cp3

    st, data = _undo(api)  # back to cp2

    assert st == 200
    assert data.get("ok") is True, data
    assert f.read_text(encoding="utf-8") == "v2", "undo must restore the file"
    assert "notes.txt" in data.get("restored", [])


def test_undo_restores_latest_backup_at_or_before_target(api):
    """a.txt was last touched at cp1 (not at the target cp2) — undo must
    restore it from cp1's backup instead of deleting it as "created after"."""
    ws = Path(api["ws"])
    a, b = ws / "a.txt", ws / "b.txt"
    a.write_text("a1", encoding="utf-8")
    _seed_checkpoint(api, ["a.txt"], message_count=5)        # cp1
    b.write_text("b1", encoding="utf-8")
    _seed_checkpoint(api, ["b.txt"], message_count=10)       # cp2 (target)
    a.write_text("a2", encoding="utf-8")
    b.write_text("b2", encoding="utf-8")
    _seed_checkpoint(api, ["a.txt", "b.txt"], message_count=15)  # cp3

    st, data = _undo(api)  # back to cp2

    assert st == 200
    assert data.get("ok") is True, data
    assert a.read_text(encoding="utf-8") == "a1", "a.txt must come from cp1's backup"
    assert b.read_text(encoding="utf-8") == "b1"


def test_undo_deletes_file_created_after_target(api):
    ws = Path(api["ws"])
    a = ws / "a.txt"
    a.write_text("a1", encoding="utf-8")
    _seed_checkpoint(api, ["a.txt"], message_count=5)   # cp1
    _seed_checkpoint(api, [], message_count=10)          # cp2 (target)
    a.write_text("a2", encoding="utf-8")
    new_file = ws / "c.txt"  # created in the latest "turn"
    new_file.write_text("new", encoding="utf-8")
    _seed_checkpoint(api, ["a.txt", "c.txt"], message_count=15)  # cp3

    st, data = _undo(api)  # back to cp2

    assert st == 200
    assert data.get("ok") is True, data
    assert a.read_text(encoding="utf-8") == "a1"
    assert not new_file.exists(), "files created after the target must be removed"
    assert any("c.txt" in r for r in data.get("restored", []))


def test_undo_with_no_checkpoints_returns_structured_error(api):
    st, data = _undo(api)
    assert st == 200
    assert data.get("ok") is False
    assert "checkpoint" in str(data.get("error", "")).lower()


def test_undo_with_single_checkpoint_returns_structured_error(api):
    """rewind(1) with only one checkpoint cannot go back before turn 0 —
    it must return a structured error, not crash."""
    ws = Path(api["ws"])
    (ws / "x.txt").write_text("x", encoding="utf-8")
    _seed_checkpoint(api, ["x.txt"], message_count=5)

    st, data = _undo(api)

    assert st == 200
    assert data.get("ok") is False
    assert "rewind" in str(data.get("error", "")).lower()


def test_undo_uses_shared_singleton_across_calls(api):
    """Two undo calls must see the same checkpoint history (the endpoint
    must not create a fresh empty manager per request)."""
    ws = Path(api["ws"])
    f = ws / "counter.txt"
    f.write_text("one", encoding="utf-8")
    _seed_checkpoint(api, ["counter.txt"], message_count=5)    # cp1
    f.write_text("two", encoding="utf-8")
    _seed_checkpoint(api, ["counter.txt"], message_count=10)   # cp2
    f.write_text("three", encoding="utf-8")
    _seed_checkpoint(api, ["counter.txt"], message_count=15)   # cp3

    st, data = _undo(api)  # first undo → back to cp2 = "two"
    assert data.get("ok") is True, data
    assert f.read_text(encoding="utf-8") == "two"

    st, data = _undo(api)  # second undo → back to cp1 = "one"
    assert data.get("ok") is True, data
    assert f.read_text(encoding="utf-8") == "one"
