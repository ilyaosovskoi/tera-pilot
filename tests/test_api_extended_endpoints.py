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
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tera_pilot.api_server import TeraPilotAPIServer  # noqa: E402


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    # Isolate ~/.tera_pilot: several endpoints (_save_settings, the
    # open_project fix, provider activation) persist config via
    # _save_config → ~/.tera_pilot/config.json. Without this the test
    # suite overwrites the developer's REAL config (project_root was
    # observed pointing at a deleted pytest tmp dir after a run).
    home = tmp_path_factory.mktemp("tera_pilot_home")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        server = TeraPilotAPIServer(port=0)
        token = server.auth_token
        server.start()
        ws = tempfile.mkdtemp(prefix="tera_pilot_api_test_")
        server.ctx.config["project_root"] = ws
        yield {"server": server, "port": server.port, "token": token, "ws": ws}
        server.stop()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


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


def test_open_project_switches_config_project_root(api):
    """The GUI's 'Open project' endpoint must update
    ``ctx.config['project_root']`` immediately — the file tree
    (/api/files/list), /api/status and _runtime() all read the CONFIG,
    not the bridge workspace, so a stale config kept the tree on the old
    directory until the next agent run finished."""
    new_ws = tempfile.mkdtemp(prefix="tera_pilot_open_proj_")
    (Path(new_ws) / "hello.txt").write_text("hi", encoding="utf-8")

    st, data = _post(api, "/api/open_project", {"path": new_ws})
    assert st == 200
    assert data.get("ok") is True

    # /api/status must report the new project root right away.
    st, status = _get(api, "/api/status")
    assert st == 200
    assert Path(status.get("project") or "").resolve() == Path(new_ws).resolve()

    # The file tree must be rooted at the new project.
    st, files = _post(api, "/api/files/list")
    assert st == 200
    assert any(f.get("path") == "hello.txt" for f in files)

    # Invalid path must be rejected without switching anything.
    st, data = _post(api, "/api/open_project", {"path": "/definitely/not/a/dir_xyz"})
    assert st == 200
    assert data.get("ok") is False


def test_providers_health_no_import_crash(api):
    """Regression (v2.3.2): /api/providers/health crashed with
    ``ImportError: No module named 'tera_pilot.providers.types'`` on EVERY
    call, and the GUI's Test button then showed the misleading
    "Invalid API key" for any failure. The endpoint must return a
    structured response (even for an unknown provider) and never
    reference the nonexistent module."""
    st, data = _post(api, "/api/providers/health", {"provider_id": "no_such_provider_xyz"})
    assert st == 200
    assert data.get("ok") is False
    assert "provider_id" in data
    assert "No module named" not in str(data.get("error", ""))


def test_providers_health_does_not_cripple_provider_config(api):
    """Regression (v2.4.x): the health probe runs with max_tokens=100. If
    that temp config leaks into the registry, the NEXT ``registry.get(pid)``
    builds a provider capped at 100 output tokens — agent tool calls (which
    carry file content) get truncated mid-JSON, the parser sees prose, and
    the run ends as a false-success "final answer". The probe must swap the
    config on the live instance only and leave the registry map untouched."""
    server = api["server"]
    ctx = server.ctx
    providers = ctx.registry.list_providers()
    assert providers, "registry should have registered providers"
    pid = providers[0]["id"]

    provider = ctx.registry.get(pid)
    original_max = provider.config.max_tokens
    assert original_max != 100

    # Stub generate() so the probe never touches the network.
    class _Resp:
        text = "hi"
        model = ""
        tokens_in = 1
        tokens_out = 1

    provider.generate = lambda messages: _Resp()

    st, data = _post(api, "/api/providers/health", {"provider_id": pid})
    assert st == 200

    # The live instance must be restored...
    assert provider.config.max_tokens == original_max
    # ...and a FRESH instance built from the registry's persistent config
    # must NOT be crippled to 100 (this is what the old code broke: it went
    # through registry.configure(), which stores the probe config in the
    # registry's _configs map).
    ctx.registry._instances.pop(pid, None)
    fresh = ctx.registry.get(pid)
    assert fresh.config.max_tokens == original_max
    assert fresh.config.max_tokens != 100


def test_providers_models_returns_live_list(api, monkeypatch):
    """v2.4.x: /api/providers/models fetches the provider's /models list."""
    import urllib.request as _u

    class _FakeResp:
        def read(self):
            return b'{"data":[{"id":"model-a"},{"id":"model-b"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real_urlopen = _u.urlopen

    def fake_urlopen(req, timeout=0):
        # Let the test's own localhost API call pass through.
        if "127.0.0.1" in str(req.full_url):
            return real_urlopen(req, timeout=timeout)
        # The external request must point at the provider's models endpoint.
        assert "/models" in str(req.full_url)
        return _FakeResp()

    monkeypatch.setattr(_u, "urlopen", fake_urlopen)
    st, data = _post(api, "/api/providers/models", {"provider_id": "openai"})
    assert st == 200
    assert data.get("ok") is True
    assert data.get("models") == ["model-a", "model-b"]


def test_providers_models_unknown_provider(api):
    st, data = _post(api, "/api/providers/models", {"provider_id": "no_such_provider_xyz"})
    assert st == 200
    assert data.get("ok") is False
    assert "provider" in str(data.get("error", "")).lower()


def test_providers_models_missing_provider_id(api):
    st, data = _post(api, "/api/providers/models", {})
    assert st == 200
    assert data.get("ok") is False


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
