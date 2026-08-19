"""Regression tests for bugs found in review (v2.3.2).

1. Daemon used a single-threaded ``HTTPServer`` while serving SSE streams
   (``/stream/:id`` blocks its handler until the task finishes) — one open
   stream stalled EVERY other endpoint (health check, task submit, cancel)
   for the whole run. It now uses ``ThreadingHTTPServer`` (matching
   api_server.py / web_server.py).

2. ``ToolEngine._execute_command`` truncated long output at the HEAD
   (``out_text[:MAX_OUTPUT]``) while ``_run_code`` kept the TAIL — for test
   runs / builds / installs the useful signal (failure summary, last log
   lines) is at the END, so a 5000-char pytest output lost the failure
   report entirely. Both now keep the tail.
"""

import json
import threading
import time
import urllib.request

import pytest

from tera_pilot.agent_runtime.tool_engine import ToolEngine


# ── 1. Daemon must be threaded (SSE must not block other endpoints) ──

def test_daemon_uses_threading_http_server():
    """The daemon server must be a threaded server: an open SSE stream
    blocks its request handler until the task finishes, and a
    single-threaded HTTPServer would stall every other endpoint."""
    import inspect
    from http.server import ThreadingHTTPServer

    import tera_pilot.daemon as daemon

    src = inspect.getsource(daemon)
    # The instantiation must be ThreadingHTTPServer, not the
    # single-threaded HTTPServer. (Note: "ThreadingHTTPServer((" also
    # contains "HTTPServer((" as a substring, so assert on the full
    # token instead.)
    assert "ThreadingHTTPServer((self.host, self.port)" in src, \
        "daemon must instantiate ThreadingHTTPServer"
    assert "= HTTPServer(" not in src, "plain HTTPServer would block on SSE"
    assert issubclass(ThreadingHTTPServer, __import__("http.server", fromlist=["HTTPServer"]).HTTPServer)


def test_daemon_health_stays_responsive_during_sse_stream(tmp_path, monkeypatch):
    """Functional check: while an SSE stream is open, /health must answer
    immediately instead of blocking for the whole task duration."""
    import tera_pilot.daemon as daemon
    from tera_pilot.daemon import TeraPilotDaemon, TaskQueue, TaskState

    port = 18746  # fixed test port; fails loudly if already in use

    # Fake a running task: emit a few events over ~1.2 s (no LLM needed).
    def fake_execute(self, task):
        with self._lock:
            task.state = TaskState.RUNNING
            task.started_at = time.time()
        for _ in range(4):
            time.sleep(0.3)
            self._emit(task.id, "progress", {"step": _})

    monkeypatch.setattr(TaskQueue, "_execute_task", fake_execute)

    d = TeraPilotDaemon(host="127.0.0.1", port=port, auth_token="test-token", max_workers=1)
    t = threading.Thread(target=d.start, daemon=True)
    t.start()
    try:
        # Wait for the server to come up.
        deadline = time.time() + 10
        while True:
            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{port}/health",
                        headers={"Authorization": "Bearer test-token"},
                    ),
                    timeout=1,
                )
                break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError("daemon did not start in time")
                time.sleep(0.2)

        # Submit a task.
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/task",
            data=json.dumps({"prompt": "hello", "workspace": str(tmp_path)}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        task_id = resp["id"]

        # Open an SSE stream (blocks the handler until the fake task ends).
        def _sse_reader():
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/stream/{task_id}",
                    headers={"Authorization": "Bearer test-token"},
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    while r.read(64):
                        pass
            except Exception:
                pass

        sse_thread = threading.Thread(target=_sse_reader, daemon=True)
        sse_thread.start()
        time.sleep(0.4)  # let the SSE connection establish

        # /health must answer promptly while the stream is open.
        t0 = time.time()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": "Bearer test-token"},
        )
        urllib.request.urlopen(req, timeout=3).read()
        elapsed = time.time() - t0
        assert elapsed < 2.5, f"/health blocked for {elapsed:.1f}s while SSE stream was open"
        sse_thread.join(timeout=10)
    finally:
        d.task_queue.shutdown()
        try:
            d._server.shutdown()
            d._server.server_close()
        except Exception:
            pass


# ── 2. execute_command keeps the TAIL of long output ──────────────────

def test_execute_command_keeps_tail_of_long_output(tmp_path):
    """A command producing more than MAX_OUTPUT chars must keep the END
    of its output (where failure summaries / errors live), not the head."""
    engine = ToolEngine(str(tmp_path))
    # 5000 chars of output with a marker at the very end.
    cmd = ["python3", "-c", "print('A' * 5000); print('FINAL_MARKER')"]
    result = engine._execute_command(" ".join(cmd), timeout=60)
    assert "FINAL_MARKER" in result, "tail of long output was truncated away"
    # And the head was dropped (only ~MAX_OUTPUT chars are kept).
    assert len(result) <= engine.MAX_OUTPUT + 32


def test_execute_command_shows_error_at_end_of_long_stderr(tmp_path):
    """Long stderr (e.g. a failing test run) must keep the final error.

    python3 -c "..." is blocked by the command policy (dangerous flag),
    so the test writes a real script file and runs it — which is also
    how the agent would do it in practice.
    """
    engine = ToolEngine(str(tmp_path))
    script = tmp_path / "noisy.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('E' * 5000 + '\\nTRACEBACK_END\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    result = engine._execute_command("python3 noisy.py", timeout=60)
    assert "TRACEBACK_END" in result
    assert "[EXIT CODE] 1" in result


# ── 3. Quota/429 retry budget + friendly error (v2.3.4) ───────────────

def _make_retry_runtime(monkeypatch):
    """A minimally-initialized AgentRuntime for _generate_with_retry tests."""
    import time as _time
    from tera_pilot.agent_runtime.runtime import AgentRuntime

    # Quota retries sleep ~10s per attempt; neutralize the clock so the
    # test doesn't wait minutes.
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    class _Tools:
        def is_cancelled(self):
            return False

    rt = object.__new__(AgentRuntime)
    rt.tools = _Tools()
    rt._model_override = None
    rt._on_token_delta = None
    rt._token_tracker = None
    rt._quota_tracker = None
    rt.section = "general"
    rt._provider_cooldown_until = 0.0
    return rt


def test_quota_error_gets_extended_retry_budget_and_friendly_error(monkeypatch):
    """Regression (v2.3.4): a persistently-429'd provider (OpenRouter free
    tier shared pool) must be retried up to _RETRY_QUOTA_MAX_ATTEMPTS, not
    the plain 5, and the final error must be actionable instead of a raw
    upstream JSON blob."""
    from tera_pilot.agent_runtime.runtime import AgentRuntime
    from tera_pilot.providers import ProviderError

    rt = _make_retry_runtime(monkeypatch)
    calls = []

    class _Provider:
        provider_id = "openrouter"

        def __init__(self):
            self.config = type("C", (), {"model": "z-ai/glm-5.2:free"})()

        def generate(self, messages, **kw):
            calls.append(1)
            raise Exception(
                'OpenRouter HTTP 429: {"error":{"message":"Provider returned error",'
                '"code":429,"metadata":{"raw":"z-ai/glm-5.2:free is temporarily '
                'rate-limited upstream","provider_name":"Decart",'
                '"retry_after_seconds":5}}}'
            )

    provider = _Provider()
    with pytest.raises(ProviderError) as ei:
        rt._generate_with_retry(provider, [])
    msg = str(ei.value)
    assert "Rate limit" in msg
    assert "openrouter" in msg
    assert "Decart" in msg  # upstream name extracted from the JSON blob
    assert "switch to another model/provider" in msg
    # The extended quota budget must have been used, not the plain 5.
    assert len(calls) == AgentRuntime._RETRY_QUOTA_MAX_ATTEMPTS
    assert AgentRuntime._RETRY_QUOTA_MAX_ATTEMPTS > AgentRuntime._RETRY_MAX_ATTEMPTS


def test_non_quota_transient_error_keeps_plain_budget(monkeypatch):
    """A non-quota transient error (e.g. 503) keeps the plain 5-attempt
    budget — only 429/quota errors get the extended patience."""
    from tera_pilot.agent_runtime.runtime import AgentRuntime

    rt = _make_retry_runtime(monkeypatch)
    calls = []

    class _Provider:
        provider_id = "x"

        def __init__(self):
            self.config = type("C", (), {"model": "m"})()

        def generate(self, messages, **kw):
            calls.append(1)
            raise Exception("HTTP 503 Service Unavailable")

    provider = _Provider()
    with pytest.raises(Exception):
        rt._generate_with_retry(provider, [])
    assert len(calls) == AgentRuntime._RETRY_MAX_ATTEMPTS
