"""Tera Pilot evaluation harness (P0.1) — run reproducible repository tasks.

Each task in `eval/tasks/<task_id>/` is a manifest (`task.json`) plus a
small fixture repository (`repo/`). The runner:

1. copies the fixture repo into a clean temp workspace (never touches the
   original fixture);
2. records the *baseline* — the pristine repo's `test_command` result (and
   the git base commit, when the fixture is a git repo);
3. drives the agent with the task prompt via a pluggable driver;
4. runs the task's `test_command` again inside the workspace;
5. writes a schema-valid result JSON into `eval/results/` (or --out).

Drivers:
- fake — deterministic, no network. Used by CI and schema tests to validate
  the whole pipeline (and to record the *baseline*: tests failing before
  the agent does anything).
- api  — drives a running Tera Pilot web server through the same SSE
  endpoint the Web UI uses (`POST /api/agent/stream`). Requires a
  configured provider on the server side. Token/cost are read from the
  server's usage tracker (`GET /api/usage/get` delta) when available,
  falling back to SSE token-event counting.

Commands:
    python3 -m eval.runner run eval/tasks/fix-config-loader-empty-file --driver fake
    python3 -m eval.runner run eval/tasks/fix-config-loader-empty-file \\
        --driver api --api-base http://127.0.0.1:18732 --api-token <token>
    python3 -m eval.runner check                      # structural check of all tasks
    python3 -m eval.runner smoke                      # fake-driver smoke set (CI)
    python3 -m eval.runner report --dir eval/results  # summary (+ --json)

Claims discipline (see TERA_PILOT_PRODUCT_READINESS.md): a result never
conflates "agent finished" with "tests passed". `status` describes the run,
`metrics.test_passed` the test execution, `metrics.verification_status` the
verification step, `workspace.baseline` the pristine repo state.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import schema, __version__

RUNNER_VERSION = __version__
RESULTS_DIR = Path(__file__).resolve().parent / "results"
SMOKE_FILE = Path(__file__).resolve().parent / "smoke.json"
REQUIRED_TASK_FIELDS = ("id", "name", "category", "prompt")
VALID_CATEGORIES = ("bug_fix", "test_repair", "refactor", "feature", "code_review", "documentation")
VALID_BASELINE = ("failing", "passing", "unknown")

#: Directories never included in the repo fingerprint — git internals and
#: bytecode caches are environment artifacts, not task content.
_FINGERPRINT_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv"}


# ── Task loading ──────────────────────────────────────────────────────

def load_task(task_dir):
    """Load and validate a task manifest from ``task_dir``.

    Returns the manifest dict with two private helpers added: ``_dir``
    (the task directory) and ``_repo_dir`` (the fixture repo directory).
    Raises ValueError on any structural problem.
    """
    task_dir = Path(task_dir)
    if not task_dir.is_dir():
        raise ValueError(f"task directory not found: {task_dir}")
    manifest_path = task_dir / "task.json"
    if not manifest_path.is_file():
        raise ValueError(f"task.json missing in {task_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"task.json is not valid JSON in {task_dir}: {exc}")
    if not isinstance(manifest, dict):
        raise ValueError(f"task.json must contain a JSON object in {task_dir}")
    for field in REQUIRED_TASK_FIELDS:
        if not manifest.get(field):
            raise ValueError(f"task.json missing required field '{field}' in {task_dir}")
    if manifest.get("category") not in VALID_CATEGORIES:
        raise ValueError(f"invalid category in {task_dir}: {manifest.get('category')!r}")
    if manifest.get("baseline_status") not in (None,) + VALID_BASELINE:
        raise ValueError(f"invalid baseline_status in {task_dir}: {manifest.get('baseline_status')!r}")
    test_command = manifest.get("test_command")
    if test_command is not None and not (
        isinstance(test_command, list) and all(isinstance(c, str) for c in test_command)
    ):
        raise ValueError(f"test_command must be a list of strings in {task_dir}")
    repo_dir = task_dir / manifest.get("repo", "repo")
    if not repo_dir.is_dir():
        raise ValueError(f"fixture repo dir missing: {repo_dir}")
    manifest["_dir"] = str(task_dir)
    manifest["_repo_dir"] = str(repo_dir)
    return manifest


def iter_tasks(tasks_dir):
    """Yield (task_id, manifest) for every task under ``tasks_dir``."""
    tasks_dir = Path(tasks_dir)
    if not tasks_dir.is_dir():
        raise ValueError(f"tasks directory not found: {tasks_dir}")
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir() or not (task_dir / "task.json").is_file():
            continue
        manifest = load_task(str(task_dir))
        yield manifest["id"], manifest


# ── Clean workspace ──────────────────────────────────────────────────

def make_clean_workspace(task):
    """Copy the fixture repo into a fresh temp dir; return the repo copy,
    its fingerprint and (when the fixture is a git repo) its base commit.

    Git fixtures are stored as ``git bundle`` files (``git_bundle`` in the
    manifest): a nested ``.git`` directory cannot be committed to this repo
    itself, so without the bundle the fixture would have no history and no
    tags on fresh checkouts / CI. The bundle is cloned at run time, giving
    the agent real history + tags on any machine.

    The fingerprint/commit are computed on the pristine copy BEFORE any
    agent runs, so ``workspace.repo_hash`` / ``workspace.commit`` always
    describe the state the task started from (never post-agent
    modifications). The caller is responsible for cleanup (see
    ``cleanup_workspace``).
    """
    ws = Path(tempfile.mkdtemp(prefix="tp_eval_"))
    repo = ws / "repo"
    bundle = task.get("git_bundle")
    if bundle:
        bundle_path = Path(task["_dir"]) / bundle
        try:
            subprocess.run(
                ["git", "clone", "-q", str(bundle_path), str(repo)],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"failed to materialize git fixture from {bundle_path}: {exc}"
            )
    else:
        shutil.copytree(task["_repo_dir"], repo)
    return repo, repo_fingerprint(repo), repo_base_commit(repo)


def cleanup_workspace(workspace):
    """Remove a temporary workspace. Never raises — best effort."""
    try:
        if workspace is not None:
            shutil.rmtree(workspace.parent if workspace.name == "repo" else workspace)
    except OSError:
        pass


def repo_fingerprint(repo_dir):
    """SHA-256 over sorted relative paths + file contents — a reproducible
    base snapshot of the workspace the task started from. Skips git
    internals and bytecode caches (environment artifacts)."""
    h = hashlib.sha256()
    for p in sorted(Path(repo_dir).rglob("*")):
        if not p.is_file():
            continue
        if any(part in _FINGERPRINT_SKIP_DIRS for part in p.relative_to(repo_dir).parts[:-1]):
            continue
        h.update(p.relative_to(repo_dir).as_posix().encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def repo_base_commit(repo_dir):
    """Return the git HEAD commit of ``repo_dir``, or None when it is not
    a git repository."""
    if not (Path(repo_dir) / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# ── Drivers ──────────────────────────────────────────────────────────

def run_fake_driver(task, workspace):
    """Deterministic CI driver: no LLM, no file changes, zero cost."""
    return {
        "provider": None,
        "model": None,
        "tokens": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "request_count": 0,
        "cost_usd": 0.0,
        "iterations": 0,
        "tools_used": [],
        "final_output": "[fake driver] no agent run (CI stub)",
        "status": "skipped",
    }


def _usage_stats(api_base, api_token):
    """Best-effort read of the server's usage tracker (total tokens/cost).
    Returns a dict or None on any failure — never raises."""
    try:
        req = urllib.request.Request(api_base.rstrip("/") + "/api/usage/get")
        if api_token:
            req.add_header("Authorization", "Bearer " + api_token)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        stats = data.get("stats") or data.get("data") or data
        if not isinstance(stats, dict):
            return None
        return {
            "tokens_in": int(stats.get("total_tokens_in", 0) or 0),
            "tokens_out": int(stats.get("total_tokens_out", 0) or 0),
            "tokens": int(stats.get("total_tokens", 0) or 0),
            "cost": float(stats.get("total_cost", 0.0) or 0.0),
            "requests": int(stats.get("request_count", 0) or 0),
        }
    except Exception:
        return None


def run_api_driver(task, workspace, api_base, api_token=None):
    """Drive a running Tera Pilot instance over POST /api/agent/stream.

    SSE event contract matches tera_pilot/api_server.py:
    chat_info / router_decision / token / step / done / error.

    Token/cost come from the server's usage tracker (`GET /api/usage/get`)
    measured before and after the run, falling back to SSE token-event
    counting when the usage endpoint is unavailable.

    Mutating endpoints are protected by the bearer token (CSRF-to-localhost
    defense), so `api_token` is forwarded as `Authorization: Bearer …` when
    provided.

    The agent blocks on a `diff_review` SSE event until the UI answers
    (`POST /api/agent/diff_review`). This driver is headless — no human is
    watching — so it auto-accepts every diff the same way the browser UI
    does when "Apply" is clicked. Without this, every file write stalls
    for the 300 s review timeout and the run dies with a timeout.
    """
    before = _usage_stats(api_base, api_token)
    payload = {"text": task["prompt"], "project_root": str(workspace)}
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = "Bearer " + api_token
    req = urllib.request.Request(
        api_base.rstrip("/") + "/api/agent/stream",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )

    def _auto_accept_diff_review(review_id: str) -> None:
        """Accept a pending diff review so the agent can continue.
        Best effort — never raises (a review response failure must not
        abort the whole run)."""
        try:
            hdrs = {"Content-Type": "application/json"}
            if api_token:
                hdrs["Authorization"] = "Bearer " + api_token
            resp_req = urllib.request.Request(
                api_base.rstrip("/") + "/api/agent/diff_review",
                data=json.dumps({"accepted": True, "review_id": review_id}).encode("utf-8"),
                headers=hdrs,
                method="POST",
            )
            with urllib.request.urlopen(resp_req, timeout=10) as resp:
                resp.read()
        except Exception:
            pass
    tokens = 0
    cost = 0.0
    iterations = 0
    tools = []
    final = ""
    status = "error"
    provider = None
    model = None
    cancelled = False
    try:
        with urllib.request.urlopen(req, timeout=task.get("timeout_secs", 300)) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    evt = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if etype == "token":
                    tokens += 1
                    final += evt.get("content", "")
                elif etype == "step":
                    iterations += 1
                    if evt.get("tool"):
                        tools.append(evt["tool"])
                elif etype == "diff_review":
                    # Headless driver: auto-accept so the agent never
                    # stalls on the 300 s review timeout.
                    _auto_accept_diff_review(evt.get("review_id", ""))
                elif etype == "router_decision":
                    provider = evt.get("provider_id") or evt.get("provider")
                    model = evt.get("model")
                elif etype == "done":
                    status = "success" if evt.get("ok", True) else "failed"
                    cancelled = bool(evt.get("cancelled"))
                    final = final or evt.get("output") or evt.get("text") or ""
                elif etype == "error":
                    status = "error"
                    final = evt.get("message", "")
    except urllib.error.HTTPError as exc:
        status = "error"
        final = f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        status = "error"
        final = f"cannot reach {api_base}: {exc.reason}"
    except TimeoutError:
        status = "error"
        final = f"request timed out after {task.get('timeout_secs', 300)}s"
    except Exception as exc:  # pragma: no cover - defensive
        status = "error"
        final = f"unexpected driver error: {exc}"

    after = _usage_stats(api_base, api_token)
    tokens_in = tokens_out = request_count = 0
    if before and after:
        tokens_in = max(0, after["tokens_in"] - before["tokens_in"])
        tokens_out = max(0, after["tokens_out"] - before["tokens_out"])
        cost = max(0.0, round(after["cost"] - before["cost"], 6))
        request_count = max(0, after["requests"] - before["requests"])
        tokens = tokens_in + tokens_out if tokens == 0 else tokens
    if cancelled and status == "success":
        status = "failed"

    return {
        "provider": provider,
        "model": model,
        "tokens": tokens,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "request_count": request_count,
        "cost_usd": cost,
        "iterations": iterations,
        "tools_used": sorted(set(tools)),
        "final_output": final,
        "status": status,
        "cancelled": cancelled,
    }


DRIVERS = {"fake": run_fake_driver, "api": run_api_driver}


# ── Test execution ───────────────────────────────────────────────────

def run_test_command(workspace, test_command, timeout_secs=120):
    """Run ``test_command`` inside ``workspace``; never raises.

    Returns {"test_exit_code", "test_passed", "test_output"} where
    ``test_passed`` is True/False when the command ran and None when it
    could not be executed at all (e.g. missing binary).
    """
    if not test_command:
        return {"test_exit_code": None, "test_passed": None, "test_output": ""}
    try:
        proc = subprocess.run(
            list(test_command),
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout_secs,
        )
        out = (proc.stdout or "")[-4000:] + (proc.stderr or "")[-2000:]
        return {
            "test_exit_code": proc.returncode,
            "test_passed": proc.returncode == 0,
            "test_output": out,
        }
    except subprocess.TimeoutExpired:
        return {"test_exit_code": None, "test_passed": False, "test_output": "[test timed out]"}
    except FileNotFoundError:
        return {"test_exit_code": None, "test_passed": None, "test_output": "[test command not found]"}


# ── Result assembly ──────────────────────────────────────────────────

def build_result(task, driver_out, test_res, base_repo_hash, base_commit, driver, duration_sec, baseline=None):
    """Assemble a schema-v1 result. Never conflates run/test/verification.

    ``baseline`` is the optional result of running ``test_command`` on the
    pristine repo (recorded for the fake driver always, for the api driver
    with ``--baseline``).
    """
    if driver_out["status"] == "error":
        status = "error"
    elif driver_out["status"] == "skipped":
        status = "skipped"
    else:
        status = "success" if test_res["test_passed"] else "failed"

    if not task.get("test_command"):
        verification_status = "not_run"
    elif test_res["test_passed"] is True:
        verification_status = "passed"
    elif test_res["test_passed"] is False:
        verification_status = "failed"
    else:
        verification_status = "unknown"

    workspace = {"repo_hash": base_repo_hash, "commit": base_commit}
    if baseline is not None:
        workspace["baseline"] = {
            "test_passed": baseline["test_passed"],
            "test_exit_code": baseline["test_exit_code"],
            "duration_sec": round(baseline.get("duration_sec", 0.0), 3),
        }

    metrics = {
        "duration_sec": round(duration_sec, 3),
        "iterations": driver_out.get("iterations", 0),
        "tokens": driver_out.get("tokens", 0),
        "cost_usd": driver_out.get("cost_usd", 0.0),
        "tools_used": driver_out.get("tools_used", []),
        "test_command": task.get("test_command"),
        "test_passed": test_res["test_passed"],
        "test_exit_code": test_res["test_exit_code"],
        "test_output": test_res["test_output"],
        "verification_status": verification_status,
    }
    for optional in ("tokens_in", "tokens_out", "request_count", "cancelled"):
        if driver_out.get(optional) is not None:
            metrics[optional] = driver_out[optional]

    return {
        "schema_version": schema.SCHEMA_VERSION,
        "task_id": task["id"],
        "task_name": task["name"],
        "category": task["category"],
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "runner_version": RUNNER_VERSION,
        "driver": driver,
        "provider": driver_out.get("provider"),
        "model": driver_out.get("model"),
        "workspace": workspace,
        "prompt": task["prompt"],
        "metrics": metrics,
        "final_output": driver_out.get("final_output", ""),
    }


# ── CLI ──────────────────────────────────────────────────────────────

def cmd_run(args):
    task = load_task(args.task)
    workspace, base_repo_hash, base_commit = make_clean_workspace(task)
    baseline = None
    try:
        if args.baseline or args.driver == "fake":
            base_start = time.perf_counter()
            baseline = run_test_command(
                workspace, task.get("test_command"), task.get("test_timeout_secs", 120)
            )
            baseline["duration_sec"] = time.perf_counter() - base_start
        driver_fn = DRIVERS[args.driver]
        start = time.perf_counter()
        if args.driver == "api":
            driver_out = driver_fn(task, workspace, args.api_base, args.api_token)
        else:
            driver_out = driver_fn(task, workspace)
        duration = time.perf_counter() - start
        test_res = run_test_command(
            workspace, task.get("test_command"), task.get("test_timeout_secs", 120)
        )
        result = build_result(
            task, driver_out, test_res, base_repo_hash, base_commit, args.driver, duration, baseline
        )
        try:
            schema.validate_result(result)
        except ValueError as exc:  # a bug in the harness — never write bad results
            print(f"internal error: result failed schema validation: {exc}", file=sys.stderr)
            return 1
        out_dir = Path(args.out or RESULTS_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{task['id']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.json"
        (out_dir / fname).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(
            {k: result[k] for k in ("task_id", "status", "timestamp")},
            indent=2, ensure_ascii=False,
        ))
        print(f"result -> {out_dir / fname}")
        if args.keep_workspace:
            print(f"workspace kept -> {workspace.parent}")
        return 0
    finally:
        if not args.keep_workspace:
            cleanup_workspace(workspace)


def _check_one_task(manifest, task_dir):
    """Structural checks for a single task manifest. Returns a list of
    problem strings (empty = OK)."""
    problems = []
    gold_dir = Path(task_dir) / "gold"
    if not gold_dir.is_dir() or not any(gold_dir.iterdir()):
        problems.append("gold/ reference solution directory is empty or missing")
    if manifest.get("test_command") and manifest.get("baseline_status") is None:
        problems.append("tasks with test_command should declare 'baseline_status' (failing|passing|unknown)")
    timeout = manifest.get("timeout_secs")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        problems.append("timeout_secs must be a positive number")
    bundle = manifest.get("git_bundle")
    if bundle is not None:
        if not isinstance(bundle, str) or not (Path(task_dir) / bundle).is_file():
            problems.append("git_bundle must reference an existing bundle file in the task dir")
    return problems


def cmd_check(args):
    """Structural validation of every task under --dir. Fast, no test runs."""
    tasks_dir = Path(args.dir)
    problems = []
    count = 0
    try:
        for task_id, manifest in iter_tasks(tasks_dir):
            count += 1
            for prob in _check_one_task(manifest, Path(manifest["_dir"])):
                problems.append(f"{task_id}: {prob}")
    except ValueError as exc:
        print(f"check failed: {exc}", file=sys.stderr)
        return 1
    if problems:
        print(f"check: {count} tasks, {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"check: {count} tasks OK (structure valid, gold/ present)")
    return 0


def cmd_smoke(args):
    """Run the fake driver on the curated smoke set (eval/smoke.json) and
    verify the declared baseline status holds. CI entry point."""
    if not SMOKE_FILE.is_file():
        print(f"smoke set file missing: {SMOKE_FILE}", file=sys.stderr)
        return 1
    smoke_ids = json.loads(SMOKE_FILE.read_text(encoding="utf-8"))
    if not isinstance(smoke_ids, list) or not smoke_ids:
        print(f"smoke.json must be a non-empty list of task ids: {SMOKE_FILE}", file=sys.stderr)
        return 1
    failures = []
    ok = 0
    for task_id in smoke_ids:
        task_dir = Path(args.dir) / task_id
        if not task_dir.is_dir():
            failures.append(f"{task_id}: task directory not found")
            continue
        try:
            manifest = load_task(str(task_dir))
        except ValueError as exc:
            failures.append(f"{task_id}: {exc}")
            continue
        code = cmd_run(argparse.Namespace(
            task=str(task_dir),
            driver="fake",
            api_base=None,
            api_token=None,
            out=args.out,
            baseline=False,
            keep_workspace=False,
        ))
        if code != 0:
            failures.append(f"{task_id}: run failed (exit {code})")
            continue
        # Verify the declared baseline against the pristine repo.
        expected = manifest.get("baseline_status", "unknown")
        workspace, _, _ = make_clean_workspace(manifest)
        try:
            baseline = run_test_command(
                workspace, manifest.get("test_command"), manifest.get("test_timeout_secs", 120)
            )
        finally:
            cleanup_workspace(workspace)
        actual = "passing" if baseline["test_passed"] is True else ("failing" if baseline["test_passed"] is False else "unknown")
        if expected != "unknown" and actual != expected:
            failures.append(
                f"{task_id}: baseline mismatch — manifest says '{expected}', pristine repo is '{actual}'"
            )
        else:
            ok += 1
    if failures:
        print(f"smoke: {ok} ok, {len(failures)} failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"smoke: {len(smoke_ids)}/{len(smoke_ids)} tasks OK (fake driver, baseline verified)")
    return 0


def cmd_report(args):
    dir_path = Path(args.dir)
    # schema.json lives in the results dir but is not a result — skip it.
    files = sorted(p for p in dir_path.glob("*.json") if p.name != "schema.json")
    if not files:
        print(f"no results in {dir_path}")
        return 1
    rows = []
    invalid = 0
    for f in files:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            schema.validate_result(r)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[invalid] {f.name}: {exc}")
            invalid += 1
            continue
        rows.append(r)
    statuses = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    passed = sum(1 for r in rows if r["metrics"]["test_passed"] is True)
    categories = {}
    for r in rows:
        categories.setdefault(r["category"], {"total": 0, "passed": 0})
        categories[r["category"]]["total"] += 1
        if r["metrics"]["test_passed"] is True:
            categories[r["category"]]["passed"] += 1
    avg_dur = sum(r["metrics"]["duration_sec"] for r in rows) / max(1, len(rows))
    total_cost = sum(r["metrics"]["cost_usd"] for r in rows)
    total_tokens = sum(r["metrics"]["tokens"] for r in rows)

    summary = {
        "results": len(rows),
        "invalid": invalid,
        "by_status": statuses,
        "test_pass_rate": f"{passed}/{len(rows)}",
        "by_category": categories,
        "avg_duration_sec": round(avg_dur, 2),
        "total_cost_usd": round(total_cost, 4),
        "total_tokens": total_tokens,
    }
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    print(f"results: {len(rows)}  |  by status: {statuses}" + (f"  |  invalid: {invalid}" if invalid else ""))
    print(f"test pass rate: {passed}/{len(rows)}  |  avg duration: {avg_dur:.2f}s"
          f"  |  cost: ${total_cost:.4f}  |  tokens: {total_tokens}")
    for cat, c in sorted(categories.items()):
        print(f"  {cat:14s} {c['passed']}/{c['total']} passed")
    for r in sorted(rows, key=lambda x: x["task_id"]):
        print(f"  {r['task_id']:42s} {r['status']:8s} tests={r['metrics']['test_passed']}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="eval.runner",
        description="Tera Pilot evaluation harness (P0.1)",
    )
    parser.add_argument("--version", action="version", version=f"eval.runner {RUNNER_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run one evaluation task")
    run_p.add_argument("task", help="path to eval/tasks/<task_id>")
    run_p.add_argument(
        "--driver",
        choices=sorted(DRIVERS),
        default="api",
        help="agent driver (default: api against a running Tera Pilot server)",
    )
    run_p.add_argument("--api-base", default=None, help="web server base URL, e.g. http://127.0.0.1:18732")
    run_p.add_argument(
        "--api-token",
        default=None,
        help="bearer token for the web server API (CSRF-to-localhost defense); "
        "omit only when the server is started with auth disabled",
    )
    run_p.add_argument(
        "--baseline",
        action="store_true",
        help="run the pristine repo's test_command first and record workspace.baseline "
        "(always on for the fake driver)",
    )
    run_p.add_argument("--out", default=None, help="results directory (default: eval/results)")
    run_p.add_argument(
        "--keep-workspace",
        action="store_true",
        help="keep the temp workspace for debugging instead of deleting it",
    )
    run_p.set_defaults(func=cmd_run)

    check_p = sub.add_parser("check", help="structural check of all tasks (fast, no test runs)")
    check_p.add_argument("--dir", default=str(Path(__file__).resolve().parent / "tasks"), help="tasks directory")
    check_p.set_defaults(func=cmd_check)

    smoke_p = sub.add_parser("smoke", help="fake-driver smoke set for CI (eval/smoke.json)")
    smoke_p.add_argument("--dir", default=str(Path(__file__).resolve().parent / "tasks"), help="tasks directory")
    smoke_p.add_argument("--out", default=None, help="results directory (default: eval/results)")
    smoke_p.set_defaults(func=cmd_smoke)

    rep_p = sub.add_parser("report", help="summarize a results directory")
    rep_p.add_argument("--dir", default=str(RESULTS_DIR), help="results directory")
    rep_p.add_argument("--json", action="store_true", help="machine-readable summary")
    rep_p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
