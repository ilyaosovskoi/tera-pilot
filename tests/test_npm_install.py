"""Hermetic tests for the npm one-command install path.

Exercises the real ``scripts/postinstall.js``, ``scripts/preuninstall.js``
and the ``bin/*.js`` launchers with a *fake* Python interpreter (a tiny
executable that emulates ``python -m venv`` and records invocations) — so
no network, no real venv, no pip are involved.

Covers what a user actually hits with ``npm install -g tera-pilot``:

- postinstall creates the venv and writes the npm-managed marker
- re-running postinstall is a no-op fast path (same version)
- the launchers resolve the venv Python and forward CLI args to it
- when the Python module is missing, the launcher exits 1 with a
  friendly recovery message instead of a raw ModuleNotFoundError
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── fake Python interpreter ────────────────────────────────────────────

FAKE_PYTHON_SRC = r'''#!/usr/bin/env python3
"""Fake python for hermetic npm-install tests (see test_npm_install.py)."""
import os
import sys


def log(*parts):
    logfile = os.environ.get("TP_STUB_LOG")
    if logfile:
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(" ".join(parts) + "\n")


args = sys.argv[1:]

# python -m venv <dir>  →  create <dir>/bin/python3 (a copy of ourselves)
if args[:2] == ["-m", "venv"]:
    vdir = args[2]
    bdir = os.path.join(vdir, "bin")
    os.makedirs(bdir, exist_ok=True)
    stub = os.path.join(bdir, "python3")
    with open(__file__, "r", encoding="utf-8") as src:
        with open(stub, "w", encoding="utf-8") as dst:
            dst.write(src.read())
    os.chmod(stub, 0o755)
    sys.exit(0)

# python -c "import <pkg>"  →  exit 0 only if TP_MODULE_OK=1
if args[:2] == ["-c", "import tera_pilot"] or args[:2] == ["-c", "import tera_pilot_tui"]:
    sys.exit(0 if os.environ.get("TP_MODULE_OK") == "1" else 1)

# python -m tera_pilot...  →  record the invocation
if args[:2] == ["-m", "tera_pilot"] \
        or args[:2] == ["-m", "tera_pilot_tui"] \
        or args[:2] == ["-m", "tera_pilot.daemon"] \
        or args[:2] == ["-m", "tera_pilot.agent.acp_server"]:
    log(*args)
    sys.exit(0)

sys.exit(0)
'''


@pytest.fixture
def fake_python(tmp_path):
    py = tmp_path / "fake-python"
    py.write_text(FAKE_PYTHON_SRC, encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return str(py)


@pytest.fixture
def npm_env(tmp_path, fake_python):
    """Env pointing postinstall + launchers at the fake interpreter."""
    logfile = tmp_path / "stub.log"
    env = dict(os.environ)
    env.update(
        {
            "TERA_PILOT_PYTHON": fake_python,
            "TERA_PILOT_VENV": str(tmp_path / "venv"),
            "TERA_PILOT_SKIP_PIP": "1",
            "HOME": str(tmp_path / "home"),
            "TP_STUB_LOG": str(logfile),
        }
    )
    return env, logfile


def run_node(script_rel, args, env):
    return subprocess.run(
        ["node", str(PROJECT_ROOT / script_rel), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# ── postinstall ────────────────────────────────────────────────────────


def test_postinstall_bootstraps_venv_and_writes_marker(tmp_path, npm_env):
    env, _ = npm_env
    venv = Path(env["TERA_PILOT_VENV"])
    assert not venv.exists()

    res = run_node("scripts/postinstall.js", [], env)
    assert res.returncode == 0, f"postinstall failed:\n{res.stdout}\n{res.stderr}"

    # venv + marker created
    assert (venv / "bin" / "python3").exists()
    marker_path = venv / ".npm-managed.json"
    assert marker_path.exists()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["package"] == "tera-pilot"
    assert marker["version"] == "2.3.3"
    assert "installed_at" in marker


def test_postinstall_fast_path_is_idempotent(tmp_path, npm_env):
    env, _ = npm_env
    assert run_node("scripts/postinstall.js", [], env).returncode == 0

    # Second run must be a silent no-op (same version marker matches).
    res = run_node("scripts/postinstall.js", [], env)
    assert res.returncode == 0
    assert "already installed" in res.stdout

    # Markers must not be duplicated.
    marker_path = Path(env["TERA_PILOT_VENV"]) / ".npm-managed.json"
    assert marker_path.read_text(encoding="utf-8").count("installed_at") == 1


def test_postinstall_force_rewrites_marker(tmp_path, npm_env):
    env, _ = npm_env
    assert run_node("scripts/postinstall.js", [], env).returncode == 0
    marker_path = Path(env["TERA_PILOT_VENV"]) / ".npm-managed.json"
    first = json.loads(marker_path.read_text(encoding="utf-8"))

    # FORCE skips the fast path and rewrites the marker even for the same version.
    env["TERA_PILOT_FORCE"] = "1"
    res = run_node("scripts/postinstall.js", [], env)
    assert res.returncode == 0
    assert "already installed" not in res.stdout
    second = json.loads(marker_path.read_text(encoding="utf-8"))
    assert second["installed_at"] != first["installed_at"]


def test_postinstall_fails_cleanly_without_python(tmp_path, npm_env):
    env, _ = npm_env
    env.pop("TERA_PILOT_PYTHON")
    # Hide python3/python from the fallback scan, but keep node resolvable:
    # a dedicated bin dir holding only a node symlink.
    clean_bin = tmp_path / "clean-bin"
    clean_bin.mkdir()
    node_bin = Path(subprocess.check_output(["which", "node"], text=True).strip())
    (clean_bin / "node").symlink_to(node_bin)
    env["PATH"] = str(clean_bin)
    res = run_node("scripts/postinstall.js", [], env)
    assert res.returncode == 1
    assert "Python 3 not found" in res.stderr


# ── launchers ──────────────────────────────────────────────────────────


def test_launcher_resolves_venv_python_and_forwards_args(tmp_path, npm_env):
    env, logfile = npm_env
    env["TP_MODULE_OK"] = "1"
    # Bootstrap the venv first, as npm's postinstall would.
    assert run_node("scripts/postinstall.js", [], env).returncode == 0

    res = run_node("bin/tera-pilot.js", ["--version"], env)
    assert res.returncode == 0, f"launcher failed:\n{res.stdout}\n{res.stderr}"
    invocations = logfile.read_text(encoding="utf-8").strip().splitlines()
    assert invocations, "launcher never invoked python"
    assert invocations[-1].split() == ["-m", "tera_pilot", "--version"]

    # The TUI launcher forwards to tera_pilot_tui.
    res = run_node("bin/tera-pilot-tui.js", ["hello"], env)
    assert res.returncode == 0
    invocations = logfile.read_text(encoding="utf-8").strip().splitlines()
    assert invocations[-1].split() == ["-m", "tera_pilot_tui", "hello"]


def test_launchers_for_daemon_and_acp_forward_to_python(tmp_path, npm_env):
    """tera-pilot-daemon and tera-pilot-acp must be npm-bin reachable and
    forward their args to the right Python module."""
    env, logfile = npm_env
    env["TP_MODULE_OK"] = "1"
    assert run_node("scripts/postinstall.js", [], env).returncode == 0

    res = run_node("bin/tera-pilot-daemon.js", ["--port", "8765"], env)
    assert res.returncode == 0, f"daemon launcher failed:\n{res.stdout}\n{res.stderr}"
    invocations = logfile.read_text(encoding="utf-8").strip().splitlines()
    assert invocations[-1].split() == ["-m", "tera_pilot.daemon", "--port", "8765"]

    res = run_node("bin/tera-pilot-acp.js", ["--mcp-server", "--workspace", "/tmp/ws"], env)
    assert res.returncode == 0, f"acp launcher failed:\n{res.stdout}\n{res.stderr}"
    invocations = logfile.read_text(encoding="utf-8").strip().splitlines()
    assert invocations[-1].split() == [
        "-m", "tera_pilot.agent.acp_server", "--mcp-server", "--workspace", "/tmp/ws",
    ]


def test_launcher_friendly_error_when_module_missing(tmp_path, npm_env):
    env, _ = npm_env
    env["TP_MODULE_OK"] = "0"  # import tera_pilot fails
    # No venv either — resolution falls back to the fake TERA_PILOT_PYTHON.
    res = run_node("bin/tera-pilot.js", [], env)
    assert res.returncode == 1
    assert "npm install -g tera-pilot" in res.stderr
    assert "ModuleNotFoundError" not in res.stderr

    res = run_node("bin/tera-pilot-tui.js", [], env)
    assert res.returncode == 1
    assert "npm install -g tera-pilot" in res.stderr


# ── preuninstall ───────────────────────────────────────────────────────


def test_preuninstall_removes_npm_managed_venv(tmp_path, npm_env):
    env, _ = npm_env
    assert run_node("scripts/postinstall.js", [], env).returncode == 0
    venv = Path(env["TERA_PILOT_VENV"])
    assert venv.exists()

    res = run_node("scripts/preuninstall.js", [], env)
    assert res.returncode == 0, f"preuninstall failed:\n{res.stdout}\n{res.stderr}"
    assert not venv.exists(), "npm-managed venv should be removed on uninstall"


def test_preuninstall_keeps_unmanaged_venv(tmp_path, npm_env):
    env, _ = npm_env
    venv = Path(env["TERA_PILOT_VENV"])
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    # No marker → the venv predates npm management → keep it.

    res = run_node("scripts/preuninstall.js", [], env)
    assert res.returncode == 0
    assert venv.exists(), "venv without the npm marker must be left alone"
