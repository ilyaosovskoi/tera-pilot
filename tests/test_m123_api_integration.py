"""HTTP-API integration tests for M1/M2/M3 (v2.3.4, Part 2).

These exercise the features end-to-end through the real API server — the
same transport where the Part 1 SSE bug lived undetected — and verify the
Pro gating is enforced identically on every HTTP surface:

  M1 Second Opinion   /api/second_opinion/{config,run,providers}
  M2 Cost Router      /api/cost/{config,cap,route}
  M3 Spend Dashboard  /api/spend/{report,export_json,export_csv}

Gating model (v2.3.4): features are licensed via the offline license system
(tera_pilot.licensing). ``TERA_PILOT_PRO=1`` is the local-dev override that
grants them without a license — used here to flip the gate without network.
Without either, every surface must fail CLOSED (pro_required / error), and
no surface may reach the feature by a different code path.

The M1 licensed path would normally call a second LLM provider; that call is
stubbed (review_with_second_model patched) so the API chain — dispatch →
bridge → license gate → config → review — runs for real minus the network.
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tera_pilot.api_server import TeraPilotAPIServer  # noqa: E402


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    home = tmp_path_factory.mktemp("tera_pilot_home_m123")
    old_home = os.environ.get("HOME")
    old_pro = os.environ.get("TERA_PILOT_PRO")
    os.environ["HOME"] = str(home)
    os.environ.pop("TERA_PILOT_PRO", None)
    try:
        server = TeraPilotAPIServer(port=0)
        token = server.auth_token
        server.start()
        ws = tempfile.mkdtemp(prefix="tera_pilot_m123_")
        server.ctx.config["project_root"] = ws
        yield {"server": server, "port": server.port, "token": token, "ws": ws}
        server.stop()
    finally:
        if old_pro is None:
            os.environ.pop("TERA_PILOT_PRO", None)
        else:
            os.environ["TERA_PILOT_PRO"] = old_pro
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def _request(api, method, path, payload=None, token=True):
    url = f"http://127.0.0.1:{api['port']}{path}"
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + api["token"]
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


# ── M1 — Second Opinion ─────────────────────────────────────────────

def test_m1_second_opinion_config_roundtrip(api):
    st, data = _get(api, "/api/second_opinion/config")
    assert st == 200
    assert data.get("ok") is True
    assert "enabled" in data
    assert "pro_enabled" in data  # license-backed status pill

    st, data = _post(api, "/api/second_opinion/config", {"enabled": True, "min_risk_level": "high"})
    assert st == 200
    assert data.get("ok") is True
    assert data.get("enabled") is True
    assert data.get("min_risk_level") == "high"


def test_m1_run_requires_license(api):
    """Without a license the run endpoint must refuse (fail closed)."""
    st, data = _post(api, "/api/second_opinion/run", {
        "prompt": "p", "response": "r", "provider_id": "groq",
    })
    assert st == 200
    assert data.get("ok") is True
    assert data.get("error") == "pro_required"
    assert data.get("verdict") == "APPROVE"


def test_m1_run_works_with_license(api, monkeypatch):
    """With a license the full chain runs (LLM review stubbed out)."""
    from tera_pilot.second_opinion import SecondOpinionVerdict

    def _fake_review(**kwargs):
        return SecondOpinionVerdict(
            verdict="REJECT", rationale="canned integration review",
            provider_id="groq", model="llama-3.3-70b-versatile",
        )

    monkeypatch.setenv("TERA_PILOT_PRO", "1")  # dev override = licensed
    monkeypatch.setattr(
        "tera_pilot.second_opinion.review_with_second_model", _fake_review
    )
    st, data = _post(api, "/api/second_opinion/run", {
        "prompt": "p", "response": "r", "provider_id": "groq",
    })
    assert st == 200
    assert data.get("error") is None
    assert data.get("verdict") == "REJECT"
    assert data.get("rationale") == "canned integration review"


# ── M2 — Cost Router ────────────────────────────────────────────────

def test_m2_config_readable_without_license(api):
    st, data = _get(api, "/api/cost/config")
    assert st == 200
    assert data.get("ok") is True
    assert "caps_usd" in data


def test_m2_config_mutation_refused_without_license(api):
    st, data = _post(api, "/api/cost/config", {"enabled": True})
    assert st == 200
    assert data.get("ok") is False
    assert "Pro" in data.get("error", "")
    st, data = _post(api, "/api/cost/cap", {"complexity": "simple", "usd": 0.01})
    assert data.get("ok") is False
    assert "Pro" in data.get("error", "")


def test_m2_route_fails_closed_without_license(api):
    st, data = _post(api, "/api/cost/route", {"prompt": "hello world"})
    assert st == 200
    assert data.get("ok") is True
    assert data.get("enabled") is False
    assert any("Pro license required" in f for f in (data.get("factors") or []))


def test_m2_route_applies_policy_with_license(api, monkeypatch):
    monkeypatch.setenv("TERA_PILOT_PRO", "1")
    st, data = _post(api, "/api/cost/route", {"prompt": "hello world"})
    assert st == 200
    assert data.get("ok") is True
    assert data.get("enabled") is True
    assert data.get("final_pick", {}).get("provider_id")


# ── M3 — Spend Dashboard ────────────────────────────────────────────

def test_m3_report_refused_without_license(api):
    st, data = _get(api, "/api/spend/report")
    assert st == 200
    assert data.get("ok") is False
    assert data.get("pro_required") is True
    assert "Pro license" in data.get("error", "")


def test_m3_exports_refused_without_license(api):
    st, data = _get(api, "/api/spend/export_json")
    assert data.get("ok") is False and data.get("pro_required") is True
    st, data = _get(api, "/api/spend/export_csv")
    assert data.get("ok") is False and data.get("pro_required") is True


def test_m3_report_works_with_license(api, monkeypatch, tmp_path):
    # Seed a small token_history.jsonl and point the dashboard singleton at
    # it explicitly (the singleton's default source list was computed when
    # the file did not exist yet — earlier tests ran first).
    hist = tmp_path / "token_history.jsonl"
    hist.write_text(json.dumps({
        "ts": 1700000000.0, "provider": "openai", "model": "gpt-4o",
        "tokens_in": 100, "tokens_out": 50, "cost": 0.01,
    }) + "\n", encoding="utf-8")
    from tera_pilot.spend_dashboard import reset_spend_dashboard_for_test
    reset_spend_dashboard_for_test(sources=[hist])
    try:
        monkeypatch.setenv("TERA_PILOT_PRO", "1")
        st, data = _get(api, "/api/spend/report")
        assert st == 200
        assert data.get("ok") is True
        assert data.get("entries_processed") == 1
        assert data.get("total_cost_usd", 0) > 0

        st, data = _get(api, "/api/spend/export_json")
        assert data.get("ok") is True
        assert "total_cost_usd" in data.get("json", "")
    finally:
        # Restore the process-wide singleton so later tests see defaults.
        reset_spend_dashboard_for_test()
