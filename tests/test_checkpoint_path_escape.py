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
