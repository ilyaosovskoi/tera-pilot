"""Backend report schema v1 — manual validator.

Mirrors `tera_pilot_tui/backend_report_schema.json` (the machine-readable
artifact) without requiring the `jsonschema` package. The producer
(`backend_runner.run_task`) validates every report before returning it;
tests validate both the JSON schema file and this validator.

Claims discipline: `status` is about the *run*, `verification.status` is
about the *verification step*, `ok` is the run-level success flag. An
unrun verification is never called successful.

Security contract: tool arguments are intentionally NOT exported
(command/MCP payloads can contain secrets); `final_output` and
`test_result.output` are potentially sensitive.
"""

from __future__ import annotations

from typing import Any, Dict

SCHEMA_VERSION = 1
VALID_STATUSES = ("success", "failed", "error")
VALID_VERIFICATION = ("not_requested", "ran", "passed", "failed", "unknown")

REQUIRED_FIELDS = (
    "schema_version",
    "ok",
    "status",
    "workspace",
    "provider",
    "model",
    "iterations",
    "duration_sec",
    "tools",
    "verification",
    "final_output",
)


def validate_report(report: Dict[str, Any]) -> bool:
    """Validate a backend report dict against schema v1.

    Raises ValueError on any violation, returns True otherwise.
    """
    if not isinstance(report, dict):
        raise ValueError("report must be a JSON object")
    for field in REQUIRED_FIELDS:
        if field not in report:
            raise ValueError(f"report missing required field: {field}")
    if report["schema_version"] != SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be {SCHEMA_VERSION!r}, got {report['schema_version']!r}"
        )
    if not isinstance(report["ok"], bool):
        raise ValueError("ok must be boolean")
    if report["status"] not in VALID_STATUSES:
        raise ValueError(f"invalid status: {report['status']!r}")
    if not isinstance(report["workspace"], str) or not report["workspace"]:
        raise ValueError("workspace must be a non-empty string")
    for field in ("provider", "model"):
        if report[field] is not None and not isinstance(report[field], str):
            raise ValueError(f"{field} must be string or null")
    if not isinstance(report["iterations"], int) or report["iterations"] < 0:
        raise ValueError("iterations must be a non-negative integer")
    if not isinstance(report["duration_sec"], (int, float)) or report["duration_sec"] < 0:
        raise ValueError("duration_sec must be a non-negative number")

    tools = report["tools"]
    if not isinstance(tools, list):
        raise ValueError("tools must be an array")
    for call in tools:
        if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
            raise ValueError("each tool entry must be an object with a string 'tool'")
        # Security invariant: raw tool arguments must never be exported.
        if "args" in call or "arguments" in call or "payload" in call:
            raise ValueError("tool entries must not contain raw arguments")

    verification = report["verification"]
    if not isinstance(verification, dict):
        raise ValueError("verification must be a JSON object")
    if not isinstance(verification.get("self_verify_called"), bool):
        raise ValueError("verification.self_verify_called must be boolean")
    if verification.get("status") not in VALID_VERIFICATION:
        raise ValueError(f"invalid verification.status: {verification.get('status')!r}")
    # Claims discipline: an unrun verification is never called successful.
    if verification.get("status") == "passed" and not verification.get("self_verify_called"):
        raise ValueError("verification.status 'passed' requires self_verify_called=True")

    if not isinstance(report["final_output"], str):
        raise ValueError("final_output must be a string (potentially sensitive field)")

    # Optional fields
    if report.get("error") is not None and not isinstance(report["error"], str):
        raise ValueError("error must be string or null")
    for field in ("tokens",):
        if field in report and (not isinstance(report[field], int) or report[field] < 0):
            raise ValueError(f"{field} must be a non-negative integer")
    if "cost_usd" in report and (
        not isinstance(report["cost_usd"], (int, float)) or report["cost_usd"] < 0
    ):
        raise ValueError("cost_usd must be a non-negative number")
    if report.get("audit_ref") is not None and not isinstance(report["audit_ref"], str):
        raise ValueError("audit_ref must be string or null")

    test_result = report.get("test_result")
    if test_result is not None:
        if not isinstance(test_result, dict):
            raise ValueError("test_result must be an object or null")
        if test_result.get("passed") not in (True, False, None):
            raise ValueError("test_result.passed must be boolean or null")
        if test_result.get("exit_code") is not None and not isinstance(
            test_result["exit_code"], int
        ):
            raise ValueError("test_result.exit_code must be integer or null")

    metadata = report.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be an object or null")
    return True


def sample_report() -> Dict[str, Any]:
    """A schema-valid report used by tests and as a documentation example."""
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "success",
        "workspace": "/tmp/project",
        "provider": "fake",
        "model": "fake-1",
        "iterations": 1,
        "duration_sec": 0.5,
        "tokens": 120,
        "cost_usd": 0.001,
        "tools": [{"tool": "read_file", "error": None, "duration_ms": 1.2}],
        "verification": {"self_verify_called": False, "status": "not_requested"},
        "test_result": None,
        "final_output": "[fake driver] no agent run (CI stub)",
        "audit_ref": None,
        "metadata": {},
    }
