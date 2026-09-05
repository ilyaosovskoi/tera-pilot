"""
Tera Pilot CLI — диспетчер подкоманд.

    tera-pilot                 # Web UI (по умолчанию)
    tera-pilot doctor [...]    # environment doctor (P0 onboarding)
    tera-pilot audit [...]     # экспорт/верификация подписанного аудита
    tera-pilot license [...]   # Pro-лицензия
    tera-pilot key [...]       # удобная настройка API-ключей
    tera-pilot fleet [...]     # несколько агентов-профилей сразу + главный терминал

Всё, что не является подкомандой, делегируется ``web_server.main()``,
поэтому флаги Web UI (--host, --port, --project, --no-browser) работают
как раньше.
"""

from __future__ import annotations

import sys
from typing import List, Optional


def _run_key(argv: List[str]) -> int:
    from tera_pilot.key_cli import run_key_cli

    return run_key_cli(argv)


def _run_fleet(argv: List[str]) -> int:
    """tera-pilot fleet — multi-agent profiles + main-terminal monitor."""
    import argparse

    parser = argparse.ArgumentParser(prog="tera-pilot fleet", description=(
        "Run several agent profiles at once and watch a summary from one terminal."
    ))
    sub = parser.add_subparsers(dest="sub")

    p_start = sub.add_parser("start", help="Start the fleet (foreground; Ctrl+C stops it)")
    p_start.add_argument("--fleet", default="default", help="Fleet id (default: default)")
    p_start.add_argument(
        "--agent", action="append", default=[], metavar="PROFILE[:WORKSPACE]",
        help="Agent to run: profile id, optionally with a workspace after ':' "
             "(e.g. --agent video:/path/to/videos). Repeat for several agents.",
    )
    p_start.add_argument("--section", default="general",
                         choices=["general", "heavy_code", "office"])
    p_start.add_argument("--max-iterations", type=int, default=None)
    # v2.4.1 (V240 §4.2): point the whole fleet at a specific provider/model
    # from the CLI instead of silently using config.json's active provider.
    p_start.add_argument("--provider", default=None,
                         help="Provider id for every agent (overrides config.json active_provider)")
    p_start.add_argument("--model", default=None,
                         help="Model for every agent (overrides config.json model)")
    p_start.add_argument("--api-base", default=None,
                         help="Provider API base URL (e.g. http://127.0.0.1:1234/v1)")

    p_watch = sub.add_parser("watch", help="Main-terminal summary of all agents")
    p_watch.add_argument("--fleet", default="default", help="Fleet id")
    p_watch.add_argument("--interval", type=float, default=1.0, help="Refresh seconds")
    p_watch.add_argument(
        "--stale-after", type=float, default=None,
        help="Seconds without a status heartbeat before an agent is treated as "
             "dead (default: 15). Lets `fleet watch` exit when the fleet "
             "process dies instead of hanging forever (V240 §4.1).",
    )

    p_task = sub.add_parser("task", help="Queue a task for a fleet agent")
    p_task.add_argument("--fleet", default="default", help="Fleet id")
    p_task.add_argument("agent", help="Agent id (profile id by default)")
    p_task.add_argument("prompt", help="Task prompt (quote it)")

    p_stop = sub.add_parser("stop", help="Signal all fleet workers to stop")
    p_stop.add_argument("--fleet", default="default", help="Fleet id")

    sub.add_parser("list", help="List fleets and their agent status")

    args = parser.parse_args(argv)

    if not args.sub:
        parser.print_help()
        return 0

    if args.sub == "start":
        from tera_pilot.fleet import start_fleet_cli

        agents = []
        for spec in args.agent:
            profile, _, workspace = spec.partition(":")
            profile = profile.strip()
            workspace = (workspace or ".").strip()
            if not profile:
                print(f"✗ bad --agent spec: {spec!r} (expected PROFILE[:WORKSPACE])")
                return 1
            agents.append({
                "profile": profile,
                "workspace": workspace,
                "section": args.section,
                "max_iterations": args.max_iterations,
            })
        if not agents:
            print("✗ no agents given. Example:")
            print("  tera-pilot fleet start --agent code:~/code --agent video:~/videos")
            return 1
        return start_fleet_cli(
            agents,
            fleet_id=args.fleet,
            provider=args.provider,
            model=args.model,
            api_base=args.api_base,
        )

    if args.sub == "task":
        from tera_pilot.fleet import submit_task

        r = submit_task(args.fleet, args.agent, args.prompt)
        if r.get("ok"):
            print(f"✓ queued for '{args.agent}' (task {r['task_id']}) → {r['mailbox']}")
            return 0
        print(f"✗ {r.get('error')}")
        return 1

    if args.sub == "watch":
        from tera_pilot.fleet import (
            collect_fleet_status, render_fleet_table,
            fleet_finished, fleet_dir,
        )
        from rich.console import Console
        from rich.live import Live

        if not fleet_dir(args.fleet).is_dir():
            print(f"✗ no such fleet: {args.fleet} (run: tera-pilot fleet start)")
            return 1
        stale_after = args.stale_after
        console = Console()
        with Live(console=console, refresh_per_second=1.0 / max(0.2, args.interval)) as live:
            try:
                while True:
                    statuses = collect_fleet_status(args.fleet)
                    table = render_fleet_table(statuses, fleet_id=args.fleet)
                    live.update(table)
                    # Exit when every agent is stopped OR stale (no heartbeat
                    # for --stale-after seconds) — v2.4.1 fix for the watch
                    # hanging forever after the fleet process dies (V240 §4.1).
                    if fleet_finished(
                        statuses,
                        stale_after=stale_after if stale_after is not None else 15.0,
                    ):
                        break
                    import time
                    time.sleep(max(0.2, args.interval))
            except KeyboardInterrupt:
                pass
        print("\n(fleet watch closed — run: tera-pilot fleet watch to reopen)")
        return 0

    if args.sub == "stop":
        from tera_pilot.fleet import stop_path, fleet_dir

        if not fleet_dir(args.fleet).is_dir():
            print(f"✗ no such fleet: {args.fleet}")
            return 1
        try:
            stop_path(args.fleet).write_text("stopped\n", encoding="utf-8")
        except OSError as e:
            print(f"✗ {e}")
            return 1
        print(f"✓ stop signal sent to fleet '{args.fleet}' — workers exit after the current task.")
        return 0

    if args.sub == "list":
        from tera_pilot.fleet import list_fleets

        fleets = list_fleets()
        if not fleets:
            print("No fleets yet. Start one: tera-pilot fleet start --agent code")
            return 0
        for f in fleets:
            state = "running" if f.get("running") else "stopped"
            print(f"[{state}] fleet '{f['fleet_id']}' — {len(f.get('agents', []))} agents")
            for a in f.get("agents", []):
                print(
                    f"    {a.get('agent_id', '?')}  [{a.get('state', '?')}]  "
                    f"tasks done: {a.get('tasks_done', 0)}  "
                    f"last: {(a.get('last_activity') or '')[:70]}"
                )
        return 0

    return 0


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
        if args[0] == "license":
            from tera_pilot.license_cli import run_license_cli
            return run_license_cli(args[1:])
        if args[0] == "key":
            return _run_key(args[1:])
        if args[0] == "fleet":
            return _run_fleet(args[1:])
    from tera_pilot.web_server import main as web_main
    return web_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
