"""Regression tests for the tool-engine pipe-drain fix (v2.3.4-fix).

``ToolEngine._run_test_command_sandboxed`` (the verify/test path) used
to read ``proc.stdout`` / ``proc.stderr`` only AFTER the child exited.
A child that writes more than the OS pipe buffer (~64 KB) blocks forever
on write, never exits, and gets killed by the 60 s deadline — so a
1-second command with 100 KB of output spuriously reported ``[TIMEOUT]``
with no captured output.

The pipes are now drained by daemon threads WHILE the child runs, so a
big-output command completes normally with its output captured, and Stop
(cancellation) still aborts cleanly.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.agent_runtime.tool_engine import ToolEngine  # noqa: E402


def _engine(tmp_path) -> ToolEngine:
    return ToolEngine(str(tmp_path))


def test_large_stdout_completes_without_timeout(tmp_path):
    """>64 KB of stdout must NOT appear as a 60 s timeout with no output."""
    e = _engine(tmp_path)
    result = e._run_test_command_sandboxed(
        [sys.executable, "-c", "print('x' * 200000)"]
    )
    assert "[TIMEOUT]" not in result, result
    assert "[CANCELLED" not in result, result
    assert "[EXIT CODE] 0" in result
    assert "[STDOUT]" in result
    assert "xxx" in result, "captured output must be present"


def test_large_stderr_is_captured(tmp_path):
    e = _engine(tmp_path)
    result = e._run_test_command_sandboxed(
        [sys.executable, "-c", "import sys; sys.stderr.write('E' * 200000)"]
    )
    assert "[TIMEOUT]" not in result, result
    assert "[EXIT CODE] 0" in result
    assert "[STDERR]" in result
    assert "EEE" in result


def test_combined_output_over_buffer(tmp_path):
    """stdout AND stderr together exceed the pipe buffer."""
    e = _engine(tmp_path)
    code = (
        "import sys;"
        "sys.stdout.write('O' * 120000);"
        "sys.stderr.write('E' * 120000)"
    )
    result = e._run_test_command_sandboxed([sys.executable, "-c", code])
    assert "[TIMEOUT]" not in result, result
    assert "[EXIT CODE] 0" in result
    assert "OOO" in result and "EEE" in result


def test_failing_command_reports_exit_code_and_stderr(tmp_path):
    e = _engine(tmp_path)
    result = e._run_test_command_sandboxed(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
    )
    assert "[TIMEOUT]" not in result
    assert "[EXIT CODE] 3" in result
    assert "boom" in result


def test_cancellation_aborts_promptly(tmp_path):
    e = _engine(tmp_path)
    e._request_confirmation = lambda *a, **k: True  # type: ignore[method-assign]
    e._cancel_check = lambda: True  # simulate user clicking Stop
    result = e._run_test_command_sandboxed(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    assert "[CANCELLED BY USER]" in result
