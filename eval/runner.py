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
import re
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
VALID_CATEGORIES = (
    "bug_fix", "test_repair", "refactor", "feature", "code_review", "documentation",
    # v2.3.4 (P0.5): adversarial security tasks — the criterion is NOT
    # "tests pass" but that a malicious action is blocked / confirmed /
    # the run fails closed (see task.json's security_expectation).
    "security",
)
#: Allowed values for a security task's declared expectation.
VALID_SECURITY_EXPECTATIONS = ("blocked", "confirm", "refused", "fail_closed")
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


def compute_workspace_diff(task, workspace) -> str:
    """The ACTUAL diff the agent produced (P1.8 evidence).

    Compares the workspace copy against the pristine fixture repo
    (``task['_repo_dir']``): lists added / deleted / modified files and,
    for small text files, includes the unified diff of the changes.
    Deterministic, no git required. Truncated to 8K chars. Returns an
    empty string on any error or when there are no changes.
    """
    import difflib

    try:
        repo = Path(workspace)
        pristine = Path(task["_repo_dir"])
        lines: list[str] = []
        for p in sorted(pristine.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(pristine)
            if any(part in _FINGERPRINT_SKIP_DIRS for part in rel.parts[:-1]):
                continue
            cur = repo / rel
            if not cur.exists():
                lines.append(f"--- {rel} (deleted)")
                continue
            if cur.read_bytes() != p.read_bytes():
                lines.append(f"--- {rel} (modified)")
                if p.stat().st_size <= 50_000 and cur.stat().st_size <= 50_000:
                    try:
                        old = p.read_text(encoding="utf-8", errors="replace").splitlines()
                        new = cur.read_text(encoding="utf-8", errors="replace").splitlines()
                        patch = list(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
                        lines.extend(patch[:60])
                    except Exception:
                        pass
        for p in sorted(repo.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(repo)
            if any(part in _FINGERPRINT_SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if not (pristine / rel).exists():
                lines.append(f"+++ {rel} (added, {p.stat().st_size} bytes)")
        out = "\n".join(lines).strip()
        return out[:8000]
    except Exception:
        return ""


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


def run_api_driver(task, workspace, api_base, api_token=None, request_timeout=None):
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

    ``request_timeout`` overrides the HTTP/SSE read timeout. Defaults to
    the manifest's ``timeout_secs`` + 120 s grace. The manifest timeout
    bounds the AGENT's work; the HTTP read timeout must be strictly
    larger or a task that legitimately uses its whole budget (e.g. with
    provider quota cooldowns) is cut off mid-run and reported as an
    error even though the agent was still making progress.
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

    def _post_json(path: str, payload: dict) -> None:
        """Best-effort POST to a mutating endpoint — never raises (a
        response failure must not abort the whole run)."""
        try:
            hdrs = {"Content-Type": "application/json"}
            if api_token:
                hdrs["Authorization"] = "Bearer " + api_token
            resp_req = urllib.request.Request(
                api_base.rstrip("/") + path,
                data=json.dumps(payload).encode("utf-8"),
                headers=hdrs,
                method="POST",
            )
            with urllib.request.urlopen(resp_req, timeout=10) as resp:
                resp.read()
        except Exception:
            pass

    def _auto_accept_diff_review(review_id: str) -> None:
        """Accept a pending diff review so the agent can continue."""
        _post_json("/api/agent/diff_review", {"accepted": True, "review_id": review_id})

    def _auto_accept_action(confirm_id: str) -> None:
        """v2.3.5-fix: answer an action confirmation (execute_command /
        write_file / git_commit / ... under autonomy 'always_ask'). On
        current servers the HTTP path fails CLOSED — without this the
        agent stalls for the 300 s confirmation timeout on EVERY side
        effect and every headless eval run dies. Auto-accept, the same
        way the browser UI's "Allow" button does."""
        _post_json("/api/action/respond", {"accepted": True, "confirm_id": confirm_id})

    def _auto_approve_guardian(review_id: str) -> None:
        """v2.3.5-fix: approve a pending Guardian MODIFY-verdict review
        (headless eval has no human reviewer). Same as clicking
        "Approve" in the GUI — the tool runs with its ORIGINAL args."""
        _post_json("/api/guardian/respond", {"verdict": "approve", "review_id": review_id})

    def _stream_once() -> dict:
        """One SSE stream attempt; never raises (returns an error dict on
        transport failures). ``_collision`` is True when the server rejected
        the request because another agent run was in progress (parallel
        launches collide on the single-agent server)."""
        tokens = 0
        cost = 0.0
        iterations = 0
        tools = []
        final = ""
        status = "error"
        provider = None
        model = None
        cancelled = False
        # v2.3.4-fix: set True once a TERMINAL SSE event (`done` / `error`) has
        # been parsed. A late read-timeout AFTER a real terminal event must not
        # overwrite the terminal state (see the TimeoutError handler below).
        terminal_seen = False
        collision = False
        # v2.3.4 (P1.8): per-run evidence counters — provider errors
        # (terminal SSE `error` events), agent/tool error steps, and the
        # self_verify tool's result (so a green test suite is never
        # conflated with a clean run).
        provider_errors = 0
        tool_errors = 0
        self_verify = None
        _pending_self_verify = False
        # True when the attempt failed at the TRANSPORT level (HTTP error,
        # connection reset, timeout) before any agent work happened. The
        # retry loop below treats these like collisions when the run never
        # started (0 iterations): a parallel launch that collides on the
        # single-agent server can surface as ECONNRESET instead of a clean
        # SSE error event when the server closes the socket while the
        # client still has unread data (macOS sends RST in that case).
        transport_error = False
        read_timeout = request_timeout or (task.get("timeout_secs", 300) + 120)
        try:
            with urllib.request.urlopen(req, timeout=read_timeout) as resp:
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
                        # Count REAL agent iterations, not every SSE step:
                        # the server emits a 'step' event for plan_created /
                        # thought / tool_called / tool_result / done too, so
                        # counting all of them inflated `iterations` ~4-6x.
                        detail = evt.get("detail")
                        if detail == "iteration_start":
                            iterations += 1
                        elif detail == "tool_called" and evt.get("tool"):
                            tools.append(evt["tool"])
                            if evt["tool"] == "self_verify":
                                _pending_self_verify = True
                        elif detail == "tool_result":
                            if _pending_self_verify and evt.get("tool") == "self_verify":
                                _pending_self_verify = False
                                self_verify = (evt.get("result") or "")[:2000]
                        elif detail == "error":
                            # Agent/tool error step (AgentEvent.ERROR).
                            tool_errors += 1
                    elif etype == "diff_review":
                        # Headless driver: auto-accept so the agent never
                        # stalls on the 300 s review timeout.
                        _auto_accept_diff_review(evt.get("review_id", ""))
                    elif etype == "action_confirm":
                        # v2.3.5-fix: auto-accept action confirmations
                        # (autonomy 'always_ask') — same as clicking Allow.
                        _auto_accept_action(evt.get("confirm_id", ""))
                    elif etype == "guardian_review":
                        # v2.3.5-fix: auto-approve Guardian reviews.
                        _auto_approve_guardian(evt.get("review_id", ""))
                    elif etype == "router_decision":
                        provider = evt.get("provider_id") or evt.get("provider")
                        model = evt.get("model")
                    elif etype == "done":
                        terminal_seen = True
                        status = "success" if evt.get("ok", True) else "failed"
                        cancelled = bool(evt.get("cancelled"))
                        final = final or evt.get("output") or evt.get("text") or ""
                    elif etype == "error":
                        # v2.3.4-fix: a parallel launch collides on the
                        # single-agent server BEFORE the run starts (0
                        # iterations). Don't treat that as a terminal error —
                        # signal the caller to retry instead.
                        if iterations == 0 and "already running" in evt.get("message", ""):
                            collision = True
                            final = evt.get("message", "")
                            break
                        # v2.3.4 (P1.8): a real provider/terminal error is
                        # recorded as evidence, not just status.
                        provider_errors += 1
                        terminal_seen = True
                        status = "error"
                        final = evt.get("message", "")
        except urllib.error.HTTPError as exc:
            status = "error"
            final = f"HTTP {exc.code}: {exc.reason}"
            transport_error = True
        except urllib.error.URLError as exc:
            status = "error"
            final = f"cannot reach {api_base}: {exc.reason}"
            transport_error = True
        except TimeoutError:
            # v2.3.4-fix: a stream that already delivered `done`/`error` can
            # still hit the read timeout when the server fails to close the
            # connection (the api_server.py SSE close bug used to leave the
            # socket open after `done`, so a client reading until EOF blocked
            # here until ITS timeout). The run DID complete — a late timeout
            # after a real terminal event must not rewrite a genuine
            # success/failure into an `error`. Only fall back to the timeout
            # verdict when no terminal event was ever seen.
            if not terminal_seen:
                status = "error"
                final = f"request timed out after {read_timeout}s"
                transport_error = True
        except Exception as exc:  # pragma: no cover - defensive
            # v2.3.4-fix parity (TimeoutError above): a late transport
            # error (e.g. ECONNRESET while the server closes the SSE
            # socket right after the last event) AFTER a real terminal
            # event must not rewrite a genuine success/failure into an
            # `error` — the run already completed. Only treat it as a
            # retryable transport failure when no terminal event was seen.
            if not terminal_seen:
                status = "error"
                final = f"unexpected driver error: {exc}"
                transport_error = True
        return {
            "provider": provider,
            "model": model,
            "tokens": tokens,
            "iterations": iterations,
            "tools_used": sorted(set(tools)),
            "final_output": final,
            "status": status,
            "cancelled": cancelled,
            "provider_errors": provider_errors,
            "tool_errors": tool_errors,
            "self_verify": self_verify,
            "_collision": collision,
            "_transport_error": transport_error,
        }

    # v2.3.4-fix: the server accepts ONE agent request at a time and rejects
    # a parallel launch with an immediate error event. Retry with a short
    # backoff so accidentally parallel eval launches serialize instead of
    # failing the task (bounded, so a genuinely stuck server still fails).
    max_attempts = 3
    driver_out = {}
    for attempt in range(1, max_attempts + 1):
        driver_out = _stream_once()
        # Retry when the run never started (0 iterations) and the attempt
        # failed either with a clean collision SSE event or at the transport
        # level (connection reset / HTTP error / timeout). Both are the same
        # real-world situation — a parallel launch colliding on the
        # single-agent server — and both are safe to retry because the agent
        # did zero work. Bounded, so a genuinely stuck server still fails.
        retryable = driver_out.pop("_collision", False) or (
            driver_out.pop("_transport_error", False)
            and driver_out.get("iterations", 0) == 0
        )
        if not retryable or attempt == max_attempts:
            break
        time.sleep(5 * attempt)

    after = _usage_stats(api_base, api_token)
    tokens_in = tokens_out = request_count = 0
    cost = 0.0
    if before and after:
        tokens_in = max(0, after["tokens_in"] - before["tokens_in"])
        tokens_out = max(0, after["tokens_out"] - before["tokens_out"])
        cost = max(0.0, round(after["cost"] - before["cost"], 6))
        request_count = max(0, after["requests"] - before["requests"])
        tokens = driver_out.get("tokens", 0)
        if tokens == 0:
            tokens = tokens_in + tokens_out
    else:
        tokens = driver_out.get("tokens", 0)
    if driver_out.get("cancelled") and driver_out["status"] == "success":
        driver_out["status"] = "failed"

    driver_out["tokens"] = tokens
    driver_out["tokens_in"] = tokens_in
    driver_out["tokens_out"] = tokens_out
    driver_out["request_count"] = request_count
    driver_out["cost_usd"] = cost
    return driver_out


# ── Direct driver (no Tera Pilot) ────────────────────────────────────
#
# Head-to-head comparison: the SAME task prompt is sent straight to an
# OpenAI-compatible endpoint (LM Studio) with NO agent loop, NO tools and
# NO sandbox. The model must produce the fix itself and return the COMPLETE
# new contents of every file it changes (```### FILE: path``` blocks); the
# driver applies them and the caller runs the SAME test_command, so
# "model alone" vs "model + Tera Pilot" are graded identically.
#
# Serial by construction: exactly one request per task, read to completion
# (non-streaming). Small local models (e.g. a 2.6B LM Studio model) can
# return an EMPTY completion when the server is still busy with a previous
# request or when requests are pipelined — the driver never pipelines: it
# waits for the previous response to fully arrive before issuing the next
# request, and retries an empty/undecodable response with a backoff.


#: File names/dirs never shipped to the model — git internals, bytecode and
#: test caches are environment artifacts, not task content.
_DIRECT_SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}


def _direct_repo_files(task, workspace):
    """Return [(relpath, content)] for every small text file in the task's
    fixture (read from the clean workspace copy). Best-effort: unreadable
    or binary-looking files are skipped."""
    out = []
    root = Path(workspace)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(part in _DIRECT_SKIP_PARTS for part in Path(rel).parts):
            continue
        try:
            if p.stat().st_size > 100_000:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue  # binary
        try:
            out.append((rel, data.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return out


_DIRECT_OUTPUT_INSTRUCTION = (
    "Return the COMPLETE new contents of every file you change, one file per "
    "section, in EXACTLY this format (nothing else, no commentary):\n"
    "### FILE: <relative path>\n"
    "<complete file content>\n"
    "Separate files with a blank line. Do not omit or abbreviate any part of "
    "a file. If a file needs no change, do not list it. Do not use code fences "
    "around the file content."
)


def _direct_build_prompt(task, files):
    """Task prompt + the fixture's current file contents + output format."""
    parts = [
        "You are fixing a small coding task. The repository files are shown "
        "below with their current contents.",
        "",
        "TASK:",
        task["prompt"],
        "",
        "CURRENT FILES:",
    ]
    if not files:
        parts.append("(no files)")
    for rel, content in files:
        parts.append(f"### FILE: {rel}")
        parts.append(content)
    parts.append("")
    parts.append(_DIRECT_OUTPUT_INSTRUCTION)
    return "\n".join(parts)


#: One fenced code block per line, e.g. ```python / ```diff — tolerate
#: extra info like a filename. Stripped from extracted file sections.
_DIRECT_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_./\\ -]*\s*$")


def _direct_parse_sections(text):
    """Parse ``### FILE: path`` sections out of a model response.

    Returns [(path, content)]. Tolerates stray prose before/after the
    sections, optional fenced code blocks around the content, and file
    paths with or without a leading ``./``. Never raises.
    """
    import re as _re

    sections = []
    marker = _re.compile(r"^\s*###\s*FILE:?\s*([^\r\n]+?)\s*$", _re.IGNORECASE)
    lines = (text or "").splitlines()
    current = None
    buf: list[str] = []
    closed = False  # a closing fence ended the current section's content
    for line in lines:
        m = marker.match(line)
        if m:
            if current is not None:
                sections.append((current, "\n".join(buf)))
            current = m.group(1).strip().lstrip("./")
            buf = []
            closed = False
            continue
        if current is None:
            continue
        # Strip fenced-code wrappers around the section content (models
        # often wrap each file in ```python ... ```). The first fence in a
        # section is its opening wrapper; the next fence closes it — after
        # that, trailing prose ("Hope that helps!") is NOT file content
        # and is dropped until the next ### FILE: marker.
        if _DIRECT_FENCE_RE.match(line):
            closed = True
            continue
        if closed:
            continue
        buf.append(line)
    if current is not None:
        sections.append((current, "\n".join(buf)))
    return sections


class _DirectPathError(ValueError):
    """Raised when a model wants to write outside the workspace."""


def _direct_safe_target(workspace, relpath):
    """Resolve a model-returned relative path against the workspace,
    refusing anything that escapes it (absolute paths, ``..``, symlink
    tricks). Returns the resolved Path. Raises ``_DirectPathError``."""
    root = Path(workspace).resolve()
    target = (root / relpath).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise _DirectPathError(f"path escapes workspace: {relpath!r}")
    return target


def _direct_apply_sections(workspace, sections):
    """Write parsed file sections into the workspace. Returns the list of
    (relpath, bytes_written) actually written, in order. Never raises —
    an invalid section is skipped, not fatal (the test_command verdict
    decides)."""
    written = []
    for relpath, content in sections:
        relpath = (relpath or "").strip().lstrip("./")
        if not relpath or relpath.startswith("/") or ".." in relpath.split("/"):
            continue
        try:
            target = _direct_safe_target(workspace, relpath)
        except _DirectPathError:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
            written.append((relpath, len(content.encode("utf-8"))))
        except OSError:
            continue
    return written


def run_direct_driver(task, workspace, api_base="http://127.0.0.1:1234/v1",
                      model=None, api_key="lmstudio", temperature=0.2,
                      max_tokens=4096, request_timeout=None):
    """One-shot chat-completions call WITHOUT Tera Pilot.

    Sends the task prompt + fixture contents to an OpenAI-compatible
    endpoint (LM Studio by default), applies the returned file sections to
    the workspace, and returns a driver_out dict shaped like the other
    drivers so ``build_result`` grades it identically (same test_command).

    The user's "no empty answers" rule: exactly ONE request per task,
    non-streaming (the full response must arrive before we continue), and
    an empty / undecodable response is retried with a short backoff — the
    retry only starts after the previous attempt has FULLY completed, so
    the model is never asked to work while it is still answering.

    Returns a driver_out dict; the caller runs test_command afterwards.
    """
    url = api_base.rstrip("/") + "/chat/completions"
    files = _direct_repo_files(task, workspace)
    prompt = _direct_build_prompt(task, files)
    payload_base = {
        "model": model or "",
        "messages": [
            {"role": "system", "content": "You are an expert Python developer. Follow the output format exactly."},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or 'lmstudio'}",
    }
    timeout = request_timeout or (task.get("timeout_secs", 300) + 120)

    # A response is only usable when it contains at least one parseable
    # file section (a real fix attempt). Empty content, a pure prose
    # answer, or a transport error are all retryable up to N attempts.
    max_attempts = 3
    last_err = ""
    last_text = ""
    tokens_in = tokens_out = 0
    resp_model = model
    written = []
    for attempt in range(1, max_attempts + 1):
        try:
            body = json.dumps(payload_base).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            choices = data.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            text = msg.get("content") or ""
            usage = data.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)
            resp_model = data.get("model") or model
            last_text = text
            sections = _direct_parse_sections(text)
            if not sections:
                # Empty completion or no file sections — the model was
                # still busy / produced nothing usable. Wait for the
                # previous attempt to fully settle, then retry.
                last_err = f"attempt {attempt}: empty or unusable response ({len(text)} chars)"
                time.sleep(3 * attempt)
                continue
            written = _direct_apply_sections(workspace, sections)
            status = "success" if written else "error"
            if not written:
                last_err = f"attempt {attempt}: parsed {len(sections)} section(s) but none were writable"
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = f"attempt {attempt}: {exc}"
            if attempt < max_attempts:
                time.sleep(3 * attempt)
        except Exception as exc:  # pragma: no cover - defensive
            last_err = f"attempt {attempt}: unexpected {exc!r}"
            if attempt < max_attempts:
                time.sleep(3 * attempt)
    else:
        status = "error"

    final = last_text or last_err
    return {
        "provider": "direct",
        "model": resp_model,
        "tokens": tokens_in + tokens_out,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "request_count": 0,  # usage endpoint is Tera Pilot's, not ours
        "cost_usd": 0.0,
        "iterations": 1 if status == "success" else 0,
        "tools_used": [],
        "final_output": final[:4000],
        "status": status,
        "_written_files": [w[0] for w in written],
    }


DRIVERS = {"fake": run_fake_driver, "api": run_api_driver, "direct": run_direct_driver}


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

def build_result(task, driver_out, test_res, base_repo_hash, base_commit, driver, duration_sec, baseline=None, diff=None):
    """Assemble a schema-v1 result. Never conflates run/test/verification.

    ``baseline`` is the optional result of running ``test_command`` on the
    pristine repo (recorded for the fake driver always, for the api driver
    with ``--baseline``).

    v2.3.4-fix (status priority): the driver's ``error`` reflects the RUN
    (e.g. the final LLM response failed after the fix was already applied),
    while ``test_res`` reflects the WORKSPACE. When the verification tests
    PASSED and the agent actually ran (iterations > 0), the task is solved —
    report ``success`` instead of letting the terminal driver error mask a
    genuine fix. A run that never started (0 iterations, e.g. a parallel-run
    collision) stays ``error`` even if the pristine tests happen to pass.
    """
    if driver_out["status"] == "skipped":
        status = "skipped"
    elif driver_out["status"] == "error" and (
        test_res["test_passed"] is not True or driver_out.get("iterations", 0) == 0
    ):
        status = "error"
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
    # v2.3.4 (P1.8): evidence — the actual diff, error counters and the
    # self_verify result. Only fields actually observed are included
    # (no hidden/empty telemetry).
    evidence = {}
    if diff:
        evidence["diff"] = diff
    for key in ("provider_errors", "tool_errors"):
        val = driver_out.get(key)
        if val:
            evidence[key] = val
    if driver_out.get("self_verify"):
        evidence["self_verify"] = driver_out["self_verify"]

    result = {
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
    if evidence:
        result["evidence"] = evidence
    return result


# ── CLI ──────────────────────────────────────────────────────────────

def _run_once(task, args) -> int:
    """Run one task end-to-end and write its result. Returns exit code."""
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
        elif args.driver == "direct":
            driver_out = driver_fn(
                task, workspace,
                api_base=args.direct_base,
                model=args.direct_model,
                api_key=args.direct_api_key,
            )
        else:
            driver_out = driver_fn(task, workspace)
        duration = time.perf_counter() - start
        test_res = run_test_command(
            workspace, task.get("test_command"), task.get("test_timeout_secs", 120)
        )
        # v2.3.4 (P1.8): record the ACTUAL diff the agent produced.
        diff = compute_workspace_diff(task, workspace)
        result = build_result(
            task, driver_out, test_res, base_repo_hash, base_commit, args.driver, duration, baseline,
            diff=diff,
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


def cmd_run(args):
    """Run a task, optionally N times on fresh workspaces (P1.6).

    ``--repeat N`` runs the task N times, each in a brand-new clean
    workspace, writing one schema-valid result per run. This is how the
    plan's "5–10 repeats per task" on one model is produced — run with
    ``--repeat 5`` (or 10 for the most problematic tasks). The first
    repeat that fails structurally (bad result) aborts the loop.
    """
    task = load_task(args.task)
    repeats = max(1, int(getattr(args, "repeat", 1) or 1))
    code = 0
    for i in range(1, repeats + 1):
        if repeats > 1:
            print(f"── repeat {i}/{repeats} ──")
        code = _run_once(task, args)
        if code != 0:
            break
    return code


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
    # v2.3.4 (P0.5): adversarial security tasks must declare what the
    # correct outcome is, so a green test suite is never mistaken for a
    # passed security task (the plan's criterion: action is blocked, the
    # user gets a clear confirmation, or the run fails closed).
    if manifest.get("category") == "security":
        exp = manifest.get("security_expectation")
        if exp is None:
            problems.append(
                "security tasks should declare 'security_expectation' "
                "(blocked|confirm|refused|fail_closed)"
            )
        elif exp not in VALID_SECURITY_EXPECTATIONS:
            problems.append(f"invalid security_expectation: {exp!r}")
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


def _load_results_dir(dir_path):
    """Load all schema-valid results in a directory, keyed by task_id.
    Returns (by_task, invalid_count)."""
    dir_path = Path(dir_path)
    files = sorted(p for p in dir_path.glob("*.json") if p.name != "schema.json")
    by_task = {}
    invalid = 0
    for f in files:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            schema.validate_result(r)
        except (json.JSONDecodeError, ValueError):
            invalid += 1
            continue
        by_task.setdefault(r["task_id"], []).append(r)
    return by_task, invalid


def _result_badge(r):
    """Compact per-run badge: status + tests, e.g. ``success/tests✓``."""
    tests = r["metrics"]["test_passed"]
    t = "✓" if tests is True else ("✗" if tests is False else "-")
    return f"{r['status']}/t{t}"


def cmd_compare(args):
    """Side-by-side summary of two results directories — the head-to-head
    "with Tera Pilot" vs "without Tera Pilot" comparison."""
    a, a_inv = _load_results_dir(args.a)
    b, b_inv = _load_results_dir(args.b)
    if not a and not b:
        print(f"no results in {args.a} or {args.b}")
        return 1
    tasks = sorted(set(a) | set(b))
    print(f"comparing {args.a}  vs  {args.b}")
    print(f"{'task':<42} {'A (with TP)':<16} {'B (direct)':<16}")
    print("-" * 78)
    a_pass = b_pass = a_total = b_total = 0
    for tid in tasks:
        ra = a.get(tid, [])
        rb = b.get(tid, [])
        ba = ", ".join(_result_badge(r) for r in ra) or "—"
        bb = ", ".join(_result_badge(r) for r in rb) or "—"
        a_total += len(ra)
        b_total += len(rb)
        a_pass += sum(1 for r in ra if r["metrics"]["test_passed"] is True)
        b_pass += sum(1 for r in rb if r["metrics"]["test_passed"] is True)
        print(f"{tid:<42} {ba:<16} {bb:<16}")
    print("-" * 78)
    print(f"A {args.a}: {a_pass}/{a_total} tests passed" + (f" ({a_inv} invalid)" if a_inv else ""))
    print(f"B {args.b}: {b_pass}/{b_total} tests passed" + (f" ({b_inv} invalid)" if b_inv else ""))
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
    # v2.3.5 (eval): direct driver — the same task prompt WITHOUT Tera Pilot
    # (one-shot OpenAI-compatible call, no tools/agent loop) for the
    # head-to-head comparison. Serial + retry-on-empty by construction.
    run_p.add_argument(
        "--direct-base",
        default="http://127.0.0.1:1234/v1",
        help="OpenAI-compatible base URL for the direct driver (default: LM Studio)",
    )
    run_p.add_argument(
        "--direct-model",
        default=None,
        help="model name for the direct driver (default: server's configured model)",
    )
    run_p.add_argument(
        "--direct-api-key",
        default="lmstudio",
        help="API key/bearer for the direct driver (LM Studio ignores it)",
    )
    run_p.add_argument("--out", default=None, help="results directory (default: eval/results)")
    run_p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run the task N times, each on a fresh clean workspace, writing "
        "one result per run (P1.6: 5-10 repeats per task on one model)",
    )
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

    cmp_p = sub.add_parser(
        "compare",
        help="side-by-side summary of two results dirs (with vs without Tera Pilot)",
    )
    cmp_p.add_argument("a", help="first results directory")
    cmp_p.add_argument("b", help="second results directory")
    cmp_p.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
