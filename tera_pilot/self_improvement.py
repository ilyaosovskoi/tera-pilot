"""
Self-improvement loop — the agent learns from its own runs.

Problem
-------
Tera Pilot already records *what* happened during a run (activity log,
audit trail, learnings) but it never turns those observations into a
concrete picture of how the agent keeps failing. A run that ended in a
tool-call loop, burned its iteration budget, or shipped an unverified
write leaves no trace that the *next* run should behave differently.

This module closes that loop in three layers:

1. **Observe** — :func:`analyze_run` inspects a finished ``TaskResult``
   and emits ``ImprovementProposal`` objects for measurable failure
   patterns: exhausted budgets, degenerate prose, unverified writes,
   high tool-error rates, repeated identical tool calls, file thrashing,
   and empty plans. Every proposal carries *evidence taken from the run*
   (iteration counts, error strings, file names) — no speculation, which
   matches the project's claims discipline.

2. **Remember** — :class:`ImprovementBacklog` persists proposals as
   JSONL under ``~/.tera_pilot/improvements/<project>.jsonl``. Recurring
   signals bump ``occurrences`` instead of creating duplicates, so the
   backlog ranks itself by how often the agent actually fails that way.
   :func:`build_self_improvement_fragment` injects the top recurring
   patterns into the system prompt as *rules to avoid* — the agent
   changes its own future behaviour, per project.

3. **Fix (dogfooding)** — :func:`build_self_task` turns a proposal into
   a ready-to-run task on Tera Pilot's own repository, and
   :func:`handle_improve_command` exposes the whole loop as ``/improve``.
   The task is only generated when the workspace really is the Tera
   Pilot repo (or ``TERA_PILOT_ALLOW_SELF_TASK=1`` is set), so the
   feature cannot silently point the agent at an unrelated project.

Design boundaries
-----------------
- **No telemetry.** Everything lives on the user's disk; nothing phones
  home. Proposals are derived from local run data only.
- **No silent self-editing.** ``/improve task`` *prepares* a task; the
  human submits it, and the resulting run goes through the normal
  workspace sandbox, command policy and approval flow. There is no
  privileged "self-modify" path.
- **Never breaks a run.** :func:`observe_run` swallows every exception —
  a broken backlog must not fail the user's task.
- **Duck-typed input.** ``analyze_run`` reads attributes off the result
  object instead of importing ``TaskResult``, so it can be called from
  the runtime, from tests with plain stubs, and from future result
  shapes without an import cycle.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

#: Failure prefixes ToolEngine puts on a non-productive tool result.
#: Kept in sync with the runtime's auto-extension check (see
#: ``AgentRuntime._run_agent_loop``) — one definition of «tool failed».
_FAILURE_PREFIXES = ("[TOOL ERROR]", "[FILE NOT FOUND]", "[BLOCKED]")

#: Errors that mean «no work happened», not «the agent did badly».
_SKIP_ERRORS = {"awaiting_plan_approval", "quota_exhausted"}

#: Tools that mutate the workspace — a run that used one of these
#: without any verification tool is an unverified-change signal.
_WRITE_TOOLS = {
    "write_file", "str_replace", "apply_diff", "delete_file",
    "rename_file", "mkdir", "write_binary_file",
    "office_create", "office_save_as", "office_find_replace",
}

#: Tools that actually check the result of a change.
_VERIFY_TOOLS = {"execute_command", "run_code", "self_verify", "run_tests"}

#: Task types for which «the agent used no tools at all» is a real
#: failure. Pure chat / analysis / planning turns legitimately answer
#: in prose.
_TOOL_EXPECTED_TYPES = {"agentic", "write", "edit", "refactor", "test", "debug"}

CATEGORY_LABELS = {
    "budget": "Budget & endurance",
    "loop": "Repetition / stuck loops",
    "prompting": "Prompt & tool-call adherence",
    "tooling": "Tool reliability",
    "verification": "Verification discipline",
    "quality": "Edit quality",
}

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

_VALID_STATUSES = {"open", "planned", "applied", "dismissed"}

#: Injected fragment budget — deliberately tiny so self-improvement
#: guidance can never dominate the prompt.
_MAX_INJECTED = 3
_MAX_INJECTED_CHARS = 1200

_GLOBAL_DIR = Path.home() / ".tera_pilot" / "improvements"


# ── Settings ──────────────────────────────────────────────────────────

_SETTINGS_KEY = "self_improvement"


def _config_path() -> Path:
    return Path.home() / ".tera_pilot" / "config.json"


def _settings() -> Dict[str, Any]:
    """Read the ``self_improvement`` config block (tolerant).

    Defaults: observation on, injection on, 3 injected patterns. A user
    who wants zero automatic behaviour can flip either switch off
    without downgrading the package.
    """
    out = {"enabled": True, "inject": True, "max_injected": _MAX_INJECTED}
    try:
        p = _config_path()
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            raw = cfg.get(_SETTINGS_KEY, {}) or {}
            if isinstance(raw, dict):
                if "enabled" in raw:
                    out["enabled"] = bool(raw["enabled"])
                if "inject" in raw:
                    out["inject"] = bool(raw["inject"])
                if "max_injected" in raw:
                    out["max_injected"] = max(0, min(10, int(raw["max_injected"])))
    except Exception:
        pass
    return out


# ── Small helpers ─────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _norm_day(ts: str) -> str:
    return (ts or "")[:10]


def _fingerprint(category: str, key: str) -> str:
    h = hashlib.sha1(f"{category}\x1f{key}".encode("utf-8")).hexdigest()
    return h[:10]


def _slug(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "project")[:max_len]


def _project_key(workspace: str) -> str:
    """Stable, filesystem-safe namespace for one workspace."""
    if not workspace:
        return "global"
    try:
        real = str(Path(workspace).resolve())
    except Exception:
        real = str(workspace)
    return f"{_slug(Path(real).name)}-{hashlib.sha1(real.encode('utf-8')).hexdigest()[:10]}"


def _tool_name(call: Any) -> str:
    name = getattr(call, "name", None)
    if name is None:
        return ""
    return str(getattr(name, "value", name))


def _arg_path(args: Dict[str, Any]) -> str:
    for key in ("path", "file_path", "filename", "file", "target"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _truncate(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ── Proposal model ────────────────────────────────────────────────────


@dataclass
class ImprovementProposal:
    """One measurable way the agent could do better next time."""

    id: str
    category: str
    severity: str
    title: str
    evidence: str
    root_cause: str
    suggested_action: str
    task_prompt: str = ""
    occurrences: int = 1
    first_seen: str = ""
    last_seen: str = ""
    status: str = "open"
    source: str = "run"
    workspace: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "evidence": self.evidence,
            "root_cause": self.root_cause,
            "suggested_action": self.suggested_action,
            "task_prompt": self.task_prompt,
            "occurrences": int(self.occurrences),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "status": self.status,
            "source": self.source,
            "workspace": self.workspace,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ImprovementProposal":
        return cls(
            id=str(d.get("id", "")),
            category=str(d.get("category", "quality")),
            severity=str(d.get("severity", "medium")),
            title=str(d.get("title", "")),
            evidence=str(d.get("evidence", "")),
            root_cause=str(d.get("root_cause", "")),
            suggested_action=str(d.get("suggested_action", "")),
            task_prompt=str(d.get("task_prompt", "")),
            occurrences=int(d.get("occurrences", 1) or 1),
            first_seen=str(d.get("first_seen", "")),
            last_seen=str(d.get("last_seen", "")),
            status=str(d.get("status", "open")),
            source=str(d.get("source", "run")),
            workspace=str(d.get("workspace", "")),
        )


def _severity_rank(sev: str) -> int:
    return _SEVERITY_RANK.get((sev or "").lower(), 3)


def _make(
    category: str,
    severity: str,
    key: str,
    title: str,
    evidence: str,
    root_cause: str,
    suggested_action: str,
    *,
    workspace: str = "",
    source: str = "run",
    task_prompt: str = "",
) -> ImprovementProposal:
    now = _now_iso()
    return ImprovementProposal(
        id=_fingerprint(category, key),
        category=category,
        severity=severity,
        title=title,
        evidence=evidence,
        root_cause=root_cause,
        suggested_action=suggested_action,
        task_prompt=task_prompt,
        first_seen=now,
        last_seen=now,
        workspace=workspace,
        source=source,
    )


# ── Run analysis ──────────────────────────────────────────────────────


def _collect_tool_records(result: Any) -> List[Dict[str, Any]]:
    """Normalise steps/tool_calls into ``{name, args, obs, failed}`` rows."""
    records: List[Dict[str, Any]] = []
    for step in list(getattr(result, "steps", []) or []):
        action = getattr(step, "action", None)
        if action is None:
            continue
        obs = getattr(step, "observation", "") or ""
        records.append({
            "name": _tool_name(action),
            "args": dict(getattr(action, "args", {}) or {}),
            "obs": obs,
            "failed": bool(obs) and obs.startswith(_FAILURE_PREFIXES),
        })
    if records:
        return records
    # Fall back to tool_calls when steps were not recorded (e.g. a
    # result reconstructed from a summary).
    for call in list(getattr(result, "tool_calls", []) or []):
        err = getattr(call, "error", None)
        records.append({
            "name": _tool_name(call),
            "args": dict(getattr(call, "args", {}) or {}),
            "obs": str(err or ""),
            "failed": bool(err),
        })
    return records


def analyze_run(
    result: Any,
    *,
    workspace: str = "",
    section: str = "general",
    planned_iterations: int = 0,
) -> List[ImprovementProposal]:
    """Turn one finished run into concrete improvement proposals.

    Pure function: no I/O, no globals, no exceptions for malformed
    input. Returns proposals ordered by severity (high first).
    """
    proposals: List[ImprovementProposal] = []
    try:
        error = str(getattr(result, "error", None) or "")
        if error in _SKIP_ERRORS or "cancel" in error.lower():
            return []
        md = dict(getattr(result, "metadata", {}) or {})
        iterations = int(getattr(result, "iterations", 0) or 0)
        tok_in = int(getattr(result, "metadata", {}).get("total_tokens_in", 0) or 0) if isinstance(md, dict) else 0
        tok_out = int(md.get("total_tokens_out", 0) or 0)
        task_type = str(md.get("task_type", "") or "agentic").lower()
        records = _collect_tool_records(result)
        failed = [r for r in records if r["failed"]]

        proposals.extend(_signal_budget(
            error, iterations, tok_in, tok_out, workspace, planned_iterations,
        ))
        proposals.extend(_signal_prompting(result, md, records, iterations, task_type, workspace))
        proposals.extend(_signal_verification(records, workspace))
        proposals.extend(_signal_tool_errors(records, failed, workspace))
        proposals.extend(_signal_loops(records, workspace))
        proposals.extend(_signal_thrash(records, workspace))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("[self_improvement] analyze_run failed: %s", e)
        return []
    proposals.sort(key=lambda p: (_severity_rank(p.severity), -p.occurrences))
    return proposals


def _signal_budget(error: str, iterations: int, tok_in: int, tok_out: int,
                   ws: str, planned_iterations: int = 0) -> List[ImprovementProposal]:
    low = error.lower()
    if not (error.startswith("Max iterations") or "wall-clock" in low or "time budget" in low):
        return []
    return [_make(
        "budget", "high", "ceiling",
        "Run ended at the iteration/time ceiling without a final answer",
        evidence=f"error={error!r}; iterations={iterations}; "
                 f"soft_cap={int(planned_iterations or 0)}; "
                 f"tokens_in={tok_in}; tokens_out={tok_out}",
        root_cause="The task needed more steps than the endurance limits allow, "
                   "or the loop kept acting without converging on an answer.",
        suggested_action="Either raise the limits (`/endurance`) for tasks of this "
                         "size, or split the task into smaller steps so each run "
                         "can finish and report. If the run was looping, fix the "
                         "repetition signal first.",
        workspace=ws,
    )]


def _signal_prompting(result: Any, md: Dict[str, Any], records: List[Dict[str, Any]],
                      iterations: int, task_type: str, ws: str) -> List[ImprovementProposal]:
    out: List[ImprovementProposal] = []
    if md.get("degraded_prose") or (
        not records and iterations >= 1 and task_type in _TOOL_EXPECTED_TYPES
    ):
        out.append(_make(
            "prompting", "high", "no_tool_use",
            "Agent answered in prose without using any tool",
            evidence=f"task_type={task_type}; iterations={iterations}; "
                     f"tool_calls=0; degraded_prose={bool(md.get('degraded_prose'))}",
            root_cause="The model did not internalise that it IS the agent and "
                       "should act through tools (common with small/local models).",
            suggested_action="Strengthen tool-call adherence: keep the tool schema "
                             "tight, prefer a model with reliable tool calling, and "
                             "make the task statement explicitly action-oriented.",
            workspace=ws,
        ))
    return out


def _signal_verification(records: List[Dict[str, Any]], ws: str) -> List[ImprovementProposal]:
    writes = [r for r in records if r["name"] in _WRITE_TOOLS and not r["failed"]]
    if not writes:
        return []
    if any(r["name"] in _VERIFY_TOOLS for r in records):
        return []
    paths = sorted({p for p in (_arg_path(r["args"]) for r in writes) if p})
    return [_make(
        "verification", "high", "unverified_write",
        "Workspace was modified without any verification step",
        evidence=f"{len(writes)} mutating tool call(s) "
                 f"({', '.join(sorted({r['name'] for r in writes}))}) "
                 f"but no {'/'.join(sorted(_VERIFY_TOOLS))} call; "
                 f"files={', '.join(paths[:5]) or '(none reported)'}",
        root_cause="The agent considered the edit «done» as soon as it wrote the "
                   "file, so a broken change could be reported as success.",
        suggested_action="Require a verification step after edits: run the project's "
                         "tests or call self_verify before emitting final_answer.",
        workspace=ws,
    )]


def _signal_tool_errors(records: List[Dict[str, Any]], failed: List[Dict[str, Any]], ws: str) -> List[ImprovementProposal]:
    if len(failed) < 3:
        return []
    rate = len(failed) / max(1, len(records))
    if rate < 0.4:
        return []
    by_tool = Counter(r["name"] for r in failed)
    top_tool, top_n = by_tool.most_common(1)[0]
    sample = next((r["obs"] for r in failed if r["name"] == top_tool), "")
    return [_make(
        "tooling", "high" if rate >= 0.6 else "medium", "error_rate",
        "High tool-error rate during the run",
        evidence=f"{len(failed)}/{len(records)} tool calls failed "
                 f"({rate:.0%}); most frequent={top_tool} ({top_n}×); "
                 f"sample={_truncate(sample, 160)!r}",
        root_cause="Tool arguments were rejected (bad paths, blocked commands, "
                   "sandbox/policy denials) — the agent kept retrying instead of "
                   "adapting to the workspace.",
        suggested_action="Improve the failure text the tool returns so the model can "
                         "self-correct (name the real workspace root, list valid "
                         "alternatives), and add a guard that stops repeating a "
                         "failing call.",
        workspace=ws,
    )]


def _signal_loops(records: List[Dict[str, Any]], ws: str) -> List[ImprovementProposal]:
    out: List[ImprovementProposal] = []
    counts: Counter = Counter()
    samples: Dict[str, str] = {}
    for r in records:
        try:
            args_json = json.dumps(r["args"], sort_keys=True, default=str)
        except Exception:
            args_json = str(r["args"])
        counts[(r["name"], args_json)] += 1
        samples.setdefault(f"{r['name']}", args_json)
    aggregated: Counter = Counter()
    args_by_tool: Dict[str, str] = {}
    for (name, args_json), n in counts.items():
        aggregated[name] += n
        if n >= 3:
            args_by_tool.setdefault(name, _truncate(args_json, 140))
    for name, n in aggregated.items():
        if n < 3 or name not in args_by_tool:
            continue
        out.append(_make(
            "loop", "high", f"loop:{name}",
            f"Repeated identical `{name}` calls",
            evidence=f"{n} identical call(s) of {name}; args={args_by_tool[name]}",
            root_cause="The loop had no memory of «I already tried this», so a "
                       "failing call was retried until the budget ran out.",
            suggested_action="Detect the repeat earlier (same tool + same args) and "
                             "inject corrective guidance that forces a different "
                             "action instead of another identical retry.",
            workspace=ws,
        ))
    return out


def _signal_thrash(records: List[Dict[str, Any]], ws: str) -> List[ImprovementProposal]:
    out: List[ImprovementProposal] = []
    paths = Counter(p for p in (_arg_path(r["args"]) for r in records) if p)
    for path, n in paths.items():
        if n < 4:
            continue
        out.append(_make(
            "quality", "medium", f"thrash:{path}",
            f"Repeated edits to {path}",
            evidence=f"{n} tool call(s) targeted {path!r} in a single run",
            root_cause="The change was applied in many small attempts instead of "
                       "one considered edit — usually a sign the agent lacked "
                       "enough context about the file before editing it.",
            suggested_action="Read the whole file (or the relevant region) before "
                             "the first edit, and prefer one str_replace over many.",
            workspace=ws,
        ))
    return out


# ── Backlog (persistent) ──────────────────────────────────────────────


class ImprovementBacklog:
    """Per-project JSONL store of improvement proposals.

    Thread-safe (RLock) and crash-safe (write-to-temp + atomic replace),
    because the fleet/daemon can finish several runs concurrently. The
    file is small — a full rewrite per update keeps the implementation
    simple and always compact.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    # ── I/O ──────────────────────────────────────────────────────────

    def _read(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        if not self.path.exists():
            return out
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and rec.get("id"):
                        out[str(rec["id"])] = rec
        except OSError as e:
            logger.warning("[self_improvement] failed to read backlog: %s", e)
        return out

    def _write(self, records: Dict[str, Dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in records.values():
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("[self_improvement] failed to write backlog: %s", e)

    # ── Mutations ────────────────────────────────────────────────────

    def record(self, proposals: Iterable[ImprovementProposal]) -> Dict[str, List[str]]:
        """Merge proposals in. Returns ``{"created": [...], "bumped": [...]}``."""
        created: List[str] = []
        bumped: List[str] = []
        with self._lock:
            records = self._read()
            for p in proposals:
                if not p.id:
                    continue
                existing = records.get(p.id)
                if existing is None:
                    records[p.id] = p.to_dict()
                    created.append(p.id)
                    continue
                existing["occurrences"] = int(existing.get("occurrences", 1) or 1) + 1
                existing["last_seen"] = p.last_seen or _now_iso()
                existing["evidence"] = p.evidence or existing.get("evidence", "")
                existing["title"] = p.title or existing.get("title", "")
                existing["root_cause"] = p.root_cause or existing.get("root_cause", "")
                existing["suggested_action"] = p.suggested_action or existing.get("suggested_action", "")
                if p.task_prompt:
                    existing["task_prompt"] = p.task_prompt
                # Escalate severity if the newest run looked worse; never
                # silently downgrade a known-serious pattern.
                if _severity_rank(p.severity) < _severity_rank(str(existing.get("severity", "low"))):
                    existing["severity"] = p.severity
                # A dismissed proposal stays dismissed (the user said no);
                # it still accumulates occurrences for informational value.
                bumped.append(p.id)
            self._write(records)
        return {"created": created, "bumped": bumped}

    def set_status(self, proposal_id: str, status: str) -> bool:
        status = (status or "").lower()
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {sorted(_VALID_STATUSES)}")
        with self._lock:
            records = self._read()
            if proposal_id not in records:
                return False
            records[proposal_id]["status"] = status
            records[proposal_id]["last_seen"] = records[proposal_id].get("last_seen") or _now_iso()
            self._write(records)
            return True

    def clear(self) -> int:
        with self._lock:
            n = len(self._read())
            self._write({})
            return n

    # ── Queries ──────────────────────────────────────────────────────

    def all(self) -> List[ImprovementProposal]:
        with self._lock:
            return [ImprovementProposal.from_dict(r) for r in self._read().values()]

    def list(self, status: Optional[str] = "open", limit: int = 50) -> List[ImprovementProposal]:
        items = self.all()
        if status:
            items = [p for p in items if p.status == status]
        items.sort(key=lambda p: (
            _severity_rank(p.severity), -p.occurrences, p.last_seen),)
        return items[:limit] if limit and limit > 0 else items

    def get(self, proposal_id: str) -> Optional[ImprovementProposal]:
        with self._lock:
            rec = self._read().get(proposal_id)
        return ImprovementProposal.from_dict(rec) if rec else None

    def counts(self) -> Dict[str, int]:
        counts: Counter = Counter(p.status for p in self.all())
        return {
            "total": sum(counts.values()),
            "open": counts.get("open", 0),
            "planned": counts.get("planned", 0),
            "applied": counts.get("applied", 0),
            "dismissed": counts.get("dismissed", 0),
        }

    def top(self) -> Optional[ImprovementProposal]:
        open_items = self.list(status="open", limit=1)
        return open_items[0] if open_items else None

    # ── Prompt fragment ──────────────────────────────────────────────

    def to_fragment(self, workspace: str = "", *, max_items: int = _MAX_INJECTED,
                    max_chars: int = _MAX_INJECTED_CHARS) -> str:
        """Inject the top recurring patterns as *rules to avoid*."""
        if max_items <= 0 or max_chars <= 0:
            return ""
        items = self.list(status="open", limit=max_items)
        items = [p for p in items if p.severity in ("high", "medium")]
        if not items:
            return ""
        lines: List[str] = []
        for p in items:
            block = (
                f"- [{p.category}/{p.severity}] {p.title} "
                f"(seen {p.occurrences}×): {_truncate(p.suggested_action, 220)}"
            )
            if sum(len(x) + 1 for x in lines) + len(block) > max_chars:
                break
            lines.append(block)
        if not lines:
            return ""
        body = (
            "Recurring failure patterns recorded from previous runs in this "
            "workspace. Treat them as RULES TO AVOID while doing the current "
            "task — not as new tasks to start:\n" + "\n".join(lines)
        )
        try:
            from tera_pilot.agent.context_fragments import build_fragment, stable_id
            return build_fragment("self_improvement", stable_id(workspace or "global"), body)
        except Exception:
            return (
                '<context_fragment type="self_improvement" id="local">\n'
                + body
                + "\n</context_fragment>"
            )


# ── Process-wide registry ─────────────────────────────────────────────

_backlogs: Dict[str, ImprovementBacklog] = {}
_backlogs_lock = threading.RLock()


def backlog_path_for(workspace: str, *, global_store: bool = False) -> Path:
    """Where the backlog for ``workspace`` lives."""
    if global_store or not workspace:
        return _GLOBAL_DIR / "global.jsonl"
    return _GLOBAL_DIR / f"{_project_key(workspace)}.jsonl"


def get_backlog(workspace: str) -> ImprovementBacklog:
    """Return the (cached) backlog for a workspace."""
    key = str(Path(workspace).resolve()) if workspace else ""
    with _backlogs_lock:
        bl = _backlogs.get(key)
        if bl is None:
            bl = ImprovementBacklog(backlog_path_for(workspace))
            _backlogs[key] = bl
        return bl


def reset_backlog_cache_for_test() -> None:
    """Drop cached backlogs (tests only — the cache keys on resolved paths)."""
    with _backlogs_lock:
        _backlogs.clear()


# ── Runtime entry point ───────────────────────────────────────────────


def observe_run(
    result: Any,
    *,
    workspace: str = "",
    section: str = "general",
    planned_iterations: int = 0,
) -> Dict[str, Any]:
    """Analyse a finished run and persist any new proposals.

    Called by ``AgentRuntime`` after every turn. Must never raise and
    never slow a run noticeably — analysis is pure string work over the
    run's own steps.
    """
    summary: Dict[str, Any] = {"enabled": True, "recorded": 0, "created": [], "bumped": []}
    try:
        if not _settings()["enabled"]:
            summary["enabled"] = False
            return summary
        if not workspace:
            return summary
        proposals = analyze_run(
            result, workspace=workspace, section=section,
            planned_iterations=planned_iterations,
        )
        if not proposals:
            return summary
        bl = get_backlog(workspace)
        res = bl.record(proposals)
        summary.update({
            "recorded": len(proposals),
            "created": res["created"],
            "bumped": res["bumped"],
            "path": str(bl.path),
        })
        if res["created"]:
            logger.info(
                "[self_improvement] recorded %d new proposal(s) for %s: %s",
                len(res["created"]), workspace, ", ".join(res["created"]),
            )
        return summary
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("[self_improvement] observe_run failed: %s", e)
        summary["error"] = str(e)
        return summary


def build_self_improvement_fragment(workspace: str) -> str:
    """System-prompt fragment for the current workspace (no-op when off)."""
    try:
        s = _settings()
        if not s["inject"] or not workspace:
            return ""
        return get_backlog(workspace).to_fragment(
            workspace, max_items=int(s["max_injected"]),
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("[self_improvement] fragment build failed: %s", e)
        return ""


# ── Dogfooding: proposal → real task ──────────────────────────────────


def is_tera_pilot_repo(path: str) -> bool:
    """True when ``path`` looks like the Tera Pilot source tree.

    Deliberately strict: the self-task builder must not silently target
    an unrelated project. ``TERA_PILOT_ALLOW_SELF_TASK=1`` is the
    explicit escape hatch for forks/clones with a different layout.
    """
    if os.environ.get("TERA_PILOT_ALLOW_SELF_TASK", "").strip() in ("1", "true", "yes"):
        return True
    try:
        root = Path(path)
        if not (root / "tera_pilot" / "agent_runtime").is_dir():
            return False
        pyproject = root / "pyproject.toml"
        if not pyproject.is_file():
            return False
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        return 'name = "tera-pilot"' in text or "name=\"tera-pilot\"" in text
    except Exception:
        return False


def build_self_task(proposal: ImprovementProposal, workspace: str = "") -> str:
    """Compose a ready-to-run dogfooding task from a proposal.

    The prompt is deliberately prescriptive: smallest change, tests
    required, no weakening of security controls, and a report that
    states measured evidence. It is *prepared*, not executed — the human
    submits it and the run goes through the usual approvals.
    """
    return (
        "Self-improvement task for Tera Pilot itself (dogfooding).\n\n"
        f"## Objective\n{proposal.suggested_action}\n\n"
        f"## Why (evidence from a real run)\n{proposal.evidence}\n\n"
        f"## Suspected root cause\n{proposal.root_cause}\n\n"
        "## Constraints\n"
        "- Work only inside this repository; change the smallest surface that "
        "addresses the objective.\n"
        "- Add or update a test that fails before your change and passes after it.\n"
        "- Run the relevant test suite before reporting the result.\n"
        "- Do NOT weaken the workspace sandbox, command policy, approval flow, "
        "Guardian, or the audit trail — a «fix» that relaxes those is not a fix.\n"
        "- If the objective is already handled, say so and stop instead of "
        "inventing work.\n\n"
        "## Report\n"
        "State what changed, which files, which tests you ran, and the measured "
        "before/after. Then close the proposal with `/improve done "
        f"{proposal.id}`.\n"
    )


# ── Slash-command surface ─────────────────────────────────────────────


def build_self_task_compact(proposal: ImprovementProposal) -> str:
    """One-line variant of :func:`build_self_task`.

    The TUI composer is a single-line ``Input`` widget, so a multi-line
    prompt would render mangled. This variant collapses the same
    objective, evidence and guardrails into one line: the full markdown
    task is printed to the chat log for reading, and this is what gets
    pre-filled for one-press execution.
    """
    return (
        f"Self-improvement task for Tera Pilot (dogfooding) [{proposal.id}]: "
        f"{_truncate(proposal.suggested_action, 400)} "
        f"Evidence from a real run: {_truncate(proposal.evidence, 400)}. "
        "Constraints: make the smallest change that addresses the objective; "
        "add a test that fails before the change and passes after it; run the "
        "relevant tests; do NOT weaken the sandbox, command policy, approval "
        "flow or audit trail; if the objective is already handled, say so and "
        "stop. Report the changed files, the tests you ran and the measured "
        f"before/after, then run /improve done {proposal.id}."
    )


def _fmt_proposal(p: ImprovementProposal) -> str:
    return (
        f"  [{p.id}] {p.severity:6s} {p.title}\n"
        f"      category: {p.category}   seen: {p.occurrences}×   "
        f"last: {_norm_day(p.last_seen)}\n"
        f"      action: {_truncate(p.suggested_action, 160)}"
    )


def handle_improve_command(workspace: str, arg: str = "") -> Dict[str, Any]:
    """Handle the ``/improve`` slash command.

    Subcommands::

        /improve                — list open proposals + counts
        /improve show <id>      — full detail for one proposal
        /improve next           — the highest-priority open proposal
        /improve task [id]      — build a self-improvement task (human submits)
        /improve done <id>      — mark applied (after the fix lands)
        /improve dismiss <id>   — stop injecting / stop surfacing it
        /improve restore <id>   — undo a dismissal
        /improve reset          — delete the backlog for this workspace

    Returns ``{"ok": bool, "text": str}`` plus ``task_prompt`` for the
    TUI to prefill the composer with.
    """
    arg = (arg or "").strip()
    parts = arg.split(None, 1) if arg else []
    sub = parts[0].lower() if parts else ""
    sub_arg = parts[1].strip() if len(parts) > 1 else ""

    if not workspace:
        return {"ok": False, "error": "no workspace set — /improve needs a project root"}

    backlog = get_backlog(workspace)
    counts = backlog.counts()

    if sub in ("", "list"):
        if counts["total"] == 0:
            return {"ok": True, "text":
                    "No improvement proposals yet for this workspace.\n"
                    "Proposals are recorded automatically after runs that hit "
                    "budget limits, loop, fail tools, or skip verification."}
        lines = [
            f"Self-improvement backlog for {workspace} "
            f"({counts['open']} open / {counts['applied']} applied / "
            f"{counts['dismissed']} dismissed, {counts['total']} total):",
            "",
        ]
        items = backlog.list(status="open", limit=15)
        if not items:
            lines.append("  (no open proposals — /improve restore <id> to reopen one)")
        for p in items:
            lines.append(_fmt_proposal(p))
        lines += [
            "",
            "  /improve show <id> · /improve task [id] · /improve done <id> · "
            "/improve dismiss <id>",
        ]
        return {"ok": True, "text": "\n".join(lines)}

    if sub == "show":
        if not sub_arg:
            return {"ok": False, "error": "Usage: /improve show <id>"}
        p = _resolve(backlog, sub_arg)
        if p is None:
            return {"ok": False, "error": f"no proposal matches {sub_arg!r}"}
        return {"ok": True, "text": "\n".join([
            f"# {p.title}",
            f"id:        {p.id}",
            f"category:  {p.category} ({CATEGORY_LABELS.get(p.category, '?')})",
            f"severity:  {p.severity}",
            f"status:    {p.status}",
            f"seen:      {p.occurrences}× (first {_norm_day(p.first_seen)}, "
            f"last {_norm_day(p.last_seen)})",
            "",
            f"Evidence:  {p.evidence}",
            f"Root cause: {p.root_cause}",
            f"Action:    {p.suggested_action}",
            "",
            f"Build a task with: /improve task {p.id}",
        ])}

    if sub == "next":
        p = backlog.top()
        if p is None:
            return {"ok": True, "text": "No open proposals."}
        return {"ok": True, "text": _fmt_proposal(p)}

    if sub in ("task", "plan"):
        p = _resolve(backlog, sub_arg) if sub_arg else backlog.top()
        if p is None:
            return {"ok": True, "text": "No open proposals — nothing to plan."}
        if not is_tera_pilot_repo(workspace):
            return {
                "ok": False,
                "text": (
                    f"Proposal {p.id} prepared, but this workspace is not the Tera "
                    "Pilot source tree, so no self-task was built.\n"
                    "Use /improve show for the action, or set "
                    "TERA_PILOT_ALLOW_SELF_TASK=1 to override deliberately."
                ),
                "proposal_id": p.id,
            }
        task = build_self_task(p, workspace)
        return {
            "ok": True,
            "text": (
                f"Self-improvement task prepared for proposal {p.id} "
                f"({p.severity}, {p.category}).\n"
                "The composer is pre-filled — press Enter to run it, and the "
                "run goes through the normal approvals. Full task:\n\n" + task
            ),
            "task_prompt": task,
            "composer_prompt": build_self_task_compact(p),
            "proposal_id": p.id,
        }

    if sub in ("done", "applied"):
        if not sub_arg:
            return {"ok": False, "error": "Usage: /improve done <id>"}
        p = _resolve(backlog, sub_arg)
        if p is None:
            return {"ok": False, "error": f"no proposal matches {sub_arg!r}"}
        backlog.set_status(p.id, "applied")
        return {"ok": True, "text": f"Marked applied: {p.id} — {p.title}"}

    if sub == "dismiss":
        if not sub_arg:
            return {"ok": False, "error": "Usage: /improve dismiss <id>"}
        p = _resolve(backlog, sub_arg)
        if p is None:
            return {"ok": False, "error": f"no proposal matches {sub_arg!r}"}
        backlog.set_status(p.id, "dismissed")
        return {"ok": True, "text":
                f"Dismissed: {p.id} — it will no longer be injected into prompts. "
                "Use /improve restore to undo."}

    if sub == "restore":
        if not sub_arg:
            return {"ok": False, "error": "Usage: /improve restore <id>"}
        p = _resolve(backlog, sub_arg)
        if p is None:
            return {"ok": False, "error": f"no proposal matches {sub_arg!r}"}
        backlog.set_status(p.id, "open")
        return {"ok": True, "text": f"Reopened: {p.id} — {p.title}"}

    if sub == "reset":
        n = backlog.clear()
        return {"ok": True, "text": f"Cleared {n} proposal(s) for this workspace."}

    return {
        "ok": False,
        "error": (
            f"Unknown /improve subcommand: {sub!r}\n"
            "Usage: /improve [list|show|next|task|done|dismiss|restore|reset]"
        ),
    }


def _resolve(backlog: ImprovementBacklog, needle: str) -> Optional[ImprovementProposal]:
    """Find a proposal by id, id-prefix, or title substring."""
    needle = (needle or "").strip()
    if not needle:
        return None
    exact = backlog.get(needle)
    if exact is not None:
        return exact
    lowered = needle.lower()
    for p in backlog.all():
        if p.id.startswith(needle):
            return p
    for p in backlog.all():
        if lowered in p.title.lower() or lowered in p.category.lower():
            return p
    return None
