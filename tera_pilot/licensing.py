"""
tera_pilot.licensing — offline, zero-telemetry license verification (v2.3.4).

Gates ``tera_pilot_pro`` (and future paid tiers) behind a signed license key
that verifies ENTIRELY offline. Consistent with the project's "no telemetry"
constraint: there is no license-check network call, no phone-home, and no
usage reporting tied to the key. This is test-enforced (the suite
monkeypatches ``socket.socket`` / ``urllib`` to raise if any network call is
attempted during activation or feature checks).

Model
-----
The SELLER holds the Ed25519 private signing key, offline, outside this
repository. The PUBLIC key ships embedded in the package
(``tera_pilot/license_pubkey.pem``), so every client can verify signatures
without any network access. Key issuance is a seller-side step and is NOT
part of this module — see ``LICENSING.md``.

License format
--------------
A license is a signed JSON payload:

    {
      "customer_id": "usr_abc...",
      "tier": "pro",
      "issued_at": "2026-08-17T00:00:00Z",
      "expires_at": "2027-08-17T00:00:00Z"   (or null = never expires),
      "features": ["second_opinion", "cost_router", "spend_dashboard"]
    }

serialized as::

    <base64url(canonical JSON payload)>.<base64url(Ed25519 signature)>

where "canonical JSON" is ``json.dumps(payload, sort_keys=True,
separators=(",", ":"))`` and the signature covers exactly those bytes.
``sign_payload()`` below is the seller-side/test helper that produces this
string; the client path only ever *verifies*.

Behaviour
---------
- ``activate_license(key)``  — decode, verify signature against the embedded
  public key, check ``expires_at`` against the local clock, persist to
  ``~/.tera_pilot/license.json`` (mirrors how ``audit_key`` is stored).
- ``get_license_status()``   — re-verifies the persisted license on every
  read, so an expired / tampered license flips to ``valid: False`` without
  re-activation.
- ``is_feature_licensed(feature)`` — the ONLY gate Pro features should call.
  Fails CLOSED (returns False, never raises) on invalid / expired / missing
  license; a failed check falls back to the free tier, never crashes.
- ``TERA_PILOT_PRO=1`` env var remains a LOCAL-DEV override (documented as
  dev-only, never for production use). It grants all Pro features without a
  license.

Known limitations (honest): the local clock is trusted for expiry checks
(a user can shift their clock forward — acceptable for this threat model,
the same trust boundary as the rest of the local-first app); there is no
revocation list (a leaked key stays valid until it expires — key rotation is
a seller-side concern); the public key can be overridden via
``TERA_PILOT_LICENSE_PUBKEY`` (used by the test suite and for key rotation —
same trust level as the dev override, never for production).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Env var for the LOCAL-DEV override (grants Pro without a license).
_DEV_OVERRIDE_ENV = "TERA_PILOT_PRO"
#: Env var pointing at an alternate Ed25519 public key (PEM) — tests / rotation.
_PUBKEY_ENV = "TERA_PILOT_LICENSE_PUBKEY"

# NOTE: the license file path is resolved lazily via _license_file() (not
# at import time) so HOME changes — test isolation, portable installs — are
# respected. Mirrors how audit_signing stores audit_key under
# ~/.tera_pilot/.


def _license_file() -> Path:
    return Path.home() / ".tera_pilot" / "license.json"

#: The public key shipped with the package. The private key never lives here.
_EMBEDDED_PUBKEY = Path(__file__).resolve().parent / "license_pubkey.pem"

_DEV_OVERRIDE_TRUE = ("1", "true", "yes", "on")


class LicenseError(Exception):
    """Raised when a license string cannot be activated or verified."""


class LicenseRequiredError(LicenseError):
    """Raised when a Pro-gated feature is used without a valid license.

    Subclass of ``LicenseError`` so callers can catch either. Pro-gated
    modules raise this from their config-mutation / feature entry points;
    the bridge/API layers convert it to an ``{ok: False, error: ...}``
    response — the app never crashes.
    """


@dataclass(frozen=True)
class LicenseInfo:
    """Decoded, signature-verified license payload."""

    customer_id: str
    tier: str
    issued_at: str
    expires_at: Optional[str]  # ISO-8601, or None = never expires
    features: List[str] = field(default_factory=list)
    activated_at: str = ""


# ── Helpers ──────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dev_override() -> bool:
    """True when the TERA_PILOT_PRO local-dev override is set."""
    return os.environ.get(_DEV_OVERRIDE_ENV, "").strip().lower() in _DEV_OVERRIDE_TRUE


def _canonical(payload: Dict[str, Any]) -> str:
    """Deterministic JSON serialization — the exact bytes that get signed."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _load_pubkey_pem() -> bytes:
    """Load the Ed25519 public key (PEM SPKI) used to verify licenses."""
    override = os.environ.get(_PUBKEY_ENV)
    path = Path(override) if override else _EMBEDDED_PUBKEY
    if not path.is_file():
        raise LicenseError(
            f"license public key not found: {path} — the package is incomplete "
            "or TERA_PILOT_LICENSE_PUBKEY points at a missing file"
        )
    return path.read_bytes()


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (tolerates a trailing 'Z')."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise LicenseError(f"invalid date in license: {value!r}") from e


def _payload_to_info(payload: Dict[str, Any]) -> LicenseInfo:
    """Validate required fields + expiry; raises LicenseError otherwise."""
    customer_id = payload.get("customer_id")
    tier = payload.get("tier")
    features = payload.get("features", [])
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise LicenseError("license is missing customer_id")
    if not isinstance(tier, str) or not tier.strip():
        raise LicenseError("license is missing tier")
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise LicenseError("license 'features' must be a list of strings")

    issued_at = payload.get("issued_at")
    if issued_at is not None and not isinstance(issued_at, str):
        raise LicenseError("license 'issued_at' must be an ISO-8601 string or absent")
    expires_at = payload.get("expires_at")
    if expires_at is not None:
        if not isinstance(expires_at, str):
            raise LicenseError("license 'expires_at' must be an ISO-8601 string or null")
        if _parse_iso(expires_at) <= datetime.now(timezone.utc):
            raise LicenseError(f"license expired at {expires_at}")

    return LicenseInfo(
        customer_id=customer_id.strip(),
        tier=tier.strip(),
        issued_at=issued_at or "",
        expires_at=expires_at,
        features=[str(f) for f in features],
        activated_at=_now_iso(),
    )


# ── Verification (client path) ───────────────────────────────────────

def verify_license(license_string: str) -> LicenseInfo:
    """Decode and verify a license string against the public key.

    Raises ``LicenseError`` on any failure (malformed, wrong signature,
    expired, unknown key). Pure local computation — no network I/O.
    """
    try:
        payload_b64, sig_b64 = license_string.strip().split(".", 1)
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
        signature = _b64decode(sig_b64)
    except Exception as e:
        raise LicenseError(f"malformed license string: {e}") from e
    if not isinstance(payload, dict):
        raise LicenseError("license payload must be a JSON object")

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    canonical = _canonical(payload).encode("utf-8")
    try:
        pub = load_pem_public_key(_load_pubkey_pem())
        if not isinstance(pub, Ed25519PublicKey):
            raise LicenseError("license public key is not an Ed25519 key")
        pub.verify(signature, canonical)
    except LicenseError:
        raise
    except InvalidSignature as e:
        raise LicenseError("license signature is invalid — key does not match") from e
    except Exception as e:
        raise LicenseError(f"license verification failed: {e}") from e

    return _payload_to_info(payload)


def activate_license(license_string: str) -> LicenseInfo:
    """Verify a license and persist it to ``~/.tera_pilot/license.json``.

    Raises ``LicenseError`` on any failure; the previous license (if any)
    is left untouched until the new one verifies. Returns the verified
    ``LicenseInfo``.
    """
    info = verify_license(license_string)
    try:
        lic_file = _license_file()
        lic_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "license": license_string.strip(),
            "activated_at": info.activated_at,
            "payload": {
                "customer_id": info.customer_id,
                "tier": info.tier,
                "issued_at": info.issued_at,
                "expires_at": info.expires_at,
                "features": info.features,
            },
        }
        tmp = lic_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(lic_file)
    except OSError as e:
        raise LicenseError(f"could not persist license: {e}") from e
    logger.info("[licensing] license activated: tier=%s customer=%s",
                info.tier, info.customer_id)
    return info


def deactivate_license() -> None:
    """Remove the persisted license (if any). Never raises."""
    try:
        lic_file = _license_file()
        if lic_file.exists():
            lic_file.unlink()
            logger.info("[licensing] license deactivated")
    except OSError as e:
        logger.warning("[licensing] failed to remove license file: %s", e)


def get_license_status() -> Dict[str, Any]:
    """Return the current license status.

    Shape: ``{"valid": bool, "tier": str|None, "expires_at": str|None,
    "features": [...], "dev_override": bool, ...}``. Re-verifies the
    persisted license on every read, so expiry/tampering is reflected
    without re-activation. Never raises.
    """
    dev = _dev_override()
    out: Dict[str, Any] = {
        "valid": False,
        "tier": None,
        "expires_at": None,
        "features": [],
        "dev_override": dev,
    }
    if dev:
        out["valid"] = True
        out["tier"] = "pro"
        return out
    try:
        lic_file = _license_file()
        if not lic_file.is_file():
            return out
        raw = json.loads(lic_file.read_text(encoding="utf-8"))
        info = verify_license(raw.get("license", ""))
    except Exception as e:
        logger.debug("[licensing] license status read failed: %s", e)
        return out
    out.update({
        "valid": True,
        "tier": info.tier,
        "expires_at": info.expires_at,
        "features": info.features,
        "customer_id": info.customer_id,
        "issued_at": info.issued_at,
        "activated_at": raw.get("activated_at") or info.activated_at,
    })
    return out


# ── Gating ───────────────────────────────────────────────────────────

def is_feature_licensed(feature: str) -> bool:
    """True when ``feature`` is enabled by a valid license.

    Fails CLOSED: invalid / expired / missing license (or any unexpected
    error) returns False — the caller falls back to the free tier and the
    app never crashes. The ``TERA_PILOT_PRO=1`` local-dev override returns
    True for every feature.
    """
    try:
        st = get_license_status()
        if st.get("dev_override"):
            return True
        return bool(st.get("valid")) and feature in (st.get("features") or [])
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("[licensing] feature check failed for %s: %s", feature, e)
        return False


def is_pro_enabled() -> bool:
    """DEPRECATED status shim: True when a valid Pro license (or the
    ``TERA_PILOT_PRO`` dev override) is active.

    Feature gating should call :func:`is_feature_licensed` instead. Kept
    for status displays (TUI pro pill, /api/pro/status, ...).
    """
    try:
        st = get_license_status()
        if st.get("dev_override"):
            return True
        return bool(st.get("valid")) and st.get("tier") == "pro"
    except Exception:  # pragma: no cover - defensive
        return False


# ── Seller-side / test helpers (NOT the client verification path) ────

def generate_keypair() -> Tuple[bytes, bytes]:
    """Generate a fresh Ed25519 keypair for signing licenses.

    Returns ``(private_key_pem, public_key_pem)``. The PRIVATE key belongs
    to the seller and must stay offline, outside the repo; only the public
    key is embedded (``tera_pilot/license_pubkey.pem``). Used by the test
    suite and by whoever issues keys — see LICENSING.md.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub_pem = priv.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    return priv_pem, pub_pem


def sign_payload(payload: Dict[str, Any], private_key_pem: bytes) -> str:
    """Seller-side helper: sign a payload with the Ed25519 private key (PEM).

    Returns the license string in the same format the client verifies. This
    is NOT part of the shipped client path — it exists so tests (and the
    seller) can mint keys locally.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise LicenseError("private key is not an Ed25519 key")
    canonical = _canonical(payload).encode("utf-8")
    signature = key.sign(canonical)
    return f"{_b64encode(canonical)}.{_b64encode(signature)}"


def issue_license(
    private_key_pem: bytes,
    *,
    customer_id: str,
    tier: str = "pro",
    features: Optional[List[str]] = None,
    expires_at: Optional[str] = None,
    issued_at: Optional[str] = None,
) -> str:
    """Seller-side helper: build + sign a license payload in one call.

    This is the companion to :func:`sign_payload` for the seller CLI
    (``tera-pilot license issue``): it fills in ``issued_at`` when
    omitted, validates the required fields, and returns the license
    string ready to hand to a customer. Entirely offline — no network,
    no telemetry. The private key belongs to the seller and must stay
    offline, outside the repo (see LICENSING.md).

    ``expires_at``: ISO-8601 string, or None for a non-expiring key.
    ``features``: feature ids to grant (e.g. ``second_opinion``,
    ``cost_router``, ``spend_dashboard``); empty list = base Pro tier.
    """
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise LicenseError("customer_id is required")
    if not isinstance(tier, str) or not tier.strip():
        raise LicenseError("tier is required")
    feats = [str(f) for f in (features or []) if str(f).strip()]
    if expires_at is not None:
        # Validate early so the seller never hands out an expired key.
        if _parse_iso(expires_at) <= datetime.now(timezone.utc):
            raise LicenseError(f"expires_at is in the past: {expires_at}")
    payload: Dict[str, Any] = {
        "customer_id": customer_id.strip(),
        "tier": tier.strip(),
        "issued_at": issued_at or _now_iso(),
        "expires_at": expires_at,
        "features": feats,
    }
    return sign_payload(payload, private_key_pem)


def load_private_key(path: str) -> bytes:
    """Read a seller's Ed25519 private key (PEM) from disk.

    Raises ``LicenseError`` when the file is missing or unreadable —
    never returns an empty key. Used by the seller CLI.
    """
    p = Path(path)
    if not p.is_file():
        raise LicenseError(f"private key file not found: {path}")
    data = p.read_bytes()
    if not data:
        raise LicenseError(f"private key file is empty: {path}")
    return data
