"""End-to-end tests for ``tera-pilot license`` CLI (v2.3.4, Part 3).

Runs the real CLI entry points (``tera_pilot.cli.main`` and
``tera_pilot.license_cli.run_license_cli``) against a locally-signed test
key, with HOME isolated so the developer's real license is never touched.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tera_pilot import cli as tera_cli  # noqa: E402
from tera_pilot.licensing import generate_keypair, sign_payload  # noqa: E402

PAYLOAD = {
    "customer_id": "usr_cli_test",
    "tier": "pro",
    "issued_at": "2026-08-17T00:00:00Z",
    "expires_at": None,
    "features": ["second_opinion", "cost_router", "spend_dashboard"],
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated HOME + a locally-signed test key + pubkey wiring."""
    priv, pub = generate_keypair()
    (tmp_path / "pubkey.pem").write_bytes(pub)
    monkeypatch.setenv("TERA_PILOT_LICENSE_PUBKEY", str(tmp_path / "pubkey.pem"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TERA_PILOT_PRO", raising=False)
    key = sign_payload(PAYLOAD, priv)
    return {"key": key}


def test_cli_license_activate_status_deactivate(env, capsys):
    # status before: invalid -> exit code 1
    code = tera_cli.main(["license", "status"])
    assert code == 1
    out = capsys.readouterr().out
    assert '"valid": false' in out

    # activate -> exit 0
    code = tera_cli.main(["license", "activate", env["key"]])
    assert code == 0
    out = capsys.readouterr().out
    assert "tier=pro" in out
    assert "usr_cli_test" in out

    # status after: valid -> exit 0
    code = tera_cli.main(["license", "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"valid": true' in out
    assert '"tier": "pro"' in out

    # deactivate -> exit 0, status back to invalid
    code = tera_cli.main(["license", "deactivate"])
    assert code == 0
    code = tera_cli.main(["license", "status"])
    assert code == 1


def test_cli_rejects_bad_key(env, capsys):
    code = tera_cli.main(["license", "activate", "totally-bogus"])
    assert code == 1
    err = capsys.readouterr().err
    assert "activation failed" in err


def test_cli_usage_errors(env, capsys):
    assert tera_cli.main(["license"]) == 2
    assert tera_cli.main(["license", "bogus-subcommand"]) == 2
    assert tera_cli.main(["license", "activate"]) == 2
