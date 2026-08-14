import json
from pathlib import Path

import pytest

from tera_pilot.github_automation import GitHubAutomation
from tera_pilot_tui.backend_report import (
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    sample_report,
    validate_report,
)
from tera_pilot_tui.backend_runner import report_to_json, run_task


def test_tui_backend_module_is_importable():
    assert callable(run_task)


def test_backend_report_schema_json_matches_validator():
    """The published JSON Schema and the manual validator must agree on the
    required fields and verification vocabulary."""
    schema_path = (
        Path(__file__).resolve().parent.parent
        / "tera_pilot_tui"
        / "backend_report_schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert set(schema["required"]) == set(REQUIRED_FIELDS)
    assert set(schema["properties"]["verification"]["properties"]["status"]["enum"]) == {
        "not_requested", "ran", "passed", "failed", "unknown",
    }
    # Security invariant in the schema: tool entries never carry raw args.
    assert "args" not in schema["properties"]["tools"]["items"]["properties"]


def test_sample_report_is_schema_valid_and_json_serializable():
    report = sample_report()
    assert validate_report(report) is True
    json.dumps(report)


def test_validator_rejects_tool_arguments_leak():
    """Raw tool arguments must never be part of the report (secrets)."""
    report = sample_report()
    report["tools"] = [{"tool": "execute_command", "args": {"command": "cat ~/.ssh/id_rsa"}}]
    with pytest.raises(ValueError, match="raw arguments"):
        validate_report(report)


def test_validator_rejects_missing_required_field():
    report = sample_report()
    del report["duration_sec"]
    with pytest.raises(ValueError, match="missing required field: duration_sec"):
        validate_report(report)


def test_validator_rejects_unrun_verification_as_passed():
    """Claims discipline: an unrun check is never called successful."""
    report = sample_report()
    report["verification"] = {"self_verify_called": False, "status": "passed"}
    with pytest.raises(ValueError, match="verification"):
        validate_report(report)


def test_report_to_json_serializes_non_json_metadata():
    """Runtime metadata (paths, enums, sets) must not break CI JSON output."""
    report = sample_report()
    report["metadata"] = {"path": Path("/tmp/x"), "flags": {1, 2}, "when": None}
    text = report_to_json(report)
    parsed = json.loads(text)
    assert parsed["metadata"]["path"] == "/tmp/x"
    assert parsed["metadata"]["flags"] == [1, 2]


def test_github_actions_use_tui_backend_and_evidence_artifact():
    automation = GitHubAutomation(token="test")
    for trigger in ("pull_request", "push", "workflow_dispatch"):
        template = automation.generate_action_template(trigger=trigger)["yaml"]

        assert "tera-pilot-cli" not in template
        assert "tera_pilot_tui.backend_runner" in template
        assert "TERA_PILOT_TASK:" in template
        assert "TERA_PILOT_PROVIDER: openrouter" in template
        assert "TERA_PILOT_MODEL: ${{ vars.TERA_PILOT_MODEL }}" in template
        assert "actions/upload-artifact@v4" in template
        assert "pull-requests: write" not in template

    pull_request = automation.generate_action_template(trigger="pull_request")["yaml"]
    assert "pull-requests: read" in pull_request
    for trigger in ("push", "workflow_dispatch"):
        template = automation.generate_action_template(trigger=trigger)["yaml"]
        assert "pull-requests: read" not in template


def test_backend_report_shape_without_running_a_provider():
    """Exercise report parsing with a fake bridge result without an LLM call."""
    from tera_pilot_tui import backend_runner

    class FakeResult:
        success = True
        output = "ok"
        error = None
        iterations = 1
        metadata = {}
        tool_calls = []

    class FakeBridge:
        def __init__(self, **_kwargs):
            pass

        def run_prompt(self, _prompt):
            return FakeResult()

        def ensure_agent(self):
            class Agent:
                _registry = None

            return Agent()

    original = backend_runner.TeraPilotBridge
    backend_runner.TeraPilotBridge = FakeBridge
    try:
        report = backend_runner.run_task("test", workspace=".")
    finally:
        backend_runner.TeraPilotBridge = original

    assert report["schema_version"] == 1
    assert report["ok"] is True
    assert report["verification"]["status"] == "not_requested"
    json.dumps(report)
