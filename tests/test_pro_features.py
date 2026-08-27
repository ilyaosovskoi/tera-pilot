"""Tests for the two Pro features added on top of the free core:

  1. ``office_export_pro`` — batch/templated export of a structured result
     into .docx/.xlsx/.pptx (new module ``tera_pilot.office_export_pro``).
  2. ``smart_project_memory`` — offline, deterministic memory indexing,
     dedup, and full-text search (new module
     ``tera_pilot.smart_project_memory``).

Both follow the product rule "don't cut the core": they are fail-closed
side-channel helpers. An unlicensed caller gets a structured
``{ok: False, error: "pro_required"}`` (and a ``LicenseRequiredError``
from the strict API) WITHOUT touching the free coding core and without
crashing.

These tests license the NEW feature ids by minting a signed key whose
``features`` list includes ``office_export_pro`` and
``smart_project_memory``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.licensing import (  # noqa: E402
    LicenseRequiredError,
    activate_license,
    generate_keypair,
    sign_payload,
)
from tera_pilot.office_export_pro import (  # noqa: E402
    FEATURE_ID as OFFICE_FID,
    OfficeExportPro,
    ReportSection,
)
from tera_pilot.smart_project_memory import (  # noqa: E402
    FEATURE_ID as MEMORY_FID,
    SmartProjectMemory,
)


BASE_FEATURES = ["second_opinion", "cost_router", "spend_dashboard"]
ALL_FEATURES = BASE_FEATURES + [OFFICE_FID, MEMORY_FID]


@pytest.fixture()
def unlicensed(tmp_path, monkeypatch):
    """Isolated HOME + pubkey wiring, but NO license activated."""
    priv, pub = generate_keypair()
    pub_file = tmp_path / "pub.pem"
    pub_file.write_bytes(pub)
    monkeypatch.setenv("TERA_PILOT_LICENSE_PUBKEY", str(pub_file))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TERA_PILOT_PRO", raising=False)
    return {"priv": priv, "pub_file": pub_file}


def _activate(priv, features):
    from tera_pilot.licensing import deactivate_license
    deactivate_license()  # clear any persisted license from prior tests
    payload = {
        "customer_id": "usr_pro_test",
        "tier": "pro",
        "issued_at": "2026-08-27T00:00:00Z",
        "expires_at": None,
        "features": features,
    }
    key = sign_payload(payload, priv)
    return activate_license(key)


# ═══════════════════════════════════════════════════════════════════
# office_export_pro — fail-closed
# ═══════════════════════════════════════════════════════════════════

def test_office_export_unlicensed_fails_closed(tmp_path, unlicensed):
    exporter = OfficeExportPro()
    res = exporter.export_bundle(
        title="Report", sections=[ReportSection(title="S", body="hi")],
        out_dir=str(tmp_path / "out"),
    )
    assert res == {"ok": False, "error": "pro_required"}
    # No files were written.
    assert not (tmp_path / "out").exists()


def test_office_export_strict_api_raises_when_unlicensed(unlicensed):
    exporter = OfficeExportPro()
    with pytest.raises(LicenseRequiredError):
        exporter.require()


def test_office_export_unlicensed_does_not_disturb_core(tmp_path, unlicensed):
    """The free core still works while the Pro feature is locked (the
    explicit 'don't cut the core' guarantee)."""
    from tera_pilot.licensing import is_feature_licensed
    exporter = OfficeExportPro()
    assert exporter.export_bundle(
        title="R", sections=[], out_dir=str(tmp_path / "out"),
    ) == {"ok": False, "error": "pro_required"}
    # Free-tier features remain available and unlicensed.
    assert is_feature_licensed("second_opinion") is False
    assert is_feature_licensed("cost_router") is False


# ═══════════════════════════════════════════════════════════════════
# office_export_pro — licensed
# ═══════════════════════════════════════════════════════════════════

def test_office_export_licensed_writes_all_formats(tmp_path, unlicensed):
    _activate(unlicensed["priv"], ALL_FEATURES)
    out = tmp_path / "out"
    sections = [
        ReportSection(title="Overview", body="A short summary.",
                      headers=["Item", "Value"],
                      rows=[["Users", 10], ["Sessions", 42]]),
        ReportSection(title="Notes", body="Everything looked fine."),
    ]
    res = OfficeExportPro().export_bundle(
        title="Build Report", sections=sections, out_dir=str(out),
        brand={"header": "Tera Pilot", "footer": "— generated offline"},
    )
    assert res["ok"] is True, res
    files = res["files"]
    assert len(files) == 3
    names = {Path(f).suffix for f in files}
    assert names >= {".docx", ".xlsx", ".pptx"}
    for f in files:
        assert Path(f).exists() and Path(f).stat().st_size > 0


def test_office_export_select_formats(tmp_path, unlicensed):
    _activate(unlicensed["priv"], ALL_FEATURES)
    out = tmp_path / "out"
    res = OfficeExportPro().export_bundle(
        title="Only XLSX", sections=[ReportSection(title="S", body="x")],
        out_dir=str(out), formats={"xlsx": {}},
    )
    assert res["ok"] is True
    assert [Path(f).suffix for f in res["files"]] == [".xlsx"]


def test_office_export_writes_into_nested_out_dir(tmp_path, unlicensed):
    """The exporter creates the target directory if needed and ignores
    unknown format ids (no crash, no stray partial files)."""
    _activate(unlicensed["priv"], ALL_FEATURES)
    out = tmp_path / "deep" / "nested" / "out"
    res = OfficeExportPro().export_bundle(
        title="R", sections=[ReportSection(title="S", body="x")],
        out_dir=str(out), formats={"docx": {}, "pdf": {}},
    )
    assert res["ok"] is True, res
    assert [Path(f).suffix for f in res["files"]] == [".docx"]
    assert (out / "r.docx").exists()


# ═══════════════════════════════════════════════════════════════════
# smart_project_memory — fail-closed
# ═══════════════════════════════════════════════════════════════════

def test_memory_unlicensed_fails_closed(tmp_path, unlicensed):
    mem = SmartProjectMemory(workspace=str(tmp_path / "ws"))
    res = mem.index()
    assert res == {"ok": False, "error": "pro_required"}
    res2 = mem.search("anything")
    assert res2 == {"ok": False, "error": "pro_required"}
    with pytest.raises(LicenseRequiredError):
        mem.require()


# ═══════════════════════════════════════════════════════════════════
# smart_project_memory — licensed
# ═══════════════════════════════════════════════════════════════════

def test_memory_index_dedups_and_searches(tmp_path, unlicensed):
    _activate(unlicensed["priv"], ALL_FEATURES)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    # Duplicated lesson appears twice → collapsed by dedup.
    (ws / "TERA_PILOT.md").write_text(
        "# Conventions\n"
        "Always use ruff for formatting.\n"
        "Keep functions under 40 lines.\n"
        "Always use ruff for formatting.\n",  # dup
        encoding="utf-8",
    )
    mem = SmartProjectMemory(workspace=str(ws))
    idx = mem.index()
    assert idx["ok"] is True, idx
    assert idx["report"]["indexed"] == 2
    assert idx["report"]["duplicates_collapsed"] == 1

    res = mem.search("ruff")
    assert res["ok"] is True
    assert len(res["results"]) >= 1
    assert "ruff" in res["results"][0]["text"].lower()
    assert res["results"][0]["heading"] == "Conventions"


def test_memory_index_notes_dir(tmp_path, unlicensed):
    _activate(unlicensed["priv"], ALL_FEATURES)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    notes = ws / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# API\nWebhooks arrive via POST.", encoding="utf-8")
    mem = SmartProjectMemory(workspace=str(ws))
    idx = mem.index(notes_dir=str(notes))
    assert idx["ok"] and idx["report"]["indexed"] == 1
    res = mem.search("webhook")
    assert res["ok"] and res["results"]
    assert res["results"][0]["source"].endswith("a.md")


def test_memory_no_hits_returns_empty_ok(tmp_path, unlicensed):
    _activate(unlicensed["priv"], ALL_FEATURES)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "TERA_PILOT.md").write_text("# Rules\nUse tabs.\n", encoding="utf-8")
    mem = SmartProjectMemory(workspace=str(ws))
    mem.index()
    res = mem.search("zzztotallymissing")
    assert res["ok"] is True
    assert res["results"] == []


def test_memory_empty_query_returns_error(tmp_path, unlicensed):
    _activate(unlicensed["priv"], ALL_FEATURES)
    mem = SmartProjectMemory(workspace=str(tmp_path / "ws"))
    res = mem.search("   ")
    assert res["ok"] is False
    assert "query" in res["error"]


# ═══════════════════════════════════════════════════════════════════
# Licensing the NEW feature ids must actually work via the real gate
# ═══════════════════════════════════════════════════════════════════

def test_new_feature_ids_recognized_when_licensed(unlicensed):
    from tera_pilot.licensing import is_feature_licensed
    _activate(unlicensed["priv"], ALL_FEATURES)
    assert is_feature_licensed(OFFICE_FID) is True
    assert is_feature_licensed(MEMORY_FID) is True


def test_feature_still_gated_if_only_old_features_licensed(unlicensed):
    from tera_pilot.licensing import is_feature_licensed
    _activate(unlicensed["priv"], BASE_FEATURES)  # does NOT include the new ones
    assert is_feature_licensed(OFFICE_FID) is False
    assert is_feature_licensed(MEMORY_FID) is False