import json
from pathlib import Path

import pytest

from tera_pilot import audit_cli


def _sample_entries():
    return [
        {
            "ts": 1.0,
            "ts_iso": "2026-01-01T00:00:00Z",
            "category": "shell",
            "kind": "command",
            "tool": "execute_command",
            "title": "Run: ls",
            "summary": "ok",
            "status": "ok",
            "args": {},
            "result_preview": "ok",
            "duration_ms": 5,
            "section": None,
            "chat_id": None,
            "iteration": 1,
            "path": None,
            "command": "ls",
            "diff_stat": None,
            "meta": {},
        }
    ]


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Изолируем ~/.tera_pilot от реального конфига пользователя."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_audit_signed_export_and_verify_roundtrip(isolated_home, tmp_path):
    out = tmp_path / "audit.json"
    code, count = audit_cli.export_entries(_sample_entries(), str(out), unsigned=False)
    assert code == 0
    assert count == 1
    assert out.exists()

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data[0]["_signature"]
    assert data[0]["_hash"]
    assert data[0]["_prev_hash"] == ""  # genesis entry

    # Публичный ключ сохраняется рядом с приватным для проверки на другой машине.
    assert (isolated_home / ".tera_pilot" / "audit_key.pub").exists()

    assert audit_cli._cmd_verify(str(out)) == 0


def test_audit_tampering_is_detected(isolated_home, tmp_path):
    out = tmp_path / "audit.json"
    audit_cli.export_entries(_sample_entries(), str(out), unsigned=False)

    data = json.loads(out.read_text(encoding="utf-8"))
    data[0]["command"] = "rm -rf /"  # подмена записи
    out.write_text(json.dumps(data), encoding="utf-8")

    assert audit_cli._cmd_verify(str(out)) == 1


def test_audit_reordering_is_detected(isolated_home, tmp_path):
    entries = _sample_entries() * 2
    entries[1]["ts_iso"] = "2026-01-02T00:00:00Z"
    out = tmp_path / "audit.json"
    audit_cli.export_entries(entries, str(out), unsigned=False)

    data = json.loads(out.read_text(encoding="utf-8"))
    data.reverse()  # переупорядочивание ломает цепочку хешей
    out.write_text(json.dumps(data), encoding="utf-8")

    assert audit_cli._cmd_verify(str(out)) == 1


def test_audit_cli_dispatch():
    assert audit_cli.run_audit_cli([]) == 2
    assert audit_cli.run_audit_cli(["bogus"]) == 2
    assert audit_cli.run_audit_cli(["verify"]) == 2
