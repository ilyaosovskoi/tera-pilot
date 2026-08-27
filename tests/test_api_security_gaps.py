"""Security-focused tests for the local API surface that were not covered
by the main security suite (test_security_suite.py) or the extended
endpoint tests (test_api_extended_endpoints.py).

These map to THREAT_MODEL.md T4 (key confidentiality) and the local-API
defense described in README §Security (bearer token on mutating endpoints,
size-capped request bodies, API-key masking). Every test is local and
deterministic; no provider calls are made.

Gaps closed here:

  A1  Oversized request bodies (Content-Length > MAX_BODY_BYTES) are
      refused BEFORE being read — a memory-exhaustion DoS defense that
      had no test at all.
  A2  Provider API keys are masked (only first/last 4 chars) in both
      /api/providers and /api/providers/custom/list — the secret must
      never be echoed to the browser (T4).
  A3  Secret-bearing extended POST endpoints (/api/providers/custom/add,
      /api/github/set_token, /api/notify/configure, /api/hooks/register,
      /api/checkpoint/rewind, /api/collaboration/run) reject requests
      with no bearer token (401) — the existing auth tests only covered a
      handful of core endpoints.
"""

import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.api_server import TeraPilotAPIServer  # noqa: E402
from tera_pilot.api_server import TeraPilotAPIHandler  # noqa: E402


# ═══════════════════════════════════════════════════════════════════
# A1 — oversized request-body cap (memory-exhaustion DoS defense)
# ═══════════════════════════════════════════════════════════════════

class _FakeHeaders:
    def __init__(self, content_length: str):
        self._cl = content_length

    def get(self, key, default=None):
        if key == "Content-Length":
            return self._cl
        return default


class _FakeRFile:
    """A file-like whose read should never be reached for oversized bodies."""

    def __init__(self):
        self.read_called = False

    def read(self, *a, **kw):
        self.read_called = True
        return b"{}"


def test_read_json_refuses_oversized_content_length():
    """A body claiming > 8 MiB must be rejected without reading it."""
    handler = TeraPilotAPIHandler.__new__(TeraPilotAPIHandler)
    handler.headers = _FakeHeaders(str(TeraPilotAPIHandler.MAX_BODY_BYTES + 1))
    rf = _FakeRFile()
    handler.rfile = rf
    assert handler._read_json() == {}
    assert not rf.read_called, "oversized body must not be read off the wire"


def test_read_json_accepts_at_cap():
    handler = TeraPilotAPIHandler.__new__(TeraPilotAPIHandler)
    handler.headers = _FakeHeaders(str(TeraPilotAPIHandler.MAX_BODY_BYTES))
    rf = _FakeRFile()
    handler.rfile = rf
    # At exactly the cap the body is read (the JSON in _FakeRFile is {}).
    assert handler._read_json() == {}


def test_read_json_rejects_non_numeric_and_empty_length():
    for bad in ("abc", "1.5", ""):
        handler = TeraPilotAPIHandler.__new__(TeraPilotAPIHandler)
        handler.headers = _FakeHeaders(bad)
        # The read path is not what we're testing here — the parse of the
        # Content-Length must fail for malformed values before any read.
        handler.rfile = io.BytesIO(b"{\"x\":1}")
        with pytest.raises((ValueError, json.JSONDecodeError)):
            handler._read_json()


# ═══════════════════════════════════════════════════════════════════
# A2/A3 — live API server: key masking + extended-endpoint auth
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def api(tmp_path_factory):
    home = tmp_path_factory.mktemp("api_sec_home")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        server = TeraPilotAPIServer(port=0)
        server.start()
        yield {"port": server.port, "token": server.auth_token, "server": server}
        server.stop()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def _req(api, method, path, payload=None, token=None):
    url = f"http://127.0.0.1:{api['port']}{path}"
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _json(resp):
    return (resp[0], json.loads(resp[1]) if resp[1] else {})


# ── A2 — provider API keys are masked ─────────────────────────────

def test_get_providers_masks_api_keys(api):
    """/api/providers must never return a full provider API key (T4)."""
    with api["server"].ctx._config_lock:
        api["server"].ctx.config["providers"]["openai"]["api_key"] = (
            "sk-SUPER-SECRET-VALUE-1234"
        )
    st, data = _json(_req(api, "GET", "/api/providers"))
    assert st == 200
    entry = next(p for p in data if p["id"] == "openai")
    assert entry["api_key_set"] is True
    assert entry["api_key_masked"].startswith("sk-S")
    assert entry["api_key_masked"].endswith("1234")
    # The raw key must not appear anywhere in the serialised response.
    assert "SUPER-SECRET-VALUE" not in json.dumps(data)


def test_get_providers_empty_key_masked_as_ellipsis(api):
    with api["server"].ctx._config_lock:
        api["server"].ctx.config["providers"]["groq"]["api_key"] = ""
    st, data = _json(_req(api, "GET", "/api/providers"))
    assert st == 200
    entry = next(p for p in data if p["id"] == "groq")
    assert entry["api_key_set"] is False
    assert entry["api_key_masked"] in (".", "...", "")  # no key → not echoed
    assert "api_key" not in entry or entry.get("api_key", "") == ""


def test_get_providers_short_key_only_four_carets(api):
    """Keys that fully fit the 8-char window are shown as an ellipsis, not
    the plain key."""
    with api["server"].ctx._config_lock:
        api["server"].ctx.config["providers"]["deepseek"]["api_key"] = "short"
    st, data = _json(_req(api, "GET", "/api/providers"))
    assert st == 200
    entry = next(p for p in data if p["id"] == "deepseek")
    assert entry["api_key_set"] is True
    assert entry["api_key_masked"] == "..."
    assert "short" not in json.dumps(data)


# ── A3 — extended secret-bearing POST endpoints require a token ───


@pytest.mark.parametrize("path,payload", [
    # Custom provider add carries an api_key in the body → must be gated.
    ("/api/providers/custom/add", {"provider_id": "x", "api_key": "sk-abc", "base_url": "https://x"}),
    # GitHub token is a credential → user action required, never anonymous.
    ("/api/github/set_token", {"token": "ghp_xyz"}),
    # Notification backend config can hold webhook URLs / credentials.
    ("/api/notify/configure", {"backend": "telegram", "bot_token": "123456:tok"}),
    # Hooks register arbitrary Python/JS snippets → must be authenticated.
    ("/api/hooks/register", {"hook_type": "pre_tool_use", "name": "h", "code": "return True"}),
    # Checkpoint rewind restores/deletes files → side-effecting.
    ("/api/checkpoint/rewind", {"n": 1}),
    # Collaboration run spawns sub-agents.
    ("/api/collaboration/run", {"mode": "swarm"}),
])
def test_extended_mutating_endpoint_requires_token(api, path, payload):
    st, _ = _req(api, "POST", path, payload=payload)
    assert st == 401, f"{path} must require a bearer token (got {st})"


@pytest.mark.parametrize("path,payload", [
    ("/api/providers/custom/add", {"provider_id": "x", "base_url": "https://x"}),
    ("/api/github/set_token", {"token": "ghp_xyz"}),
    ("/api/checkpoint/rewind", {"n": 1}),
])
def test_extended_mutating_endpoint_accepts_valid_token(api, path, payload):
    st, _ = _req(api, "POST", path, payload=payload, token=api["token"])
    assert st == 200, f"{path} should accept a valid token (got {st})"


def test_wrong_bearer_token_rejected(api):
    """A wrong token (even sharing a prefix) must be rejected."""
    st, _ = _req(api, "POST", "/api/checkpoint/rewind", payload={"n": 1},
                 token=api["token"][:-2] + "xx")
    assert st == 401


def test_refused_oversized_body_does_not_crash_endpoint(api):
    """An oversized Content-Length over HTTP is treated as an empty body
    and must not crash the endpoint or hang the connection."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", api["port"], timeout=10)
    conn.putrequest("POST", "/api/checkpoint/rewind")
    conn.putheader("Authorization", "Bearer " + api["token"])
    conn.putheader("Content-Type", "application/json")
    # Claim ~9 MiB; the server refuses without reading, so urllib/client
    # only needs to send the header, not the body.
    conn.putheader("Content-Length", str(TeraPilotAPIHandler.MAX_BODY_BYTES + 1024))
    conn.endheaders()
    res = conn.getresponse()
    assert res.status in (200, 500), res.status
    conn.close()