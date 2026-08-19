"""Tera Pilot CLI entry point.

By default launches the Web UI. Subcommands:

    python -m tera_pilot doctor    # environment doctor (P0 onboarding)
    python -m tera_pilot audit     # signed audit export/verify
    python -m tera_pilot license   # offline license activate/status/deactivate
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
