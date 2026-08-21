"""Result schema v1 — manual validator.

Mirrors `eval/results/schema.json` (the machine-readable artifact) without
requiring the `jsonschema` package. The runner validates every result before
writing it; tests validate both the JSON schema file and this validator.

Claims discipline: `status` is about the *run*, `metrics.test_passed` about
the *test run*, `metrics.verification_status` about the *verification
step*, and `workspace.baseline` about the *pristine repo*. They are never
conflated.
"""

SCHEMA_VERSION = "1.0"
VALID_STATUSES = ("success", "failed", "error", "skipped")
VALID_VERIFICATION = ("ran", "passed", "failed", "unknown", "not_run")

METRICS_REQUIRED = (
    "duration_sec",
    "iterations",
    "tokens",
    "cost_usd",
    "tools_used",
    "test_command",
    "test_passed",
    "test_exit_code",
    "test_output",
    "verification_status",
)

#: Optional metric fields — validated only when present (forward-compatible).
METRICS_OPTIONAL = (
    "tokens_in", "tokens_out", "request_count", "cancelled",
    # v2.3.4 (P1.8): evidence counters — provider/tool errors are counted
    # separately so a green test suite is never confused with a clean run.
    "provider_errors", "tool_errors",
)

#: Optional top-level evidence object — real telemetry for the run, no
#: hidden tracking. Validated only when present (forward-compatible).
EVIDENCE_KEYS = ("diff", "provider_errors", "tool_errors", "self_verify")

RESULT_REQUIRED = (
    "schema_version",
    "task_id",
    "task_name",
    "category",
    "status",
    "timestamp",
    "runner_version",
    "driver",
    "provider",
    "model",
    "workspace",
    "prompt",
    "metrics",
    "final_output",
)


def validate_result(result):
    """Validate a result dict against schema v1. Raises ValueError on any
    violation, returns True otherwise."""
    if not isinstance(result, dict):
        raise ValueError("result must be a JSON object")
    for field in RESULT_REQUIRED:
        if field not in result:
            raise ValueError(f"result missing required field: {field}")
    if result["schema_version"] != SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be {SCHEMA_VERSION!r}, got {result['schema_version']!r}"
        )
    if result["status"] not in VALID_STATUSES:
        raise ValueError(f"invalid status: {result['status']!r}")
    if not isinstance(result["timestamp"], str) or not result["timestamp"]:
        raise ValueError("timestamp must be a non-empty ISO-8601 string")

    workspace = result["workspace"]
    if not isinstance(workspace, dict):
        raise ValueError("workspace must be a JSON object")
    if workspace.get("commit") is not None and not isinstance(workspace["commit"], str):
        raise ValueError("workspace.commit must be a string or null")
    if not isinstance(workspace.get("repo_hash", ""), str) or not workspace.get("repo_hash"):
        raise ValueError("workspace.repo_hash must be a non-empty string")
    baseline = workspace.get("baseline")
    if baseline is not None:
        if not isinstance(baseline, dict):
            raise ValueError("workspace.baseline must be a JSON object")
        if baseline.get("test_passed") not in (True, False, None):
            raise ValueError("workspace.baseline.test_passed must be boolean or null")
        if baseline.get("test_exit_code") is not None and not isinstance(
            baseline["test_exit_code"], int
        ):
            raise ValueError("workspace.baseline.test_exit_code must be integer or null")
        if not isinstance(baseline.get("duration_sec", 0), (int, float)) or baseline.get("duration_sec", 0) < 0:
            raise ValueError("workspace.baseline.duration_sec must be a non-negative number")

    metrics = result["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be a JSON object")
    for field in METRICS_REQUIRED:
        if field not in metrics:
            raise ValueError(f"metrics missing required field: {field}")
    if not isinstance(metrics["duration_sec"], (int, float)) or metrics["duration_sec"] < 0:
        raise ValueError("metrics.duration_sec must be a non-negative number")
    if not isinstance(metrics["iterations"], int) or metrics["iterations"] < 0:
        raise ValueError("metrics.iterations must be a non-negative integer")
    if not isinstance(metrics["tokens"], int) or metrics["tokens"] < 0:
        raise ValueError("metrics.tokens must be a non-negative integer")
    if not isinstance(metrics["cost_usd"], (int, float)) or metrics["cost_usd"] < 0:
        raise ValueError("metrics.cost_usd must be a non-negative number")
    if not isinstance(metrics["tools_used"], list):
        raise ValueError("metrics.tools_used must be an array")
    if metrics["test_passed"] not in (True, False, None):
        raise ValueError("metrics.test_passed must be boolean or null")
    if metrics["test_exit_code"] is not None and not isinstance(metrics["test_exit_code"], int):
        raise ValueError("metrics.test_exit_code must be integer or null")
    if metrics["verification_status"] not in VALID_VERIFICATION:
        raise ValueError(f"invalid verification_status: {metrics['verification_status']!r}")
    for field in METRICS_OPTIONAL:
        if field not in metrics:
            continue
        if field == "cancelled":
            if metrics[field] not in (True, False):
                raise ValueError("metrics.cancelled must be boolean")
        elif not isinstance(metrics[field], int) or metrics[field] < 0:
            raise ValueError(f"metrics.{field} must be a non-negative integer")

    # v2.3.4 (P1.8): optional evidence object (diff, error counters,
    # self_verify result).
    evidence = result.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be a JSON object")
        for key in evidence:
            if key not in EVIDENCE_KEYS:
                raise ValueError(f"unknown evidence key: {key!r}")
        if evidence.get("diff") is not None and not isinstance(evidence["diff"], str):
            raise ValueError("evidence.diff must be a string or null")
        if evidence.get("self_verify") is not None and not isinstance(evidence["self_verify"], str):
            raise ValueError("evidence.self_verify must be a string or null")
        for key in ("provider_errors", "tool_errors"):
            if evidence.get(key) is not None and (
                not isinstance(evidence[key], int) or evidence[key] < 0
            ):
                raise ValueError(f"evidence.{key} must be a non-negative integer")
    if not isinstance(result["final_output"], str):
        raise ValueError("final_output must be a string (potentially sensitive field)")
    return True


def sample_result():
    """A schema-valid result used by tests and as a documentation example."""
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": "fix-config-loader-empty-file",
        "task_name": "Fix config loader crash on empty file",
        "category": "bug_fix",
        "status": "skipped",
        "timestamp": "2026-08-13T00:00:00+00:00",
        "runner_version": "0.2.0",
        "driver": "fake",
        "provider": None,
        "model": None,
        "workspace": {
            "repo_hash": "0123456789abcdef",
            "commit": None,
            "baseline": {"test_passed": False, "test_exit_code": 1, "duration_sec": 0.4},
        },
        "prompt": "Fix the bug.",
        "metrics": {
            "duration_sec": 0.004,
            "iterations": 0,
            "tokens": 0,
            "cost_usd": 0.0,
            "tools_used": [],
            "test_command": ["python3", "-m", "pytest", "-q"],
            "test_passed": False,
            "test_exit_code": 1,
            "test_output": "1 failed",
            "verification_status": "failed",
        },
        "final_output": "[fake driver] no agent run (CI stub)",
    }
