"""Regression tests for the daemon SSE stream fix (v2.3.4-fix).

``DaemonHandler._handle_sse`` sent the initial ``state`` event and then
entered a keepalive loop for tasks that were ALREADY in a terminal state
(completed / failed / cancelled). Since no further events can ever
arrive for such a task, the stream would keepalive-loop until the client
gave up — every SSE client that polled a finished task leaked an open
connection forever.

The handler now closes the stream immediately after the initial state
event when the task is already terminal.
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.daemon import (  # noqa: E402
    DaemonHandler,
    SSESubscriber,
    TaskRecord,
    TaskState,
    TeraPilotDaemon,
)


@pytest.fixture
def daemon_http(tmp_path, monkeypatch):
    """A live daemon HTTP server with a hand-seeded task queue.

    We wire the handler exactly like ``TeraPilotDaemon.start()`` does but
    skip the blocking ``serve_forever`` call and the worker threads, so
    the test controls which tasks exist and in which state.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    daemon = TeraPilotDaemon(
        host="127.0.0.1", port=0, auth_token="test-token", max_workers=0
    )
    DaemonHandler.task_queue = daemon.task_queue
    DaemonHandler.auth_token = daemon.auth_token

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DaemonHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, daemon
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _seed_task(daemon, state: TaskState) -> str:
    task = TaskRecord(prompt="p", state=state)
    with daemon.task_queue._lock:
        daemon.task_queue._tasks[task.id] = task
        daemon.task_queue._subscribers[task.id] = SSESubscriber()
    return task.id


def _stream_url(httpd, task_id: str) -> str:
    port = httpd.server_address[1]
    return f"http://127.0.0.1:{port}/stream/{task_id}"


def _open_stream(url: str, timeout: float):
    """Open an SSE stream with the daemon's Bearer auth header."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer test-token"})
    return urllib.request.urlopen(req, timeout=timeout)


def test_terminal_task_stream_closes_immediately(daemon_http):
    httpd, daemon = daemon_http
    task_id = _seed_task(daemon, TaskState.COMPLETED)

    # urlopen returns only when the connection closes — if the handler
    # keepalive-looped, this would block until the 5 s timeout fails.
    with _open_stream(_stream_url(httpd, task_id), timeout=5) as r:
        body = r.read().decode()

    assert r.status == 200
    assert "event: state" in body
    assert '"state": "completed"' in body


def test_failed_task_stream_closes_immediately(daemon_http):
    httpd, daemon = daemon_http
    task_id = _seed_task(daemon, TaskState.FAILED)

    with _open_stream(_stream_url(httpd, task_id), timeout=5) as r:
        body = r.read().decode()

    assert "event: state" in body
    assert '"state": "failed"' in body


def test_cancelled_task_stream_closes_immediately(daemon_http):
    httpd, daemon = daemon_http
    task_id = _seed_task(daemon, TaskState.CANCELLED)

    with _open_stream(_stream_url(httpd, task_id), timeout=5) as r:
        body = r.read().decode()

    assert "event: state" in body
    assert '"state": "cancelled"' in body


def test_running_task_stream_stays_open(daemon_http):
    """A non-terminal task must NOT close the stream right away — the
    connection stays open waiting for events (proves the early-return is
    gated on the terminal-state check, not a general behavior change)."""
    httpd, daemon = daemon_http
    task_id = _seed_task(daemon, TaskState.RUNNING)

    # urllib propagates a read timeout as socket.timeout (an OSError
    # subclass), not always as urllib.error.URLError.
    with pytest.raises((urllib.error.URLError, OSError)):
        _open_stream(_stream_url(httpd, task_id), timeout=1.5).read()


def test_sse_race_task_completes_between_check_and_subscribe(daemon_http):
    """v2.3.9-fix: a task that reaches a terminal state between the
    handler's initial state check and its subscribe call emits its
    terminal event to nobody (the subscriber list is still empty). The
    stream must still close — otherwise the SSE connection keepalive-
    loops forever and the client blocks in read() until its own timeout.

    Before the fix this test hung for 5s and failed with a timeout:
    the "completed" event was emitted before the callback registered,
    so the loop never saw it.
    """
    import time
    httpd, daemon = daemon_http
    task_id = _seed_task(daemon, TaskState.PENDING)

    real_subscribe = daemon.task_queue.subscribe

    def racing_subscribe(tid, callback):
        # The task finishes in the window BEFORE the callback registers:
        # the terminal event goes to an empty subscriber list and is lost.
        task = daemon.task_queue._tasks[tid]
        task.state = TaskState.COMPLETED
        task.completed_at = time.time()
        daemon.task_queue._emit(tid, "completed", task.to_dict())
        return real_subscribe(tid, callback)

    daemon.task_queue.subscribe = racing_subscribe

    with _open_stream(_stream_url(httpd, task_id), timeout=5) as r:
        body = r.read().decode()

    assert r.status == 200
    assert "event: state" in body


def test_sse_keepalive_rechecks_terminal_task(daemon_http, monkeypatch):
    """v2.3.9-fix: even when the terminal event never reaches the SSE
    event queue (e.g. the bounded queue dropped it under load), the
    keepalive branch re-checks the task state and closes the stream on a
    finished task instead of looping forever."""
    import queue as _queue
    import time
    httpd, daemon = daemon_http
    task_id = _seed_task(daemon, TaskState.RUNNING)

    real_subscribe = daemon.task_queue.subscribe

    def subscribe_then_complete_later(tid, callback):
        result = real_subscribe(tid, callback)
        # The task finishes shortly AFTER the handler's post-subscribe
        # re-check (so that check sees RUNNING and lets the loop start),
        # but WITHOUT emitting any event (simulates a dropped terminal
        # event). The keepalive re-check must be what notices it.
        def _finish():
            time.sleep(0.4)
            task = daemon.task_queue._tasks[tid]
            task.state = TaskState.COMPLETED
            task.completed_at = time.time()

        threading.Thread(target=_finish, daemon=True).start()
        return result

    daemon.task_queue.subscribe = subscribe_then_complete_later

    # Force the event wait to time out immediately so the keepalive branch
    # (which re-checks the task state) runs right away. No workers are
    # running in this fixture, so no other queue.Queue.get() is affected.
    def _always_empty(self, timeout=None):
        raise _queue.Empty

    monkeypatch.setattr(_queue.Queue, "get", _always_empty)

    with _open_stream(_stream_url(httpd, task_id), timeout=5) as r:
        body = r.read().decode()

    assert r.status == 200
    assert "event: state" in body
    assert "event: keepalive" in body
