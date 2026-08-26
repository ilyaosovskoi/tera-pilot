"""Tests for the signed audit trail (G16) — Ed25519 signatures + hash chain.

Covers:
- sign/export/verify round-trip on a real generated keypair,
- tamper detection: modified entry payload, modified hash, modified
  signature, reordered entries, deleted entry (chain break),
- wrong-key rejection,
- genesis entry (prev_hash == ""),
- file-based verify helper.

Uses a throwaway keypair and points the module's public-key loader at
it via TERA_PILOT_LICENSE_PUBKEY-style override? No — audit_signing
loads the public key from ~/.tera_pilot/audit_key.pub, so these tests
monkeypatch ``load_public_key`` / run in a temp HOME. No real keys,
no network, no LLM.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot import audit_signing  # noqa: E402
from tera_pilot.audit_signing import (  # noqa: E402
    AuditSigningError,
    _canonical_payload,
    export_signed_json,
    load_or_create_keypair,
    sign_entry,
    verify_signed_file,
    verify_signed_json,
)


@pytest.fixture()
def keypair(tmp_path, monkeypatch):
    """Generate a throwaway keypair and isolate the module's HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Re-import isn't needed — the module resolves paths lazily via
    # Path.home(), which honours HOME at call time on most platforms.
    priv, pub = load_or_create_keypair()
    return priv, pub


def _entry(i: int) -> dict:
    return {
        "ts": 1700000000 + i,
        "ts_iso": f"2026-08-{10 + i:02d}T00:00:00Z",
        "category": "tool",
        "kind": "tool_call",
        "tool": "read_file",
        "title": f"entry {i}",
        "summary": f"read file {i}.py",
        "status": "ok",
        "path": f"{i}.py",
        "command": "",
        "chat_id": "chat_1",
        "iteration": i,
        "section": "general",
        "duration_ms": 12,
    }


def _export(entries) -> str:
    return export_signed_json(entries)


def _verify(exported: str, pub) -> dict:
    return verify_signed_json(exported, pub_bytes=pub)


# ═══════════════════════════════════════════════════════════════════
# Round trip
# ═══════════════════════════════════════════════════════════════════

def test_sign_verify_roundtrip(keypair):
    priv, pub = keypair
    exported = _export([_entry(1), _entry(2), _entry(3)])
    entries = json.loads(exported)
    assert len(entries) == 3
    # Genesis has empty prev_hash; the chain links the rest.
    assert entries[0]["_prev_hash"] == ""
    assert entries[1]["_prev_hash"] == entries[0]["_hash"]
    assert entries[2]["_prev_hash"] == entries[1]["_hash"]

    report = _verify(exported, pub)
    assert report.ok
    assert report.entries_checked == 3
    assert report.signatures_valid == 3
    assert report.signatures_invalid == 0
    assert report.chain_breaks == 0


def test_sign_entry_returns_sig_and_hash(keypair):
    priv, pub = keypair
    signed = sign_entry(_entry(1), "", priv)
    assert signed.prev_hash == ""
    assert len(signed.signature) == 128  # 64 bytes hex-encoded
    assert len(signed.hash) == 64        # sha256 hex
    # The hash must cover prev_hash + canonical payload.
    import hashlib
    expected = hashlib.sha256(
        b"" + b"\x1f" + _canonical_payload(_entry(1))
    ).hexdigest()
    assert signed.hash == expected


# ═══════════════════════════════════════════════════════════════════
# Tamper detection
# ═══════════════════════════════════════════════════════════════════

def test_tampered_payload_detected(keypair):
    priv, pub = keypair
    exported = _export([_entry(1), _entry(2)])
    entries = json.loads(exported)
    # Modify the middle entry's summary WITHOUT touching hash/signature.
    entries[0]["summary"] = "evil change"
    report = _verify(json.dumps(entries), pub)
    assert not report.ok
    assert report.first_failure_index == 0
    assert "hash mismatch" in report.first_failure


def test_tampered_hash_detected(keypair):
    priv, pub = keypair
    exported = _export([_entry(1), _entry(2)])
    entries = json.loads(exported)
    entries[0]["_hash"] = "0" * 64
    report = _verify(json.dumps(entries), pub)
    assert not report.ok
    assert report.first_failure_index == 0


def test_tampered_signature_detected(keypair):
    priv, pub = keypair
    exported = _export([_entry(1)])
    entries = json.loads(exported)
    # Flip a bit in the signature.
    sig = bytes.fromhex(entries[0]["_signature"])
    flipped = bytes([sig[0] ^ 0x01]) + sig[1:]
    entries[0]["_signature"] = flipped.hex()
    report = _verify(json.dumps(entries), pub)
    assert not report.ok
    assert "signature verification failed" in report.first_failure


def test_reordered_entries_breaks_chain(keypair):
    priv, pub = keypair
    exported = _export([_entry(1), _entry(2), _entry(3)])
    entries = json.loads(exported)
    # Swap the first two entries — each keeps its own _prev_hash, so the
    # chain link of entry at index 1 no longer matches.
    entries[0], entries[1] = entries[1], entries[0]
    report = _verify(json.dumps(entries), pub)
    assert not report.ok
    assert report.chain_breaks >= 1
    assert "prev_hash mismatch" in report.first_failure


def test_deleted_entry_breaks_chain(keypair):
    priv, pub = keypair
    exported = _export([_entry(1), _entry(2), _entry(3)])
    entries = json.loads(exported)
    del entries[1]
    report = _verify(json.dumps(entries), pub)
    assert not report.ok
    assert report.chain_breaks >= 1


def test_wrong_key_rejected(keypair, tmp_path):
    _priv, pub = keypair
    exported = _export([_entry(1)])
    # Generate a DIFFERENT keypair and verify with its public key.
    monkeypatch_home = tmp_path / "other_home"
    monkeypatch_home.mkdir(exist_ok=True)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(monkeypatch_home)
    try:
        other_priv, other_pub = load_or_create_keypair()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
    report = _verify(exported, other_pub)
    assert not report.ok
    assert "signature verification failed" in report.first_failure


# ═══════════════════════════════════════════════════════════════════
# Malformed input
# ═══════════════════════════════════════════════════════════════════

def test_verify_garbage_json(keypair):
    _priv, pub = keypair
    report = _verify("{not json", pub)
    assert not report.ok
    assert "JSON parse error" in report.first_failure


def test_verify_non_list_top_level(keypair):
    _priv, pub = keypair
    report = _verify('{"a": 1}', pub)
    assert not report.ok
    assert "not a list" in report.first_failure


def test_verify_empty_list_is_ok(keypair):
    _priv, pub = keypair
    report = _verify("[]", pub)
    assert report.ok
    assert report.entries_checked == 0


def test_verify_bad_signature_hex(keypair):
    _priv, pub = keypair
    report = _verify(json.dumps([{**_entry(1), "_signature": "zzz", "_hash": "0" * 64, "_prev_hash": ""}]), pub)
    assert not report.ok
    assert "not valid hex" in report.first_failure


def test_verify_signed_file(tmp_path, keypair):
    priv, pub = keypair
    exported = _export([_entry(1)])
    f = tmp_path / "audit.json"
    f.write_text(exported, encoding="utf-8")
    report = verify_signed_file(str(f))
    assert report.ok


def test_verify_signed_file_missing(tmp_path, keypair):
    report = verify_signed_file(str(tmp_path / "nope.json"))
    assert not report.ok
    assert "failed to read file" in report.first_failure


# ═══════════════════════════════════════════════════════════════════
# Key handling
# ═══════════════════════════════════════════════════════════════════

def test_load_or_create_generates_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    priv1, pub1 = load_or_create_keypair()
    priv2, pub2 = load_or_create_keypair()
    assert priv1 == priv2
    assert pub1 == pub2
    assert len(priv1) == 32
    assert len(pub1) == 32


def test_corrupt_keypair_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    load_or_create_keypair()
    # Corrupt the private key file.
    priv_path = Path.home() / ".tera_pilot" / "audit_key"
    priv_path.write_bytes(b"short")
    with pytest.raises(AuditSigningError):
        load_or_create_keypair()
