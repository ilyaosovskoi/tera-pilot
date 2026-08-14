import json

import pytest

from tera_pilot import environment_doctor as doctor


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Изолируем ~/.tera_pilot от реального конфига пользователя."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_doctor_checks_are_wellformed(isolated_home):
    checks = doctor.run_checks()
    assert len(checks) >= 8
    names = {c.name for c in checks}
    assert {"python", "package", "config_dir", "workspace"} <= names
    for c in checks:
        assert c.status in ("ok", "warn", "fail")
        assert c.name
        assert c.detail


def test_doctor_json_report_shape(isolated_home):
    checks = doctor.run_checks()
    report = doctor.build_json_report(checks)
    assert report["schema_version"] == 1
    assert report["product"] == "tera-pilot"
    assert report["ready"] == (report["counts"]["fail"] == 0)
    assert len(report["checks"]) == len(checks)
    json.dumps(report)  # must be serializable


def test_doctor_exit_code_reflects_failures():
    # Несуществующая рабочая директория → fail → код выхода 1.
    code = doctor.run_doctor(json_output=True, project="/nonexistent/tera-pilot-doctor-test")
    assert code == 1


def test_doctor_cli_parses_flags():
    assert doctor.run_doctor_cli(["--json", "--project", "/nonexistent/tera-pilot-doctor-test"]) == 1
    # Без флагов тоже работает (stdout перехватывается pytest).
    assert doctor.run_doctor_cli([]) in (0, 1)
