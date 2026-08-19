"""
``tera-pilot license`` — offline license-key management (v2.3.4).

    tera-pilot license activate <key>
    tera-pilot license status
    tera-pilot license deactivate

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
from typing import List


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

    print(f"unknown license subcommand: {cmd}", file=sys.stderr)
    _usage()
    return 2


def _usage() -> None:
    print(
        "usage: tera-pilot license <activate <key> | status | deactivate>",
        file=sys.stderr,
    )
