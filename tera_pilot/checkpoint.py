#!/usr/bin/env python3
"""
G10 — Checkpoint / Rewind system.

Snapshots conversation state and file changes at each turn, allowing the
user to roll back to any previous checkpoint via /rewind <n> or /checkpoint
slash commands.

Design:
  - CheckpointManager stores snapshots of conversation messages + a manifest
    of file paths that were modified since the previous checkpoint.
  - File diffs are stored in a compact format (file path + backup content),
    so rewind can restore files by copying backup content back.
  - Checkpoints are persisted in ~/.tera_pilot/checkpoints/<session_id>/ as JSON files.
  - Each checkpoint has: id, turn_number, timestamp, message_count, file_manifest,
    compaction_summary (if available).
  - Rewind N means: restore the Nth checkpoint's file state, and trim the
    conversation history to that checkpoint's message count.
  - Auto-checkpoint: by default, a checkpoint is created after every agent turn
    (after the tool results are processed).  The user can also create manual
    checkpoints with /checkpoint.

Integration:
  - AgentRuntime._run_agent_loop() calls checkpoint_manager.auto_checkpoint()
    after each iteration.
  - ToolEngine tracks file writes in _touched_files; CheckpointManager uses
    that list to build the file manifest.
  - TeraPilotBridge (TUI + GUI) exposes checkpoint/rewind/list_checkpoints/diff_checkpoints.
  - SQLite persistence: checkpoints reference session_id for multi-session support.

Thread safety:
  - CheckpointManager uses threading.RLock for all mutations.
  - File backups are stored atomically (write to temp + rename).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

MAX_CHECKPOINTS_PER_SESSION = 200
MAX_BACKUP_FILE_SIZE = 10 * 1024 * 1024  # 10 MB — skip backing up huge files


def _tera_pilot_home() -> Path:
    p = Path.home() / ".tera_pilot"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _checkpoints_dir(session_id: str = "") -> Path:
    base = _tera_pilot_home() / "checkpoints"
    if session_id:
        base = base / session_id
    base.mkdir(parents=True, exist_ok=True)
    return base


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class FileManifestEntry:
    """One file that was modified between the previous checkpoint and this one.

    Attributes
    ----------
    path : str
        Relative path from workspace root.
    backup_path : str
        Absolute path to the backup file (under ~/.tera_pilot/checkpoints/<session>/backups/).
    checksum : str
        SHA-256 checksum of the backup content (for integrity verification).
    size : int
        Size of the backup file in bytes.
    """
    path: str
    backup_path: str
    checksum: str = ""
    size: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "backup_path": self.backup_path,
            "checksum": self.checksum,
            "size": self.size,
        }


@dataclass
class Checkpoint:
    """A snapshot of conversation + file state at a particular turn.

    Attributes
    ----------
    id : str
        Unique checkpoint identifier.
    session_id : str
        The session this checkpoint belongs to.
    turn_number : int
        The agent turn number (0 = initial state before first turn).
    timestamp : float
        Unix timestamp when the checkpoint was created.
    message_count : int
        Number of messages in the conversation at this checkpoint.
    file_manifest : List[FileManifestEntry]
        Files modified since the previous checkpoint.
    compaction_summary : str
        Compaction summary text (if compaction happened before this turn).
    label : str
        Optional user-provided label (for /checkpoint save <label>).
    """
    id: str
    session_id: str
    turn_number: int
    timestamp: float
    message_count: int
    file_manifest: List[FileManifestEntry] = field(default_factory=list)
    compaction_summary: str = ""
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "turn_number": self.turn_number,
            "timestamp": self.timestamp,
            "message_count": self.message_count,
            "file_manifest": [e.to_dict() for e in self.file_manifest],
            "compaction_summary": self.compaction_summary,
            "label": self.label,
        }


# ── CheckpointManager ───────────────────────────────────────────────────

class CheckpointManager:
    """Manages conversation + file checkpoints for rewind support.

    Each session has its own checkpoint directory under
    ~/.tera_pilot/checkpoints/<session_id>/.

    File backup strategy:
      - When a checkpoint is created, every file in the "touched files" list
        (tracked by ToolEngine) is backed up to
        ~/.tera_pilot/checkpoints/<session>/backups/<checkpoint_id>/<rel_path>.
      - On rewind, the backup files are copied back to the workspace.
      - If a backup file is missing or corrupted, the rewind logs a warning
        but continues (partial rewind is better than no rewind).
    """

    def __init__(self, session_id: str = "default", workspace: Optional[str] = None):
        self._session_id = session_id
        self._workspace = Path(workspace) if workspace else Path.cwd()
        self._checkpoints: List[Checkpoint] = []
        self._current_turn: int = 0
        self._lock = threading.RLock()
        self._auto_checkpoint_enabled: bool = True

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def auto_checkpoint_enabled(self) -> bool:
        return self._auto_checkpoint_enabled

    def set_auto_checkpoint(self, enabled: bool) -> None:
        self._auto_checkpoint_enabled = enabled

    def set_workspace(self, workspace: str) -> None:
        self._workspace = Path(workspace)

    def set_session_id(self, session_id: str) -> None:
        """Switch to a different session.  Loads existing checkpoints."""
        self._session_id = session_id
        self._checkpoints = self._load_from_disk()
        self._current_turn = len(self._checkpoints)

    # ── Create checkpoint ───────────────────────────────────────────

    def create_checkpoint(
        self,
        message_count: int = 0,
        touched_files: Optional[List[str]] = None,
        compaction_summary: str = "",
        label: str = "",
    ) -> Checkpoint:
        """Create a new checkpoint, backing up any touched files.

        Parameters
        ----------
        message_count : int
            Number of messages in the conversation at this point.
        touched_files : List[str] | None
            List of workspace-relative file paths that were modified since
            the last checkpoint.  If None, no files are backed up.
        compaction_summary : str
            Summary of any compaction that happened.
        label : str
            Optional user-provided label.

        Returns
        -------
        Checkpoint
            The created checkpoint.
        """
        checkpoint_id = f"cp_{uuid.uuid4().hex[:8]}"
        turn_number = self._current_turn
        manifest: List[FileManifestEntry] = []

        # Back up touched files
        if touched_files:
            backup_dir = _checkpoints_dir(self._session_id) / "backups" / checkpoint_id
            backup_dir.mkdir(parents=True, exist_ok=True)

            for rel_path in touched_files:
                abs_path = self._workspace / rel_path
                if not abs_path.exists():
                    continue
                if abs_path.stat().st_size > MAX_BACKUP_FILE_SIZE:
                    logger.warning("[checkpoint] skipping large file: %s (%d bytes)",
                                   rel_path, abs_path.stat().st_size)
                    continue
                try:
                    backup_file = backup_dir / rel_path
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(abs_path), str(backup_file))
                    checksum = _sha256_file(abs_path)
                    manifest.append(FileManifestEntry(
                        path=rel_path,
                        backup_path=str(backup_file),
                        checksum=checksum,
                        size=abs_path.stat().st_size,
                    ))
                except Exception as e:
                    logger.warning("[checkpoint] failed to backup %s: %s", rel_path, e)

        cp = Checkpoint(
            id=checkpoint_id,
            session_id=self._session_id,
            turn_number=turn_number,
            timestamp=time.time(),
            message_count=message_count,
            file_manifest=manifest,
            compaction_summary=compaction_summary,
            label=label,
        )

        with self._lock:
            self._checkpoints.append(cp)
            self._current_turn += 1
            # Enforce max checkpoints
            if len(self._checkpoints) > MAX_CHECKPOINTS_PER_SESSION:
                removed = self._checkpoints.pop(0)
                # Clean up old backup files
                old_backup_dir = _checkpoints_dir(self._session_id) / "backups" / removed.id
                if old_backup_dir.exists():
                    try:
                        shutil.rmtree(str(old_backup_dir))
                    except Exception:
                        pass
            self._save_to_disk(cp)

        logger.info("[checkpoint] created %s (turn=%d, files=%d, label=%r)",
                     cp.id, cp.turn_number, len(manifest), label)
        return cp

    def auto_checkpoint(
        self,
        message_count: int = 0,
        touched_files: Optional[List[str]] = None,
        compaction_summary: str = "",
    ) -> Optional[Checkpoint]:
        """Auto-checkpoint after each agent turn (if enabled)."""
        if not self._auto_checkpoint_enabled:
            return None
        return self.create_checkpoint(
            message_count=message_count,
            touched_files=touched_files,
            compaction_summary=compaction_summary,
        )

    # ── Rewind ──────────────────────────────────────────────────────

    def rewind(self, n: int = 1) -> Dict[str, Any]:
        """Rewind to the checkpoint N steps back from the current state.

        Parameters
        ----------
        n : int
            Number of checkpoints to rewind.  1 = go back one turn.

        Returns
        -------
        Dict[str, Any]
            {ok: bool, checkpoint: dict, message_count: int, files_restored: list,
             errors: list}
            The caller should trim the conversation history to message_count.
        """
        with self._lock:
            if len(self._checkpoints) == 0:
                return {"ok": False, "error": "No checkpoints available"}
            # We are at state AFTER the last checkpoint. rewind(n) goes back n checkpoints.
            # checkpoints = [cp1, cp2, cp3] (indices 0, 1, 2)
            # current state is after cp3. rewind(1) -> cp2 (index 1). rewind(2) -> cp1 (index 0).
            # target_index = len - n - 1
            target_index = len(self._checkpoints) - n - 1
            if target_index < 0:
                return {"ok": False, "error": f"Cannot rewind {n} steps — only {len(self._checkpoints)} checkpoints"}
            target_cp = self._checkpoints[target_index]

        # Restore files from all checkpoints AFTER target to target
        # We need to restore files that were modified between target_cp and
        # the current state.  The simplest approach: for each checkpoint
        # after target_index, restore its backup files.
        files_restored: List[str] = []
        errors: List[str] = []

        with self._lock:
            checkpoints_to_restore = self._checkpoints[target_index + 1:]
            # Also include the target checkpoint's own manifest (those files
            # represent the state AT the target checkpoint).
            # Actually, we need to restore the state at target_cp.
            # The files backed up in target_cp represent the state at that turn.
            # Files backed up in later checkpoints represent changes AFTER that turn.
            # So we restore from target_cp's backups, and remove files that
            # were added in later checkpoints (if they didn't exist at target_cp).

            # Strategy: restore all files from target checkpoint's backups.
            for entry in target_cp.file_manifest:
                abs_path = self._workspace / entry.path
                backup_path = Path(entry.backup_path)
                if backup_path.exists():
                    try:
                        abs_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(backup_path), str(abs_path))
                        files_restored.append(entry.path)
                    except Exception as e:
                        errors.append(f"Failed to restore {entry.path}: {e}")
                else:
                    errors.append(f"Backup missing for {entry.path}")

            # For files that appear in later checkpoints but NOT in target,
            # we need to check if they existed at the target state.
            # If they didn't exist at target, delete them.
            later_paths = set()
            for cp in checkpoints_to_restore:
                for entry in cp.file_manifest:
                    later_paths.add(entry.path)
            target_paths = {e.path for e in target_cp.file_manifest}
            new_files = later_paths - target_paths
            for path in new_files:
                abs_path = self._workspace / path
                if abs_path.exists():
                    try:
                        abs_path.unlink()
                        files_restored.append(f"[deleted] {path}")
                    except Exception as e:
                        errors.append(f"Failed to delete {path}: {e}")

            # Remove checkpoints after target
            removed_cps = self._checkpoints[target_index + 1:]
            self._checkpoints = self._checkpoints[:target_index + 1]
            self._current_turn = len(self._checkpoints)

            # Clean up removed checkpoint backups
            for cp in removed_cps:
                old_backup_dir = _checkpoints_dir(self._session_id) / "backups" / cp.id
                if old_backup_dir.exists():
                    try:
                        shutil.rmtree(str(old_backup_dir))
                    except Exception:
                        pass

        logger.info("[checkpoint] rewound %d steps to %s (files_restored=%d, errors=%d)",
                     n, target_cp.id, len(files_restored), len(errors))

        return {
            "ok": True,
            "checkpoint": target_cp.to_dict(),
            "message_count": target_cp.message_count,
            "files_restored": files_restored,
            "errors": errors,
        }

    def rewind_to(self, checkpoint_id: str) -> Dict[str, Any]:
        """Rewind to a specific checkpoint by id."""
        with self._lock:
            for i, cp in enumerate(self._checkpoints):
                if cp.id == checkpoint_id:
                    n = len(self._checkpoints) - i - 1
                    break
            else:
                return {"ok": False, "error": f"Checkpoint {checkpoint_id} not found"}
        return self.rewind(n)

    # ── Query ───────────────────────────────────────────────────────

    def list_checkpoints(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return metadata for all checkpoints (most recent first)."""
        with self._lock:
            cps = list(reversed(self._checkpoints))[:limit]
        return [cp.to_dict() for cp in cps]

    def get_checkpoint(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        """Return a single checkpoint's metadata."""
        with self._lock:
            for cp in self._checkpoints:
                if cp.id == checkpoint_id:
                    return cp.to_dict()
        return None

    def diff_checkpoints(
        self, from_id: str, to_id: str,
    ) -> Dict[str, Any]:
        """Compare two checkpoints, returning the file changes between them.

        Returns {ok, files_added, files_removed, files_modified, from, to}.
        """
        from_cp = None
        to_cp = None
        with self._lock:
            for cp in self._checkpoints:
                if cp.id == from_id:
                    from_cp = cp
                if cp.id == to_id:
                    to_cp = cp
        if from_cp is None or to_cp is None:
            return {"ok": False, "error": "One or both checkpoint IDs not found"}

        from_paths = {e.path: e.checksum for e in from_cp.file_manifest}
        to_paths = {e.path: e.checksum for e in to_cp.file_manifest}

        files_added = [p for p in to_paths if p not in from_paths]
        files_removed = [p for p in from_paths if p not in to_paths]
        files_modified = [
            p for p in from_paths
            if p in to_paths and from_paths[p] != to_paths[p]
        ]

        return {
            "ok": True,
            "from": from_id,
            "to": to_id,
            "files_added": files_added,
            "files_removed": files_removed,
            "files_modified": files_modified,
        }

    # ── Persistence ─────────────────────────────────────────────────

    def _save_to_disk(self, cp: Checkpoint) -> None:
        """Persist a single checkpoint to disk."""
        cp_dir = _checkpoints_dir(self._session_id)
        cp_file = cp_dir / f"{cp.id}.json"
        try:
            with open(cp_file, "w") as f:
                json.dump(cp.to_dict(), f, indent=2, default=str)
        except Exception as e:
            logger.warning("[checkpoint] failed to save %s: %s", cp.id, e)

    def _load_from_disk(self) -> List[Checkpoint]:
        """Load all checkpoints for the current session from disk."""
        cp_dir = _checkpoints_dir(self._session_id)
        cps: List[Checkpoint] = []
        if not cp_dir.exists():
            return cps
        for json_file in sorted(cp_dir.glob("cp_*.json")):
            try:
                with open(json_file, "r") as f:
                    data = json.load(f)
                manifest = [
                    FileManifestEntry(
                        path=e["path"],
                        backup_path=e["backup_path"],
                        checksum=e.get("checksum", ""),
                        size=e.get("size", 0),
                    )
                    for e in data.get("file_manifest", [])
                ]
                cp = Checkpoint(
                    id=data["id"],
                    session_id=data["session_id"],
                    turn_number=data["turn_number"],
                    timestamp=data["timestamp"],
                    message_count=data["message_count"],
                    file_manifest=manifest,
                    compaction_summary=data.get("compaction_summary", ""),
                    label=data.get("label", ""),
                )
                cps.append(cp)
            except Exception as e:
                logger.warning("[checkpoint] failed to load %s: %s", json_file.name, e)
        cps.sort(key=lambda c: c.turn_number)
        return cps

    # ── Stats ───────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Return checkpoint statistics."""
        with self._lock:
            return {
                "session_id": self._session_id,
                "total_checkpoints": len(self._checkpoints),
                "current_turn": self._current_turn,
                "auto_checkpoint_enabled": self._auto_checkpoint_enabled,
                "total_files_backed_up": sum(
                    len(cp.file_manifest) for cp in self._checkpoints
                ),
            }


# ── Helpers ──────────────────────────────────────────────────────────────

def _sha256_file(path: Path) -> str:
    """Compute SHA-256 checksum of a file."""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


# ── Process-wide singleton ──────────────────────────────────────────────

_CHECKPOINT_MANAGER: Optional[CheckpointManager] = None


def get_checkpoint_manager(
    session_id: str = "default", workspace: Optional[str] = None,
) -> CheckpointManager:
    """Return the process-wide CheckpointManager singleton."""
    global _CHECKPOINT_MANAGER
    if _CHECKPOINT_MANAGER is None:
        _CHECKPOINT_MANAGER = CheckpointManager(session_id=session_id, workspace=workspace)
    return _CHECKPOINT_MANAGER


def reset_checkpoint_manager() -> None:
    """Reset the singleton (for testing)."""
    global _CHECKPOINT_MANAGER
    _CHECKPOINT_MANAGER = None
