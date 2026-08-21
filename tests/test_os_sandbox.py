"""Tests for the OS-level sandbox (P1.10).

Covers the pure profile/bwrap builders and — on macOS, where
``/usr/bin/sandbox-exec`` exists — REAL end-to-end isolation: network is
denied, writes outside the workspace are blocked, sensitive paths are
unreadable, and legitimate workspace commands still work. Skips the
backend-dependent integration tests when no backend is available.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tera_pilot import os_sandbox  # noqa: E402


# ── Pure helpers ─────────────────────────────────────────────────────

def test_sanitize_mode_normalizes():
    assert os_sandbox.sanitize_mode("off") == "off"
    assert os_sandbox.sanitize_mode(" auto ") == "auto"
    assert os_sandbox.sanitize_mode("ON") == "on"
    assert os_sandbox.sanitize_mode("bogus") == "auto"
    assert os_sandbox.sanitize_mode(None) == os.environ.get("TERA_PILOT_OS_SANDBOX", "auto")


def test_build_seatbelt_profile_denies_network_and_restricts_writes(tmp_path):
    ws = os.path.realpath(str(tmp_path))
    prof = os_sandbox.build_seatbelt_profile(str(tmp_path))
    assert "(deny default)" in prof
    assert "(deny network*)" in prof
    assert "(allow file-write*" in prof
    assert f'(subpath "{ws}")' in prof
    assert '(subpath "/private/tmp")' in prof
    # Sensitive paths under the real home are denied reads.
    assert "(deny file-read*" in prof
    assert ".ssh" in prof


def test_build_seatbelt_profile_with_custom_home(tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    prof = os_sandbox.build_seatbelt_profile(str(tmp_path), home=str(fake_home))
    assert os.path.realpath(str(fake_home / ".ssh")) in prof


def test_build_bwrap_args_structure(tmp_path):
    ws = os.path.realpath(str(tmp_path))
    args = os_sandbox.build_bwrap_args(["pytest", "-q"], str(tmp_path))
    joined = " ".join(args)
    assert args[0] == "bwrap" or args[0].endswith("/bwrap")
    assert "--unshare-net" in args
    assert "--ro-bind" in args and "/" in args
    assert "--bind" in args and ws in args
    assert "--tmpfs" in args and "/tmp" in args
    assert args[-1] == "-q"
    assert "pytest" in joined


def test_wrap_command_off_never_wraps(tmp_path):
    args = ["git", "status"]
    wrapped, backend = os_sandbox.wrap_command(args, str(tmp_path), mode="off")
    assert wrapped == args
    assert backend is None


def test_wrap_command_on_without_backend_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(os_sandbox, "detect_backend", lambda: None)
    wrapped, backend = os_sandbox.wrap_command(["ls"], str(tmp_path), mode="on")
    assert wrapped is None and backend is None


def test_wrap_command_auto_without_backend_runs_unwrapped(tmp_path, monkeypatch):
    monkeypatch.setattr(os_sandbox, "detect_backend", lambda: None)
    args = ["ls"]
    wrapped, backend = os_sandbox.wrap_command(args, str(tmp_path), mode="auto")
    assert wrapped == args and backend is None


# ── Real backend integration (macOS sandbox-exec / Linux bwrap) ──────

BACKEND = os_sandbox.detect_backend()
BACKEND_REASON = "no OS sandbox backend on this platform"


def _run_sandboxed(argv, cwd=None, timeout=30):
    return subprocess.run(
        argv, capture_output=True, text=True, cwd=cwd, timeout=timeout,
    )


@pytest.mark.skipif(BACKEND != "macos_sandbox_exec", reason=BACKEND_REASON)
def test_macos_sandbox_blocks_network(tmp_path):
    ws = str(tmp_path)
    code = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://example.com', timeout=3)\n"
        "    print('NETWORK_OK')\n"
        "except Exception as e:\n"
        "    print('BLOCKED:', type(e).__name__)\n"
    )
    argv = os_sandbox.wrap_macos(["python3", "-c", code], ws)
    res = _run_sandboxed(argv, cwd=ws)
    assert res.returncode == 0, res.stderr
    assert "BLOCKED" in res.stdout, res.stdout
    assert "NETWORK_OK" not in res.stdout


@pytest.mark.skipif(BACKEND != "macos_sandbox_exec", reason=BACKEND_REASON)
def test_macos_sandbox_allows_workspace_write(tmp_path):
    ws = str(tmp_path)
    out = tmp_path / "out.txt"
    code = f"open({str(out)!r}, 'w').write('sandboxed'); print(open({str(out)!r}).read())"
    argv = os_sandbox.wrap_macos(["python3", "-c", code], ws)
    res = _run_sandboxed(argv, cwd=ws)
    assert res.returncode == 0, res.stderr
    assert out.read_text() == "sandboxed"


@pytest.mark.skipif(BACKEND != "macos_sandbox_exec", reason=BACKEND_REASON)
def test_macos_sandbox_blocks_outside_write(tmp_path):
    # NB: pytest's tmp_path lives under the SYSTEM temp dir, which the
    # profile deliberately allows for legitimate temp usage — so "outside"
    # here means a path under the user's HOME (neither workspace nor temp
    # is writable there).
    ws = str(tmp_path)
    outside = Path.home() / ".tera_pilot_test_sandbox_pwn.txt"
    try:
        code = f"open({str(outside)!r}, 'w').write('pwn')"
        argv = os_sandbox.wrap_macos(["python3", "-c", code], ws)
        res = _run_sandboxed(argv, cwd=ws)
        assert not outside.exists(), "sandbox allowed a write outside the workspace!"
    finally:
        try:
            outside.unlink()
        except FileNotFoundError:
            pass


@pytest.mark.skipif(BACKEND != "macos_sandbox_exec", reason=BACKEND_REASON)
def test_macos_sandbox_blocks_sensitive_read(tmp_path):
    fake_home = tmp_path / "fake_home"
    (fake_home / ".ssh").mkdir(parents=True)
    key = fake_home / ".ssh" / "id_rsa"
    key.write_text("SUPER-SECRET-MARKER")
    ws = tmp_path / "ws"
    ws.mkdir()
    code = f"print(open({str(key)!r}).read()[:30])"
    argv = os_sandbox.wrap_macos(["python3", "-c", code], str(ws), home=str(fake_home))
    res = _run_sandboxed(argv, cwd=str(ws))
    assert res.returncode != 0, res.stdout
    assert "SUPER-SECRET-MARKER" not in res.stdout


@pytest.mark.skipif(BACKEND is None, reason=BACKEND_REASON)
def test_wrap_command_on_with_backend_wraps(tmp_path):
    wrapped, backend = os_sandbox.wrap_command(["ls"], str(tmp_path), mode="on")
    assert wrapped is not None and wrapped[0] != "ls"
    assert backend == BACKEND


# ── ToolEngine integration ────────────────────────────────────────────

def test_toolengine_os_sandbox_blocks_network(tmp_path):
    from tera_pilot.agent_runtime.tool_engine import ToolEngine
    if os_sandbox.detect_backend() is None:
        pytest.skip("no OS sandbox backend")
    (tmp_path / "net.py").write_text(
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://example.com', timeout=3)\n"
        "    print('NETWORK_OK')\n"
        "except Exception as e:\n"
        "    print('BLOCKED:', type(e).__name__)\n",
        encoding="utf-8",
    )
    e = ToolEngine(str(tmp_path))
    e.headless_confirm = "allow"
    e.os_sandbox = "on"
    res = e._execute_command("python3 net.py", timeout=60)
    assert "NETWORK_OK" not in res, res
    assert "BLOCKED" in res, res


def test_toolengine_os_sandbox_off_works(tmp_path):
    from tera_pilot.agent_runtime.tool_engine import ToolEngine
    (tmp_path / "ok.py").write_text("print('sandbox-off-works')\n", encoding="utf-8")
    e = ToolEngine(str(tmp_path))
    e.headless_confirm = "allow"
    e.os_sandbox = "off"
    res = e._execute_command("python3 ok.py", timeout=60)
    assert "sandbox-off-works" in res, res


def test_toolengine_run_code_sandboxed(tmp_path):
    """P1.10: run_code under the OS sandbox can write inside its temp
    workspace but cannot open network connections (the highest-
    exfiltration-risk surface)."""
    from tera_pilot.agent_runtime.tool_engine import ToolEngine
    if os_sandbox.detect_backend() is None:
        pytest.skip("no OS sandbox backend")
    e = ToolEngine(str(tmp_path))
    e.headless_confirm = "allow"
    e.os_sandbox = "on"
    res = e._run_code('open("made.txt","w").write("hi"); print("wrote ok")', language="python")
    assert "wrote ok" in res, res
    res2 = e._run_code(
        "import urllib.request\n"
        "urllib.request.urlopen('http://example.com', timeout=3)",
        language="python",
    )
    assert "wrote ok" not in res2
    assert "URLError" in res2 or "gaierror" in res2
