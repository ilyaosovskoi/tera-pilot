"""
Audit export & verification CLI — ``tera-pilot audit`` (P0).

    tera-pilot audit export [--out PATH] [--unsigned]
    tera-pilot audit verify PATH

Export
    Writes the current activity log as JSON. By default — in the signed
    format (Ed25519 + hash chain via ``tera_pilot.audit_signing``),
    so substitution / reordering / deletion of records can be
    detected. ``--unsigned`` — the legacy flat format.

    Note: the activity log lives in process memory. In a fresh CLI process
    it is empty — to export real activity use the commands
    inside a running TUI/Web (slash commands /audit, /audit-signed),
    or run export from the same process where the agent worked.

Verify
    Verifies the signatures and hash chain of the exported file.
    Exit code: 0 — chain intact, 1 — tampering detected.

The format and cryptography are described in ``tera_pilot/audit_signing.py``
and in ``THREAT_MODEL.md`` (the "Verification & evidence" section).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def export_entries(
    entries: List[Dict[str, Any]],
    out_path: str,
    unsigned: bool = False,
) -> Tuple[int, int]:
    """Export a list of entries to a file. Returns (exit_code, count).

    ``unsigned=False`` — signed format via ``audit_signing``
    (generates keys on first call, stores them in ~/.tera_pilot/).
    """
    if unsigned:
        data = json.dumps(entries, indent=2, default=str, ensure_ascii=False)
    else:
        from tera_pilot.audit_signing import export_signed_json
        data = export_signed_json(entries)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(data, encoding="utf-8")
    return 0, len(entries)


def _cmd_export(out: Optional[str], unsigned: bool) -> int:
    from tera_pilot.activity_log import get_activity_log
    log = get_activity_log()
    try:
        if unsigned:
            data = log.export_json()
            fmt = "unsigned"
        else:
            data = log.export_signed_json()
            fmt = "signed (Ed25519 + hash chain)"
    except Exception as e:
        print(f"[audit] signed export unavailable ({e}); using unsigned")
        data = log.export_json()
        fmt = "unsigned (fallback)"

    if not out:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = str(Path.home() / ".tera_pilot" / f"audit_export_{stamp}.json")
    dest = Path(out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(data, encoding="utf-8")
    count = len(json.loads(data))
    print(f"[audit] exported entries: {count} ({fmt})")
    print(f"[audit] file: {out}")
    if count == 0:
        print("[audit] note: activity log is process-scoped; in a fresh CLI process it is empty. "
              "Export from inside a running TUI/Web (slash commands /audit, /audit-signed).")
    return 0


def _cmd_verify(path: str) -> int:
    from tera_pilot.audit_signing import verify_signed_file
    report = verify_signed_file(path)
    print(f"[audit] entries checked: {report.entries_checked}")
    if report.ok:
        print(f"[audit] OK — signatures verified: {report.signatures_valid}, hash chain intact.")
        return 0
    print(f"[audit] VIOLATION — {report.first_failure or 'unknown error'}"
          + (f" (entry #{report.first_failure_index})" if report.first_failure_index is not None else ""))
    return 1


def _print_usage() -> None:
    print("Tera Pilot audit CLI")
    print("Commands:")
    print("  tera-pilot audit export [--out PATH] [--unsigned]   export the activity log (signed by default)")
    print("  tera-pilot audit verify PATH                        verify signatures and the hash chain")


def run_audit_cli(argv: Optional[List[str]] = None) -> int:
    """CLI entry point: tera-pilot audit <export|verify> ..."""
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        _print_usage()
        return 2
    sub = args[0]
    if sub == "export":
        rest = args[1:]
        unsigned = "--unsigned" in rest
        out: Optional[str] = None
        if "--out" in rest:
            i = rest.index("--out")
            if i + 1 < len(rest):
                out = rest[i + 1]
        return _cmd_export(out, unsigned)
    if sub == "verify":
        if len(args) < 2:
            print("usage: tera-pilot audit verify <file>")
            return 2
        return _cmd_verify(args[1])
    _print_usage()
    return 2
