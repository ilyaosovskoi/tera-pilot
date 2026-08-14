"""
Tera Pilot CLI — диспетчер подкоманд.

    tera-pilot                 # Web UI (по умолчанию)
    tera-pilot doctor [...]    # environment doctor (P0 onboarding)
    tera-pilot audit [...]     # экспорт/верификация подписанного аудита

Всё, что не является подкомандой, делегируется ``web_server.main()``,
поэтому флаги Web UI (--host, --port, --project, --no-browser) работают
как раньше.
"""

from __future__ import annotations

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа CLI. Возвращает код выхода процесса."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        if args[0] == "doctor":
            from tera_pilot.environment_doctor import run_doctor_cli
            return run_doctor_cli(args[1:])
        if args[0] == "audit":
            from tera_pilot.audit_cli import run_audit_cli
            return run_audit_cli(args[1:])
    from tera_pilot.web_server import main as web_main
    return web_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
