"""Quality gate for the evaluation task set (P0.1).

Verifies for every task in eval/tasks/:

- the manifest is structurally valid (required fields, category, gold/);
- the declared baseline_status matches the pristine repo's test_command
  result ("tests fail before the agent does anything");
- the reference solution in gold/ actually makes test_command pass
  ("the task is solvable and verified through tests").

No network, no LLM — pure subprocess runs, deterministic.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import runner  # noqa: E402

TASKS_DIR = ROOT / "eval" / "tasks"
TASK_IDS = sorted(p.name for p in TASKS_DIR.iterdir() if (p / "task.json").is_file())


def _load(task_id):
    return runner.load_task(str(TASKS_DIR / task_id))


def _run_command(task, workspace, timeout=60):
    """Run the task's test_command inside ``workspace``; never raises."""
    return runner.run_test_command(workspace, task.get("test_command"), timeout)


@pytest.fixture()
def task_list():
    return [_load(tid) for tid in TASK_IDS]


def test_at_least_30_tasks_across_all_categories(task_list):
    """P0.1 target: a broad, category-balanced task set."""
    assert len(TASK_IDS) >= 30, f"only {len(TASK_IDS)} tasks — P0.1 target is 30+"
    categories = {t["category"] for t in task_list}
    assert categories == set(runner.VALID_CATEGORIES), (
        f"missing categories: {set(runner.VALID_CATEGORIES) - categories}"
    )
    for cat in runner.VALID_CATEGORIES:
        count = sum(1 for t in task_list if t["category"] == cat)
        assert count >= 3, f"category {cat} has only {count} tasks"


def test_all_task_ids_unique():
    assert len(TASK_IDS) == len(set(TASK_IDS))


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_task_manifest_structure(task_id):
    problems = runner._check_one_task(_load(task_id), TASKS_DIR / task_id)
    assert problems == [], f"{task_id}: {problems}"
    manifest = _load(task_id)
    assert manifest["id"] == task_id
    assert manifest.get("schema_version") == "1.0"


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_baseline_status_matches_pristine_repo(task_id):
    """The manifest's baseline_status claim must hold on the pristine copy."""
    task = _load(task_id)
    expected = task.get("baseline_status")
    if expected is None:
        pytest.skip("task does not declare baseline_status")
    workspace, _, _ = runner.make_clean_workspace(task)
    try:
        baseline = _run_command(task, workspace)
    finally:
        runner.cleanup_workspace(workspace)
    if expected == "unknown":
        pytest.skip("baseline_status is unknown by design")
    actual = "passing" if baseline["test_passed"] is True else "failing"
    assert actual == expected, (
        f"{task_id}: manifest says baseline is {expected!r}, "
        f"pristine repo tests are {actual!r} (exit {baseline['test_exit_code']})"
    )


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_gold_solution_passes_test_command(task_id):
    """The reference solution in gold/ must make the task's tests pass —
    this proves every task is solvable and verifiable.

    Security tasks (P0.5) have no test_command on purpose: their
    criterion is NOT "tests pass" but "the malicious action is blocked /
    confirmed / the run fails closed" (task.json security_expectation),
    verified by inspecting final_output + tools_used after a real run.
    """
    task = _load(task_id)
    if not task.get("test_command"):
        pytest.skip(
            "no test_command (security task — evaluated by blocked/refused outcome, not tests)"
        )
    gold_dir = TASKS_DIR / task_id / "gold"
    workspace, _, _ = runner.make_clean_workspace(task)
    try:
        # Overlay the gold solution onto the pristine copy.
        for src in sorted(gold_dir.iterdir()):
            dst = workspace / src.name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        result = _run_command(task, workspace)
        assert result["test_passed"] is True, (
            f"{task_id}: gold solution did not pass test_command "
            f"(exit {result['test_exit_code']}):\n{result['test_output'][-1200:]}"
        )
    finally:
        runner.cleanup_workspace(workspace)


def test_git_task_records_base_commit():
    """A git fixture must produce a workspace.commit in the result."""
    task_id = "doc-changelog-from-git"
    out_dir = ROOT / "eval" / "results"
    code = runner.main(["run", str(TASKS_DIR / task_id), "--driver", "fake", "--out", str(out_dir)])
    assert code == 0
    result_file = sorted(out_dir.glob(f"{task_id}_*.json"))[-1]
    result = json.loads(result_file.read_text(encoding="utf-8"))
    runner.schema.validate_result(result)
    try:
        commit = result["workspace"]["commit"]
        assert commit and len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
        baseline = result["workspace"]["baseline"]
        assert baseline["test_passed"] is False  # CHANGELOG.md missing at baseline
        assert result["metrics"]["verification_status"] == "failed"
    finally:
        result_file.unlink()


def test_fake_driver_records_baseline():
    """The fake driver records workspace.baseline by default."""
    task_id = "fix-missing-return"
    out_dir = ROOT / "eval" / "results"
    code = runner.main(["run", str(TASKS_DIR / task_id), "--driver", "fake", "--out", str(out_dir)])
    assert code == 0
    result_file = sorted(out_dir.glob(f"{task_id}_*.json"))[-1]
    result = json.loads(result_file.read_text(encoding="utf-8"))
    try:
        assert result["driver"] == "fake"
        assert result["status"] == "skipped"
        assert result["workspace"]["baseline"]["test_passed"] is False
        assert result["metrics"]["verification_status"] == "failed"
        assert result["metrics"]["tokens"] == 0
        assert result["metrics"]["cost_usd"] == 0.0
    finally:
        result_file.unlink()


def test_smoke_set_is_valid():
    """eval/smoke.json must reference existing tasks only."""
    smoke = json.loads((ROOT / "eval" / "smoke.json").read_text(encoding="utf-8"))
    assert isinstance(smoke, list) and len(smoke) >= 5
    missing = [tid for tid in smoke if tid not in TASK_IDS]
    assert not missing, f"smoke.json references unknown tasks: {missing}"
    categories = {_load(tid)["category"] for tid in smoke}
    assert categories == set(runner.VALID_CATEGORIES), "smoke set must cover all categories"


def test_check_command_reports_ok():
    """The fast structural check command must pass on the whole set."""
    code = runner.main(["check", "--dir", str(TASKS_DIR)])
    assert code == 0
