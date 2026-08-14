"""
Signed audit trail — Ed25519 signatures + hash chaining for the
existing ``ActivityLog`` (G16).

Builds on:

- ``tera_pilot/activity_log.py`` — the existing ``ActivityLog`` singleton
  (every tool call is already recorded there).
- ``tera_pilot/agent/encrypted_prompt.py`` — the existing ChaCha20 key-storage
  pattern. We reuse the same ``~/.tera_pilot/`` directory convention and
  the same "fail-open with a loud log on missing crypto" philosophy.

What this module adds (additive — ``ActivityLog`` itself is unchanged
in its public API):

1. A local Ed25519 keypair, generated on first use and stored under
   ``~/.tera_pilot/audit_key`` (private) and ``~/.tera_pilot/audit_key.pub``
   (public). The private key file is chmod 0600. Zero-cloud — the key
   never leaves the user's machine, exactly like the rest of Tera Pilot's
   audit/telemetry story.

2. ``sign_entry(entry, prev_hash)`` — signs an activity entry's
   canonical payload + the previous entry's hash. Returns a
   ``SignedAuditEntry`` with ``signature`` (hex) and ``hash`` (hex).

3. ``export_signed_json(entries)`` — emits a list of signed entries
   where each entry carries its own signature AND the hash of the
   previous entry, so tampering/reordering/deletion is detectable.

4. ``verify_signed_json(signed_entries, pubkey)`` — recomputes the
   hash chain and checks every signature. Returns a ``VerificationReport``
   with the first broken link (if any).

5. ``/audit verify <file>`` slash command — calls ``verify_signed_json``
   on an exported file and prints the result.

Hash chain design:

- Each entry's ``hash`` is SHA-256 over (``prev_hash`` + canonical JSON
  of the entry's signed projection).
- Each entry's ``signature`` is Ed25519 over the same bytes that go
  into the hash. So a tampered entry fails BOTH the hash-chain check
  (next entry's prev_hash won't match) AND the signature check.
- The first entry has ``prev_hash = ""`` (empty string), so the chain
  has a well-defined genesis.

Backward compatibility: ``ActivityLog.export_json()`` is unchanged —
it returns the unsigned list as before. The new
``ActivityLog.export_signed_json()`` method delegates to this module
to produce the signed/chained format. Existing callers that read
``export_json()`` are unaffected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Key storage ────────────────────────────────────────────────────────
# We reuse the ~/.tera_pilot/ directory convention. Files:
#   ~/.tera_pilot/audit_key       — 32-byte Ed25519 private key (raw, chmod 0600)
#   ~/.tera_pilot/audit_key.pub   — 32-byte Ed25519 public key (raw, chmod 0644)

_PRIVATE_KEY_NAME = "audit_key"
_PUBLIC_KEY_NAME = "audit_key.pub"

# Magic prefix for the public key file (so a future format change is
# detectable without guessing from file size).
_KEY_MAGIC = b"CLWA1"


def _tera_pilot_home() -> Path:
    p = Path.home() / ".tera_pilot"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _private_key_path() -> Path:
    return _tera_pilot_home() / _PRIVATE_KEY_NAME


def _public_key_path() -> Path:
    return _tera_pilot_home() / _PUBLIC_KEY_NAME


# ── Ed25519 keypair management ─────────────────────────────────────────


def _generate_keypair() -> Tuple[bytes, bytes]:
    """Generate a fresh Ed25519 keypair. Returns (priv_bytes, pub_bytes).

    Both are the raw 32-byte forms — no PEM/DER wrapping. We keep the
    on-disk format minimal so the file is small and easy to copy to a
    backup if the user wants to verify audit logs on a different
    machine.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption,
    )
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    return (
        priv.private_bytes(
            encoding=Encoding.Raw,
            format=PrivateFormat.Raw,
            encryption_algorithm=NoEncryption(),
        ),
        pub.public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        ),
    )


def _save_keypair(priv_bytes: bytes, pub_bytes: bytes) -> None:
    """Persist the keypair to ~/.tera_pilot/ with restrictive permissions."""
    priv_path = _private_key_path()
    pub_path = _public_key_path()
    # Write private key with 0600.
    fd = os.open(str(priv_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, priv_bytes)
    finally:
        os.close(fd)
    # Verify the mode (in case umask interfered).
    try:
        os.chmod(priv_path, 0o600)
    except OSError:
        pass
    # Write public key with the magic prefix.
    with open(pub_path, "wb") as f:
        f.write(_KEY_MAGIC + pub_bytes)
    try:
        os.chmod(pub_path, 0o644)
    except OSError:
        pass


def load_or_create_keypair() -> Tuple[bytes, bytes]:
    """Load the Ed25519 keypair from ~/.tera_pilot/, creating it on first use.

    Returns (priv_bytes, pub_bytes). On any crypto-related failure
    (e.g. cryptography not installed), raises ``AuditSigningError`` —
    callers should catch and degrade to the unsigned export path
    rather than crashing the audit log.
    """
    priv_path = _private_key_path()
    pub_path = _public_key_path()

    if priv_path.exists() and pub_path.exists():
        try:
            priv_bytes = priv_path.read_bytes()
            pub_bytes = pub_path.read_bytes()
            # Strip the magic prefix from the public key file if present.
            if pub_bytes.startswith(_KEY_MAGIC):
                pub_bytes = pub_bytes[len(_KEY_MAGIC):]
            if len(priv_bytes) != 32 or len(pub_bytes) != 32:
                raise AuditSigningError(
                    f"existing audit key files are corrupt (priv={len(priv_bytes)}B, "
                    f"pub={len(pub_bytes)}B, expected 32B each)"
                )
            return priv_bytes, pub_bytes
        except AuditSigningError:
            raise
        except Exception as e:
            raise AuditSigningError(f"failed to load audit keypair: {e}")

    # Generate fresh.
    try:
        priv_bytes, pub_bytes = _generate_keypair()
    except Exception as e:
        raise AuditSigningError(
            f"failed to generate Ed25519 keypair (is `cryptography` installed?): {e}"
        )
    _save_keypair(priv_bytes, pub_bytes)
    logger.info("[audit-signing] generated new Ed25519 keypair at %s", _private_key_path())
    return priv_bytes, pub_bytes


def load_public_key() -> bytes:
    """Load only the public key (for verification on a different machine).

    Raises ``AuditSigningError`` if the public key file is missing or
    corrupt. Verification callers should catch this and report "no
    public key — cannot verify" rather than crashing.
    """
    pub_path = _public_key_path()
    if not pub_path.exists():
        raise AuditSigningError(
            f"public key not found at {pub_path} — run /audit sign first to generate one"
        )
    raw = pub_path.read_bytes()
    if raw.startswith(_KEY_MAGIC):
        raw = raw[len(_KEY_MAGIC):]
    if len(raw) != 32:
        raise AuditSigningError(
            f"public key file is corrupt ({len(raw)}B, expected 32B)"
        )
    return raw


class AuditSigningError(Exception):
    """Raised when signing/verification cannot proceed (e.g. missing
    crypto deps, corrupt key files). Callers should catch and degrade
    gracefully — the audit log must NEVER be the reason a tool call
    fails."""


# ── Canonical payload + signing ────────────────────────────────────────
# The signed payload is a STABLE projection of the entry — we don't sign
# the entire entry dict because some fields (e.g. ``meta`` may contain
# objects with non-deterministic key order). The projection picks the
# fields that matter for audit integrity.

_SIGNED_FIELDS: Tuple[str, ...] = (
    "ts", "ts_iso", "category", "kind", "tool", "title",
    "summary", "status", "path", "command", "chat_id",
    "iteration", "section", "duration_ms",
)


def _canonical_payload(entry: Dict[str, Any]) -> bytes:
    """Build the canonical bytes that get hashed + signed for an entry.

    We project to a fixed set of fields + sort keys so the same entry
    always produces the same bytes regardless of dict insertion order.
    """
    proj = {k: entry.get(k) for k in _SIGNED_FIELDS}
    canonical = json.dumps(proj, sort_keys=True, default=str, ensure_ascii=False)
    return canonical.encode("utf-8")


def _chain_bytes(payload: bytes, prev_hash: str) -> bytes:
    """The bytes that are both hashed and signed: prev_hash + payload."""
    return prev_hash.encode("utf-8") + b"\x1f" + payload


@dataclass(frozen=True)
class SignedAuditEntry:
    """An activity entry + its signature + hash + chain pointer."""
    entry: Dict[str, Any]
    signature: str   # hex-encoded Ed25519 signature
    hash: str        # hex-encoded SHA-256 of (prev_hash + payload)
    prev_hash: str   # hex-encoded hash of the previous entry ("" for genesis)

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.entry)
        out["_signature"] = self.signature
        out["_hash"] = self.hash
        out["_prev_hash"] = self.prev_hash
        return out


def sign_entry(entry: Dict[str, Any], prev_hash: str, priv_bytes: bytes) -> SignedAuditEntry:
    """Sign a single activity entry with the local Ed25519 key.

    Args:
        entry: the activity log entry dict (must have at least ``ts``
            and ``category`` — other fields are optional but will be
            included in the signed payload if present).
        prev_hash: hex-encoded SHA-256 of the previous entry's chained
            bytes. Use ``""`` for the first entry in a chain.
        priv_bytes: raw 32-byte Ed25519 private key.

    Returns:
        ``SignedAuditEntry`` with ``signature`` and ``hash`` filled in.

    Raises:
        ``AuditSigningError`` if the cryptography backend fails.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption,
    )
    try:
        priv = Ed25519PrivateKey.from_private_bytes(priv_bytes)
    except Exception as e:
        raise AuditSigningError(f"invalid private key: {e}")
    payload = _canonical_payload(entry)
    chain = _chain_bytes(payload, prev_hash)
    try:
        sig = priv.sign(chain)
    except Exception as e:
        raise AuditSigningError(f"signing failed: {e}")
    h = hashlib.sha256(chain).hexdigest()
    return SignedAuditEntry(
        entry=entry,
        signature=sig.hex(),
        hash=h,
        prev_hash=prev_hash,
    )


# ── Chain export ───────────────────────────────────────────────────────


def export_signed_json(entries: List[Dict[str, Any]]) -> str:
    """Sign and hash-chain a list of activity entries.

    Returns a JSON string with one signed entry per element. The
    chain is built in list order — entry 0 has prev_hash="", entry 1
    has prev_hash=entry0.hash, etc.

    If the keypair doesn't exist yet, it's generated transparently
    on first call (and stored to ~/.tera_pilot/ for future use).

    Raises ``AuditSigningError`` only if the cryptography backend is
    completely unavailable — in that case callers should fall back
    to the unsigned ``export_json()`` path.
    """
    priv_bytes, _pub_bytes = load_or_create_keypair()
    signed: List[Dict[str, Any]] = []
    prev_hash = ""
    for entry in entries:
        signed_entry = sign_entry(entry, prev_hash, priv_bytes)
        signed.append(signed_entry.to_dict())
        prev_hash = signed_entry.hash
    return json.dumps(signed, indent=2, default=str, ensure_ascii=False)


# ── Verification ───────────────────────────────────────────────────────


@dataclass
class VerificationReport:
    """Result of verifying a signed/chained audit log."""
    ok: bool
    entries_checked: int = 0
    signatures_valid: int = 0
    signatures_invalid: int = 0
    chain_breaks: int = 0
    first_failure: Optional[str] = None  # human-readable description
    first_failure_index: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "entries_checked": self.entries_checked,
            "signatures_valid": self.signatures_valid,
            "signatures_invalid": self.signatures_invalid,
            "chain_breaks": self.chain_breaks,
            "first_failure": self.first_failure,
            "first_failure_index": self.first_failure_index,
        }


def verify_signed_json(
    signed_json: str,
    pub_bytes: Optional[bytes] = None,
) -> VerificationReport:
    """Verify a signed/chained audit log.

    Args:
        signed_json: the JSON string produced by ``export_signed_json()``.
        pub_bytes: optional raw 32-byte Ed25519 public key. If None,
            loads from ``~/.tera_pilot/audit_key.pub``.

    Returns:
        ``VerificationReport``. ``ok`` is True iff every signature
        verifies AND the hash chain is intact (each entry's prev_hash
        matches the previous entry's hash).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature

    if pub_bytes is None:
        try:
            pub_bytes = load_public_key()
        except AuditSigningError as e:
            return VerificationReport(
                ok=False, entries_checked=0,
                first_failure=f"public key unavailable: {e}",
            )
    try:
        pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
    except Exception as e:
        return VerificationReport(
            ok=False, entries_checked=0,
            first_failure=f"invalid public key: {e}",
        )

    try:
        entries = json.loads(signed_json)
    except Exception as e:
        return VerificationReport(
            ok=False, entries_checked=0,
            first_failure=f"JSON parse error: {e}",
        )
    if not isinstance(entries, list):
        return VerificationReport(
            ok=False, entries_checked=0,
            first_failure="top-level JSON is not a list",
        )

    report = VerificationReport(ok=True, entries_checked=len(entries))
    expected_prev = ""
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            report.ok = False
            report.first_failure = f"entry {i} is not an object"
            report.first_failure_index = i
            break
        sig_hex = e.get("_signature", "")
        entry_hash = e.get("_hash", "")
        prev_hash = e.get("_prev_hash", "")
        try:
            sig = bytes.fromhex(sig_hex)
        except ValueError:
            report.ok = False
            report.signatures_invalid += 1
            report.first_failure = f"entry {i}: signature is not valid hex"
            report.first_failure_index = i
            break
        # Rebuild the canonical payload from the entry (strip our
        # private keys first so they don't go into the payload).
        clean_entry = {k: v for k, v in e.items() if not k.startswith("_")}
        payload = _canonical_payload(clean_entry)
        chain = _chain_bytes(payload, prev_hash)
        # 1. Hash chain check: prev_hash must match expected_prev.
        if prev_hash != expected_prev:
            report.ok = False
            report.chain_breaks += 1
            report.first_failure = (
                f"entry {i}: prev_hash mismatch (expected {expected_prev[:12]}…, "
                f"got {prev_hash[:12]}…) — chain broken (entry tampered, "
                f"reordered, or deleted before this point)"
            )
            report.first_failure_index = i
            break
        # 2. Hash check: SHA-256(chain) must match entry_hash.
        recomputed = hashlib.sha256(chain).hexdigest()
        if recomputed != entry_hash:
            report.ok = False
            report.first_failure = (
                f"entry {i}: hash mismatch (expected {entry_hash[:12]}…, "
                f"recomputed {recomputed[:12]}…) — entry payload was tampered"
            )
            report.first_failure_index = i
            break
        # 3. Signature check.
        try:
            pub.verify(sig, chain)
            report.signatures_valid += 1
        except InvalidSignature:
            report.ok = False
            report.signatures_invalid += 1
            report.first_failure = (
                f"entry {i}: signature verification failed — entry was "
                f"modified after signing, or signed by a different key"
            )
            report.first_failure_index = i
            break
        # Advance the chain.
        expected_prev = entry_hash
    return report


def verify_signed_file(path: str) -> VerificationReport:
    """Convenience wrapper: load a file and verify it."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
    except Exception as e:
        return VerificationReport(
            ok=False, entries_checked=0,
            first_failure=f"failed to read file: {e}",
        )
    return verify_signed_json(data)
