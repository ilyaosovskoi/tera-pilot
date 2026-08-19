"""Regression tests for the checkpoint path-escape protection (v2.3.1).

``CheckpointManager.rewind()`` restores and DELETES files listed in a
checkpoint's file manifest. Because manifests are also loaded from disk
(a hand-edited JSON could contain absolute paths or ``..`` segments),
every path must be re-validated against the workspace at use time —
``Path / <absolute>`` silently replaces the base, so an unvalidated
entry could overwrite or delete files outside the workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tera_pilot.checkpoint as cp


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cp, "_tera_pilot_home", lambda: home)
    return workspace, home


def test_create_checkpoint_skips_escaping_paths(isolated):
    workspace, _ = isolated
    mgr = cp.CheckpointManager(session_id="s1", workspace=str(workspace))

    (workspace / "good.txt").write_text("good")
    outside = workspace.parent / "outside.txt"
    outside.write_text("SECRET")

    mgr.create_checkpoint(
        message_count=1,
        touched_files=["good.txt", str(outside), "../evil.txt"],
    )
    cps = mgr.list_checkpoints()
    manifest_paths = {e["path"] for e in cps[0]["file_manifest"]}
    assert manifest_paths == {"good.txt"}


def test_rewind_refuses_escaping_manifest_path(isolated):
    workspace, _ = isolated
    mgr = cp.CheckpointManager(session_id="s1", workspace=str(workspace))

    # Hand-craft a later checkpoint whose manifest contains an escaping path.
    evil = cp.FileManifestEntry(
        path="../pwned.txt",
        backup_path=str(workspace.parent / "fake_backup"),
        checksum="",
        size=0,
    )
    mgr._checkpoints = [
        cp.Checkpoint(
            id="c1", session_id="s1", turn_number=0, timestamp=0,
            message_count=0, file_manifest=[],
        ),
        cp.Checkpoint(
            id="c2", session_id="s1", turn_number=1, timestamp=0,
            message_count=1, file_manifest=[evil],
        ),
    ]
    pwned = workspace.parent / "pwned.txt"
    pwned.write_text("KEEP")

    result = mgr.rewind(1)

    assert pwned.exists(), "rewind deleted a file outside the workspace!"
    assert result["ok"]
    assert any("outside workspace" in e for e in result["errors"])


def test_rewind_restores_file_modified_before_target_but_not_at_target(isolated):
    """Regression: rewind must restore the LATEST backup at-or-before the
    target checkpoint, not just the target's own manifest.

    Scenario: a.txt is written at cp1 ("v1"), untouched at cp2, then
    modified at cp3 ("v2"). Rewinding one step (to cp2) must restore
    a.txt to "v1" — the old code restored nothing from cp2 (empty
    manifest) and then DELETED a.txt because it appeared in a later
    checkpoint but not in the target's manifest.
    """
    workspace, home = isolated
    mgr = cp.CheckpointManager(session_id="s1", workspace=str(workspace))

    a = workspace / "a.txt"
    a.write_text("v1")
    mgr.create_checkpoint(message_count=5, touched_files=["a.txt"])
    mgr.create_checkpoint(message_count=8, touched_files=[])  # turn 2: untouched
    a.write_text("v2")
    mgr.create_checkpoint(message_count=12, touched_files=["a.txt"])

    result = mgr.rewind(1)  # back to after cp2

    assert result["ok"]
    assert a.read_text() == "v1", f"expected a.txt restored to v1, got {a.read_text()!r}"
    assert a.exists()
    assert "a.txt" in result["files_restored"]


def test_rewind_restores_all_files_to_latest_pre_target_backup(isolated):
    """Rewinding further back restores every touched file, including ones
    whose only backup predates the target (they must be restored, not
    deleted as "new").
    """
    workspace, home = isolated
    mgr = cp.CheckpointManager(session_id="s1", workspace=str(workspace))

    a = workspace / "a.txt"
    a.write_text("v1")
    mgr.create_checkpoint(message_count=5, touched_files=["a.txt"])  # cp1
    a.write_text("v2")
    mgr.create_checkpoint(message_count=8, touched_files=["a.txt"])  # cp2
    a.write_text("v3")
    mgr.create_checkpoint(message_count=12, touched_files=["a.txt"])  # cp3

    result = mgr.rewind(2)  # back to after cp1 → a.txt = "v1"

    assert result["ok"]
    assert a.read_text() == "v1"


def test_rewind_refuses_backup_outside_checkpoints_dir(isolated):
    workspace, _ = isolated
    mgr = cp.CheckpointManager(session_id="s1", workspace=str(workspace))

    (workspace / "f.txt").write_text("new")
    # A manifest entry whose backup lives outside ~/.tera_pilot/checkpoints
    # must not be copied from (local file disclosure via backup path).
    rogue = cp.FileManifestEntry(
        path="f.txt",
        backup_path=str(workspace.parent / "secret.txt"),
        checksum="",
        size=0,
    )
    secret = workspace.parent / "secret.txt"
    secret.write_text("ROGUE CONTENT")
    mgr._checkpoints = [
        cp.Checkpoint(
            id="c1", session_id="s1", turn_number=0, timestamp=0,
            message_count=0, file_manifest=[rogue],
        ),
    ]

    result = mgr.rewind(0)

    assert (workspace / "f.txt").read_text() == "new", (
        "file was overwritten from an out-of-tree backup path"
    )
    assert any("outside checkpoints dir" in e for e in result["errors"])
