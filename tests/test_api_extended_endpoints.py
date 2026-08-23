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
    # v2.3.4-security: pin/unpin moved from GET to POST (bearer-auth)
    st, data = _post(api, "/api/context/pin", {"path": "src/app.py"})
    assert st == 200
    assert data.get("ok") is True
    st, data = _post(api, "/api/context/unpin", {"path": "src/app.py"})
    assert st == 200
    assert data.get("ok") is True


def test_context_pin_requires_path(api):
    st, data = _post(api, "/api/context/pin", {})
    assert st == 200
    assert data.get("ok") is False


def test_context_pin_requires_token(api):
    import urllib.request as _ur
    url = f"http://127.0.0.1:{api['port']}/api/context/pin"
    req = _ur.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=10) as r:
            st = r.status
    except Exception as e:
        st = getattr(e, "code", 500)
    assert st == 401


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


def test_open_project_large_dir_is_fast(api):
    """Regression (v2.3.4): picking a big folder must NOT block the
    /api/open_project request for ~20s. AgentRuntime.__init__ used to walk
    the ENTIRE picked directory synchronously via
    ContextManager.set_root → _index_project — on ~/Documents (or any
    large folder) that was 346k files ≈ 20s, so the GUI's change-directory
    button appeared frozen and the picker "opened but no directory and an
    error". The index is now built lazily (first agent use) and bounded."""
    import time
    import tera_pilot.context_manager as cm_mod

    big = tempfile.mkdtemp(prefix="tera_pilot_open_proj_big_")
    # 15k files — enough that the old synchronous walk was visibly slow.
    for i in range(300):
        d = os.path.join(big, f"dir{i}")
        os.makedirs(d, exist_ok=True)
        for j in range(50):
            with open(os.path.join(d, f"f{j}.txt"), "w") as f:
                f.write("x" * 100)

    # Force the pre-fix behaviour to be measurable: a synchronous full
    # walk of 15k files takes long enough that the old code would hang;
    # with lazy indexing the open_project call itself does zero walking.
    old_ensure = cm_mod.ContextManager._ensure_indexed

    def counting_ensure(self):
        # Mirror the real implementation (index once per set_root) but
        # count how many times the walk actually runs.
        if not self._index_dirty:
            return
        self._index_dirty = False
        self._index_project()

    cm_mod.ContextManager._ensure_indexed = counting_ensure
    try:
        t0 = time.time()
        st, data = _post(api, "/api/open_project", {"path": big})
        elapsed = time.time() - t0
        assert st == 200
        assert data.get("ok") is True
        # The request itself must not index anything (that's deferred).
        assert elapsed < 5.0, f"open_project took {elapsed:.1f}s — index should be lazy"
    finally:
        cm_mod.ContextManager._ensure_indexed = old_ensure

    # The index still gets built (bounded) on first agent use.
    from tera_pilot.context_manager import get_context_manager
    cm = get_context_manager()
    cm.set_root(big)
    sel = cm.select_context()
    assert sel["total_indexed"] > 0


def test_native_file_picker_cancel_and_error(api, monkeypatch):
    """Regression (v2.3.4): the GUI's change-directory button calls
    /api/native_file_picker. The OLD implementation created a tkinter
    dialog from the HTTP daemon thread, which on macOS aborts the whole
    process (AppKit: "NSWindow should only be instantiated on the main
    thread!") — the picker either crashed the backend or, on cancel,
    returned ``{ok: false, error: ...}`` which the GUI turned into an
    error toast. Now:
      * cancellation returns ``{ok: false, cancelled: true}`` (no error),
      * a REAL picker failure (osascript automation permission denied,
        missing dialog binary) returns ``{ok: false, error: ...}`` so the
        GUI can fall back to the manual path-entry modal instead of
        silently doing nothing,
      * success returns the picked path.
    """
    import tera_pilot.api_extended as ae

    # Cancel — must be distinguishable from a real error.
    monkeypatch.setattr(ae, "_pick_directory", lambda initial_dir="": None)
    st, data = _post(api, "/api/native_file_picker")
    assert st == 200
    assert data == {"ok": False, "cancelled": True}

    # Success.
    monkeypatch.setattr(ae, "_pick_directory", lambda initial_dir="": "/tmp/project_x")
    st, data = _post(api, "/api/native_file_picker")
    assert st == 200
    assert data.get("ok") is True
    assert data.get("path") == "/tmp/project_x"

    # No usable picker -> structured error (GUI falls back to modal).
    def _boom(initial_dir=""):
        raise RuntimeError("no usable native directory picker")
    monkeypatch.setattr(ae, "_pick_directory", _boom)
    st, data = _post(api, "/api/native_file_picker")
    assert st == 200
    assert data.get("ok") is False
    assert "picker" in data.get("error", "")


def test_pick_directory_distinguishes_cancel_from_failure(monkeypatch):
    """Regression (v2.3.4): _pick_directory must NOT swallow a real
    osascript failure as if the user cancelled. Previously BOTH cases
    returned None, so when macOS denied automation permission (-1743) or
    osascript was missing the GUI silently did nothing — no dialog, no
    error toast, no fallback modal. Cancel (-128 / "User canceled") must
    stay None, any other failure must raise."""
    import shutil
    import subprocess
    import sys
    import tera_pilot.api_extended as ae

    # Force the macOS branch regardless of the test host. The function
    # imports shutil/subprocess/sys locally, so patch the real modules.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(ae.os.path, "isfile", lambda p: True)

    def _fake_run(proc):
        def run(args, **kwargs):
            return proc
        return run

    # Successful dialog → returns the path (newline stripped).
    proc_ok = type("P", (), {"returncode": 0, "stdout": "/Users/x/proj\n", "stderr": ""})()
    monkeypatch.setattr(subprocess, "run", _fake_run(proc_ok))
    assert ae._pick_directory() == "/Users/x/proj"

    # User cancel → None (exit -128 reported inside stderr).
    proc_cancel = type("P", (), {"returncode": 1, "stdout": "", "stderr": "execution error: User canceled. (-128)"})()
    monkeypatch.setattr(subprocess, "run", _fake_run(proc_cancel))
    assert ae._pick_directory() is None

    # Real failure (automation permission denied) → must RAISE, not cancel.
    proc_denied = type("P", (), {"returncode": 1, "stdout": "", "stderr": "execution error: Not authorized to send Apple events to System Events. (-1743)"})()
    monkeypatch.setattr(subprocess, "run", _fake_run(proc_denied))
    try:
        ae._pick_directory()
        assert False, "expected RuntimeError for a real osascript failure"
    except RuntimeError as e:
        assert "-1743" in str(e) or "choose-folder failed" in str(e)


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
    """Regression (v2.3.4): the health probe runs with max_tokens=100. If
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
    """v2.3.4: /api/providers/models fetches the provider's /models list."""
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


def test_settings_save_ignores_unknown_provider(api):
    """v2.3.5-fix (config hygiene): a stray provider id in the settings
    body (e.g. a JS ``card.dataset.id`` that was never set → "undefined")
    must NOT be persisted into config.json — the old code created the
    entry verbatim and then warned ``Unknown provider: undefined`` on
    every server start."""
    st, data = _post(api, "/api/settings/save", {
        "providers": {
            "openrouter": {"model": "anthropic/claude-sonnet-5"},
            "undefined": {"model": "", "api_key": "", "api_base": ""},
            "totally-bogus": {"model": "x"},
        },
    })
    assert st == 200
    assert data.get("ok") is True

    cfg = api["server"].ctx.config["providers"]
    assert "openrouter" in cfg, "known provider must still be saved"
    assert cfg["openrouter"]["model"] == "anthropic/claude-sonnet-5"
    assert "undefined" not in cfg, "garbage provider id must not persist"
    assert "totally-bogus" not in cfg, "unknown provider id must not persist"


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


def test_guardian_level_applied_to_runtime(api):
    """v2.3.4-fix: setting the Guardian level over HTTP must push it onto
    the live AgentRuntime's ToolEngine — previously it was persisted to
    config only, so Guardian stayed silently OFF in the browser GUI."""
    st, data = _post(api, "/api/agent/guardian", {"level": "all"})
    assert st == 200
    assert data.get("ok") is True
    rt = api["server"].ctx.get_agent_runtime(api["ws"])
    cfg = getattr(rt.tools, "_guardian_config", None)
    assert cfg is not None, "guardian config was never applied to the runtime"
    assert cfg.level == "all"


def test_autonomy_setting_persists_to_runtime(api):
    """v2.3.4-fix: the saved autonomy level must be applied when the
    runtime is created (after a server restart) — not only when the user
    re-saves the settings in the GUI."""
    from tera_pilot.api_server import _save_config
    with api["server"].ctx._config_lock:
        api["server"].ctx.config["agent_autonomy"] = "never_ask"
        _save_config(api["server"].ctx.config)
    # Force the runtime to be re-created so it picks up the saved config.
    api["server"].ctx._agent_runtime = None
    rt = api["server"].ctx.get_agent_runtime(api["ws"])
    assert rt.tools.autonomy == "never_ask"


def test_action_respond_route_exists(api):
    """v2.3.4-fix: /api/action/respond must exist (the GUI's Allow/Deny
    modal posts to it). Unknown ids return a structured error, not 404."""
    st, data = _post(api, "/api/action/respond", {"accepted": True, "confirm_id": "nope"})
    assert st == 200
    assert data.get("ok") is False
    assert "no pending" in data.get("error", "")


def test_guardian_respond_route_exists(api):
    """v2.3.4-fix: /api/guardian/respond must exist (the GUI's Guardian
    modal posts to it). Invalid verdicts and unknown ids are rejected."""
    st, data = _post(api, "/api/guardian/respond", {"verdict": "bogus", "review_id": "nope"})
    assert st == 200
    assert data.get("ok") is False
    st, data = _post(api, "/api/guardian/respond", {"verdict": "approve", "review_id": "nope"})
    assert st == 200
    assert data.get("ok") is False
    assert "no pending" in data.get("error", "")


def test_action_guardian_respond_require_token(api):
    import urllib.request as _ur
    url = f"http://127.0.0.1:{api['port']}/api/action/respond"
    req = _ur.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=10) as r:
            st = r.status
    except urllib.error.HTTPError as e:
        st = e.code
    assert st == 401


def test_os_sandbox_route_sets_and_validates_mode(api):
    """P1.10: /api/agent/os_sandbox persists + applies the sandbox mode
    (off|auto|on); invalid values are normalized to auto, never crash."""
    st, data = _post(api, "/api/agent/os_sandbox", {"mode": "on"})
    assert st == 200
    assert data.get("ok") is True
    assert data["settings"]["agent"]["os_sandbox"] == "on"
    # The live runtime gets the mode too.
    rt = api["server"].ctx.get_agent_runtime(api["ws"])
    assert rt.tools.os_sandbox == "on"

    st, data = _post(api, "/api/agent/os_sandbox", {"mode": "off"})
    assert st == 200 and data.get("ok") is True
    assert data["settings"]["agent"]["os_sandbox"] == "off"

    # Bogus values normalize to 'auto' instead of erroring.
    st, data = _post(api, "/api/agent/os_sandbox", {"mode": "bogus"})
    assert st == 200 and data.get("ok") is True
    assert data["settings"]["agent"]["os_sandbox"] == "auto"


def test_os_sandbox_route_requires_token(api):
    import urllib.request as _ur
    url = f"http://127.0.0.1:{api['port']}/api/agent/os_sandbox"
    req = _ur.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=10) as r:
            st = r.status
    except urllib.error.HTTPError as e:
        st = e.code
    assert st == 401


def test_quota_breakdown_returns_list(api):
    """v2.3.4-fix: /api/quota/breakdown must return a plain JSON array the
    Usage modal can iterate (it used to 404, leaving "By provider" empty)."""
    st, data = _get(api, "/api/quota/breakdown")
    assert st == 200
    assert isinstance(data, list)
    for b in data:
        assert "provider" in b
        assert "cost" in b
        assert "tokens" in b


def test_token_optimization_tips_shape(api):
    """v2.3.4-fix: /api/token_optimization/tips must return a structured
    result (it used to 404, so the status-bar tip indicator never showed)."""
    st, data = _get(api, "/api/token_optimization/tips")
    assert st == 200
    assert data.get("ok") is True
    assert isinstance(data.get("tips"), list)
    assert "total_potential_savings" in data


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
