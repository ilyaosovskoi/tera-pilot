"""Tests for the evaluation harness (P0.1): schema integrity and the
fake-driver end-to-end pipeline. No network, deterministic."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import schema as eschema  # noqa: E402
from eval import runner  # noqa: E402


def test_schema_json_is_valid_json():
    schema = json.loads((ROOT / "eval" / "results" / "schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].startswith("http://json-schema.org/draft-07")
    assert schema["title"]
    for field in ("schema_version", "task_id", "status", "timestamp", "metrics", "final_output"):
        assert field in schema["properties"], f"schema missing property {field}"


def test_manual_validator_mirrors_schema_json():
    """The manual validator (eval/schema.py) and the JSON Schema artifact
    must agree on the required fields, so the runner and external tooling
    validate the same contract."""
    schema = json.loads((ROOT / "eval" / "results" / "schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == eschema.SCHEMA_VERSION
    assert set(schema["properties"]["status"]["enum"]) == set(eschema.VALID_STATUSES)
    assert set(schema["properties"]["metrics"]["required"]) == set(eschema.METRICS_REQUIRED)
    assert set(schema["properties"]["metrics"]["properties"]["verification_status"]["enum"]) == set(
        eschema.VALID_VERIFICATION
    )
    # Top-level required fields must match the manual validator exactly.
    assert set(schema["required"]) == set(eschema.RESULT_REQUIRED)
    # Driver and category enums stay in sync.
    assert set(schema["properties"]["driver"]["enum"]) == set(runner.DRIVERS)
    assert set(schema["properties"]["category"]["enum"]) == set(runner.VALID_CATEGORIES)


def test_validate_result_accepts_valid_sample():
    good = eschema.sample_result()
    assert eschema.validate_result(good) is True


def test_validate_result_rejects_bad_samples():
    good = eschema.sample_result()

    bad_version = dict(good)
    bad_version["schema_version"] = "9.9"
    _expect_invalid(bad_version)

    bad_status = dict(good)
    bad_status["status"] = "bogus"
    _expect_invalid(bad_status)

    bad_metrics = dict(good)
    bad_metrics["metrics"] = dict(good["metrics"])
    del bad_metrics["metrics"]["verification_status"]
    _expect_invalid(bad_metrics)

    bad_verify = dict(good)
    bad_verify["metrics"] = dict(good["metrics"])
    bad_verify["metrics"]["verification_status"] = "nope"
    _expect_invalid(bad_verify)

    missing_final = dict(good)
    del missing_final["final_output"]
    _expect_invalid(missing_final)


def _expect_invalid(result):
    import pytest

    with pytest.raises(ValueError):
        eschema.validate_result(result)


def test_validate_result_accepts_optional_usage_fields():
    """Optional metrics (tokens_in/out, request_count, cancelled) and
    workspace.baseline must be accepted when present."""
    good = eschema.sample_result()
    good["metrics"]["tokens_in"] = 120
    good["metrics"]["tokens_out"] = 40
    good["metrics"]["request_count"] = 3
    good["metrics"]["cancelled"] = False
    good["workspace"]["baseline"] = {"test_passed": False, "test_exit_code": 1, "duration_sec": 0.42}
    assert eschema.validate_result(good) is True


def test_validate_result_rejects_bad_optional_fields():
    good = eschema.sample_result()

    bad_tokens = dict(good)
    bad_tokens["metrics"] = dict(good["metrics"])
    bad_tokens["metrics"]["tokens_in"] = -1
    _expect_invalid(bad_tokens)

    bad_cancelled = dict(good)
    bad_cancelled["metrics"] = dict(good["metrics"])
    bad_cancelled["metrics"]["cancelled"] = "yes"
    _expect_invalid(bad_cancelled)

    bad_baseline = dict(good)
    bad_baseline["workspace"] = dict(good["workspace"])
    bad_baseline["workspace"]["baseline"] = {"test_passed": "nope"}
    _expect_invalid(bad_baseline)

    bad_baseline_exit = dict(good)
    bad_baseline_exit["workspace"] = dict(good["workspace"])
    bad_baseline_exit["workspace"]["baseline"] = {"test_passed": True, "test_exit_code": "zero"}
    _expect_invalid(bad_baseline_exit)


def test_fake_driver_end_to_end(tmp_path):
    """A fake-driver run must produce exactly one schema-valid result file,
    with test_passed=False (baseline: the bug is not fixed by a no-op run)."""
    task_dir = ROOT / "eval" / "tasks" / "fix-config-loader-empty-file"
    out_dir = tmp_path / "results"
    code = runner.main(["run", str(task_dir), "--driver", "fake", "--out", str(out_dir)])
    assert code == 0
    files = list(out_dir.glob("*.json"))
    assert len(files) == 1
    result = json.loads(files[0].read_text(encoding="utf-8"))
    eschema.validate_result(result)
    assert result["task_id"] == "fix-config-loader-empty-file"
    assert result["driver"] == "fake"
    assert result["status"] == "skipped"
    assert result["metrics"]["test_passed"] is False  # baseline: test fails
    assert result["metrics"]["verification_status"] == "failed"
    assert result["workspace"]["baseline"]["test_passed"] is False
    assert "final_output" in result  # potentially sensitive, always present


def test_report_aggregates(tmp_path):
    out_dir = tmp_path / "results"
    for task in ("fix-config-loader-empty-file", "add-multiply-function", "write-project-readme"):
        code = runner.main(
            ["run", str(ROOT / "eval" / "tasks" / task), "--driver", "fake", "--out", str(out_dir)]
        )
        assert code == 0
    code = runner.main(["report", "--dir", str(out_dir)])
    assert code == 0
    assert len(list(out_dir.glob("*.json"))) == 3


def test_task_loading_validation(tmp_path):
    """A task manifest without required fields must be rejected."""
    import pytest

    bad = tmp_path / "bad-task"
    (bad / "repo").mkdir(parents=True)
    (bad / "repo" / "x.py").write_text("x = 1")
    (bad / "task.json").write_text(json.dumps({"id": "no-prompt"}), encoding="utf-8")
    with pytest.raises(ValueError):
        runner.load_task(str(bad))
