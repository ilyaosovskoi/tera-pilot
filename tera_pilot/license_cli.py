"""
``tera-pilot license`` — offline license-key management (v2.3.4).

Customer commands:
    tera-pilot license activate <key>
    tera-pilot license status
    tera-pilot license deactivate

Seller commands (v2.3.5 — offline key issuance, zero telemetry):
    tera-pilot license gen-keypair --out <dir>            # new Ed25519 keypair
    tera-pilot license issue --private-key <key.pem> \\
        --customer <id> [--tier pro] [--expires ISO] [--features a,b,c]

All verification is fully offline (Ed25519 signature check against the
embedded public key + local clock expiry check — see ``licensing.py``).
No network call is ever made. Exit codes: 0 on success, 1 on verification
failure, 2 on usage errors. ``status`` exits 0 only when a valid license
(or the TERA_PILOT_PRO dev override) is active, so it can be used in
shell conditions.
"""

from __future__ import annotations

import json
import sys
from typing import List, Optional


def run_license_cli(argv: List[str]) -> int:
    """Dispatch ``tera-pilot license <subcommand>``. Returns the exit code."""
    if not argv:
        _usage()
        return 2
    cmd = argv[0]

    if cmd == "activate":
        if len(argv) < 2:
            print("usage: tera-pilot license activate <license-key>", file=sys.stderr)
            return 2
        from tera_pilot.licensing import LicenseError, activate_license
        try:
            info = activate_license(argv[1])
        except LicenseError as e:
            print(f"license activation failed: {e}", file=sys.stderr)
            return 1
        print(f"license activated — tier={info.tier} customer={info.customer_id}")
        print(f"  expires: {info.expires_at or 'never'}")
        print(f"  features: {', '.join(info.features) or '(none)'}")
        return 0

    if cmd == "status":
        from tera_pilot.licensing import get_license_status
        st = get_license_status()
        print(json.dumps(st, indent=2, ensure_ascii=False))
        if st.get("dev_override"):
            print("note: TERA_PILOT_PRO is set — local-dev override active (not for production)", file=sys.stderr)
        return 0 if st.get("valid") else 1

    if cmd == "deactivate":
        from tera_pilot.licensing import deactivate_license
        deactivate_license()
        print("license deactivated")
        return 0

    if cmd == "gen-keypair":
        # v2.3.5 (seller-side): generate a fresh Ed25519 keypair for
        # issuing licenses. Private key is written with 0600 perms; the
        # seller keeps it OFFLINE — only the public key is embedded in
        # the shipped package (tera_pilot/license_pubkey.pem).
        return _cmd_gen_keypair(argv[1:])

    if cmd == "issue":
        # v2.3.5 (seller-side): sign a license payload with the seller's
        # private key and print the license string for a customer.
        # Entirely offline — no network, no telemetry, no phone-home.
        return _cmd_issue(argv[1:])

    print(f"unknown license subcommand: {cmd}", file=sys.stderr)
    _usage()
    return 2


# ── Seller-side commands (offline key issuance) ─────────────────────

def _cmd_gen_keypair(argv: List[str]) -> int:
    out_dir = _flag_value(argv, "--out")
    if not out_dir:
        print("usage: tera-pilot license gen-keypair --out <dir>", file=sys.stderr)
        return 2
    from pathlib import Path
    from tera_pilot.licensing import generate_keypair

    target = Path(out_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
        priv_pem, pub_pem = generate_keypair()
        priv_path = target / "license_priv.pem"
        pub_path = target / "license_pubkey.pem"
        # 0600 for the private key — it signs every license.
        priv_path.write_bytes(priv_pem)
        try:
            priv_path.chmod(0o600)
        except OSError:
            pass
        pub_path.write_bytes(pub_pem)
    except Exception as e:
        print(f"keypair generation failed: {e}", file=sys.stderr)
        return 1
    print(f"keypair written:")
    print(f"  private: {priv_path}  (KEEP OFFLINE — never ship or commit this)")
    print(f"  public:  {pub_path}  (embed as tera_pilot/license_pubkey.pem)")
    return 0


def _cmd_issue(argv: List[str]) -> int:
    key_path = _flag_value(argv, "--private-key")
    customer = _flag_value(argv, "--customer")
    if not key_path or not customer:
        print(
            "usage: tera-pilot license issue --private-key <key.pem> "
            "--customer <id> [--tier pro] [--expires ISO] [--features a,b,c]",
            file=sys.stderr,
        )
        return 2
    tier = _flag_value(argv, "--tier") or "pro"
    expires = _flag_value(argv, "--expires")
    features = [
        f.strip() for f in (_flag_value(argv, "--features") or "").split(",")
        if f.strip()
    ]

    from tera_pilot.licensing import LicenseError, issue_license, load_private_key
    try:
        priv = load_private_key(key_path)
        key = issue_license(
            priv,
            customer_id=customer,
            tier=tier,
            features=features,
            expires_at=expires,
        )
    except LicenseError as e:
        print(f"license issuance failed: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"license issuance failed: {e}", file=sys.stderr)
        return 1

    print(f"license issued — customer={customer} tier={tier} expires={expires or 'never'}")
    if features:
        print(f"  features: {', '.join(features)}")
    print(f"\n{key}")
    return 0


def _flag_value(argv: List[str], flag: str) -> Optional[str]:
    """Return the value for ``--flag <value>`` or ``--flag=value``, else None."""
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg[len(flag) + 1:]
    return None


def _usage() -> None:
    print(
        "usage: tera-pilot license <activate <key> | status | deactivate>\n"
        "       tera-pilot license <gen-keypair --out <dir> | issue ...>  (seller-side, offline)",
        file=sys.stderr,
    )
