"""Entry point: `python -m tera_pilot_tui`.

Thin CLI shim — parses a few optional overrides and launches the Textual app.
The full-screen TUI is deliberately separate from tera_pilot/cli.py (the traditional
one-shot argparse CLI).
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tera-pilot-tui",
        description="Full-screen terminal UI for the Tera Pilot agent.",
    )
    parser.add_argument("--workspace", "-w", default=os.getcwd(),
                        help="Workspace root (default: current directory).")
    parser.add_argument("--provider", "-p", default=None,
                        help="Provider id override (default: saved config).")
    parser.add_argument("--model", "-m", default=None,
                        help="Model override (default: saved config).")
    parser.add_argument("--api-base", default=None, help="API base URL override.")
    parser.add_argument("--section", default="general",
                        choices=["general", "heavy_code", "office"],
                        help="Runtime section (default: general).")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help=("Max agent iterations per turn (soft cap; the agent "
                              "auto-extends while it is still executing tools). "
                              "Default: agent_max_iterations from config.json, "
                              "then /budget iterations, then 8."))
    parser.add_argument("--planning", action="store_true", default=False,
                        help="Enable planning mode (agent creates a plan before executing).")
    args = parser.parse_args(argv)

    try:
        from tera_pilot_tui.app import TeraPilotTUIApp
        from tera_pilot_tui.bridge import TeraPilotBridge, ProviderChoice
    except ModuleNotFoundError as e:
        if "textual" in str(e):
            sys.stderr.write(
                "tera_pilot_tui requires the 'textual' package.\n"
                "Install it with:  pip install textual\n"
            )
            return 2
        raise

    bridge = TeraPilotBridge(
        workspace=args.workspace,
        provider=ProviderChoice(
            provider_id=args.provider,
            model=args.model,
            api_base=args.api_base,
        ),
        section=args.section,
        max_iterations=args.max_iterations,
        enable_planning=args.planning,
    )
    TeraPilotTUIApp(bridge=bridge).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
