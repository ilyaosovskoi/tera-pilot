"""Tests for the eval `direct` driver — the head-to-head "without Tera Pilot"
comparison.

The direct driver sends the task prompt + fixture contents straight to an
OpenAI-compatible endpoint (LM Studio) with no agent loop and no tools,
applies the model's ``### FILE: path`` sections to the workspace, and the
caller grades with the SAME test_command as the api driver.

Two behaviors are load-bearing for real runs against a small local model:
- serial by construction — exactly ONE request per task, read to full
  completion (non-streaming), never pipelined;
- retry-on-empty — an empty / unusable completion is retried with a
  backoff, and the retry only starts after the previous attempt has fully
  settled (the user-reported "empty answers" failure mode of LM Studio).

These tests run a tiny local mock chat/completions server (no network,
no LLM).
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = Path = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import runner  # noqa: E402


class _MockChatServer(BaseHTTPRequestHandler):
    """Serves POST /v1/chat/completions with scripted responses.

    ``responses`` is a list of (content, http_code) consumed in order; the
    last one repeats. Records every request body for assertions.
    """

    responses = []
    received_bodies = []

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        _MockChatServer.received_bodies.append(body)
        content, code = _MockChatServer.responses[min(
            len(_MockChatServer.received_bodies) - 1, len(_MockChatServer.responses) - 1
        )]
        if code != 200:
            payload = json.dumps({"error": {"message": content}}).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps({
            "model": "lfm2.5-2.6b-heretic-abliterated",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def chat_server():
    _MockChatServer.received_bodies = []
    srv = HTTPServer(("127.0.0.1", 0), _MockChatServer)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    srv.server_close()


def _task(tmp_path):
    task_dir = tmp_path / "task"
    (task_dir / "repo").mkdir(parents=True)
    (task_dir / "repo" / "discount.py").write_text(
        "def apply_discount(price, percent):\n    return price * (1 - percent / 100)\n"
    )
    (task_dir / "task.json").write_text(json.dumps({
        "schema_version": "1.0",
        "id": "mock-direct",
        "name": "Mock direct",
        "category": "bug_fix",
        "prompt": "fix discount.py",
        "repo": "repo",
        "test_command": ["python3", "-c", "import discount"],
        "baseline_status": "failing",
    }), encoding="utf-8")
    return runner.load_task(str(task_dir))


def test_direct_driver_applies_file_sections(tmp_path, chat_server):
    _MockChatServer.responses = [(
        "Here is the fix:\n### FILE: discount.py\n"
        "def apply_discount(price, percent):\n"
        "    result = price * (1 - percent / 100)\n"
        "    return result\n",
        200,
    )]
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "discount.py").write_text("old\n")
    out = runner.run_direct_driver(_task(tmp_path), str(ws), api_base=chat_server)
    assert out["status"] == "success"
    assert out["provider"] == "direct"
    assert out["model"] == "lfm2.5-2.6b-heretic-abliterated"
    assert out["iterations"] == 1
    assert "return result" in (ws / "discount.py").read_text()
    assert out["tokens"] == 46  # 12 in + 34 out from the mock usage
    # Exactly one request — serial, never pipelined.
    assert len(_MockChatServer.received_bodies) == 1
    # The model prompt includes the task prompt AND the current file.
    assert "fix discount.py" in _MockChatServer.received_bodies[0]["messages"][1]["content"]
    assert "### FILE: discount.py" in _MockChatServer.received_bodies[0]["messages"][1]["content"]
    assert _MockChatServer.received_bodies[0]["stream"] is False


def test_direct_driver_retries_empty_response(tmp_path, chat_server):
    """An empty first completion must be retried, and only AFTER the first
    attempt has fully completed (serial). The second attempt is usable."""
    _MockChatServer.responses = [
        ("", 200),
        ("### FILE: discount.py\nreturn result\n", 200),
    ]
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "discount.py").write_text("old\n")
    out = runner.run_direct_driver(_task(tmp_path), str(ws), api_base=chat_server)
    assert out["status"] == "success"
    assert len(_MockChatServer.received_bodies) == 2  # retried once
    assert "return result" in (ws / "discount.py").read_text()


def test_direct_driver_gives_up_after_repeated_empty(tmp_path, chat_server):
    _MockChatServer.responses = [("", 200)]
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "discount.py").write_text("old\n")
    out = runner.run_direct_driver(_task(tmp_path), str(ws), api_base=chat_server)
    assert out["status"] == "error"
    assert out["iterations"] == 0
    assert len(_MockChatServer.received_bodies) == 3  # bounded retries
    assert "empty or unusable" in out["final_output"]


def test_direct_driver_rejects_path_escape(tmp_path, chat_server):
    """A model returning `### FILE: ../evil.py` must NOT write outside the
    workspace — the escape is dropped, the safe section still applies."""
    _MockChatServer.responses = [(
        "### FILE: ../evil.py\nPWNED = True\n"
        "### FILE: discount.py\nreturn result\n",
        200,
    )]
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "discount.py").write_text("old\n")
    out = runner.run_direct_driver(_task(tmp_path), str(ws), api_base=chat_server)
    assert out["status"] == "success"
    assert not (tmp_path / "evil.py").exists()
    assert "return result" in (ws / "discount.py").read_text()


def test_direct_driver_handles_http_error_then_success(tmp_path, chat_server):
    _MockChatServer.responses = [
        ("provider busy", 429),
        ("### FILE: discount.py\nreturn result\n", 200),
    ]
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "discount.py").write_text("old\n")
    out = runner.run_direct_driver(_task(tmp_path), str(ws), api_base=chat_server)
    assert out["status"] == "success"
    assert len(_MockChatServer.received_bodies) == 2


def test_direct_parse_sections_tolerates_fences_and_prose():
    text = (
        "Sure, here is the fix.\n"
        "```python\n"
        "### FILE: ./discount.py\n"
        "def f():\n"
        "    return 1\n"
        "```\n"
        "```python\n"
        "### FILE: utils.py\n"
        "x = 2\n"
        "```\n"
        "Hope that helps!\n"
    )
    sections = runner._direct_parse_sections(text)
    assert sections == [
        ("discount.py", "def f():\n    return 1"),
        ("utils.py", "x = 2"),
    ]


def test_direct_parse_sections_no_markers():
    assert runner._direct_parse_sections("just prose, no file blocks") == []


def test_direct_safe_target_blocks_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(runner._DirectPathError):
        runner._direct_safe_target(str(ws), "../evil.py")
    with pytest.raises(runner._DirectPathError):
        runner._direct_safe_target(str(ws), "/etc/passwd")
    ok = runner._direct_safe_target(str(ws), "sub/discount.py")
    assert ok == (ws / "sub" / "discount.py").resolve()
