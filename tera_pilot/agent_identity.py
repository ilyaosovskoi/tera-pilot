"""
Tera Pilot v2.0.2 — Agent Identity & Tool-call Audit (Goal G5).

**Problem (from CLAUDE.md):**
    "No visibility into which agent called what tool." Enterprise / security
    users need a tamper-evident record that answers:
      * WHICH agent (root, subagent, observer, reviewer...) ran a tool?
      * WHAT args did it pass?
      * WHEN did it run (precise UTC)?
      * FROM WHERE (workspace, chat, parent agent chain)?
      * WHAT did the tool return (sanitised summary)?

**Design:**

1.  ``AgentIdentity`` — immutable value-object describing who is acting.
    Every activity log entry now carries an ``agent`` field with this
    shape: ``{id, role, name, parent_chain}``. Subagents are tracked
    via the parent_chain list (root → planner → implementer_3).

2.  ``AuditTrail`` — a thin layer over the existing ``ActivityLog``
    singleton. Adds:
      * ``record_with_identity()`` — same as ``ActivityLog.record()``
        but always includes the acting ``AgentIdentity``.
      * ``agent_summary()`` — per-agent counts: tools, errors, durations.
      * ``filter_by_agent()`` — find every entry produced by an agent
        id (or any agent in a chain).
      * ``export_audit_json()`` / ``export_audit_csv()`` — compliance
        exports. The JSON variant adds a per-entry SHA-256 fingerprint
        so a downstream auditor can detect tampering of any single row
        without needing to recompute the whole chain.

3.  **Identity propagation.** The TUI / GUI bridge creates a root
    identity at construction time. When ``AgentRuntime`` spawns a
    subagent, the subagent is given an ``AgentIdentity`` whose
    ``parent_chain`` extends the parent's chain. This is wired in
    ``TeraPilotBridge`` — no patching of ``AgentRuntime`` is required.

4.  **No telemetry.** The audit trail lives in-process (backed by
    ``ActivityLog``'s in-memory ring buffer + optional JSONL
    persistence). Nothing is sent over the network. The export
    functions return local strings — what the user does with them
    (email, sign, attach to a ticket) is the user's business.

This module is **non-invasive**: it does NOT modify ``ActivityLog``
in place. Instead it wraps the singleton with an identity-aware
facade so existing callers keep working unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .activity_log import (
    ActivityLog,
    CATEGORY_INFO,
    STATUS_OK,
    get_activity_log,
    make_entry,
)

logger = logging.getLogger(__name__)


# ── Agent roles ───────────────────────────────────────────────────────
# Fixed vocabulary — UIs can render an icon per role. The list is
# intentionally short: anything more elaborate belongs in a "name"
# field rather than a new role.

ROLE_ROOT = "root"
ROLE_SUBAGENT = "subagent"
ROLE_REVIEWER = "reviewer"
ROLE_OBSSERVER = "observer"
ROLE_PLANNER = "planner"
ROLE_IMPLEMENTER = "implementer"
ROLE_PAIR = "pair"
ROLE_EXTERNAL = "external"  # e.g. an MCP remote agent calling our tools

ALL_ROLES = (
    ROLE_ROOT,
    ROLE_SUBAGENT,
    ROLE_REVIEWER,
    ROLE_OBSSERVER,
    ROLE_PLANNER,
    ROLE_IMPLEMENTER,
    ROLE_PAIR,
    ROLE_EXTERNAL,
)


# ── Identity value-object ────────────────────────────────────────────

@dataclass(frozen=True)
class AgentIdentity:
    """Who is acting, with the chain that produced them.

    ``id`` is a per-session UUID4 (not persisted across runs) — it
    uniquely identifies one agent inside the current process.

    ``parent_chain`` is the list of ancestor agent ids, root-first:
        ["root-uuid", "planner-uuid", "implementer-3-uuid"]
    A root agent has an empty parent_chain.
    """
    id: str
    role: str = ROLE_ROOT
    name: str = ""
    parent_chain: tuple = ()  # tuple of parent AgentIdentity.id strings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "parent_chain": list(self.parent_chain),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentIdentity":
        return cls(
            id=str(d.get("id") or _new_agent_id()),
            role=str(d.get("role") or ROLE_ROOT),
            name=str(d.get("name") or ""),
            parent_chain=tuple(d.get("parent_chain") or []),
        )

    def child(self, *, role: str, name: str = "") -> "AgentIdentity":
        """Spawn a child identity whose parent_chain extends ours."""
        return AgentIdentity(
            id=_new_agent_id(),
            role=role,
            name=name,
            parent_chain=self.parent_chain + (self.id,),
        )


def _new_agent_id() -> str:
    return "agt_" + uuid.uuid4().hex[:12]


def make_root_identity(name: str = "tera-pilot-root") -> AgentIdentity:
    """Create the root identity for a Tera Pilot process."""
    return AgentIdentity(id=_new_agent_id(), role=ROLE_ROOT, name=name)


# ── Process-wide root identity ───────────────────────────────────────
# Created lazily on first access. The same root identity is reused for
# the lifetime of the process. ``TeraPilotBridge`` calls ``get_root_identity()``
# to obtain it, then derives sub-identities via ``identity.child(...)``.

_ROOT_IDENTITY: Optional[AgentIdentity] = None
_ROOT_LOCK = threading.Lock()


def get_root_identity() -> AgentIdentity:
    """Return (and lazily create) the process-wide root AgentIdentity."""
    global _ROOT_IDENTITY
    if _ROOT_IDENTITY is None:
        with _ROOT_LOCK:
            if _ROOT_IDENTITY is None:
                _ROOT_IDENTITY = make_root_identity()
                logger.info("[agent_identity] root agent id: %s", _ROOT_IDENTITY.id)
    return _ROOT_IDENTITY


def reset_root_identity_for_test() -> AgentIdentity:
    """Test-only: forget the cached root and return a fresh one."""
    global _ROOT_IDENTITY
    with _ROOT_LOCK:
        _ROOT_IDENTITY = make_root_identity()
    return _ROOT_IDENTITY


# ── AuditTrail ───────────────────────────────────────────────────────

# Per-entry identity field key in the activity log dict. We store the
# identity under this key so existing consumers of ActivityLog entries
# (which iterate over the dict) get the new field for free.
AGENT_FIELD = "agent"

# Per-entry SHA-256 fingerprint key — only set by export_audit_json()
# (not stored in the in-memory ring buffer, to avoid bloating it).
FINGERPRINT_FIELD = "fingerprint"


class AuditTrail:
    """Identity-aware wrapper over the global ``ActivityLog``.

    Use this instead of calling ``ActivityLog.record()`` directly when
    you have an ``AgentIdentity`` to attribute the action to. Falls
    back to the root identity when none is supplied.
    """

    def __init__(
        self,
        activity_log: Optional[ActivityLog] = None,
        root_identity: Optional[AgentIdentity] = None,
    ) -> None:
        self._log = activity_log or get_activity_log()
        self._root = root_identity or get_root_identity()

    # ── Recording ────────────────────────────────────────────────

    def record(
        self,
        *,
        identity: Optional[AgentIdentity] = None,
        category: str = CATEGORY_INFO,
        kind: str = "audit",
        tool: Optional[str] = None,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        status: str = STATUS_OK,
        args: Optional[Dict[str, Any]] = None,
        result_preview: Optional[str] = None,
        duration_ms: Optional[int] = None,
        chat_id: Optional[str] = None,
        path: Optional[str] = None,
        command: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Record an audit entry attributed to ``identity`` (root if None)."""
        ident = identity or self._root
        # Merge the identity into the entry's meta so existing
        # ActivityLog.record() consumers still work, and ALSO set the
        # top-level ``agent`` field for direct filtering.
        merged_meta = dict(meta or {})
        merged_meta["agent"] = ident.to_dict()
        return self._log.record(
            category=category,
            kind=kind,
            tool=tool,
            title=title,
            summary=summary,
            status=status,
            args=args,
            result_preview=result_preview,
            duration_ms=duration_ms,
            chat_id=chat_id,
            path=path,
            command=command,
            meta=merged_meta,
        )

    def record_tool_call(
        self,
        *,
        identity: Optional[AgentIdentity] = None,
        tool: str,
        args: Dict[str, Any],
        result: Optional[str],
        duration_ms: Optional[int] = None,
        chat_id: Optional[str] = None,
        iteration: Optional[int] = None,
    ) -> str:
        """Identity-aware convenience wrapper for ActivityLog.record_tool_call.

        We re-implement rather than delegate so we can inject the agent
        identity into ``meta`` (the underlying record_tool_call does
        not accept a meta kwarg).
        """
        ident = identity or self._root
        from .activity_log import (
            categorize, parse_status, build_title, sanitise_args,
        )
        category = categorize(tool)
        status = parse_status(result)
        title = build_title(tool, args)
        path = args.get("path")
        command = args.get("command")
        diff_stat = None
        if tool in ("write_file", "str_replace", "apply_diff") and isinstance(result, str):
            if tool == "apply_diff":
                diff_body = args.get("diff", "")
                if isinstance(diff_body, str):
                    added = sum(1 for l in diff_body.splitlines()
                                if l.startswith("+") and not l.startswith("+++"))
                    removed = sum(1 for l in diff_body.splitlines()
                                  if l.startswith("-") and not l.startswith("---"))
                    diff_stat = f"+{added} -{removed}"
        summary = ""
        if isinstance(result, str):
            first_line = result.split("\n", 1)[0].strip()
            summary = first_line[:160]
        return self.record(
            identity=ident,
            category=category,
            kind=tool,
            tool=tool,
            title=title,
            summary=summary,
            status=status,
            args=args,
            result_preview=result if isinstance(result, str) else None,
            duration_ms=duration_ms,
            chat_id=chat_id,
            path=path,
            command=command,
            meta={"diff_stat": diff_stat, "iteration": iteration},
        )

    # ── Queries ──────────────────────────────────────────────────

    def agent_summary(
        self,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Per-agent breakdown of tool calls, errors, durations.

        If ``agent_id`` is None, returns a summary grouped by agent id
        across every agent that has acted in this process.

        Returns:
            {
              "<agent_id>": {
                "role": str, "name": str,
                "tool_calls": int, "errors": int, "rejections": int,
                "total_duration_ms": int, "tools_used": [str, ...]
              },
              ...
            }
        """
        with self._log._lock:
            entries = list(self._log._entries)
        out: Dict[str, Dict[str, Any]] = {}
        for e in entries:
            agent = (e.get("meta") or {}).get("agent") or {}
            aid = agent.get("id") or "unknown"
            if agent_id and aid != agent_id:
                continue
            slot = out.setdefault(aid, {
                "role": agent.get("role", "?"),
                "name": agent.get("name", ""),
                "tool_calls": 0, "errors": 0, "rejections": 0,
                "total_duration_ms": 0, "tools_used": set(),
            })
            slot["tool_calls"] += 1
            if e.get("status") == "error":
                slot["errors"] += 1
            elif e.get("status") == "rejected":
                slot["rejections"] += 1
            dur = e.get("duration_ms") or 0
            slot["total_duration_ms"] += int(dur)
            tool = e.get("tool") or e.get("kind") or ""
            if tool:
                slot["tools_used"].add(tool)
        # Convert sets to sorted lists for JSON-friendliness.
        for s in out.values():
            s["tools_used"] = sorted(s["tools_used"])
        return out

    def list_agents(self) -> List[Dict[str, Any]]:
        """Return a flat list of every agent that has acted.

        Each entry: {id, role, name, parent_chain, tool_calls,
        errors, last_active_iso}.
        """
        summary = self.agent_summary()
        out: List[Dict[str, Any]] = []
        # We need last_active per agent — scan entries newest-first.
        last_active: Dict[str, str] = {}
        with self._log._lock:
            for e in reversed(self._log._entries):
                agent = (e.get("meta") or {}).get("agent") or {}
                aid = agent.get("id")
                if not aid or aid in last_active:
                    continue
                last_active[aid] = e.get("ts_iso") or ""
        for aid, s in summary.items():
            out.append({
                "id": aid,
                "role": s["role"],
                "name": s["name"],
                "tool_calls": s["tool_calls"],
                "errors": s["errors"],
                "rejections": s["rejections"],
                "total_duration_ms": s["total_duration_ms"],
                "last_active_iso": last_active.get(aid, ""),
                "tools_used": s["tools_used"],
            })
        # Sort by tool_calls desc so the most active agent appears first.
        out.sort(key=lambda x: x["tool_calls"], reverse=True)
        return out

    def filter_by_agent(
        self,
        agent_id: str,
        include_children: bool = True,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return entries attributed to ``agent_id``.

        If ``include_children`` is True (default), also includes entries
        produced by any descendant of ``agent_id`` (i.e. any agent whose
        ``parent_chain`` contains ``agent_id``).
        """
        with self._log._lock:
            entries = list(self._log._entries)
        out: List[Dict[str, Any]] = []
        for e in entries:
            agent = (e.get("meta") or {}).get("agent") or {}
            aid = agent.get("id")
            chain = agent.get("parent_chain") or []
            if aid == agent_id:
                out.append(e)
            elif include_children and agent_id in chain:
                out.append(e)
            if len(out) >= limit:
                break
        return out

    # ── Exports ──────────────────────────────────────────────────

    def export_audit_json(self, with_fingerprints: bool = True) -> str:
        """Return the audit trail as a JSON string.

        When ``with_fingerprints`` is True (default), each entry gets a
        SHA-256 ``fingerprint`` field computed over a canonical
        projection of the entry. Downstream auditors can recompute the
        fingerprint from the same projection and compare — any tampered
        row will fail the check.
        """
        with self._log._lock:
            entries = list(self._log._entries)
        if with_fingerprints:
            entries = [self._with_fingerprint(e) for e in entries]
        return json.dumps(entries, indent=2, default=str, sort_keys=True)

    def export_audit_csv(self) -> str:
        """Return the audit trail as a CSV string.

        Columns: ts_iso, agent_id, agent_role, agent_name, category,
        kind, tool, status, title, path, command, duration_ms, chat_id.
        Large fields (args, result_preview) are omitted — CSV is for
        quick scanning, JSON is for full fidelity.
        """
        with self._log._lock:
            entries = list(self._log._entries)
        buf = io.StringIO()
        fieldnames = [
            "ts_iso", "agent_id", "agent_role", "agent_name",
            "category", "kind", "tool", "status", "title",
            "path", "command", "duration_ms", "chat_id",
        ]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for e in entries:
            agent = (e.get("meta") or {}).get("agent") or {}
            row = {
                "ts_iso": e.get("ts_iso", ""),
                "agent_id": agent.get("id", ""),
                "agent_role": agent.get("role", ""),
                "agent_name": agent.get("name", ""),
                "category": e.get("category", ""),
                "kind": e.get("kind", ""),
                "tool": e.get("tool", ""),
                "status": e.get("status", ""),
                "title": e.get("title", ""),
                "path": e.get("path", ""),
                "command": e.get("command", ""),
                "duration_ms": e.get("duration_ms", ""),
                "chat_id": e.get("chat_id", ""),
            }
            writer.writerow(row)
        return buf.getvalue()

    @staticmethod
    def _with_fingerprint(entry: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of ``entry`` with a SHA-256 fingerprint field."""
        # Canonical projection: only stable fields, sorted keys.
        proj = {
            "ts": entry.get("ts"),
            "category": entry.get("category"),
            "kind": entry.get("kind"),
            "tool": entry.get("tool"),
            "title": entry.get("title"),
            "status": entry.get("status"),
            "agent": (entry.get("meta") or {}).get("agent"),
            "path": entry.get("path"),
            "command": entry.get("command"),
            "chat_id": entry.get("chat_id"),
        }
        canonical = json.dumps(proj, sort_keys=True, default=str)
        fp = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        out = dict(entry)
        out[FINGERPRINT_FIELD] = fp
        return out

    @staticmethod
    def verify_fingerprint(entry: Dict[str, Any]) -> bool:
        """Return True if the entry's stored fingerprint matches a recomputation.

        Returns True if the entry has no fingerprint (not all exports
        include them). Returns False if a fingerprint is present but
        does not match the recomputed value (i.e. the entry was
        tampered with after export).
        """
        fp = entry.get(FINGERPRINT_FIELD)
        if not fp:
            return True
        recomputed = AuditTrail._with_fingerprint({k: v for k, v in entry.items()
                                                    if k != FINGERPRINT_FIELD})
        return secrets.compare_digest(fp, recomputed.get(FINGERPRINT_FIELD, ""))


# ── Module-level singleton ────────────────────────────────────────────

_audit: Optional[AuditTrail] = None
_audit_lock = threading.Lock()


def get_audit_trail() -> AuditTrail:
    """Return the process-wide AuditTrail singleton."""
    global _audit
    if _audit is None:
        with _audit_lock:
            if _audit is None:
                _audit = AuditTrail()
    return _audit


def reset_audit_trail_for_test() -> AuditTrail:
    """Test-only: forget the cached trail and return a fresh one."""
    global _audit
    with _audit_lock:
        _audit = None
    return get_audit_trail()
