"""Regression tests for daemon registry wiring (v2.4.1-fix).

Previously ``daemon._run_task`` and ``daemon.run_single_task`` called
``AgentRuntime(workspace=...)`` with no ``registry`` argument, but
``AgentRuntime.__init__`` requires one — so every daemon task crashed
with ``TypeError`` before the agent started. ``_build_registry()`` now
loads ``~/.tera_pilot/config.json`` and wires providers + active
provider, mirroring the api_server path.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tera_pilot.daemon as daemon  # noqa: E402


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a temp dir so config.json is isolated per test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".tera_pilot").mkdir(exist_ok=True)
    return tmp_path


def _write_config(home: Path, payload: dict) -> None:
    (home / ".tera_pilot" / "config.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_build_registry_applies_provider_and_active(isolated_home):
    _write_config(isolated_home, {
        "active_provider": "gemini",
        "providers": {
            "gemini": {"api_key": "FAKE-KEY", "model": "gemini-2.5-flash"},
        },
    })
    reg = daemon._build_registry()
    assert reg.active_id == "gemini"
    prov = reg.get("gemini")
    assert prov.config.api_key == "FAKE-KEY"
    assert prov.config.model == "gemini-2.5-flash"


def test_build_registry_defaults_when_no_config(isolated_home):
    # No config.json at all — must not raise, falls back to defaults.
    reg = daemon._build_registry()
    assert reg is not None


def test_build_registry_tolerates_bad_provider_config(isolated_home):
    # A provider entry with junk values must not break the others.
    _write_config(isolated_home, {
        "active_provider": "ollama",
        "providers": {
            "bogus_provider": {"api_key": "X"},
            "gemini": {"api_key": "FAKE-KEY", "model": "gemini-2.5-flash",
                       "temperature": "not-a-float"},
        },
    })
    reg = daemon._build_registry()
    assert reg.active_id == "ollama"
    # gemini still configured despite the bad temperature on... itself —
    # configure() failures are caught per-provider.
    prov = reg.get("gemini")
    assert prov.config.api_key == "FAKE-KEY"


def test_agent_runtime_constructs_with_daemon_registry(isolated_home):
    _write_config(isolated_home, {
        "active_provider": "gemini",
        "providers": {"gemini": {"api_key": "FAKE-KEY"}},
    })
    from tera_pilot.agent_runtime import AgentRuntime
    reg = daemon._build_registry()
    agent = AgentRuntime(registry=reg, workspace=str(isolated_home))
    assert agent._registry is reg
    assert agent.tools._registry is reg
