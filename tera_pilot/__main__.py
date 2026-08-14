"""Tera Pilot CLI entry point.

По умолчанию запускает Web UI. Подкоманды:

    python -m tera_pilot doctor    # environment doctor (P0 onboarding)
    python -m tera_pilot audit     # экспорт/верификация подписанного аудита
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
