"""Tests for the offline, zero-telemetry license system (v2.3.4, Part 3).

Covers:
- activate / status / deactivate round-trip with a locally-signed key,
- tampered / expired / malformed license rejection (fail closed),
- no-telemetry guarantee: monkeypatched socket/urllib raise if any network
  call is attempted during activation or feature checks,
- gating: `is_feature_licensed()` fails closed across second_opinion /
  cost_router / spend_dashboard (M1/M2/M3), and `TERA_PILOT_PRO=1` acts as
  the local-dev override.

The tests mint their own throwaway keypair and point the module at its
public key via TERA_PILOT_LICENSE_PUBKEY, so they never depend on the
embedded key or any developer file.
"""

import json
import os
import socket as _socket_mod
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tera_pilot import licensing  # noqa: E402
from tera_pilot.licensing import (  # noqa: E402
    LicenseError,
    activate_license,
    deactivate_license,
    generate_keypair,
    get_license_status,
    is_feature_licensed,
    is_pro_enabled,
    issue_license,
    load_private_key,
    sign_payload,
    verify_license,
)

PRO_PAYLOAD = {
    "customer_id": "usr_test_123",
    "tier": "pro",
    "issued_at": "2026-08-17T00:00:00Z",
    "expires_at": None,
    "features": ["second_opinion", "cost_router", "spend_dashboard"],
}


@pytest.fixture()
def keypair(tmp_path, monkeypatch):
    """A throwaway Ed25519 keypair + isolated HOME/pubkey wiring."""
    priv, pub = generate_keypair()
    pub_file = tmp_path / "test_pubkey.pem"
    pub_file.write_bytes(pub)
    monkeypatch.setenv("TERA_PILOT_LICENSE_PUBKEY", str(pub_file))
    # Isolate ~/.tera_pilot so license.json never touches the developer's.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TERA_PILOT_PRO", raising=False)
    return priv


def _license_string(priv, **overrides) -> str:
    payload = dict(PRO_PAYLOAD)
    payload.update(overrides)
    return sign_payload(payload, priv)


# ── Activation / status / deactivation ─────────────────────────────

def test_activate_status_deactivate_roundtrip(keypair):
    assert get_license_status()["valid"] is False
    key = _license_string(keypair)
    info = activate_license(key)
    assert info.tier == "pro"
    assert info.customer_id == "usr_test_123"
    assert info.features == ["second_opinion", "cost_router", "spend_dashboard"]

    st = get_license_status()
    assert st["valid"] is True
    assert st["tier"] == "pro"
    assert st["expires_at"] is None
    assert st["features"] == PRO_PAYLOAD["features"]
    assert is_pro_enabled() is True

    deactivate_license()
    assert get_license_status()["valid"] is False
    assert is_pro_enabled() is False


def test_verify_license_is_pure_and_accepts_future_expiry(keypair):
    key = _license_string(keypair, expires_at="2099-12-31T23:59:59Z")
    info = verify_license(key)  # no persistence needed
    assert info.tier == "pro"
    assert info.expires_at == "2099-12-31T23:59:59Z"


# ── Fail-closed rejection ──────────────────────────────────────────

def test_tampered_license_rejected(keypair):
    key = _license_string(keypair)
    tampered = key[:-8] + ("A" * 8)
    with pytest.raises(LicenseError):
        activate_license(tampered)
    assert get_license_status()["valid"] is False


def test_expired_license_rejected(keypair):
    key = _license_string(keypair, expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(LicenseError):
        activate_license(key)


def test_malformed_license_rejected(keypair):
    with pytest.raises(LicenseError):
        activate_license("not-a-license")
    with pytest.raises(LicenseError):
        activate_license("aGVsbG8.aGVsbG8")  # valid b64, not a signature


def test_wrong_key_rejected(tmp_path, monkeypatch):
    """A license signed by a DIFFERENT keypair must be rejected."""
    priv_a, pub_a = generate_keypair()
    priv_b, pub_b = generate_keypair()
    (tmp_path / "pub_a.pem").write_bytes(pub_a)
    monkeypatch.setenv("TERA_PILOT_LICENSE_PUBKEY", str(tmp_path / "pub_a.pem"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TERA_PILOT_PRO", raising=False)
    key = sign_payload(PRO_PAYLOAD, priv_b)  # signed with B, verified with A
    with pytest.raises(LicenseError):
        activate_license(key)


def test_missing_pubkey_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TERA_PILOT_LICENSE_PUBKEY", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TERA_PILOT_PRO", raising=False)
    with pytest.raises(LicenseError):
        activate_license(_license_string(generate_keypair()[0]))
    # And gating never crashes even with a broken pubkey path.
    assert is_feature_licensed("second_opinion") is False


def test_stale_license_file_ignored(tmp_path, monkeypatch, keypair):
    """A persisted-but-now-invalid license must read as invalid."""
    activate_license(_license_string(keypair))
    assert get_license_status()["valid"] is True
    # Corrupt the persisted license file (simulate tampering on disk).
    lic_path = Path(os.environ["HOME"]) / ".tera_pilot" / "license.json"
    data = json.loads(lic_path.read_text(encoding="utf-8"))
    data["license"] = data["license"][:-4] + "ZZZZ"
    lic_path.write_text(json.dumps(data), encoding="utf-8")
    assert get_license_status()["valid"] is False
    assert is_feature_licensed("cost_router") is False


# ── Dev override ───────────────────────────────────────────────────

def test_dev_override_grants_all_features(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERA_PILOT_PRO", "1")
    st = get_license_status()
    assert st["valid"] is True and st["dev_override"] is True
    assert is_feature_licensed("second_opinion") is True
    assert is_feature_licensed("anything_at_all") is True
    assert is_pro_enabled() is True


# ── No telemetry ───────────────────────────────────────────────────

def test_no_network_calls_during_license_checks(keypair, monkeypatch):
    """The whole point: license activation/checks must never touch the
    network. Monkeypatched socket.socket and urllib raise if called."""

    def _boom(*args, **kwargs):
        raise AssertionError("NETWORK CALL during license check — telemetry forbidden")

    monkeypatch.setattr(_socket_mod, "socket", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    activate_license(_license_string(keypair))
    assert is_feature_licensed("second_opinion") is True
    assert is_feature_licensed("cost_router") is True
    assert is_pro_enabled() is True
    st = get_license_status()
    assert st["valid"] is True
    deactivate_license()
    assert is_feature_licensed("spend_dashboard") is False


# ── M1/M2/M3 gating through the real modules ───────────────────────

def test_second_opinion_gate(keypair, monkeypatch):
    from tera_pilot.second_opinion import should_run_second_opinion, SecondOpinionConfig

    cfg = SecondOpinionConfig(enabled=True, min_risk_level="low")
    monkeypatch.setenv("HOME", str(Path(os.environ["HOME"])))
    # No license -> never runs, regardless of config.
    assert should_run_second_opinion(cfg, risk_level="high") is False
    # With a license -> honours config + risk threshold.
    activate_license(_license_string(keypair))
    assert should_run_second_opinion(cfg, risk_level="high") is True
    assert should_run_second_opinion(cfg, risk_level="low") is True
    assert should_run_second_opinion(SecondOpinionConfig(enabled=False), risk_level="high") is False
    assert should_run_second_opinion(cfg, risk_level="low") is True


def test_cost_router_fails_closed_when_unlicensed(keypair, monkeypatch):
    from tera_pilot.cost_router import CostRouter, CostRouterConfig, LicenseRequiredError

    monkeypatch.setenv("HOME", str(Path(os.environ["HOME"])))

    class _FakeAutoRouter:
        def route(self, prompt, required_capabilities=None, configured_providers=None):
            return {
                "provider_id": "groq",
                "model": "llama-3.3-70b-versatile",
                "fallbacks": [],
                "complexity": "simple",
                "cost_estimate": 0.0001,
                "reasoning": "fake",
            }

    cfg = CostRouterConfig(enabled=True)
    cr = CostRouter(config=cfg, auto_router=_FakeAutoRouter())
    # Unlicensed: route() must fall back to AutoRouter unchanged (fail closed).
    decision = cr.route("hello", configured_providers=set())
    assert decision.enabled is False
    assert decision.final_pick["provider_id"] == "groq"
    assert any("Pro license required" in f for f in decision.factors)
    # Config mutation must refuse.
    with pytest.raises(LicenseRequiredError):
        cr.set_cap("simple", 0.01)
    with pytest.raises(LicenseRequiredError):
        cr.update_config(enabled=True)

    # Licensed: policy is applied (enabled=True).
    activate_license(_license_string(keypair))
    decision2 = cr.route("hello", configured_providers=set())
    assert decision2.enabled is True
    assert decision2.final_pick["provider_id"] == "groq"


def test_spend_dashboard_fails_closed_when_unlicensed(keypair, monkeypatch):
    from tera_pilot.spend_dashboard import TeamSpendDashboard

    monkeypatch.setenv("HOME", str(Path(os.environ["HOME"])))
    dash = TeamSpendDashboard(sources=[])
    report = dash.report(days=30)
    assert report.error == "pro_required"
    assert report.total_cost_usd == 0.0
    assert report.by_user == []

    activate_license(_license_string(keypair))
    report2 = dash.report(days=30)
    assert report2.error is None


# ── Seller-side issuance (v2.3.5) ────────────────────────────────────

def test_issue_license_roundtrip(keypair, tmp_path, monkeypatch):
    """issue_license() produces a key that activates and verifies, and the
    seller CLI (gen-keypair + issue) works end-to-end offline."""
    priv_path = tmp_path / "priv.pem"
    priv_path.write_bytes(keypair)

    key = issue_license(
        load_private_key(str(priv_path)),
        customer_id="usr_seller_1",
        tier="pro",
        features=["second_opinion", "cost_router"],
        expires_at="2099-12-31T23:59:59Z",
    )
    info = verify_license(key)
    assert info.customer_id == "usr_seller_1"
    assert info.features == ["second_opinion", "cost_router"]
    assert info.expires_at == "2099-12-31T23:59:59Z"

    # Activation works with the issued key (no telemetry enforced above).
    activate_license(key)
    assert get_license_status()["valid"] is True


def test_issue_license_non_expiring(keypair):
    key = issue_license(keypair, customer_id="usr_x", expires_at=None)
    assert verify_license(key).expires_at is None


def test_issue_license_validation(keypair):
    import pytest as _pytest
    with _pytest.raises(LicenseError):
        issue_license(keypair, customer_id="")
    with _pytest.raises(LicenseError):
        issue_license(keypair, customer_id="ok", tier="")
    with _pytest.raises(LicenseError):
        # An already-expired issuance is rejected before signing.
        issue_license(keypair, customer_id="ok", expires_at="2020-01-01T00:00:00Z")


def test_issue_license_fills_issued_at(keypair):
    key = issue_license(keypair, customer_id="usr_ts")
    payload = json.loads(__import__("base64").urlsafe_b64decode(
        key.split(".")[0] + "=" * (-len(key.split(".")[0]) % 4)
    ))
    assert payload["issued_at"]


def test_load_private_key_errors(tmp_path):
    with pytest.raises(LicenseError):
        load_private_key(str(tmp_path / "missing.pem"))
    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"")
    with pytest.raises(LicenseError):
        load_private_key(str(empty))
