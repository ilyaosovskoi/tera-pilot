"""Tests for the eval api driver's diff-review handling (headless mode).

The agent server blocks a file write on a `diff_review` SSE event until
the UI answers via POST /api/agent/diff_review. The eval driver is
headless — no human watches — so it must auto-accept every diff,
otherwise a real run stalls for the 300 s review timeout and dies.
These tests run a tiny local mock server (no network, no LLM).
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import runner  # noqa: E402


class _MockAgentServer(BaseHTTPRequestHandler):
    """Serves one /api/agent/stream SSE response that emits a diff_review
    event (as the real agent does before a file write), then records any
    POST /api/agent/diff_review bodies for assertions."""

    received_reviews = []

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/api/agent/diff_review":
            _MockAgentServer.received_reviews.append(body)
            resp = json.dumps({"ok": True, "accepted": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
        if self.path != "/api/agent/stream":
            self.send_response(404)
            self.end_headers()
            return
        self._send_stream()

    def do_GET(self):
        if self.path == "/api/usage/get":
            resp = json.dumps({"total_tokens": 0, "total_cost": 0}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
        if self.path != "/api/agent/stream":
            self.send_response(404)
            self.end_headers()
            return
        self._send_stream()

    def _send_stream(self):
        # SSE stream: a diff_review event, then done.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        events = [
            {"type": "router_decision", "provider_id": "gemini", "model": "gemini-3.1-flash-lite"},
            {"type": "step", "tool": "str_replace", "detail": "tool_called"},
            {"type": "diff_review", "review_id": "review-abc123", "path": "discount.py"},
            {"type": "done", "ok": True, "output": "fixed"},
        ]
        for evt in events:
            self.wfile.write(("data: " + json.dumps(evt) + "\n\n").encode("utf-8"))
            self.wfile.flush()


@pytest.fixture()
def mock_server():
    _MockAgentServer.received_reviews = []
    srv = HTTPServer(("127.0.0.1", 0), _MockAgentServer)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _task_with_no_test(tmp_path):
    """A minimal task manifest (no test_command) so the run is about the
    driver, not the verification step."""
    task_dir = tmp_path / "task"
    (task_dir / "repo").mkdir(parents=True)
    (task_dir / "repo" / "x.py").write_text("x = 1\n")
    (task_dir / "task.json").write_text(json.dumps({
        "schema_version": "1.0",
        "id": "mock-diff-review",
        "name": "Mock diff review",
        "category": "bug_fix",
        "prompt": "mock",
        "repo": "repo",
        "baseline_status": "failing",
    }), encoding="utf-8")
    return task_dir


def test_api_driver_auto_accepts_diff_review(tmp_path, mock_server):
    task_dir = _task_with_no_test(tmp_path)
    result = runner.run_api_driver(runner.load_task(str(task_dir)), tmp_path / "ws", mock_server)
    assert result["status"] == "success"
    # The driver must have answered the diff review by POSTing an accept.
    assert _MockAgentServer.received_reviews == [
        {"accepted": True, "review_id": "review-abc123"}
    ]
    assert result["provider"] == "gemini"
    assert result["model"] == "gemini-3.1-flash-lite"
    assert result["tools_used"] == ["str_replace"]


def test_api_driver_accepts_diff_review_with_bearer_token(tmp_path, mock_server):
    task_dir = _task_with_no_test(tmp_path)
    result = runner.run_api_driver(
        runner.load_task(str(task_dir)), tmp_path / "ws", mock_server, api_token="secret"
    )
    assert result["status"] == "success"
    assert _MockAgentServer.received_reviews == [
        {"accepted": True, "review_id": "review-abc123"}
    ]


class _HoldOpenAgentServer(BaseHTTPRequestHandler):
    """Deliberately reproduces the pre-fix api_server behavior: send a
    `done` event, then KEEP THE CONNECTION OPEN (never send EOF). A client
    reading until EOF — like run_api_driver — blocks until its read
    timeout. The driver must not rewrite the already-parsed success into
    an error just because the late timeout fires."""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/api/agent/stream":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        evt = {"type": "done", "ok": True, "output": "fixed"}
        self.wfile.write(("data: " + json.dumps(evt) + "\n\n").encode("utf-8"))
        self.wfile.flush()
        # Hold the socket open past any plausible read timeout.
        time.sleep(30)

    def do_GET(self):
        resp = json.dumps({"total_tokens": 0, "total_cost": 0}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


@pytest.fixture()
def hold_open_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HoldOpenAgentServer)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_api_driver_late_timeout_does_not_clobber_success(tmp_path, hold_open_server):
    """v2.3.4-fix: a read timeout AFTER a real `done` event must not turn a
    genuine success into an `error`. The socket never closes here, so the
    driver hits its read timeout — but it already parsed `done` and must
    keep reporting success."""
    task_dir = _task_with_no_test(tmp_path)
    start = time.monotonic()
    result = runner.run_api_driver(
        runner.load_task(str(task_dir)), tmp_path / "ws", hold_open_server,
        request_timeout=3,
    )
    elapsed = time.monotonic() - start
    assert result["status"] == "success"
    assert "timed out" not in result["final_output"]
    assert result["final_output"] == "fixed"
    # It waited out the read timeout rather than hanging forever.
    assert 2.0 <= elapsed < 20.0


class _RetryCollisionAgentServer(BaseHTTPRequestHandler):
    """Rejects the FIRST /api/agent/stream with the server's parallel-run
    error (`Another agent request is already running`), then serves a normal
    success stream. Reproduces the 2026-08-19 batch where parallel eval
    launches collided on the single-agent server."""

    attempts = 0

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/api/agent/stream":
            self.send_response(404)
            self.end_headers()
            return
        _RetryCollisionAgentServer.attempts += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if _RetryCollisionAgentServer.attempts == 1:
            evt = {"type": "error", "message": "Another agent request is already running. Wait for it to finish."}
        else:
            evt = {"type": "done", "ok": True, "output": "fixed on retry"}
        self.wfile.write(("data: " + json.dumps(evt) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def do_GET(self):
        resp = json.dumps({"total_tokens": 0, "total_cost": 0}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


@pytest.fixture()
def retry_collision_server():
    _RetryCollisionAgentServer.attempts = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RetryCollisionAgentServer)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_api_driver_retries_parallel_collision(tmp_path, retry_collision_server):
    """v2.3.4-fix: a parallel launch colliding on the single-agent server
    (`Another agent request is already running`, 0 iterations) must be
    retried with backoff instead of failing the task immediately."""
    task_dir = _task_with_no_test(tmp_path)
    result = runner.run_api_driver(
        runner.load_task(str(task_dir)), tmp_path / "ws", retry_collision_server,
        request_timeout=3,
    )
    assert result["status"] == "success"
    assert result["final_output"] == "fixed on retry"
    assert _RetryCollisionAgentServer.attempts == 2


def _task_for_build(tmp_path, task_id="mock", name="Mock", category="bug_fix", has_test=True):
    task_dir = tmp_path / task_id
    (task_dir / "repo").mkdir(parents=True)
    (task_dir / "repo" / "x.py").write_text("x = 1\n")
    manifest = {
        "schema_version": "1.0",
        "id": task_id,
        "name": name,
        "category": category,
        "prompt": "mock",
        "repo": "repo",
        "baseline_status": "failing",
    }
    if has_test:
        manifest["test_command"] = ["python3", "-m", "pytest", "-q"]
    (task_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return runner.load_task(str(task_dir))


def test_build_result_error_does_not_mask_passing_tests(tmp_path):
    """v2.3.4-fix: a run whose verification tests PASSED is a solved task
    even when the driver ended with an `error` (e.g. the final LLM response
    failed after the fix was already applied) — provided the agent actually
    ran (iterations > 0)."""
    task = _task_for_build(tmp_path)
    driver_out = {
        "status": "error", "iterations": 10, "tools_used": ["str_replace"],
        "final_output": "LM Studio HTTP 400 ...",
    }
    test_res = {"test_passed": True, "test_exit_code": 0, "test_output": "2 passed"}
    result = runner.build_result(
        task, driver_out, test_res, "hash", None, "api", 110.0
    )
    assert result["status"] == "success"
    assert result["metrics"]["verification_status"] == "passed"


def test_build_result_error_stays_error_when_agent_never_ran(tmp_path):
    """v2.3.4-fix: a run that never started (0 iterations, e.g. a
    parallel-run collision) stays `error` even when the pristine tests
    happen to pass — the agent did no work, so it is not a success."""
    task = _task_for_build(tmp_path)
    driver_out = {
        "status": "error", "iterations": 0, "tools_used": [],
        "final_output": "Another agent request is already running...",
    }
    test_res = {"test_passed": True, "test_exit_code": 0, "test_output": "2 passed"}
    result = runner.build_result(
        task, driver_out, test_res, "hash", None, "api", 0.02
    )
    assert result["status"] == "error"


def test_build_result_failed_when_tests_fail_even_with_error_driver(tmp_path):
    """v2.3.4-fix: a driver `error` with failing verification stays `error`."""
    task = _task_for_build(tmp_path)
    driver_out = {
        "status": "error", "iterations": 6, "tools_used": ["str_replace"],
        "final_output": "provider died mid-run",
    }
    test_res = {"test_passed": False, "test_exit_code": 1, "test_output": "F"}
    result = runner.build_result(
        task, driver_out, test_res, "hash", None, "api", 90.0
    )
    assert result["status"] == "error"
