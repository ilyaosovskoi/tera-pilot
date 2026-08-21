#!/usr/bin/env python3
"""eval/run_all.py — batch-run the real agent over the eval task set.

Boots a Tera Pilot web server in-process (or reuses a running one) and
drives every selected task through the same SSE endpoint the Web UI uses
(``POST /api/agent/stream``), recording a baseline for each task, then
aggregates the results into a metrics report.

Usage:
    # All 43 tasks, in-process server:
    python3 -m eval.run_all

    # Only the CI smoke set (10 tasks across all 6 categories):
    python3 -m eval.run_all --subset smoke

    # Tasks from one category:
    python3 -m eval.run_all --category bug_fix

    # Explicit task ids:
    python3 -m eval.run_all --tasks fix-missing-return,add-clamp-function

    # Reuse a server that is already running:
    python3 -m eval.run_all --api-base http://127.0.0.1:18732 --api-token <token>

    # Skip tasks that already have a result in --out:
    python3 -m eval.run_all --skip-existing

Output: per-task result JSON in --out (default eval/results) and a printed
metrics report (success rate, test pass rate, cost, latency, tokens,
iterations, tool histogram, per-category breakdown).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eval import schema  # noqa: E402
from eval import runner as er  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent / "results"
DEFAULT_PORT = 18777


# ── Server bootstrap ──────────────────────────────────────────────────

def boot_server(port: int = DEFAULT_PORT) -> "tuple[str, str]":
    """Start a TeraPilotWebServer in-process on *port*.

    Returns (api_base, api_token). The server runs in a background
    daemon thread; the caller is responsible for calling stop().
    """
    from tera_pilot.web_server import TeraPilotWebServer

    srv = TeraPilotWebServer(
        host="127.0.0.1",
        port=port,
        project=str(PROJECT_ROOT),
        open_browser=False,
    )
    srv.start()
    # Wait until /api/status answers.
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{srv.port}/api/status",
                    headers={"Authorization": f"Bearer {srv.api_token}"},
                ),
                timeout=2,
            ) as resp:
                resp.read()
            break
        except Exception:
            time.sleep(0.3)
    else:
        srv.stop()
        raise RuntimeError("web server did not become ready in time")
    return f"http://127.0.0.1:{srv.port}", srv.api_token, srv


# ── Task selection ────────────────────────────────────────────────────

def select_tasks(tasks_dir: Path, args) -> "list[tuple[str, dict]]":
    """Return [(task_id, manifest)] according to --subset/--category/--tasks."""
    all_tasks = list(er.iter_tasks(str(tasks_dir)))
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
        selected = [(tid, m) for tid, m in all_tasks if tid in wanted]
        missing = wanted - {tid for tid, _ in selected}
        if missing:
            print(f"warning: unknown task id(s): {sorted(missing)}", file=sys.stderr)
        return selected
    if args.category:
        return [(tid, m) for tid, m in all_tasks if m.get("category") == args.category]
    if args.subset == "smoke":
        smoke = json.loads(er.SMOKE_FILE.read_text(encoding="utf-8"))
        by_id = {tid: m for tid, m in all_tasks}
        return [(tid, by_id[tid]) for tid in smoke if tid in by_id]
    return all_tasks


# ── One-task driver (mirrors eval.runner.cmd_run but in-process) ─────

def run_one(task_id: str, manifest: dict, api_base: str, api_token: str,
            out_dir: Path, timeout_secs: int) -> dict:
    """Run one task end-to-end; return the schema-valid result dict.

    ``timeout_secs`` (default: manifest timeout + 120 s grace) bounds the
    whole task including baseline + test runs; a task that exceeds it is
    reported as ``error`` so the batch never hangs on a stuck task.
    """
    deadline = time.time() + (timeout_secs or (manifest.get("timeout_secs", 300) + 120))

    def _within() -> bool:
        return time.time() < deadline

    task_dir = Path(manifest["_dir"])
    workspace, base_hash, base_commit = er.make_clean_workspace(manifest)
    try:
        # Baseline: tests on the pristine repo.
        base_start = time.perf_counter()
        baseline = er.run_test_command(
            workspace, manifest.get("test_command"), manifest.get("test_timeout_secs", 120)
        )
        baseline["duration_sec"] = time.perf_counter() - base_start
        if not _within():
            raise TimeoutError(f"task {task_id} exceeded its wall-clock deadline")

        driver_out = er.run_api_driver(
            manifest, workspace, api_base, api_token,
            request_timeout=max(120, int(deadline - time.time())),
        )
        if not _within():
            raise TimeoutError(f"task {task_id} exceeded its wall-clock deadline")
        test_res = er.run_test_command(
            workspace, manifest.get("test_command"), manifest.get("test_timeout_secs", 120)
        )
        # v2.3.4 (P1.8): record the ACTUAL diff the agent produced.
        diff = er.compute_workspace_diff(manifest, workspace)
        result = er.build_result(
            manifest, driver_out, test_res, base_hash, base_commit,
            "api", 0.0, baseline, diff=diff,
        )
        schema.validate_result(result)
        return result
    finally:
        er.cleanup_workspace(workspace)


# ── Report ────────────────────────────────────────────────────────────

def aggregate(results: "list[dict]") -> dict:
    """Compute the aggregated metrics report over result dicts."""
    total = len(results)
    by_status: dict = {}
    passed = 0
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["metrics"]["test_passed"] is True:
            passed += 1
    categories: dict = {}
    for r in results:
        cat = r["category"]
        categories.setdefault(cat, {"total": 0, "passed": 0, "success": 0})
        categories[cat]["total"] += 1
        if r["metrics"]["test_passed"] is True:
            categories[cat]["passed"] += 1
        if r["status"] == "success":
            categories[cat]["success"] += 1

    tools_used: dict = {}
    for r in results:
        for t in r["metrics"].get("tools_used", []):
            tools_used[t] = tools_used.get(t, 0) + 1

    def _sum(key):
        return sum(r["metrics"].get(key, 0) or 0 for r in results)

    durations = [r["metrics"]["duration_sec"] for r in results]
    durations = [d for d in durations if d is not None]
    return {
        "tasks": total,
        "by_status": by_status,
        "test_pass_rate": f"{passed}/{total}",
        "test_passed": passed,
        "by_category": categories,
        "cost_usd": round(_sum("cost_usd"), 4),
        "tokens": _sum("tokens"),
        "tokens_in": _sum("tokens_in"),
        "tokens_out": _sum("tokens_out"),
        "requests": _sum("request_count"),
        "iterations": _sum("iterations"),
        "duration_sec_total": round(sum(durations), 1),
        "duration_sec_avg": round(sum(durations) / max(1, len(durations)), 1),
        "tools_used": dict(sorted(tools_used.items(), key=lambda kv: -kv[1])),
        "provider": next((r.get("provider") for r in results if r.get("provider")), None),
        "model": next((r.get("model") for r in results if r.get("model")), None),
        "run_date": datetime.now(timezone.utc).isoformat(),
    }


def print_report(rep: dict) -> None:
    print("\n" + "=" * 62)
    print("AGENT EVAL REPORT (api driver, real provider)")
    print("=" * 62)
    print(f"  date:          {rep['run_date']}")
    print(f"  provider:      {rep['provider']}")
    print(f"  model:         {rep['model']}")
    print(f"  tasks:         {rep['tasks']}  by status: {rep['by_status']}")
    print(f"  test pass:     {rep['test_pass_rate']}")
    print(f"  cost:          ${rep['cost_usd']:.4f}")
    print(f"  tokens:        {rep['tokens']:,}  (in={rep['tokens_in']:,} out={rep['tokens_out']:,})")
    print(f"  requests:      {rep['requests']}")
    print(f"  iterations:    {rep['iterations']}")
    print(f"  duration:      {rep['duration_sec_total']}s total, {rep['duration_sec_avg']}s avg/task")
    print(f"  tools:         {', '.join(f'{k}×{v}' for k, v in rep['tools_used'].items()) or 'none'}")
    print("  by category:")
    for cat, c in sorted(rep["by_category"].items()):
        print(f"    {cat:14s} {c['success']}/{c['total']} success, {c['passed']}/{c['total']} tests passed")


# ── CLI ───────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="eval.run_all", description=__doc__)
    p.add_argument("--tasks-dir", default=str(Path(__file__).resolve().parent / "tasks"))
    p.add_argument("--subset", choices=("all", "smoke"), default="all")
    p.add_argument("--category", default=None, help="only tasks from this category")
    p.add_argument("--tasks", default=None, help="comma-separated task ids")
    p.add_argument("--api-base", default=None, help="reuse a running server instead of booting one")
    p.add_argument("--api-token", default=None)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--skip-existing", action="store_true",
                   help="skip tasks that already have a result file in --out")
    p.add_argument("--task-timeout", type=int, default=None,
                   help="per-task wall-clock deadline in seconds "
                        "(default: manifest timeout_secs + 120)")
    p.add_argument("--keep-workspace", action="store_true")
    # v2.3.4 (P1.6): run each selected task N times on fresh workspaces —
    # the plan's "5-10 repeats per task on one model". Each repeat gets
    # its own clean workspace and its own result file.
    p.add_argument("--repeat", type=int, default=1,
                   help="run each task N times on fresh workspaces (default 1)")
    args = p.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = select_tasks(Path(args.tasks_dir), args)
    if not selected:
        print("no tasks selected", file=sys.stderr)
        return 1
    print(f"selected {len(selected)} task(s): {', '.join(t for t, _ in selected)}")

    if args.api_base:
        api_base, api_token, srv = args.api_base.rstrip("/"), args.api_token, None
        if not api_token:
            print("warning: --api-token not provided; mutating endpoints may 401", file=sys.stderr)
    else:
        api_base, api_token, srv = boot_server(args.port)
        print(f"booted in-process web server: {api_base}")

    try:
        results = []
        t_start = time.time()
        repeat = max(1, int(args.repeat or 1))
        for i, (task_id, manifest) in enumerate(selected, 1):
            # Skip if a result for this task already exists.
            if args.skip_existing and any(out_dir.glob(f"{task_id}_*.json")):
                print(f"[{i}/{len(selected)}] {task_id}: already has a result — skipping")
                continue
            for r in range(1, repeat + 1):
                label = task_id if repeat == 1 else f"{task_id} (repeat {r}/{repeat})"
                print(f"[{i}/{len(selected)}] running {label} ...", flush=True)
                t0 = time.time()
                try:
                    result = run_one(
                        task_id, manifest, api_base, api_token, out_dir,
                        timeout_secs=args.task_timeout,
                    )
                except Exception as e:
                    print(f"  ! {task_id} FAILED to run: {e}", file=sys.stderr)
                    continue
                dt = time.time() - t0
            # Driver doesn't report duration — time it here.
            result["metrics"]["duration_sec"] = round(dt, 3)
            fname = f"{task_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{__import__('uuid').uuid4().hex[:6]}.json"
            (out_dir / fname).write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
            )
            status = result["status"]
            test = result["metrics"]["test_passed"]
            print(f"  -> {task_id}: status={status} tests_passed={test} "
                  f"cost=${result['metrics']['cost_usd']:.4f} {dt:.1f}s")
            results.append(result)
        print(f"\ntotal wall time: {time.time() - t_start:.0f}s")

        if results:
            rep = aggregate(results)
            print_report(rep)
            rep_path = out_dir / "aggregate_report.json"
            rep_path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\naggregate report -> {rep_path}")
        return 0
    finally:
        if srv is not None:
            srv.stop()


if __name__ == "__main__":
    sys.exit(main())
