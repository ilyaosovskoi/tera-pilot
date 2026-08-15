"""Regression tests for the extended REST endpoints that the browser GUI
depends on (v2.3.1). Before this module existed, the GUI's slash commands
(/context, /clear, /compact, /pin, /reload-context), the Collective Memory
editor, the Apply/Copy file buttons, Settings save, Stop generation, the
file tree panel and several other features called HTTP endpoints that did
not exist on the Python backend — every one of them silently failed with
``{"error": "not found"}`` (HTTP 404).

The tests below spin up the real API server (as the web GUI does) and
exercise each previously-missing endpoint.
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tera_pilot.api_server import TeraPilotAPIServer  # noqa: E402


@pytest.fixture(scope="module")
def api():
    server = TeraPilotAPIServer(port=0)
    token = server.auth_token
    server.start()
    ws = tempfile.mkdtemp(prefix="tera_pilot_api_test_")
    server.ctx.config["project_root"] = ws
    yield {"server": server, "port": server.port, "token": token, "ws": ws}
    server.stop()


def _request(api, method, path, payload=None):
    url = f"http://127.0.0.1:{api['port']}{path}"
    headers = {"Authorization": "Bearer " + api["token"]}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def _get(api, path):
    return _request(api, "GET", path)


def _post(api, path, payload=None):
    return _request(api, "POST", path, payload if payload is not None else {})


# ── Context management ──────────────────────────────────────────────


def test_context_status_shape(api):
    st, data = _get(api, "/api/context/status")
    assert st == 200
    assert data.get("ok") is True
    assert "memory" in data
    assert "project_context" in data
    assert "files" in data


def test_context_clear_and_compact(api):
    st, data = _post(api, "/api/context/clear")
    assert st == 200
    assert data.get("ok") is True
    st, data = _post(api, "/api/context/compact")
    assert st == 200
    assert "ok" in data


def test_context_reload(api):
    st, data = _post(api, "/api/context/reload")
    assert st == 200
    assert data.get("ok") is True
    assert "sources" in data


def test_context_pin_unpin(api):
    st, data = _get(api, "/api/context/pin?path=src/app.py")
    assert st == 200
    assert data.get("ok") is True
    st, data = _get(api, "/api/context/unpin?path=src/app.py")
    assert st == 200
    assert data.get("ok") is True


def test_context_pin_requires_path(api):
    st, data = _get(api, "/api/context/pin")
    assert st == 200
    assert data.get("ok") is False


# ── Memory file editor ──────────────────────────────────────────────


def test_memory_roundtrip(api):
    # Fresh project → no memory file yet
    st, data = _get(api, "/api/memory/read")
    assert st == 200
    assert data.get("ok") is True
    assert data.get("source") == "none"
    assert data.get("path", "").endswith("TERA_PILOT.md")

    # Write
    st, data = _post(api, "/api/memory/write", {"content": "# TERA_PILOT.md\n\nHello\n", "path": ""})
    assert st == 200
    assert data.get("ok") is True
    assert os.path.exists(os.path.join(api["ws"], "TERA_PILOT.md"))

    # Read back
    st, data = _get(api, "/api/memory/read")
    assert "Hello" in data.get("content", "")

    # Append lesson
    st, data = _post(api, "/api/memory/append_lesson", {"title": "Lesson A", "body": "Remember X"})
    assert st == 200
    assert data.get("ok") is True
    content = open(os.path.join(api["ws"], "TERA_PILOT.md")).read()
    assert "Lesson A" in content


def test_memory_write_blocks_path_escape(api):
    st, data = _post(api, "/api/memory/write", {"content": "x", "path": "/etc/passwd"})
    assert st == 200
    assert data.get("ok") is True  # falls back to the canonical memory file
    assert data.get("path", "").endswith("TERA_PILOT.md")
    assert not os.path.exists("/etc/passwd.bak_tera")


# ── File access ─────────────────────────────────────────────────────


def test_files_roundtrip(api):
    st, data = _post(api, "/api/files/write", {"path": "src/app.py", "content": "print('hi')\n"})
    assert st == 200
    assert data.get("ok") is True

    st, data = _post(api, "/api/files/read", {"path": "src/app.py"})
    assert st == 200
    assert data.get("ok") is True
    assert "print" in data.get("content", "")

    st, data = _post(api, "/api/files/list")
    assert st == 200
    assert isinstance(data, list)
    assert any(f.get("path") == "src/app.py" for f in data)


def test_files_write_blocks_traversal(api):
    st, data = _post(api, "/api/files/write", {"path": "../../evil.txt", "content": "x"})
    assert st == 200
    assert data.get("ok") is False
    assert "escapes" in data.get("error", "")


# ── Settings & agent controls ───────────────────────────────────────


def test_settings_save(api):
    st, data = _post(api, "/api/settings/save", {"ui": {"theme": "noir"}})
    assert st == 200
    assert data.get("ok") is True


def test_chat_stop(api):
    st, data = _post(api, "/api/chat/stop")
    assert st == 200
    assert data.get("ok") is True


def test_agent_undo_no_checkpoints(api):
    st, data = _post(api, "/api/agent/undo")
    assert st == 200
    assert "ok" in data  # structured result, not a 404


def test_agent_autonomy_and_guardian(api):
    st, data = _post(api, "/api/agent/autonomy", {"level": "always_ask"})
    assert st == 200
    assert data.get("ok") is True
    st, data = _post(api, "/api/agent/guardian", {"level": "dangerous_only"})
    assert st == 200
    assert data.get("ok") is True
    st, data = _post(api, "/api/agent/guardian", {"level": "bogus"})
    assert data.get("ok") is False


def test_advanced_settings_save(api):
    st, data = _post(api, "/api/agent/advanced_settings/save", {"agent": {"max_iterations": 5}})
    assert st == 200
    assert data.get("ok") is True


def test_diff_respond_no_pending(api):
    st, data = _post(api, "/api/diff/respond", {"accepted": True})
    assert st == 200
    assert data.get("ok") is False


# ── Misc ────────────────────────────────────────────────────────────


def test_queue_stats(api):
    st, data = _get(api, "/api/queue/stats")
    assert st == 200
    assert data.get("ok") is True
    assert "stats" in data


def test_updates_check(api):
    st, data = _get(api, "/api/updates/check")
    assert st == 200
    assert data.get("ok") is True


def test_pricing_table(api):
    st, data = _get(api, "/api/pricing/table")
    assert st == 200
    assert data.get("ok") is True
    assert data.get("live") is False


def test_snippets_crud(api):
    st, data = _post(api, "/api/snippets/save", {"name": "hello", "content": "print('x')", "language": "python"})
    assert st == 200
    assert data.get("ok") is True
    st, data = _get(api, "/api/snippets")
    assert st == 200
    assert len(data.get("snippets", [])) == 1
    st, data = _post(api, "/api/snippets/delete", {"name": "hello"})
    assert st == 200
    assert data.get("ok") is True
    st, data = _get(api, "/api/snippets")
    assert len(data.get("snippets", [])) == 0


def test_router_toggle_and_classify(api):
    st, data = _post(api, "/api/router/toggle", {"enabled": True})
    assert st == 200
    assert data.get("ok") is True
    st, data = _post(api, "/api/router/classify", {"text": "Refactor the payment module into a microservice"})
    assert st == 200
    assert data.get("ok") is True
    assert "complexity" in data


def test_rag_search_returns_list(api):
    _post(api, "/api/files/write", {"path": "searchme.py", "content": "def helper():\n    return 42\n"})
    st, data = _post(api, "/api/rag/search", {"text": "helper"})
    assert st == 200
    assert isinstance(data, list)
    assert any(r.get("path") == "searchme.py" for r in data)
    assert all(r.get("source") == "grep" for r in data)


def test_generate_title_chat_missing(api):
    st, data = _post(api, "/api/chat/generate_title", {"chat_id": "does-not-exist"})
    assert st == 200
    assert data.get("ok") is False


def test_external_open_rejects_non_http(api):
    st, data = _post(api, "/api/external/open", {"url": "file:///etc/passwd"})
    assert st == 200
    assert data.get("ok") is False


def test_swarm_lifecycle(api):
    st, data = _post(api, "/api/swarm/spawn", {"name": "A1", "goal": "do X", "role": "coder"})
    assert st == 200
    assert data.get("ok") is True
    st, data = _get(api, "/api/swarm/list")
    assert st == 200
    assert len(data.get("agents", [])) == 1
    agent_id = data["agents"][0]["id"]
    st, data = _post(api, "/api/swarm/remove", {"id": agent_id})
    assert st == 200
    st, data = _get(api, "/api/swarm/list")
    assert len(data.get("agents", [])) == 0
    st, data = _post(api, "/api/swarm/cleanup")
    assert st == 200
