"""
ToolEngine — the agent's tool dispatcher.

Each `step` in the agent loop calls `ToolEngine.execute(call)`,
which routes to a `_dispatch()` method that handles every
ToolName variant. File ops, git ops, MCP, sub-agents, office
worker, self-verify, watchdog, code execution, search, and
diff application all live here.

Kept as a single file (rather than split per-tool) because:
- the dispatcher is a single switch statement,
- many tools share private state (workspace, skills, whitelist,
  confirmation channel) that would require heavy __init__ glue,
- splitting would force subclassing or mixin patterns that make
  the call graph harder to follow.

The diff-related helpers (_str_replace_hint, _compute_diff_text,
_backup_file, _split_multi_file_diff, _apply_unified_diff) have
been moved to ..diff_utils and are imported here.
"""

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..types import AgentEvent, Task, TaskType, ToolCall, ToolName
from .._helpers import _sanitize_command
from ..diff_utils import (
    _split_multi_file_diff,
    _apply_unified_diff,
    _str_replace_hint,
    _compute_diff_text,
    _backup_file as _backup_file_func,
)
from tera_pilot.activity_log import get_activity_log, CATEGORY_INFO, STATUS_OK, STATUS_ERROR
from tera_pilot.context_manager import get_context_manager
from tera_pilot.agent.guardian import assess_risk, GuardianVerdict, GuardianConfig

logger = logging.getLogger(__name__)


class ToolEngine:
    """Executes agent tool calls in a sandboxed environment."""

    # v2.1.0 (Loop 2): default timeout raised from 15s to 180s.
    # 15s was too short for npm install, pytest, cargo build, docker build.
    # The constant is kept for backward compatibility but the actual
    # per-call timeout is now configurable via the `timeout` parameter
    # on _execute_command and _run_code.
    RUN_TIMEOUT = 180
    MAX_TIMEOUT = 3600  # 1-hour ceiling
    MIN_TIMEOUT = 1     # minimum allowed
    MAX_OUTPUT = 2000

    def __init__(self, workspace: Optional[str] = None):
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self._allowed_dirs: List[Path] = [self.workspace]
        self._backup_dir = Path(tempfile.gettempdir()) / "tera_pilot_backups"
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        # v1.0.6: cap the number of backup files to prevent unbounded
        # growth (M-RT-4). Oldest backups are deleted first.
        self._MAX_BACKUPS = 50
        # v1.0.4: diff-review support (write_file / str_replace only)
        self.diff_review_enabled: bool = False
        self._diff_review_callback: Optional[Callable] = None  # called from agent thread
        self._diff_review_event = threading.Event()
        self._diff_review_accepted: Optional[bool] = None
        # v1.1.1: generic action-confirmation gate for tools that are NOT
        # covered by diff-review (execute_command, delete_file,
        # rename_file, apply_diff, write_binary_file, git_stage,
        # git_commit). Controlled by `autonomy`:
        #   'always_ask'     — confirm before every one of these
        #   'new_files_only' — auto-approve only actions that create a
        #                      brand-new path; everything else asks
        #   'never_ask'      — never ask (previous, implicit default)
        self.autonomy: str = "always_ask"
        self._confirm_callback: Optional[Callable] = None
        self._confirm_event = threading.Event()
        self._confirm_accepted: Optional[bool] = None
        # v1.1.1: cooperative cancellation. AgentWorker hands us a
        # zero-arg callable that returns True once the user has clicked
        # Stop — we poll it between iterations AND while blocked waiting
        # on a diff-review/confirmation response, so Stop actually
        # interrupts a running agent instead of just muting UI updates.
        self._cancel_check: Optional[Callable[[], bool]] = None
        # v1.0.11: skills list — populated by AgentRuntime, used by
        # _get_skill() to return the full body of a requested skill.
        self._skills: List[Any] = []  # List[Skill] from skill_loader
        # v1.0.6: lock for workspace/allowed_dirs atomicity (M-RT-1).
        # Without this, concurrent set_workspace during an agent iteration
        # can cause RuntimeError (list changed size during iteration).
        self._workspace_lock = threading.Lock()
        # v1.1.0: section mirror — AgentRuntime.set_section() propagates
        # here so _dispatch can reject section-gated tools (spawn_subagent,
        # spawn_multi_agents) even if the model hallucinates a call.
        self.section: str = "general"
        # v1.1.3-fix (bug 1.4): role-based tool whitelist. When set (via
        # set_role_whitelist), _dispatch rejects any tool NOT in the set
        # with a "[TOOL DENIED]" message — even if the model ignores the
        # system prompt and emits a write_file/str_replace/delete_file
        # call for a "read-only" sub-agent role. None means "all tools
        # allowed" (the default for the parent agent).
        self.allowed_tools: Optional[set] = None
        # v1.2.0: Office Worker engine — instantiated lazily on first
        # office_* tool call so the import cost (python-docx /
        # openpyxl / python-pptx) is paid only when the user actually
        # enters the office section. The resolver is wired in __init__
        # to point at self._resolve_path so office tools inherit the
        # same workspace sandbox as every other tool.
        self._office_worker: Optional["OfficeWorker"] = None
        # v1.2.0: tracks every file path the agent has written/edited
        # in this run, so self_verify can re-read them at task close.
        # Cleared at the start of each _run_agent_loop call.
        self._touched_files: List[str] = []
        # v1.2.0: subagent watchdog state — populated by
        # _spawn_subagent / _spawn_multi_agents and consumed by
        # _watchdog_check (called from _run_agent_loop between waves).
        # Each entry: {"label": str, "started_at": float, "iterations":
        # int, "last_status": str}. The watchdog returns typed evidence
        # (ALL_DONE / STALL / REPEAT) — it never kills subagents, only
        # reports so the orchestrator (parent loop) can decide.
        self._subagent_watchdog_state: List[Dict[str, Any]] = []
        # v1.2.0: Activity Log — process-wide singleton. Every tool
        # dispatch records an entry here; the bridge subscribes and
        # forwards to the GUI's Activity Stream panel.
        self._activity_log = get_activity_log()
        # v1.2.0: current chat_id + iteration, set by _run_agent_loop
        # so tool dispatches can attach them to activity entries.
        self._current_chat_id: Optional[str] = None
        self._current_iteration: Optional[int] = None
        # Guardian config
        self._guardian_config: Optional[GuardianConfig] = None

    def set_workspace(self, workspace: str) -> None:
        with self._workspace_lock:
            self.workspace = Path(workspace).resolve()
            self._allowed_dirs = [self.workspace]

    def add_allowed_dir(self, path: str):
        with self._workspace_lock:
            self._allowed_dirs.append(Path(path).resolve())

    def set_skills(self, skills: List[Any]) -> None:
        """v1.0.11: inject the skill list so _get_skill can resolve ids."""
        self._skills = skills or []

    # v1.1.3-fix (bug 1.4): role-based tool whitelist.
    # v2.0.0: Added explore/plan/general-purpose subagent roles from subagent_v2.py
    ROLE_TOOL_WHITELIST: Dict[str, set] = {
        "architect": {
            "read_file", "list_files", "search_project",
            "get_project_structure", "git_status", "git_diff", "get_skill",
            "file_info", "read_binary_file",
        },
        "reviewer": {
            "read_file", "list_files", "search_project",
            "git_diff", "get_skill", "file_info", "read_binary_file",
        },
        "tester": {
            "read_file", "write_file", "run_code",
            "git_status", "get_skill", "list_files", "search_project",
            "file_info",
        },
        "implementer": {
            "read_file", "write_file", "str_replace", "mkdir",
            "run_code", "git_status", "git_diff", "git_stage", "git_commit",
            "get_skill", "list_files", "search_project",
            "get_project_structure", "file_info", "undo_write",
        },
        "generalist": {
            "read_file", "list_files", "search_project",
            "get_skill", "file_info", "get_project_structure",
            "read_binary_file", "git_status", "git_diff",
        },
        # v2.0.0 subagent roles - read-only by toolset construction
        "explore": {
            "read_file", "read_binary_file", "search_project",
            "grep", "glob", "list_files", "get_project_structure",
            "file_info", "git_status", "git_diff",
            "list_mcp_tools", "get_skill", "select_tools",
        },
        "plan": {
            "read_file", "read_binary_file", "search_project",
            "grep", "glob", "list_files", "get_project_structure",
            "file_info", "git_status", "git_diff",
            "list_mcp_tools", "get_skill", "select_tools",
        },
        "general-purpose": {
            "read_file", "read_binary_file", "search_project",
            "grep", "glob", "list_files", "get_project_structure",
            "file_info", "git_status", "git_diff",
            "list_mcp_tools", "get_skill", "select_tools",
            "write_file", "str_replace", "apply_diff", "write_binary_file",
            "delete_file", "rename_file", "mkdir", "run_code",
            "execute_command", "git_stage", "git_commit",
            "call_mcp_tool", "spawn_subagent", "watchdog_check",
            "self_verify", "undo_write",
        },
        # v2.1.0 (G18): read-only research role. Has web_search/web_fetch
        # but NO write/execute/git-write/mcp-call tools — so even if a
        # prompt-injected instruction from fetched content tries to get
        # the sub-agent to write files or run shell commands, the
        # dispatch-level whitelist rejects it regardless of what the
        # model attempts. Same defence-in-depth principle already used
        # for explore/plan.
        "researcher": {
            "web_search", "web_fetch",
            "read_file", "read_binary_file",
            "search_project", "grep", "glob", "list_files",
            "get_project_structure", "file_info", "get_skill",
        },
    }

    def set_role_whitelist(self, role: str, tools: Optional[List[str]] = None) -> None:
        """v1.1.3-fix (bug 1.4): restrict the tools this engine can
        dispatch to those allowed for ``role``. Pass ``"parent"`` or
        ``"general"`` to clear the whitelist (all tools allowed).

        Without this, the "sub-agents are read-only by default" promise
        was enforced ONLY by the system prompt — if the model ignored
        the prompt and emitted write_file/str_replace/delete_file, the
        ToolEngine would happily execute it. Now the dispatch itself
        rejects the call with a clear error.

        Args:
            role: The role name to look up in ROLE_TOOL_WHITELIST
            tools: Optional explicit list of allowed tools. If provided,
                   overrides the ROLE_TOOL_WHITELIST lookup.
        """
        if role in ("parent", "general", ""):
            self.allowed_tools = None
            return

        # If explicit tools list is provided, use it directly
        if tools is not None:
            self.allowed_tools = set(tools) if tools else None
            return

        # Otherwise fall back to role-based whitelist
        whitelist = self.ROLE_TOOL_WHITELIST.get(role)
        if whitelist is None:
            # Unknown role — fail safe (allow all) but log loudly so the
            # developer notices the typo / unsupported role.
            logger.warning("[agent] unknown role %r — not enforcing whitelist", role)
            self.allowed_tools = None
            return
        self.allowed_tools = set(whitelist)

    def is_cancelled(self) -> bool:
        """True once the user has clicked Stop on the running agent task."""
        return bool(self._cancel_check and self._cancel_check())

    def _wait_interruptible(self, event: threading.Event, timeout: float) -> bool:
        """Like ``event.wait(timeout)``, but returns early (False) the
        moment ``is_cancelled()`` becomes true, instead of blocking the
        agent thread for the full timeout after the user hit Stop."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if event.wait(timeout=0.25):
                return True
            if self.is_cancelled():
                return False
        return event.is_set()

    def respond_confirmation(self, accepted: bool) -> None:
        """Called from the main thread when the user clicks Allow/Deny on
        a non-file-write action-confirmation prompt (execute_command,
        delete_file, etc.)."""
        self._confirm_accepted = accepted
        self._confirm_event.set()

    def _request_confirmation(self, action: str, summary: str, is_new: bool = False) -> bool:
        """Ask the UI to confirm a side-effecting action that ISN'T
        already covered by diff-review, honoring the configured autonomy
        level. Returns True if the action should proceed.

        `is_new` should be True for actions that only create a brand-new
        path (e.g. writing a file that doesn't exist yet) — those are
        auto-approved under the 'new_files_only' autonomy level.
        """
        if self.autonomy == "never_ask":
            return True
        if self.autonomy == "new_files_only" and is_new:
            return True
        if self.is_cancelled():
            return False
        if not self._confirm_callback:
            # No UI wired up (e.g. headless use) — fail open so we don't
            # deadlock the caller, but log loudly so it's not silent.
            logger.warning("[agent] confirmation requested but no UI callback wired — allowing: %s", action)
            return True
        self._confirm_event.clear()
        self._confirm_accepted = None
        self._confirm_callback({"action": action, "summary": summary})
        ok = self._wait_interruptible(self._confirm_event, timeout=300)
        if not ok:
            return False
        return bool(self._confirm_accepted)

    def _guardian_review(self, tool_name: str, args: dict[str, Any]) -> None:
        """Run Guardian risk assessment and optional LLM review.
        Emits GUARDIAN_REVIEW event. May modify call.args in place on MODIFY.
        """
        if not hasattr(self, "_guardian_config") or self._guardian_config is None:
            return
        config = self._guardian_config
        if config.level == "off":
            return

        # Assess risk. Pass the RESOLVED command policy (base + user
        # ~/.tera_pilot/commands.json + project .tera_pilot/commands.json)
        # so the guardian's command-policy check reflects the effective
        # allow/deny lists. The old code passed a bare default
        # ``CommandPolicy()``, which only contains the base whitelist and
        # silently ignored user/project denylists.
        from tera_pilot.command_policy import CommandPolicy
        try:
            from tera_pilot.command_policy import get_global_policy
            policy = get_global_policy(str(self.workspace) if self.workspace else None)
        except Exception:
            policy = CommandPolicy()
        risk = assess_risk(
            tool_name=tool_name,
            args=args,
            workspace=str(self.workspace),
            command_policy=policy,
        )

        if config.level == "dangerous_only" and risk.level != "high":
            return
        if config.level == "all" and risk.level == "low":
            return

        # Build recent context from memory (AgentRuntime passes memory to tools)
        recent_context = ""
        if hasattr(self, "memory") and self.memory:
            from tera_pilot.agent.guardian import build_recent_context
            recent_context = build_recent_context(self.memory, max_messages=4, max_chars=2000)

        # Emit event with risk info (before LLM call)
        self._emit(
            AgentEvent.GUARDIAN_REVIEW,
            tool=tool_name,
            args=args,
            risk_level=risk.level,
            reasons=risk.reasons,
            guardian_verdict="PENDING",
            rationale="",
            suggested_args=None,
        )

        # Run LLM review using the guardian module's function
        import asyncio
        from tera_pilot.providers import ProviderMessage
        from tera_pilot.agent import CircuitBreakerRegistry, get_circuit_breaker_registry
        from tera_pilot.agent.guardian import review_with_llm, _parse_verdict, _looks_like_rate_limit

        # Load system prompt from template. The template lives at
        # tera_pilot/agent/templates/guardian.md — the old path computed
        # relative to this file (agent_runtime/tool_engine/…) never
        # existed, so every guardian LLM call silently fell back to the
        # generic one-line prompt instead of the full safety instructions.
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "agent", "templates", "guardian.md",
        )
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                system_prompt = f.read()
        except Exception as e:
            logger.warning("guardian: failed to load template %s: %s", template_path, e)
            system_prompt = "You are a safety reviewer. Return JSON: {verdict, rationale, suggested_args}."

        # Build user message
        user_data = {
            "tool": tool_name,
            "args": args,
            "risk_level": risk.level,
            "reasons": risk.reasons,
            "recent_context": recent_context,
        }
        user_prompt = json.dumps(user_data, ensure_ascii=False)

        # Get provider from registry - AgentRuntime sets this on tools
        provider = None
        if hasattr(self, "_provider") and self._provider is not None:
            provider = self._provider
        elif hasattr(self, "_registry") and self._registry is not None:
            provider = self._registry.active

        if provider is None:
            logger.warning("guardian: no provider available, defaulting to APPROVE")
            verdict = GuardianVerdict(
                verdict="APPROVE",
                rationale="No LLM provider available — defaulting to approve",
                suggested_args=None,
            )
        else:
            # Circuit breaker
            breaker_registry = get_circuit_breaker_registry()
            provider_id = getattr(self._registry, "active_id", "ollama") if hasattr(self, "_registry") and self._registry else "ollama"
            model = getattr(provider, "model", "auto") if hasattr(provider, "model") else "auto"
            key = f"{provider_id}/{model}"
            breaker = breaker_registry.get(key)
            if not breaker.try_claim():
                logger.warning("guardian: circuit breaker open for %s", key)
                verdict = GuardianVerdict(
                    verdict="REJECT",
                    rationale="Circuit breaker open — rate limited",
                    suggested_args=None,
                )
            else:
                try:
                    messages = [
                        ProviderMessage(role="system", content=system_prompt),
                        ProviderMessage(role="user", content=user_prompt),
                    ]
                    # provider.generate() is synchronous — call directly.
                    # Previously asyncio.run() was used on a sync method,
                    # which always raised TypeError and fell into the
                    # except block, defaulting to APPROVE (BUGS_REPORT TE-1).
                    response = provider.generate(messages)
                    raw = response.text or ""

                    # Parse JSON from response
                    verdict = _parse_verdict(raw)
                    if verdict is None:
                        logger.warning("guardian: failed to parse verdict from LLM, defaulting to APPROVE")
                        verdict = GuardianVerdict(
                            verdict="APPROVE",
                            rationale="LLM response unparseable — defaulting to approve",
                            suggested_args=None,
                        )
                    breaker.record(ok=True)
                except Exception as e:
                    logger.exception("guardian: LLM call failed: %s", e)
                    breaker.record(ok=False, rate_limited=_looks_like_rate_limit(e))
                    # Default behavior on error
                    verdict = GuardianVerdict(
                        verdict="APPROVE",
                        rationale=f"LLM error — defaulting to approve: {e}",
                        suggested_args=None,
                    )

        # Store pending args for UI to apply on "use_fix"
        if verdict.verdict == "MODIFY" and verdict.suggested_args:
            self._guardian_pending_args = verdict.suggested_args
        else:
            self._guardian_pending_args = None

        # Update call.args if MODIFY
        if verdict.verdict == "MODIFY" and verdict.suggested_args:
            args.clear()
            args.update(verdict.suggested_args)

        # Emit final event
        self._emit(
            AgentEvent.GUARDIAN_REVIEW,
            tool=tool_name,
            args=args,
            risk_level=risk.level,
            reasons=risk.reasons,
            guardian_verdict=verdict.verdict,
            rationale=verdict.rationale,
            suggested_args=verdict.suggested_args,
        )

        # If REJECT, raise to stop execution (will be caught in execute)
        if verdict.verdict == "REJECT":
            raise RuntimeError(f"Guardian rejected: {verdict.rationale}")

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path inside the workspace sandbox.

        SECURITY: the resolved path MUST live inside one of the allowed
        directories. We use `Path.is_relative_to()` (Python 3.9+) instead
        of string-prefix matching, because string-prefix matching lets
        `/home/user/work` succeed as a prefix of `/home/user/workbook`,
        which is a path-traversal vulnerability.

        v1.0.5-correctness: the old fallback for Python <3.9 used the
        vulnerable string-prefix match (`str(p).startswith(dp)`), which
        reintroduced exactly the bug the docstring warns about. We now
        use `os.path.commonpath()` which compares path COMPONENTS, not
        string characters — so `/home/user/work` is NOT considered a
        parent of `/home/user/workbook` (BUGS_REPORT M-RT-9).
        """
        p = (self.workspace / path).resolve()
        allowed = False
        for d in self._allowed_dirs:
            try:
                if p.is_relative_to(d):
                    allowed = True
                    break
            except AttributeError:
                # v1.0.6-security: removed vulnerable string-prefix fallback
                # (M-RT-9). Path.is_relative_to requires Python 3.9+.
                # If we get here, the runtime is too old — deny access.
                allowed = False
        if not allowed:
            raise PermissionError(f"Path outside workspace: {path}")
        return p

    def execute(self, call: ToolCall) -> str:
        start = time.monotonic()
        tool_name = call.name.value if isinstance(call.name, ToolName) else str(call.name)

        # --- Guardian pre-execution review ---
        try:
            self._guardian_review(tool_name, call.args)
        except RuntimeError as e:
            # Guardian REJECT — must abort, not swallow.
            # Previously all exceptions were caught here, which silently
            # approved rejected tool calls (BUGS_REPORT TE-2).
            call.error = str(e)
            call.duration_ms = 0
            return f"[GUARDIAN REJECT] {e}"
        except Exception as e:
            logger.warning("Guardian review failed (non-reject): %s", e)
        # --- End Guardian ---

        try:
            result = self._dispatch(call)
        except Exception as e:
            call.error = str(e)
            call.duration_ms = (time.monotonic() - start) * 1000
            # v1.2.0: record the exception as an activity entry too —
            # "[TOOL ERROR]" is treated as an error status by parse_status.
            err_str = f"[TOOL ERROR] {e}"
            try:
                self._activity_log.record_tool_call(
                    tool=tool_name,
                    args=call.args,
                    result=err_str,
                    duration_ms=int(call.duration_ms),
                    section=getattr(self, "section", "general"),
                    chat_id=self._current_chat_id,
                    iteration=self._current_iteration,
                )
            except Exception as log_err:
                logger.warning("[activity] failed to record tool error: %s", log_err)
            return err_str
        call.result = result[:self.MAX_OUTPUT]
        call.duration_ms = (time.monotonic() - start) * 1000
        # v1.2.0: record every tool execution in the activity log.
        # record_tool_call() derives category, status, title, path,
        # command, and diff_stat automatically — we just hand it the
        # raw ingredients.
        try:
            self._activity_log.record_tool_call(
                tool=tool_name,
                args=call.args,
                result=result,
                duration_ms=int(call.duration_ms),
                section=getattr(self, "section", "general"),
                chat_id=self._current_chat_id,
                iteration=self._current_iteration,
            )
        except Exception as log_err:
            # Logging must NEVER break tool execution — swallow errors.
            logger.warning("[activity] failed to record tool call: %s", log_err)
        return call.result

    def _dispatch(self, call: ToolCall) -> str:
        name = call.name
        args = call.args

        # v1.1.3-fix (bug 1.4): enforce role-based tool whitelist. When
        # `allowed_tools` is set (sub-agents), any tool NOT in the set is
        # rejected with a clear "[TOOL DENIED]" message. This makes the
        # "read-only by default" promise enforceable at the engine level
        # instead of relying solely on the system prompt.
        if self.allowed_tools is not None:
            tool_value = name.value if isinstance(name, ToolName) else str(name)
            if tool_value not in self.allowed_tools:
                logger.warning(
                    "[security] tool %r denied by role whitelist (allowed: %s)",
                    tool_value, sorted(self.allowed_tools),
                )
                return (
                    f"[TOOL DENIED] {tool_value} is not allowed for this "
                    f"sub-agent role. Allowed tools: "
                    f"{', '.join(sorted(self.allowed_tools))}"
                )

        # v1.1.0: defense in depth — even though PromptBuilder.system()
        # strips the spawn_* tools from the schema for non-heavy_code
        # sections, a model may still hallucinate a call. Reject it
        # explicitly with a clear error rather than executing.
        if name in (ToolName.SPAWN_SUBAGENT, ToolName.SPAWN_MULTI_AGENTS):
            if getattr(self, "section", "general") != "heavy_code":
                return (
                    f"[TOOL REJECTED] {name.value if isinstance(name, ToolName) else name} is only available in "
                    f"Heavy Code mode. Switch to Heavy Code section to use "
                    f"multi-agent capabilities."
                )

        # v1.2.0: defense in depth — office_* tools are gated to the
        # `office` section. The system prompt for general/heavy_code
        # doesn't advertise them, but a model may still hallucinate a
        # call. Reject it with a clear error.
        # v1.2.1-fix (review §4.5): name may be a plain str (for
        # dynamically-discovered MCP tools) — handle both forms.
        _name_value = name.value if isinstance(name, ToolName) else str(name)
        if _name_value.startswith("office_"):
            if getattr(self, "section", "general") != "office":
                return (
                    f"[TOOL REJECTED] {_name_value} is only available in "
                    f"Office Worker mode. Switch to the Office section to "
                    f"create or edit .docx / .xlsx / .pptx files."
                )

        dispatch_map = {
            ToolName.READ_FILE: lambda: self._read_file(args.get("path", "")),
            ToolName.WRITE_FILE: lambda: self._write_file(args.get("path", ""), args.get("content", "")),
            ToolName.RUN_CODE: lambda: self._run_code(
                args.get("code", ""), args.get("language", "python"),
                timeout=int(args.get("timeout", 180) or 180),
            ),
            ToolName.SEARCH_PROJECT: lambda: self._search_project(
                args.get("query", ""), args.get("directory", "."), args.get("file_pattern", "*.py")
            ),
            ToolName.LIST_FILES: lambda: self._list_files(args.get("directory", "."), args.get("pattern", "*")),
            ToolName.APPLY_DIFF: lambda: self._apply_diff(args.get("path", ""), args.get("diff", "")),
            ToolName.EXECUTE_COMMAND: lambda: self._execute_command(
                args.get("command", ""),
                timeout=int(args.get("timeout", 180) or 180),
            ),
            ToolName.GET_PROJECT_STRUCTURE: lambda: self._get_project_structure(args.get("directory", ".")),
            ToolName.DELETE_FILE: lambda: self._delete_file(args.get("path", "")),
            ToolName.RENAME_FILE: lambda: self._rename_file(args.get("old_path", ""), args.get("new_path", "")),
            ToolName.MKDIR: lambda: self._mkdir(args.get("path", "")),
            ToolName.READ_BINARY_FILE: lambda: self._read_binary_file(args.get("path", "")),
            ToolName.WRITE_BINARY_FILE: lambda: self._write_binary_file(args.get("path", ""), args.get("content", "")),
            ToolName.FILE_INFO: lambda: self._file_info(args.get("path", "")),
            ToolName.UNDO_WRITE: lambda: self._undo_write(args.get("path", "")),
            ToolName.STR_REPLACE: lambda: self._str_replace(
                args.get("path", ""),
                args.get("old_str", ""),
                args.get("new_str", ""),
                args.get("replace_all", False),
            ),
            # v1.0.11: git tools
            ToolName.GIT_STATUS: lambda: self._git_status(),
            ToolName.GIT_DIFF: lambda: self._git_diff(
                args.get("staged", False),
                args.get("path", ""),
            ),
            ToolName.GIT_STAGE: lambda: self._git_stage(args.get("paths", [])),
            ToolName.GIT_COMMIT: lambda: self._git_commit(
                args.get("message", ""),
                args.get("paths", []),
            ),
            # v1.0.11: skill tool
            ToolName.GET_SKILL: lambda: self._get_skill(args.get("id", "")),
            # v1.1.0: MCP tool — proxy to the MCPManager
            ToolName.CALL_MCP_TOOL: lambda: self._call_mcp_tool(
                args.get("server", ""),
                args.get("tool", ""),
                args.get("args", {}) or {},
            ),
            # v1.1.0: subagent — single child agent for a sub-task
            ToolName.SPAWN_SUBAGENT: lambda: self._spawn_subagent(
                args.get("goal", ""),
                args.get("role", "generalist"),
                args.get("max_iterations", 4),
            ),
            # v1.1.0: multi-agent — N child agents in parallel
            ToolName.SPAWN_MULTI_AGENTS: lambda: self._spawn_multi_agents(
                args.get("tasks", []) or [],
            ),
            # v1.2.0: Office Worker tools — all delegate to the lazy
            # OfficeWorker instance. The dispatch wrapper handles the
            # "office_*" section gate above; here we just route to the
            # matching OfficeWorker method.
            ToolName.OFFICE_CREATE: lambda: self._office_dispatch(
                "create", path=args.get("path", ""),
                template=args.get("template", "blank"),
            ),
            ToolName.OFFICE_VIEW: lambda: self._office_dispatch(
                "view", path=args.get("path", ""),
                mode=args.get("mode", "outline"),
            ),
            ToolName.OFFICE_ADD_PARAGRAPH: lambda: self._office_dispatch(
                "add_paragraph", path=args.get("path", ""),
                text=args.get("text", ""),
                style=args.get("style", "Normal"),
                bold=args.get("bold", False),
                italic=args.get("italic", False),
                color=args.get("color"),
                size=args.get("size"),
            ),
            ToolName.OFFICE_ADD_HEADING: lambda: self._office_dispatch(
                "add_heading", path=args.get("path", ""),
                text=args.get("text", ""),
                level=args.get("level", 1),
            ),
            ToolName.OFFICE_ADD_TABLE: lambda: self._office_dispatch(
                "add_table", path=args.get("path", ""),
                rows=args.get("rows", 1),
                cols=args.get("cols", 1),
                data=args.get("data"),
                header=args.get("header", True),
            ),
            ToolName.OFFICE_FILL_TABLE: lambda: self._office_dispatch(
                "fill_table", path=args.get("path", ""),
                table_index=args.get("table_index", 1),
                data=args.get("data") or [],
            ),
            ToolName.OFFICE_ADD_SHEET: lambda: self._office_dispatch(
                "add_sheet", path=args.get("path", ""),
                name=args.get("name", "Sheet"),
            ),
            ToolName.OFFICE_SET_CELL: lambda: self._office_dispatch(
                "set_cell", path=args.get("path", ""),
                sheet=args.get("sheet", "Sheet1"),
                cell=args.get("cell", "A1"),
                value=args.get("value"),
            ),
            ToolName.OFFICE_SET_CELL_FORMAT: lambda: self._office_dispatch(
                "set_cell_format", path=args.get("path", ""),
                sheet=args.get("sheet", "Sheet1"),
                cell=args.get("cell", "A1"),
                bold=args.get("bold"),
                italic=args.get("italic"),
                font_color=args.get("font_color") or args.get("color"),
                bg_color=args.get("bg_color") or args.get("background"),
                font_size=args.get("font_size") or args.get("size"),
                align=args.get("align"),
            ),
            ToolName.OFFICE_ADD_CHART: lambda: self._office_dispatch(
                "add_chart", path=args.get("path", ""),
                sheet=args.get("sheet", "Sheet1"),
                chart_type=args.get("chart_type", "bar"),
                data_range=args.get("data_range", "A1:B10"),
                anchor=args.get("anchor", "D2"),
                title=args.get("title"),
            ),
            ToolName.OFFICE_FILL_SHEET: lambda: self._office_dispatch(
                "fill_sheet", path=args.get("path", ""),
                sheet=args.get("sheet", "Sheet1"),
                data=args.get("data") or [],
                start_cell=args.get("start_cell", "A1"),
            ),
            ToolName.OFFICE_ADD_SLIDE: lambda: self._office_dispatch(
                "add_slide", path=args.get("path", ""),
                layout=args.get("layout", "title"),
                title=args.get("title"),
                subtitle=args.get("subtitle"),
            ),
            ToolName.OFFICE_ADD_TEXT: lambda: self._office_dispatch(
                "add_text", path=args.get("path", ""),
                slide=args.get("slide", 1),
                text=args.get("text", ""),
                x=args.get("x", "1in"),
                y=args.get("y", "1in"),
                w=args.get("w", "8in"),
                h=args.get("h", "1in"),
                bold=args.get("bold", False),
                italic=args.get("italic", False),
                color=args.get("color"),
                size=args.get("size"),
                align=args.get("align"),
            ),
            ToolName.OFFICE_ADD_SHAPE: lambda: self._office_dispatch(
                "add_shape", path=args.get("path", ""),
                slide=args.get("slide", 1),
                shape_type=args.get("shape_type", "rectangle"),
                x=args.get("x", "1in"),
                y=args.get("y", "1in"),
                w=args.get("w", "2in"),
                h=args.get("h", "1in"),
                text=args.get("text"),
                fill_color=args.get("fill_color") or args.get("fill"),
                line_color=args.get("line_color") or args.get("line"),
            ),
            ToolName.OFFICE_FIND_REPLACE: lambda: self._office_dispatch(
                "find_replace", path=args.get("path", ""),
                find=args.get("find", ""),
                replace=args.get("replace", ""),
                sheet=args.get("sheet"),
                slide=args.get("slide"),
            ),
            ToolName.OFFICE_SAVE_AS: lambda: self._office_dispatch(
                "save_as", path=args.get("path", ""),
                new_path=args.get("new_path", ""),
            ),
            # v1.2.0: self_verify — runs a verification pass at task
            # close. Re-reads touched files and asks the LLM whether
            # the stated goal is met. Returns OK or a list of gaps.
            ToolName.SELF_VERIFY: lambda: self._self_verify(
                args.get("goal", ""),
                args.get("touched_files") or [],
                mode=args.get("mode", "re_read"),
                run_tests=args.get("run_tests", False),
            ),
            # v1.2.1-fix (review §4.2): explicit watchdog probe. The
            # agent calls this between spawn_multi_agents waves to get
            # a typed evidence string about whether any sub-agents are
            # stalled or repeating. Useful when the orchestrator wants
            # to decide whether to wait, escalate, or proceed without
            # the missing result.
            ToolName.WATCHDOG_CHECK: lambda: self._watchdog_check(),
            # v1.2.1-fix (review §4.4): agentic-search — model-driven
            # grep/glob. These complement the heuristic file auto-attach
            # in ContextManager (which only sees files indexed at
            # set_root time). For monorepos, dynamically-created files,
            # or non-typical layouts, the agent can now actively search
            # instead of waiting for the right file to be auto-attached.
            ToolName.GREP: lambda: self._grep(
                args.get("pattern", ""),
                path=args.get("path", "."),
                include=args.get("include") or "*.py",
                max_results=int(args.get("max_results", 50) or 50),
                case_sensitive=bool(args.get("case_sensitive", False)),
            ),
            ToolName.GLOB: lambda: self._glob(
                args.get("pattern", "*"),
                path=args.get("path", "."),
                max_results=int(args.get("max_results", 100) or 100),
            ),
            # v2.0.0: Progressive tool disclosure — search tool catalog
            ToolName.SEARCH_TOOLS: lambda: self._search_tools_handler(args),
            # v2.1.0 (G18): web search/fetch. Available in ALL sections
            # (general, heavy_code, office) — same visibility rule as
            # call_mcp_tool. web_search routes through MCPManager with
            # ordered fallback; web_fetch is a direct HTTP GET + HTML-
            # to-text extraction. Both wrap their output as
            # <context_fragment type="web_*"> so the result participates
            # in tombstone-compaction AND is tagged as untrusted
            # external content (Guardian + downstream consumers can
            # distinguish injected instructions from user commands).
            ToolName.WEB_SEARCH: lambda: self._web_search(
                args.get("query", ""),
                num_results=int(args.get("num_results", 5) or 5),
            ),
            ToolName.WEB_FETCH: lambda: self._web_fetch(
                args.get("url", ""),
                max_chars=int(args.get("max_chars", 8000) or 8000),
            ),
        }

        if name not in dispatch_map:
            # v1.2.1-fix (review §4.5): typed MCP tools are routed by
            # name prefix ``mcp__`` — they're not in ToolName enum
            # (since they're dynamically discovered at runtime), so
            # we handle them here as a special case before giving up.
            tool_value = name.value if isinstance(name, ToolName) else str(name)
            if tool_value.startswith("mcp__"):
                return self._call_typed_mcp_tool(tool_value, args)
            if tool_value == "list_mcp_tools":
                return self._list_mcp_tools(args)
            raise ValueError(f"Unknown tool: {name}")

        return dispatch_map[name]()

    def _read_file(self, path: str) -> str:
        p = self._resolve_path(path)
        if not p.exists():
            return f"[FILE NOT FOUND] {path}"
        self._mark_context_accessed(path)
        size = p.stat().st_size
        if size > 200_000:
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            preview = "\n".join(lines[:500])
            return f"[FILE LARGE: {size} bytes, {len(lines)} lines — showing first 500 lines]\n{preview}"
        return p.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _mark_context_accessed(path: str) -> None:
        """v1.1.4-fix (bug 4.2): boost a file's relevance score in
        ContextManager whenever the agent actually reads/writes it, so
        files the agent is actively working on stay auto-attached on
        later iterations. Best-effort — never let this break a tool call.
        """
        try:
            rel = path.lstrip("./").lstrip(".\\")
            get_context_manager().mark_accessed(rel)
        except Exception:
            pass

    def _write_file(self, path: str, content: str) -> str:
        p = self._resolve_path(path)

        # v1.1.5-fix (tera_pilot_bug_report.md bug #1): when diff review is
        # disabled, autonomy settings were ignored entirely — file
        # writes happened with no gate at all. The UI explicitly
        # promises "Diff review + autonomy settings still apply"
        # (web/index.html:481), so when diff_review is off we must
        # still honor autonomy via _request_confirmation, mirroring
        # write_binary_file's existing logic. Without this, a user
        # who disables the diff review popup (just to skip the modal)
        # but leaves autonomy on `always_ask` silently loses all
        # confirmation on every write.
        if not self.diff_review_enabled:
            is_new = not p.exists()
            if not self._request_confirmation(
                "write_file",
                f"{'Create' if is_new else 'Overwrite'} file: {path}",
                is_new=is_new,
            ):
                return f"[REJECTED BY USER] {path} — write cancelled"

        # v1.0.4: diff-review — pause and ask UI if enabled.
        # v1.0.5-correctness: fail-open when no UI callback is wired
        # (headless mode, test harness, CLI use). Previously the wait
        # would block for 300 s and then return [CANCELLED], silently
        # breaking every write in headless mode (BUGS_REPORT H-RT-4).
        if self.diff_review_enabled and p.exists() and p.is_file():
            original = p.read_text(encoding="utf-8", errors="replace")
            diff_text = _compute_diff_text(path, original, content)
            if diff_text:  # only ask if there are actual changes
                if self._diff_review_callback is None:
                    # No UI wired — fail open (mirror _request_confirmation's
                    # behaviour at line ~516). Log loudly so it's not silent.
                    logger.warning(
                        "[agent] diff-review requested for %s but no UI callback "
                        "wired — applying write (headless mode)", path,
                    )
                else:
                    self._diff_review_event.clear()
                    self._diff_review_accepted = None
                    # Lines added/removed for summary
                    added = sum(1 for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++"))
                    removed = sum(1 for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---"))
                    self._diff_review_callback({
                        "path": path,
                        "diff": diff_text,
                        "original": original,
                        "proposed": content,
                        "lines_added": added,
                        "lines_removed": removed,
                    })
                    # Block agent thread until user responds (interruptible by Stop)
                    if not self._wait_interruptible(self._diff_review_event, timeout=300):
                        return f"[CANCELLED] {path} — write cancelled (agent stopped)"
                    if not self._diff_review_accepted:
                        return f"[REJECTED BY USER] {path} — write cancelled"

        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.is_file():
            _backup_file_func(self._backup_dir, self._MAX_BACKUPS, p)
        p.write_text(content, encoding="utf-8")
        self._mark_context_accessed(path)
        # v1.2.0: track for self_verify
        if path not in self._touched_files:
            self._touched_files.append(path)
        return f"[WRITTEN] {path} ({len(content)} chars)"

    def respond_diff_review(self, accepted: bool) -> None:
        """Called from the main thread when user clicks Apply/Reject."""
        self._diff_review_accepted = accepted
        self._diff_review_event.set()

    # ── v1.0.5: str_replace ────────────────────────────────────────
    # Implements §3.1 of качество_кода_llm.md ("правки, а не полная
    # перезапись файла"). The model must specify the *exact* unique
    # snippet to replace; if the snippet is not found or is ambiguous,
    # the tool returns an error and the model is forced to re-read the
    # file and try again — this is the deterministic verification the
    # document calls for.
    def _str_replace(self, path: str, old_str: str, new_str: str,
                     replace_all: bool = False) -> str:
        if not old_str:
            return "[STR_REPLACE ERROR] old_str is empty — refusing no-op"
        p = self._resolve_path(path)
        if not p.exists() or not p.is_file():
            return f"[FILE NOT FOUND] {path}"

        # v1.1.5-fix (tera_pilot_bug_report.md bug #1): same rationale as
        # _write_file — when diff review is disabled, autonomy must
        # still gate the edit. str_replace only operates on existing
        # files (we just checked), so is_new is always False here.
        # The autonomy levels then decide: `never_ask` auto-approves,
        # `new_files_only` asks (because this edits an existing file),
        # `always_ask` asks.
        if not self.diff_review_enabled:
            if not self._request_confirmation(
                "str_replace",
                f"Edit file: {path}",
                is_new=False,
            ):
                return f"[REJECTED BY USER] {path} — str_replace cancelled"

        original = p.read_text(encoding="utf-8", errors="replace")

        occurrences = original.count(old_str)
        if occurrences == 0:
            # The model is hallucinating the surrounding context —
            # return a clear, actionable error so it can re-read the
            # file and localise the change correctly.
            hint = _str_replace_hint(original, old_str)
            return (
                f"[STR_REPLACE ERROR] old_str not found in {path}. "
                f"Re-read the file, then retry with a verbatim snippet. "
                f"{hint}"
            )
        if occurrences > 1 and not replace_all:
            return (
                f"[STR_REPLACE ERROR] old_str is not unique ({occurrences} matches) "
                f"in {path}. Either include more surrounding context to make it "
                f"unique, or pass replace_all=true to replace every match."
            )

        # Apply the replacement.
        if replace_all:
            patched = original.replace(old_str, new_str)
        else:
            # Replace only the first occurrence (str.replace would also
            # do all — we already verified uniqueness above).
            patched = original.replace(old_str, new_str, 1)

        # Diff-review gate — same path as write_file (only if changed).
        # v1.0.5-correctness: fail-open when no UI callback is wired
        # (BUGS_REPORT H-RT-4). Also: the return value of
        # _wait_interruptible was being discarded, so on a 5-minute
        # timeout (no cancel, no response) we'd fall through to
        # `if not self._diff_review_accepted` and return the misleading
        # "[REJECTED BY USER]" message even though the user never
        # rejected anything. We now honour the timeout explicitly.
        if self.diff_review_enabled and patched != original:
            diff_text = _compute_diff_text(path, original, patched)
            if diff_text:
                if self._diff_review_callback is None:
                    # No UI wired — fail open (headless mode).
                    logger.warning(
                        "[agent] diff-review requested for %s but no UI callback "
                        "wired — applying str_replace (headless mode)", path,
                    )
                else:
                    self._diff_review_event.clear()
                    self._diff_review_accepted = None
                    added = sum(1 for l in diff_text.splitlines()
                                if l.startswith("+") and not l.startswith("+++"))
                    removed = sum(1 for l in diff_text.splitlines()
                                  if l.startswith("-") and not l.startswith("---"))
                    self._diff_review_callback({
                        "path": path,
                        "diff": diff_text,
                        "original": original,
                        "proposed": patched,
                        "lines_added": added,
                        "lines_removed": removed,
                    })
                    ok = self._wait_interruptible(self._diff_review_event, timeout=300)
                    if not ok or self.is_cancelled():
                        return f"[CANCELLED] {path} — str_replace cancelled (agent stopped)"
                    if self._diff_review_accepted is None:
                        # Timeout reached with no response and no cancel.
                        return f"[TIMEOUT] {path} — str_replace cancelled (no response within 300s)"
                    if not self._diff_review_accepted:
                        return f"[REJECTED BY USER] {path} — str_replace cancelled"

        # Backup + atomic write.
        _backup_file_func(self._backup_dir, self._MAX_BACKUPS, p)
        p.write_text(patched, encoding="utf-8")
        self._mark_context_accessed(path)
        # v1.2.0: track for self_verify
        if path not in self._touched_files:
            self._touched_files.append(path)
        n_replaced = occurrences if replace_all else 1
        return (
            f"[STR_REPLACE] {path} — replaced {n_replaced} occurrence(s), "
            f"{len(patched) - len(original):+d} chars"
        )

    # _str_replace_hint and _compute_diff_text are imported from ..diff_utils
    # (see top-of-file imports). The local redefinitions that previously
    # shadowed them have been removed — they were missing the difflib import
    # and were identical to the diff_utils versions anyway.

    def _backup_file(self, p: Path) -> Path:
        """Create a timestamped backup of *path* in the backup directory.
        
        v1.0.6: enforces a maximum backup count (M-RT-4).
        """
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        # v1.0.6: prune old backups if over the cap (M-RT-4)
        try:
            existing = sorted(self._backup_dir.iterdir(),
                               key=lambda f: f.stat().st_mtime)
            while len(existing) > self._MAX_BACKUPS:
                oldest = existing.pop(0)
                try:
                    oldest.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        ts = str(int(time.time()))
        h = hashlib.md5(str(p).encode()).hexdigest()[:8]
        backup_name = f"{h}_{ts}_{p.name}"
        backup_path = self._backup_dir / backup_name
        backup_path.write_bytes(p.read_bytes())
        return backup_path

    def _delete_file(self, path: str) -> str:
        p = self._resolve_path(path)
        # v1.1.1: never allow deleting the workspace root itself — a path
        # like "." or "" resolves to the workspace, which technically
        # passes _resolve_path's "inside the sandbox" check (a directory
        # is trivially "relative to" itself), but wiping the whole project
        # is never a reasonable single tool call.
        if p == self.workspace:
            return "[REFUSED] Refusing to delete the workspace root itself."
        if not p.exists():
            return f"[FILE NOT FOUND] {path}"
        kind = "directory" if p.is_dir() else "file"
        if not self._request_confirmation("delete_file", f"Delete {kind}: {path}"):
            return f"[REJECTED BY USER] {path} — delete cancelled"
        if p.is_dir():
            shutil.rmtree(p)
            return f"[DELETED DIR] {path}"
        p.unlink()
        return f"[DELETED] {path}"

    def _rename_file(self, old_path: str, new_path: str) -> str:
        old = self._resolve_path(old_path)
        new = self._resolve_path(new_path)
        if old == self.workspace or new == self.workspace:
            return "[REFUSED] Refusing to rename the workspace root itself."
        if not old.exists():
            return f"[FILE NOT FOUND] {old_path}"
        if not self._request_confirmation("rename_file", f"Rename: {old_path} → {new_path}"):
            return f"[REJECTED BY USER] {old_path} — rename cancelled"
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        return f"[RENAMED] {old_path} → {new_path}"

    def _mkdir(self, path: str) -> str:
        p = self._resolve_path(path)
        p.mkdir(parents=True, exist_ok=True)
        return f"[MKDIR] {path}"

    def _read_binary_file(self, path: str) -> str:
        p = self._resolve_path(path)
        if not p.exists():
            return f"[FILE NOT FOUND] {path}"
        size = p.stat().st_size
        if size > 10_000_000:
            return f"[FILE TOO LARGE: {size} bytes — max 10MB for binary]"
        data = p.read_bytes()
        return base64.b64encode(data).decode("utf-8")

    def _write_binary_file(self, path: str, content: str) -> str:
        p = self._resolve_path(path)
        is_new = not p.exists()
        if not is_new and not self._request_confirmation("write_binary_file", f"Overwrite binary file: {path}"):
            return f"[REJECTED BY USER] {path} — write cancelled"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.is_file():
            _backup_file_func(self._backup_dir, self._MAX_BACKUPS, p)
        data = base64.b64decode(content)
        p.write_bytes(data)
        return f"[WRITTEN BINARY] {path} ({len(data)} bytes)"



    def _file_info(self, path: str) -> str:
        p = self._resolve_path(path)
        if not p.exists():
            return f"[FILE NOT FOUND] {path}"
        stat = p.stat()
        info = {
            "path": str(p),
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "created": stat.st_ctime,
            "is_file": p.is_file(),
            "is_dir": p.is_dir(),
        }
        return json.dumps(info, indent=2)

    def _undo_write(self, path: str) -> str:
        p = self._resolve_path(path)
        h = hashlib.md5(str(p).encode()).hexdigest()[:8]
        candidates = sorted(self._backup_dir.glob(f"{h}_*_{p.name}"), reverse=True)
        if not candidates:
            return f"[NO BACKUP] {path}"
        latest = candidates[0]
        p.write_bytes(latest.read_bytes())
        return f"[UNDO] {path} restored from {latest.name}"

    # ── v1.0.11: Git tools — direct project access ──
    # These give the agent the ability to inspect git state, see diffs,
    # stage files, and commit changes — without asking the user to run
    # git commands manually. The agent wraps the existing GitService
    # (tera_pilot/git_service.py), which itself shells out to the git CLI.
    # All git operations are sandboxed to the workspace root.

    def _get_git_service(self):
        """Lazily create a GitService for the current workspace.

        Returns None if the workspace is not a git repo (so the agent
        gets a clear error instead of a crash).
        """
        if not self.workspace or not self.workspace.is_dir():
            return None
        try:
            from .git_service import GitService
            git = GitService(str(self.workspace))
            if not git.is_available:
                return None
            return git
        except Exception as e:
            logger.warning(f"[git] failed to init GitService: {e}")
            return None

    def _git_status(self) -> str:
        """Show working tree status: branch, ahead/behind, modified files."""
        git = self._get_git_service()
        if not git:
            return "[GIT ERROR] not a git repository (or git not installed)"
        status = git.status()
        # Format as human-readable text for the agent
        lines = [
            f"Branch: {status.get('branch', 'unknown')}",
            f"Ahead: {status.get('ahead', 0)}, Behind: {status.get('behind', 0)}",
        ]
        files = status.get("files", [])
        if files:
            lines.append(f"Changed files ({len(files)}):")
            for f in files[:50]:  # cap to avoid huge output
                lines.append(f"  {f.get('status', '?')} {f.get('path', '')}")
        else:
            lines.append("Working tree clean.")
        return "\n".join(lines)

    def _git_diff(self, staged: bool = False, path: str = "") -> str:
        """Show git diff. If staged=True, show staged (cached) diff.
        If path is given, show diff for that file only."""
        git = self._get_git_service()
        if not git:
            return "[GIT ERROR] not a git repository"
        try:
            # v2.4.0-security: validate the pathspec against the workspace
            # sandbox like _git_stage/_git_commit do. `git diff -- <path>`
            # resolves pathspecs relative to the repo root / cwd, so an
            # unvalidated `../outside` or an absolute path could show
            # diffs of files outside the workspace (e.g. when the
            # workspace is a subdirectory of a larger repo).
            if path:
                try:
                    resolved = self._resolve_path(path)
                    path = str(resolved.relative_to(self.workspace))
                except PermissionError:
                    return f"[GIT ERROR] path outside workspace: {path}"
            diff = git.diff(staged=staged, file_path=path if path else None)
            if not diff:
                return "[GIT] no changes (empty diff)"
            # Cap to MAX_OUTPUT chars to avoid blowing the context window
            if len(diff) > self.MAX_OUTPUT * 4:
                diff = diff[:self.MAX_OUTPUT * 4] + "\n... (diff truncated)"
            return diff
        except Exception as e:
            return f"[GIT ERROR] {e}"

    def _git_stage(self, paths) -> str:
        """Stage files. paths is a list of relative paths.
        If empty, stages all changes (git add -A)."""
        git = self._get_git_service()
        if not git:
            return "[GIT ERROR] not a git repository"
        try:
            if not paths:
                ok = git.stage_all()
                return "[GIT] staged all changes" if ok else "[GIT ERROR] stage_all failed"
            # Validate paths are inside workspace
            validated = []
            for p in paths:
                try:
                    resolved = self._resolve_path(p)
                    validated.append(str(resolved.relative_to(self.workspace)))
                except PermissionError:
                    return f"[GIT ERROR] path outside workspace: {p}"
            ok = git.stage(validated)
            if ok:
                return f"[GIT] staged {len(validated)} file(s): {', '.join(validated)}"
            return "[GIT ERROR] stage failed"
        except Exception as e:
            return f"[GIT ERROR] {e}"

    def _git_commit(self, message: str, paths=None) -> str:
        """Commit staged changes (or stage given paths first, then commit).
        message is required — never commit with an empty message."""
        git = self._get_git_service()
        if not git:
            return "[GIT ERROR] not a git repository"
        if not message or not message.strip():
            return "[GIT ERROR] commit message is required (never commit with empty message)"
        if not self._request_confirmation("git_commit", f"Commit: {message.strip()[:80]}"):
            return "[REJECTED BY USER] commit cancelled"
        try:
            # If paths given, stage them first
            if paths:
                validated = []
                for p in paths:
                    try:
                        resolved = self._resolve_path(p)
                        validated.append(str(resolved.relative_to(self.workspace)))
                    except PermissionError:
                        return f"[GIT ERROR] path outside workspace: {p}"
                git.stage(validated)
            result = git.commit(message.strip())
            if result.get("ok"):
                return f"[GIT COMMIT] {result.get('hash', '?')[:8]} — {message.strip()[:80]}"
            return f"[GIT ERROR] commit failed: {result.get('error', 'unknown')}"
        except Exception as e:
            return f"[GIT ERROR] {e}"

    # ── v1.0.11: Skill tool ──────────────────────────────────────────
    # The agent calls get_skill(id) to pull the full body of a skill
    # into context. The skill catalog (id + name + description) is
    # already in the system prompt, so the agent knows what's available
    # without consuming context tokens for the full bodies.

    def _get_skill(self, skill_id: str) -> str:
        """Return the full body of a skill by id."""
        if not skill_id:
            # List available skills if no id given
            if not self._skills:
                return "[SKILL] no skills available"
            lines = ["[SKILL] available skills:"]
            for s in self._skills:
                lines.append(f"  - {s.id}: {s.name} — {s.description[:80]}")
            return "\n".join(lines)
        for s in self._skills:
            if s.id == skill_id:
                # v1.2.0: track skill activation in activity log
                try:
                    self._activity_log.record(
                        category=CATEGORY_INFO,
                        kind="skill_activated",
                        tool="get_skill",
                        title=f"Activated skill: {s.name}",
                        summary=s.description[:200],
                        status=STATUS_OK,
                        section=self.section,
                        chat_id=self._current_chat_id,
                        meta={"skill_id": skill_id, "tag": s.tag, "project_level": s.project_level},
                    )
                except Exception:
                    pass
                return f"[SKILL: {s.id}]\n{s.body}"
        return (
            f"[SKILL ERROR] no skill with id {skill_id!r}. "
            f"Available: {', '.join(s.id for s in self._skills) or 'none'}"
        )

    # ── v1.1.0: MCP + multi-agent tools ────────────────────────────

    def _call_mcp_tool(self, server: str, tool: str,
                       args: Dict[str, Any]) -> str:
        """Invoke a tool on an MCP server via the MCPManager singleton.

        The MCP server must be configured in ~/.tera_pilot/mcp.json and running.
        The agent sees the available MCP tools in the system prompt
        (injected by MCPManager.catalog_prompt()) and calls this meta-tool
        with (server, tool, args). The result is returned as the
        observation.

        v1.1.3-fix (bug 1.3): MCP tools ARE subject to the autonomy
        confirmation gate. Previously the comment below said "we trust
        the user's MCP server config", but that ignored the reality
        that popular MCP servers (filesystem, github, browser) expose
        write_file/delete_file/create_pull_request/push/navigate — all
        side-effecting operations that bypassed the confirm dialog
        applied to native write_file/execute_command. A prompt-injection
        in any file the agent reads could trigger e.g.
        ``call_mcp_tool("filesystem", "write_file", {"path": "/etc/cron.d/...", "content": "..."})``
        with NO user prompt. We now route every call_mcp_tool through
        _request_confirmation(), unless the server is explicitly marked
        ``"trusted": true`` in mcp.json.
        """
        if not server or not tool:
            return (
                "[MCP ERROR] both 'server' and 'tool' are required. "
                "Use the catalog in the system prompt to pick a server+tool."
            )
        # v1.1.3-fix (bug 3.7): validate args type. JSON-RPC 2.0 allows
        # params as an array, but MCP requires `arguments` to be an object.
        # Most servers return "invalid params" without context; we surface
        # a clearer error before the round-trip.
        if not isinstance(args, dict):
            return (
                f"[MCP ERROR] args must be a JSON object, got "
                f"{type(args).__name__} — wrap arguments in {{}}."
            )
        # v1.1.3-fix (bug 1.3): confirmation gate. Check if the server is
        # explicitly trusted via mcp.json; if not, ask the user (subject
        # to the autonomy level).
        try:
            from .mcp_manager import get_mcp_manager
            manager = get_mcp_manager()
            trusted = manager.is_server_trusted(server)
        except Exception:
            trusted = False
        if not trusted:
            summary = f"MCP {server}.{tool}({json.dumps(args, default=str)[:120]})"
            if not self._request_confirmation("call_mcp_tool", summary):
                return f"[REJECTED BY USER] MCP {server}.{tool} cancelled"
        try:
            # Lazy import to avoid circular dependency at module load time
            from .mcp_manager import get_mcp_manager
            manager = get_mcp_manager()
            result = manager.call_tool(server, tool, args)
            # Truncate to MAX_OUTPUT for context budget
            if len(result) > self.MAX_OUTPUT:
                result = result[:self.MAX_OUTPUT] + f"\n... [truncated, {len(result)} total chars]"
            return f"[MCP {server}.{tool}]\n{result}"
        except Exception as e:
            return f"[MCP ERROR] {server}.{tool} failed: {e}"

    # ── v1.2.1-fix (review §4.5): typed MCP dispatch ────────────────

    def _call_typed_mcp_tool(self, typed_name: str,
                              args: Dict[str, Any]) -> str:
        """Dispatch a call to a typed MCP tool (``mcp__<server>__<tool>``).

        This is the v1.2.1 path that lets the model call each MCP tool
        by its OWN name (with a typed args schema) instead of routing
        everything through the ``call_mcp_tool(server, tool, args)``
        meta-tool. We delegate the actual server lookup + invocation
        to ``MCPManager.call_typed_tool`` — here we only handle the
        autonomy confirmation gate (same as _call_mcp_tool) and result
        truncation.
        """
        if not isinstance(args, dict):
            return (
                f"[MCP ERROR] args must be a JSON object, got "
                f"{type(args).__name__} — wrap arguments in {{}}."
            )
        try:
            from .mcp_manager import get_mcp_manager
            manager = get_mcp_manager()
            # Look up the server for this typed name so we can apply
            # the trusted / autonomy gate consistently with the legacy
            # call_mcp_tool path. If we can't find it, manager.call_typed_tool
            # will return the error.
            server_name = None
            catalog = manager.tool_catalog()
            for server_name_iter, tool in catalog:
                safe_server = re.sub(r"[^A-Za-z0-9_]", "_", server_name_iter)
                safe_tool = re.sub(r"[^A-Za-z0-9_]", "_", tool.name)
                if typed_name == f"mcp__{safe_server}__{safe_tool}":
                    server_name = server_name_iter
                    break
            if server_name is None:
                return (
                    f"[MCP ERROR] no typed MCP tool matches {typed_name!r}. "
                    f"Use list_mcp_tools to see the available names."
                )
            # Apply the same trusted/autonomy gate as call_mcp_tool.
            trusted = manager.is_server_trusted(server_name)
            if not trusted:
                summary = f"MCP {typed_name}({json.dumps(args, default=str)[:120]})"
                if not self._request_confirmation("call_mcp_tool", summary):
                    return f"[REJECTED BY USER] MCP {typed_name} cancelled"
            result = manager.call_typed_tool(typed_name, args)
            if len(result) > self.MAX_OUTPUT:
                result = result[:self.MAX_OUTPUT] + f"\n... [truncated, {len(result)} total chars]"
            return f"[MCP {typed_name}]\n{result}"
        except Exception as e:
            return f"[MCP ERROR] {typed_name} failed: {e}"

    def _list_mcp_tools(self, args: Dict[str, Any]) -> str:
        """v1.2.1-fix (review §4.5): list MCP tools available via the
        typed ``mcp__*`` namespace, with pagination.

        Used by the agent when the typed catalog injected into the
        system prompt was truncated (more than
        ``MCPManager.DEFAULT_TYPED_CATALOG_MAX`` tools). The agent
        calls this to discover the rest on demand — the same lazy-
        loading pattern mature coding agents use.
        """
        try:
            from .mcp_manager import get_mcp_manager
            manager = get_mcp_manager()
            offset = int(args.get("offset", 0) or 0)
            limit = int(args.get("limit", 100) or 100)
            tools = manager.list_typed_tool_names(offset=offset, limit=limit)
            if not tools:
                return (
                    "[LIST_MCP_TOOLS] no MCP tools available. "
                    "Configure servers in Settings → MCP."
                )
            lines = [f"[LIST_MCP_TOOLS] {len(tools)} tool(s) (offset={offset}):"]
            for t in tools:
                lines.append(f"  - {t['name']}: {t['description']}")
            return "\n".join(lines)
        except Exception as e:
            return f"[LIST_MCP_TOOLS ERROR] {e}"

    def _spawn_subagent(self, goal: str, role: str = "generalist",
                        max_iterations: int = 4,
                        provider_override: Optional[str] = None,
                        model_override: Optional[str] = None) -> str:
        """Spawn a single sub-agent for a sub-task (orchestrator-worker
        pattern). The sub-agent runs in its own AgentRuntime instance
        with a narrower scope and returns its final answer as the
        observation.

        role: "generalist" | "architect" | "implementer" | "reviewer" | "tester"
        max_iterations: how many tool-call iterations the sub-agent gets
                       (default 4 — much less than the parent's 8-30)

        Sub-agents share the parent's workspace and provider registry.
        They are read-only by default (no write tools) to prevent
        uncontrolled side effects. Pass role="implementer" to allow
        writes (still subject to the parent's autonomy setting).

        G20b: ``provider_override`` / ``model_override`` let the caller
        route this specific sub-agent to a DIFFERENT model than the
        parent's active one. Used by the task-decomposition router to
        place each subtask on whichever configured model is best suited
        for it. When both are None, the sub-agent uses the parent's
        active provider/model (today's behavior).
        """
        if not goal or not goal.strip():
            return "[SUBAGENT ERROR] goal is required"
        try:
            return self._run_subagent_internal(
                goal=goal.strip(),
                role=role or "generalist",
                max_iterations=int(max_iterations or 4),
                label="subagent",
                provider_override=provider_override,
                model_override=model_override,
            )
        except Exception as e:
            return f"[SUBAGENT ERROR] {e}"

    # v1.2.1-fix (review §4.2): wall-clock budget for one spawn_multi_agents
    # wave. The previous implementation called ``concurrent.futures.
    # as_completed(futures)`` and blocked the parent agent thread until
    # EVERY sub-agent finished — one stuck child (network hang, infinite
    # LLM retry loop, runaway iteration count) hung the whole wave until
    # the child's own max_iterations cap fired, which could be minutes.
    # We now cap the entire wave at WAVE_TIMEOUT seconds and return
    # partial results + a list of still-in-flight task labels so the
    # orchestrator can decide whether to wait, retry, or proceed without
    # them. The default of 180s matches the existing watchdog STALL
    # threshold (120s) with a small grace window.
    MULTI_AGENT_WAVE_TIMEOUT: float = 180.0

    def _spawn_multi_agents(self, tasks: List[Any]) -> str:
        """Spawn N sub-agents in parallel for independent sub-tasks.

        tasks: list of {goal, role?, max_iterations?, timeout?} dicts

        Each task runs in its own thread; results are joined and
        returned as a single observation. Tasks that fail are
        reported but don't abort the others.

        v1.2.1-fix (review §4.2): the wave now has a wall-clock budget
        (``MULTI_AGENT_WAVE_TIMEOUT``, default 180s). If the budget is
        exceeded, partial results are returned for tasks that finished,
        AND an explicit ``[WAVE TIMEOUT]`` section lists the labels of
        tasks that were still in flight. This unblocks the parent
        orchestrator instead of leaving it stuck behind one slow
        sub-agent. Per-task ``timeout`` overrides the wave default for
        that specific sub-agent (capped at the wave budget).

        v1.2.1-fix (review §4.2): watchdog state is populated BEFORE
        the wave starts and updated as futures complete, so a
        subsequent ``_watchdog_check`` call has real per-task progress
        to inspect (STALL + REPEAT detection).
        """
        if not tasks or not isinstance(tasks, list):
            return "[MULTI-AGENTS ERROR] tasks must be a non-empty list"
        if len(tasks) > 5:
            return (
                "[MULTI-AGENTS ERROR] too many tasks — max 5 parallel "
                "sub-agents (to avoid overwhelming the provider)."
            )
        import concurrent.futures
        results: List[str] = []
        wave_deadline = time.monotonic() + self.MULTI_AGENT_WAVE_TIMEOUT

        # v1.2.1-fix: seed the watchdog state so _watchdog_check has
        # something to inspect even before the first future completes.
        # Each entry is mutated in-place as the wave progresses.
        watchdog_entries: List[Dict[str, Any]] = []
        for i, task_spec in enumerate(tasks):
            if not isinstance(task_spec, dict):
                continue
            label = f"multi-agent #{i+1}"
            entry = {
                "label": label,
                "started_at": time.time(),
                "status": "running",
                "iterations": 0,
                "last_observations": [],  # last N observation snippets
                "last_error": None,
                "completed_at": None,
            }
            watchdog_entries.append(entry)
            self._subagent_watchdog_state.append(entry)

        # v1.2.1-fix: per-task event tap. We wrap the parent's on_event
        # so each sub-agent's TOOL_RESULT observations are also captured
        # into the watchdog entry's ``last_observations`` deque for
        # REPEAT detection. The wrap is per-spawn (not per-runtime), so
        # concurrent waves don't interfere with each other.
        per_task_obs: Dict[str, List[str]] = {e["label"]: [] for e in watchdog_entries}
        parent_on_event = self.on_event
        MAX_OBS_SNIPPETS = 5

        def _tap_event(label: str):
            def _tap(event, data):
                if event == AgentEvent.TOOL_RESULT:
                    snippet = str(data.get("result", ""))[:300]
                    per_task_obs[label].append(snippet)
                    if len(per_task_obs[label]) > MAX_OBS_SNIPPETS:
                        per_task_obs[label] = per_task_obs[label][-MAX_OBS_SNIPPETS:]
                # Also forward to the parent's normal event handler.
                if parent_on_event:
                    try:
                        enriched = dict(data)
                        enriched["parent_label"] = label
                        enriched["subagent"] = True
                        parent_on_event(event, enriched)
                    except Exception:
                        pass
            return _tap

        # Run sub-agents in parallel using a thread pool.
        #
        # v1.2.2-fix (found while validating review §4.2): do NOT use
        # ``with ThreadPoolExecutor(...) as pool:``. ``Executor.__exit__``
        # calls ``shutdown(wait=True)`` unconditionally, which blocks
        # until every submitted thread has actually finished — even
        # ones we've already given up on and reported as
        # ``[WAVE TIMEOUT]``. That made the v1.2.1 wave timeout purely
        # cosmetic: the observation text says the wave was unblocked,
        # but the Python call itself still would not return until the
        # straggler thread exited on its own (confirmed empirically —
        # a 1s ``wait(timeout=1)`` still took the full 5s to return once
        # inside a ``with`` block, because ``__exit__`` re-blocks).
        #
        # We manage the executor manually and shut it down with
        # ``wait=False, cancel_futures=True`` instead, so a genuinely
        # stuck sub-agent (hung provider call, infinite loop inside a
        # tool) no longer blocks the parent agent loop past the wave
        # budget. NOTE: CPython's ``concurrent.futures`` module still
        # registers every worker thread for a join at interpreter exit,
        # so a truly hung straggler can delay process shutdown even
        # though it no longer delays this call — that residual risk is
        # bounded by the provider layer's own request timeout, not by
        # this function.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks))
        try:
            futures = {}
            skipped_indices: set = set()
            for i, task_spec in enumerate(tasks):
                if not isinstance(task_spec, dict):
                    results.append(f"[task {i+1}] invalid spec — must be a dict")
                    skipped_indices.add(i)
                    continue
                goal = task_spec.get("goal", "")
                role = task_spec.get("role", "generalist")
                mi = int(task_spec.get("max_iterations", 4))
                label = f"multi-agent #{i+1}"
                # Find the matching watchdog entry to pass to the child.
                entry = next((e for e in watchdog_entries if e["label"] == label), None)
                # Pre-install the event tap so child TOOL_RESULTs are captured.
                fut = pool.submit(
                    self._run_subagent_internal,
                    goal=goal, role=role, max_iterations=mi, label=label,
                    watchdog_entry=entry,
                    event_tap=_tap_event(label),
                )
                futures[fut] = (i + 1, label, entry)

            # v1.2.1-fix: use wait(timeout=...) instead of as_completed().
            # ``as_completed`` blocks until every future is done; we want
            # to return as soon as either (a) all finish, or (b) the wave
            # wall-clock budget is exhausted.
            remaining = set(futures.keys())
            timed_out = False
            while remaining:
                time_left = wave_deadline - time.monotonic()
                if time_left <= 0:
                    timed_out = True
                    break
                done, remaining = concurrent.futures.wait(
                    remaining, timeout=time_left,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    idx, label, entry = futures[fut]
                    try:
                        res = fut.result()
                        results.append(f"[task {idx}]\n{res}")
                        if entry is not None:
                            entry["status"] = "done"
                            entry["completed_at"] = time.time()
                    except Exception as e:
                        results.append(f"[task {idx}] FAILED: {e}")
                        if entry is not None:
                            entry["status"] = "failed"
                            entry["last_error"] = str(e)
                            entry["completed_at"] = time.time()

            # Sync the per-task observation snippets back into the
            # watchdog state for any still-running tasks — _watchdog_check
            # reads from there.
            for label, snippets in per_task_obs.items():
                for entry in self._subagent_watchdog_state:
                    if entry.get("label") == label:
                        entry["last_observations"] = list(snippets)
                        break

            if timed_out:
                pending_labels = []
                for fut, (idx, label, entry) in futures.items():
                    if not fut.done():
                        pending_labels.append(label)
                        if entry is not None:
                            entry["status"] = "timed_out"
                            entry["last_error"] = (
                                f"wave exceeded {self.MULTI_AGENT_WAVE_TIMEOUT:.0f}s budget"
                            )
                results.append(
                    f"[WAVE TIMEOUT] {len(pending_labels)} task(s) still in-flight "
                    f"after {self.MULTI_AGENT_WAVE_TIMEOUT:.0f}s: "
                    f"{', '.join(pending_labels)}. Partial results above. "
                    f"The still-running sub-agent(s) were detached (not "
                    f"cancelled — Python threads can't be force-killed "
                    f"safely) and will keep running in the background; "
                    f"they will NOT report back to this conversation. "
                    f"Call _watchdog_check() on a subsequent iteration to "
                    f"see whether they eventually settle."
                )
        finally:
            # wait=False: don't block this call on stragglers.
            # cancel_futures=True: drop any submitted-but-not-yet-started
            # tasks outright (Python 3.9+). Already-running threads keep
            # running to completion in the background; we simply stop
            # waiting for them.
            pool.shutdown(wait=False, cancel_futures=True)
        # Order results by task index for readability
        # Order results by task index for readability
        results.sort(key=lambda r: (
            int(r.split("]")[0].replace("[task ", "")) if r.startswith("[task ") else 999
        ))
        return "\n\n---\n\n".join(results)

    def _run_subagent_internal(self, goal: str, role: str,
                                max_iterations: int,
                                label: str,
                                watchdog_entry: Optional[Dict[str, Any]] = None,
                                event_tap: Optional[Callable] = None,
                                provider_override: Optional[str] = None,
                                model_override: Optional[str] = None) -> str:
        """Internal helper: spawn a child AgentRuntime and run it to
        completion. Used by both _spawn_subagent (single) and
        _spawn_multi_agents (parallel).

        Sub-agents:
          - Share the parent's ProviderRegistry (so they use the same
            active model/API key)
          - Share the parent's workspace
          - Have their own ContextMemory (fresh — no parent history)
          - Have their own ToolEngine (fresh — no parent's skills loaded)
          - Emit events to the PARENT's on_event callback with a
            `parent_label` field so the UI can nest them visually
          - Are read-only by default (role=generalist/architect/reviewer/tester)
            — role=implementer is the only one that gets write tools

        v1.2.1-fix (review §4.2): ``watchdog_entry`` is a mutable dict
        shared with the parent's ``_subagent_watchdog_state``. The child
        bumps ``iterations`` and appends observation snippets to
        ``last_observations`` so the parent's ``_watchdog_check`` can
        detect STALL and REPEAT without waiting for the child to finish.
        ``event_tap`` is an optional per-task event interceptor used by
        ``_spawn_multi_agents`` to capture TOOL_RESULT observations for
        REPEAT detection. If provided, it IS the entire event callback
        for the child (no separate parent_on_event wrap is needed).

        G20b: ``provider_override`` / ``model_override`` route this
        specific child to a different model than the parent's active one.
        When set, ``child.set_provider_override(pid, model)`` is called
        after construction (and before the first LLM call) so the child
        uses the override for its entire lifetime. When both are None,
        the child uses the parent's active provider/model (today's
        behavior). The override does NOT propagate to grand-children —
        a child spawning its own sub-agent would have to pass the
        override down explicitly.
        """
        if not self._registry:
            return f"[{label}] no provider registry available"
        # Role → system prompt suffix + tool whitelist
        role_prompts = {
            "architect": (
                "You are a sub-agent focused on PLANNING and DESIGN. "
                "Read files, analyze structure, propose a plan. Do NOT write "
                "or modify any files — return your plan as the final answer."
            ),
            "implementer": (
                "You are a sub-agent focused on IMPLEMENTATION. Make the "
                "requested changes precisely. Prefer str_replace over "
                "write_file. Verify your changes by re-reading the file."
            ),
            "reviewer": (
                "You are a sub-agent focused on CODE REVIEW. Read the "
                "specified files, identify bugs / style issues / risks. "
                "Do NOT modify files — return your review as the final answer."
            ),
            "tester": (
                "You are a sub-agent focused on TESTING. Generate test cases "
                "for the specified code. You may write test files but do NOT "
                "modify production code."
            ),
            "generalist": (
                "You are a sub-agent. Complete the assigned sub-task. "
                "Read files as needed, return your findings/changes as "
                "the final answer."
            ),
        }
        system_suffix = role_prompts.get(role, role_prompts["generalist"])
        # Build the child runtime — fresh ContextMemory, same registry
        import tempfile as _tf
        child_persist = _tf.NamedTemporaryFile(
            prefix=f"tera_pilot_subagent_{label.replace(' ', '_')}_",
            suffix=".json", delete=False,
        )
        child_persist.close()
        # v1.1.3-fix (bug 1.2): inherit the parent's section so quota is
        # accounted against the SAME counter (e.g. heavy_code). Without
        # this, sub-agents defaulted to section="general" (unlimited),
        # which made them a free bypass of the daily quota.
        # Lazy import to avoid circular dependency:
        # _engine.py is imported by runtime.py at module load time,
        # so importing AgentRuntime at the top level would create a cycle.
        from tera_pilot.agent_runtime.runtime import AgentRuntime

        child = AgentRuntime(
            registry=self._registry,
            workspace=str(self.workspace),
            max_iterations=max(1, min(max_iterations, 10)),
            enable_planning=False,  # sub-agents skip planning — parent already planned
            on_event=None,  # we'll forward events with a parent_label
            memory_persist_path=child_persist.name,
            token_tracker=getattr(self, "_token_tracker", None),
            section=getattr(self, "section", "general"),
        )
        # v1.1.3-fix (bug 1.1): propagate the parent's cancel-check so
        # Stop halts sub-agents too. Without this, child.tools._cancel_check
        # stays None and is_cancelled() always returns False — the parent
        # loop stops, but spawn_multi_agents children keep running LLM
        # calls and tools until they finish naturally.
        child.set_cancel_check(self.is_cancelled)
        # v1.1.3-fix (bug 1.2): propagate the quota tracker so sub-agent
        # LLM calls are counted against the parent's daily quota. Combined
        # with the section inheritance above, this closes the "orchestrator
        # spawns 5 implementers and bypasses the 10/day limit" hole.
        child.set_quota_tracker(getattr(self, "_quota_tracker", None))
        # Sub-agent inherits the parent's autonomy + diff-review settings
        child.tools.autonomy = self.autonomy
        child.tools.diff_review_enabled = self.diff_review_enabled
        # v1.1.3-fix (bug 1.4): apply role-based tool whitelist so the
        # "read-only" promise for non-implementer roles is ENFORCED at
        # the ToolEngine level, not just the system prompt. Even if the
        # model ignores the prompt and emits write_file/str_replace/
        # delete_file/etc., the dispatch will be rejected.
        child.tools.set_role_whitelist(role)
        # G20b — apply provider/model override if the caller requested a
        # different model than the parent's active one. Done AFTER
        # construction (matching the existing pattern: cancel/quota/
        # autonomy/role-whitelist are all wired post-construction too).
        # set_provider_override() also re-syncs context budgets so the
        # child sizes its memory window to the override provider's
        # context window, not the parent's.
        if provider_override or model_override:
            try:
                child.set_provider_override(provider_override, model_override)
            except Exception as e:
                # Don't fail the whole spawn — log and fall back to the
                # parent's active provider. The task-decomposition router
                # treats this as a soft failure (the subtask still runs,
                # just on the parent's model instead of the requested one).
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "[subagent] provider_override failed (%s/%s): %s — "
                    "falling back to parent's active provider",
                    provider_override, model_override, e,
                )
        # Note: sub-agent's diff-review/confirm callbacks are NOT wired
        # to the parent UI — they fail open (headless mode), which is
        # fine because sub-agents are read-only by default. For
        # role="implementer" we should ideally forward these to the
        # parent UI, but that's a v1.2 enhancement.
        # For now: implementer sub-agents run with autonomy="never_ask"
        # so they don't deadlock waiting for a UI they don't have.
        if role == "implementer":
            child.tools.autonomy = "never_ask"
            # v1.1.3-fix (bug 1.4): keep diff_review enabled — disabling
            # it was a separate hole that let implementers silently apply
            # writes. The child's diff-review callback is None (headless),
            # so it fails-open to "allow" anyway, but the flag stays True
            # so a future implementation that forwards the callback to
            # the parent UI would Just Work.
            child.tools.diff_review_enabled = self.diff_review_enabled
        # Forward child events to the parent's on_event (if any),
        # tagged with parent_label so the UI can nest them.
        # v1.2.1-fix (review §4.2): if ``event_tap`` is provided (used by
        # _spawn_multi_agents), it becomes the child's event callback.
        # The tap itself forwards to the parent — we don't double-wrap.
        parent_on_event = self.on_event
        if event_tap is not None:
            child.on_event = event_tap
        elif parent_on_event:
            def _child_forward(event, data):
                data = dict(data)
                data["parent_label"] = label
                data["subagent"] = True
                try:
                    parent_on_event(event, data)
                except Exception:
                    pass
            child.on_event = _child_forward
        # v1.2.1-fix (review §4.2): if we have a watchdog_entry, wrap
        # the child's on_event one more time so we can bump iteration
        # counts and capture observation snippets. Done as a thin
        # decorator so the parent forwarder / event_tap above stays intact.
        if watchdog_entry is not None:
            _orig_cb = child.on_event

            def _watchdog_tap(event, data):
                try:
                    if event == AgentEvent.ITERATION_START:
                        watchdog_entry["iterations"] = (
                            int(data.get("iteration", 0)) or
                            (watchdog_entry.get("iterations", 0) + 1)
                        )
                    elif event == AgentEvent.TOOL_RESULT:
                        snippet = str(data.get("result", ""))[:300]
                        obs_list = watchdog_entry.setdefault("last_observations", [])
                        obs_list.append(snippet)
                        # Keep only the last N for REPEAT detection.
                        if len(obs_list) > 5:
                            del obs_list[:len(obs_list) - 5]
                except Exception:
                    pass
                if _orig_cb is not None:
                    try:
                        _orig_cb(event, data)
                    except Exception:
                        pass
            child.on_event = _watchdog_tap
        # Build a focused task
        task = Task(
            type=TaskType.AGENTIC,
            description=(
                f"{system_suffix}\n\n"
                f"## Sub-task (assigned by parent agent)\n{goal}\n\n"
                f"Return your final answer concisely — the parent agent "
                f"will incorporate it into its own response."
            ),
            language="python",
        )
        try:
            result = child._run_agent_loop(task)
            if result.success:
                return (
                    f"[{label} OK in {result.iterations} iterations]\n"
                    f"{result.output}"
                )
            else:
                return (
                    f"[{label} FAILED after {result.iterations} iterations: "
                    f"{result.error or 'unknown'}]\n{result.output}"
                )
        finally:
            # Clean up the child's persist file
            try:
                import os as _os
                _os.unlink(child_persist.name)
            except OSError:
                pass

    # ── v1.2.0: Office Worker dispatch ─────────────────────────────

    def _office_dispatch(self, method: str, **kwargs) -> str:
        """Lazy-instantiate the OfficeWorker on first call and route
        the request to its matching method.

        The OfficeWorker is constructed with this engine's
        ``_resolve_path`` so the existing workspace sandbox applies
        to every office file access — no path can escape the project.

        After any successful office_* call that writes/edits a file,
        the path is added to ``_touched_files`` so self_verify can
        re-read it at task close.
        """
        if self._office_worker is None:
            # Lazy import to avoid pulling in python-docx/openpyxl/python-pptx
            # at module load time — they're only needed when office tools run.
            from tera_pilot.office_worker import OfficeWorker
            self._office_worker = OfficeWorker(
                resolve_path_fn=self._resolve_path,
            )
        fn = getattr(self._office_worker, method, None)
        if fn is None:
            return f"[OFFICE ERROR] unknown method: {method}"
        try:
            result = fn(**kwargs)
        except TypeError as e:
            # kwarg mismatch — surface a clear error to the agent so it
            # can correct the call shape instead of getting a stack trace.
            return f"[OFFICE ERROR] {method}: {e}"
        # Track the file for self_verify, unless the call explicitly failed.
        if isinstance(result, str) and result.startswith("["):
            ok_marker = any(
                result.startswith(prefix) for prefix in (
                    "[CREATED", "[ADDED", "[SET", "[FILLED",
                    "[SAVED AS", "[FIND/REPLACE]",
                )
            )
            if ok_marker:
                path = kwargs.get("path") or kwargs.get("new_path") or ""
                if path and path not in self._touched_files:
                    self._touched_files.append(path)
        return result

    # ── v1.2.0: Self-verify (architect-loop inspired) ──────────────

    # v1.2.1-fix (review §4.1): supported self_verify modes.
    #   re_read          — the original behaviour: re-read touched files
    #                      and present them to the SAME context. Cheap
    #                      (zero extra LLM calls) but suffers from the
    #                      "model rarely catches its own error twice"
    #                      blind spot the review flagged.
    #   run_tests        — auto-detect the project's test/lint command
    #                      (pytest / npm test / ruff / mypy / cargo test /
    #                      go test) and run it. The exit code + output
    #                      become the verification evidence. Falls back
    #                      to re_read if no command is detected.
    #   review_subagent  — spawn a fresh-context reviewer sub-agent
    #                      (role="reviewer") over the touched files +
    #                      goal. Costs 2-4 LLM calls but gives an
    #                      INDEPENDENT check, which is what the review
    #                      specifically asked for. Available in all
    #                      sections (not just Heavy Code) — the spawned
    #                      reviewer is read-only so it's safe everywhere.
    #   full             — run_tests AND review_subagent. Use for
    #                      high-stakes changes (security, data handling).
    SELF_VERIFY_MODES = {"re_read", "run_tests", "review_subagent", "full"}

    # v1.2.1-fix (review §4.1): how we detect a project's test command.
    # Each entry is (marker_file, command, description). We walk the
    # workspace root looking for the marker; the first match wins.
    # Commands are subject to the existing ALLOWED_COMMANDS whitelist
    # and path sandbox — if a command isn't whitelisted we report it
    # as "detected but not allowed by sandbox" so the agent knows to
    # fall back to re_read mode.
    _TEST_COMMAND_DETECTORS = (
        ("pytest.ini", "pytest -x", "pytest (pytest.ini)"),
        ("pyproject.toml", "pytest -x", "pytest (pyproject.toml)"),
        ("setup.cfg", "pytest -x", "pytest (setup.cfg)"),
        ("tox.ini", "pytest -x", "pytest (tox.ini)"),
        ("package.json", "npm test", "npm test (package.json)"),
        ("Cargo.toml", "cargo test", "cargo test (Cargo.toml) — NOT in whitelist"),
        ("go.mod", "go test ./...", "go test (go.mod) — NOT in whitelist"),
        ("Makefile", "make test", "make test (Makefile) — NOT in whitelist"),
    )
    _LINT_COMMAND_DETECTORS = (
        ("pyproject.toml", "ruff check .", "ruff (pyproject.toml)"),
        (".ruff.toml", "ruff check .", "ruff (.ruff.toml)"),
        ("setup.cfg", "flake8 .", "flake8 (setup.cfg) — NOT in whitelist"),
        (".flake8", "flake8 .", "flake8 (.flake8) — NOT in whitelist"),
        ("package.json", "npm run lint", "npm run lint (package.json)"),
    )

    def _self_verify(self, goal: str, touched_files: List[str],
                     mode: str = "re_read",
                     run_tests: bool = False) -> str:
        """Verification pass at task close.

        v1.2.1-fix (review §4.1): the original implementation just
        re-read the touched files in the SAME agent context. As the
        review noted, a model that mis-wrote code on the first pass
        is statistically unlikely to catch the same error on re-read
        — it has the same blind spots. We now support four modes:

          - ``re_read`` (default, backward-compatible): re-read files,
            present them, let the parent agent's next iteration be
            the verification LLM call. Costs zero extra LLM calls.
          - ``run_tests``: auto-detect the project's test/lint command
            and execute it (subject to the existing whitelist + sandbox).
            The exit code + output become verification evidence.
          - ``review_subagent``: spawn a fresh-context reviewer
            sub-agent (role="reviewer") over the touched files + goal.
            Independent verification — what a "reviewer
            subagent" pattern does, now available in ALL sections
            (not just Heavy Code).
          - ``full``: run_tests + review_subagent. Use for high-stakes
            changes (security, data handling, refactors touching public
            API).

        The legacy ``run_tests=True`` kwarg is treated as a shorthand
        for ``mode="run_tests"`` so existing call sites keep working.

        Inspired by architect-loop's "fresh-context verifier subagent"
        pattern; the ``re_read`` mode preserves the original cheap
        behaviour for tasks where the cost of an extra LLM call isn't
        justified.
        """
        # Normalize legacy ``run_tests=True`` → mode="run_tests".
        if run_tests and mode == "re_read":
            mode = "run_tests"
        if mode not in self.SELF_VERIFY_MODES:
            return (
                f"[SELF-VERIFY ERROR] unknown mode {mode!r}. "
                f"Supported: {sorted(self.SELF_VERIFY_MODES)}"
            )

        files_to_check = touched_files or self._touched_files
        if not files_to_check and mode not in ("run_tests", "full"):
            return (
                "[SELF-VERIFY] no files to verify (no writes in this run). "
                "If the task was purely informational, emit final_answer now."
            )

        out: List[str] = []
        out.append(f"[SELF-VERIFY mode={mode}]")
        out.append(f"Goal: {goal or '(not specified)'}")
        if files_to_check:
            out.append(f"Files touched: {len(files_to_check)}")

        # ── Mode: re_read (also used as the file-content portion of
        # run_tests / review_subagent / full).
        if mode in ("re_read", "run_tests", "review_subagent", "full"):
            for path in files_to_check[-10:]:  # cap at last 10 files
                try:
                    p = self._resolve_path(path)
                    if not p.exists():
                        out.append(f"\n--- {path} ---\n[MISSING]")
                        continue
                    size = p.stat().st_size
                    text = p.read_text(encoding="utf-8", errors="replace")
                    # Truncate large files — we want a summary, not the
                    # full content. The agent already saw the content when
                    # it wrote the file; this is just a sanity check.
                    if len(text) > 4000:
                        text = text[:2000] + f"\n... [{len(text)} total chars, {text.count(chr(10))} lines, truncated]"
                    out.append(f"\n--- {path} ({size} bytes) ---\n{text}")
                except PermissionError as e:
                    out.append(f"\n--- {path} ---\n[PERMISSION DENIED] {e}")
                except Exception as e:
                    out.append(f"\n--- {path} ---\n[READ ERROR] {e}")

        # ── Mode: run_tests / full — execute the project's test command.
        if mode in ("run_tests", "full"):
            test_section = self._self_verify_run_tests()
            out.append("\n" + test_section)

        # ── Mode: review_subagent / full — independent reviewer.
        if mode in ("review_subagent", "full"):
            review_section = self._self_verify_review_subagent(
                goal=goal, touched_files=files_to_check,
            )
            out.append("\n" + review_section)

        out.append(
            "\n[SELF-VERIFY END] Compare the evidence above against the "
            "goal. If everything matches, emit final_answer with a summary. "
            "If gaps exist, fix them now with str_replace or write_file "
            "BEFORE emitting final_answer."
        )
        return "\n".join(out)

    def _self_verify_run_tests(self) -> str:
        """v1.2.1-fix (review §4.1): detect and run the project's test
        and lint commands. Returns a formatted string with the detected
        commands, their exit codes, and (truncated) output.

        We respect the existing ``ALLOWED_COMMANDS`` whitelist — if a
        detected command isn't whitelisted, we report it as "detected
        but not allowed by sandbox" rather than silently skipping. This
        lets the agent know it should fall back to manual verification
        or ask the user to extend the whitelist.
        """
        out = ["## Test / lint execution"]
        if not self.workspace or not self.workspace.is_dir():
            out.append("[SKIP] workspace not set — cannot detect test command")
            return "\n".join(out)

        # Try test command first.
        test_cmd, test_desc = self._detect_project_command(self._TEST_COMMAND_DETECTORS)
        if test_cmd is None:
            out.append("[NO TEST COMMAND DETECTED] "
                       "No pytest.ini / pyproject.toml / package.json / "
                       "Cargo.toml / go.mod / Makefile in workspace root. "
                       "Skipping test execution.")
        else:
            out.append(f"Detected: {test_desc}")
            args, is_safe = _sanitize_command(test_cmd, project_root=str(self.workspace) if self.workspace else None)
            if not is_safe:
                out.append(
                    f"[BLOCKED] Command {test_cmd!r} is not allowed by "
                    f"the security whitelist (ALLOWED_COMMANDS). The "
                    f"test command was detected but not executed. To "
                    f"run it, either add the binary to the whitelist "
                    f"(see Settings) or run the command manually."
                )
            else:
                result = self._run_test_command_sandboxed(args)
                out.append(result)

        # Try lint command (best-effort, never blocks).
        lint_cmd, lint_desc = self._detect_project_command(self._LINT_COMMAND_DETECTORS)
        if lint_cmd is not None:
            out.append(f"\nDetected lint: {lint_desc}")
            args, is_safe = _sanitize_command(lint_cmd, project_root=str(self.workspace) if self.workspace else None)
            if is_safe:
                result = self._run_test_command_sandboxed(args)
                out.append(result)
            else:
                out.append(f"[BLOCKED] {lint_cmd!r} not in whitelist — skipped")

        return "\n".join(out)

    def _detect_project_command(
        self, detectors: Tuple[Tuple[str, str, str], ...]
    ) -> Tuple[Optional[str], Optional[str]]:
        """v1.2.1-fix: walk ``detectors`` looking for a marker file in
        ``self.workspace``. Returns (command, description) or (None, None).
        """
        for marker, command, desc in detectors:
            try:
                if (self.workspace / marker).exists():
                    return command, desc
            except OSError:
                continue
        return None, None

    def _run_test_command_sandboxed(self, args: List[str]) -> str:
        """v1.2.1-fix: run a pre-validated test/lint command and return
        a formatted result string. Uses the same Popen + polling pattern
        as ``_execute_command`` so Stop cancels it. The result is
        truncated to fit in the agent's observation budget.
        """
        try:
            proc = subprocess.Popen(
                args, shell=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.workspace,
            )
            deadline = time.time() + 60  # 60s cap for test/lint in verify
            try:
                while proc.poll() is None:
                    if self.is_cancelled():
                        proc.kill()
                        proc.wait()
                        return "[CANCELLED BY USER] test/lint aborted"
                    if time.time() > deadline:
                        proc.kill()
                        proc.wait()
                        return f"[TIMEOUT] test/lint exceeded 60s"
                    time.sleep(0.25)
                stdout = proc.stdout.read() if proc.stdout else b""
                stderr = proc.stderr.read() if proc.stderr else b""
            except Exception:
                proc.kill()
                proc.wait()
                raise
            out_text = stdout.decode("utf-8", errors="replace")
            err_text = stderr.decode("utf-8", errors="replace")
            # Truncate aggressively — verification output is evidence,
            # not the full log.
            out_text = out_text[-1500:] if len(out_text) > 1500 else out_text
            err_text = err_text[-1500:] if len(err_text) > 1500 else err_text
            parts = [f"[EXIT CODE] {proc.returncode}"]
            if out_text:
                parts.append(f"[STDOUT]\n{out_text}")
            if err_text:
                parts.append(f"[STDERR]\n{err_text}")
            return "\n".join(parts)
        except Exception as e:
            return f"[EXEC ERROR] {e}"

    def _self_verify_review_subagent(self, goal: str,
                                      touched_files: List[str]) -> str:
        """v1.2.1-fix (review §4.1): spawn a fresh-context reviewer
        sub-agent over the touched files + goal.

        Unlike the Heavy Code section's adversarial reviewer (which
        is part of the orchestrator workflow), this is a SELF-VERIFY
        primitive available in ALL sections. The reviewer is read-only
        (role="reviewer" → tool whitelist excludes write_file /
        str_replace / delete_file) so it's safe to invoke even in
        General section.

        Returns a formatted string with the reviewer's findings. If
        the spawn fails (no provider, no registry, etc.) we degrade
        gracefully to a note telling the agent to do a manual review.

        The reviewer's prompt deliberately does NOT include the
        parent's conversation history — only the goal + the actual
        file contents. This is what makes the check INDEPENDENT: the
        reviewer can't be primed by the same blind spots that caused
        the original mistake.
        """
        out = ["## Independent reviewer sub-agent"]
        if not self._registry:
            out.append(
                "[SKIP] no provider registry available — cannot spawn "
                "reviewer sub-agent. Fall back to manual re_read review."
            )
            return "\n".join(out)
        if not touched_files:
            out.append(
                "[SKIP] no files to review — reviewer sub-agent needs "
                "at least one touched file."
            )
            return "\n".join(out)
        # Build a focused review task. The reviewer reads the files
        # fresh (no parent history) and reports gaps.
        file_list = "\n".join(
            f"  - {p}" for p in touched_files[-10:]
        )
        review_goal = (
            f"You are an INDEPENDENT reviewer. Your job is to find "
            f"gaps between the stated goal and what's actually in the "
            f"files. Do NOT trust that the previous agent did the "
            f"right thing — verify every claim by reading the actual "
            f"file contents.\n\n"
            f"## Goal\n{goal or '(not specified)'}\n\n"
            f"## Files to review\n{file_list}\n\n"
            f"## What to check\n"
            f"1. Does each file actually implement what the goal asks for?\n"
            f"2. Are there syntax errors, obvious bugs, or missing imports?\n"
            f"3. Are edge cases handled (empty input, null, boundary values)?\n"
            f"4. Does the code match the project's existing conventions?\n"
            f"5. Are there any TODOs or stubs left that should have been completed?\n\n"
            f"## Output format\n"
            f"Return a concise review:\n"
            f"- VERDICT: OK | GAPS_FOUND | BLOCKED\n"
            f"- For each gap: file path + 1-sentence description + suggested fix\n"
            f"- If everything looks good, just say 'VERDICT: OK' and 1-2 sentences why."
        )
        try:
            review_result = self._run_subagent_internal(
                goal=review_goal,
                role="reviewer",
                max_iterations=4,
                label="self-verify-reviewer",
            )
            out.append(review_result)
        except Exception as e:
            out.append(
                f"[REVIEWER ERROR] failed to spawn reviewer sub-agent: {e}. "
                f"Fall back to manual re_read review."
            )
        return "\n".join(out)

    # ── v1.2.0: Subagent watchdog (architect-loop inspired) ────────

    # v1.2.1-fix (review §4.2): parameters for the subagent watchdog.
    # STALL_THRESHOLD = how long (seconds) an in-flight subagent can run
    # without completing before the watchdog flags it as stalled.
    # REPEAT_THRESHOLD = how many of the last N observations must be
    # identical (by content hash) for the watchdog to flag a repeat loop.
    # REPEAT_MIN_OBSERVATIONS = minimum number of observations needed
    # before REPEAT detection kicks in (avoid false positives when a
    # sub-agent has only made 1-2 tool calls legitimately returning the
    # same content, e.g. reading the same file twice).
    WATCHDOG_STALL_THRESHOLD: float = 120.0
    WATCHDOG_REPEAT_THRESHOLD: int = 3
    WATCHDOG_REPEAT_MIN_OBSERVATIONS: int = 3

    def _watchdog_check(self) -> str:
        """Inspect the in-flight subagent state and return typed
        evidence. Inspired by architect-loop's watchdog which never
        kills, only reports.

        Returns one of:
          - "ALL_DONE": no in-flight subagents
          - "STALL: subagents [...] have been in-flight >Ns"
          - "REPEAT: subagents [...] appear stuck on identical observations"
          - "OK": subagents in flight, none stalled, no repeats

        Called by the parent agent loop between spawn_multi_agents
        waves. The orchestrator decides what to do with the evidence.

        v1.2.1-fix (review §4.2): REPEAT detection is now IMPLEMENTED.
        Previously it was a stub ("out of scope for v1.2.0"). We now
        compare the last N observation snippets captured from each
        sub-agent's TOOL_RESULT events — if at least REPEAT_THRESHOLD
        of them share the same content hash AND the sub-agent has made
        at least REPEAT_MIN_OBSERVATIONS tool calls, we flag it as a
        likely retry loop (e.g. model keeps hitting the same error and
        retrying the same call without changing strategy).

        Note: the watchdog never kills sub-agents — it only reports
        so the orchestrator can decide. This is intentional: the
        orchestrator may want to wait one more iteration, or proceed
        without the result, depending on the task's criticality.
        """
        state = self._subagent_watchdog_state
        if not state:
            return "ALL_DONE"
        now = time.time()
        stalled: List[str] = []
        repeating: List[str] = []
        for entry in state:
            status = entry.get("status", "running")
            if status in ("done", "failed", "timed_out"):
                continue
            started = entry.get("started_at", 0)
            if now - started > self.WATCHDOG_STALL_THRESHOLD:
                stalled.append(entry.get("label", "?"))
                continue
            # REPEAT detection: hash the last N observation snippets and
            # count distinct hashes. If there are >= REPEAT_MIN_OBSERVATIONS
            # observations AND at least REPEAT_THRESHOLD of them are the
            # same hash, flag as repeating.
            obs = entry.get("last_observations", []) or []
            if len(obs) >= self.WATCHDOG_REPEAT_MIN_OBSERVATIONS:
                hashes = [hashlib.md5(o.encode("utf-8", errors="replace")).hexdigest()[:8] for o in obs]
                # Count how many times the most-common hash appears.
                from collections import Counter
                counts = Counter(hashes)
                most_common_count = counts.most_common(1)[0][1] if counts else 0
                if most_common_count >= self.WATCHDOG_REPEAT_THRESHOLD:
                    repeating.append(entry.get("label", "?"))
        if stalled and repeating:
            return (
                f"STALL+REPEAT: subagents {stalled} stalled "
                f"(>{self.WATCHDOG_STALL_THRESHOLD:.0f}s), "
                f"{repeating} appear stuck on identical observations"
            )
        if stalled:
            return (
                f"STALL: subagents {stalled} have been in-flight "
                f">{self.WATCHDOG_STALL_THRESHOLD:.0f}s"
            )
        if repeating:
            return (
                f"REPEAT: subagents {repeating} appear stuck on identical "
                f"observations (last {self.WATCHDOG_REPEAT_THRESHOLD}+ "
                f"tool results had the same content hash). Likely a retry "
                f"loop — consider cancelling or escalating."
            )
        return "OK"

    def _run_code(self, code: str, language: str = "python", timeout: int = 180) -> str:
        if not code.strip():
            return "[EMPTY CODE]"

        # v2.1.0 (Loop 2): configurable per-call timeout with bounds.
        timeout = max(self.MIN_TIMEOUT, min(timeout, self.MAX_TIMEOUT))

        # v1.0.6-security: require user confirmation before running code
        # (C-RT-1). Without this, prompt injection in any file the agent
        # reads could trigger arbitrary code execution.
        if not self._request_confirmation("run_code", f"Run {language} code ({len(code)} chars)"):
            return "[REJECTED BY USER] run_code cancelled"

        with tempfile.TemporaryDirectory() as tmpdir:
            if language in ("python", "py"):
                fpath = os.path.join(tmpdir, "run.py")
                cmd = ["python3", fpath]
            elif language in ("javascript", "js", "node"):
                fpath = os.path.join(tmpdir, "run.js")
                cmd = ["node", fpath]
            elif language in ("bash", "sh", "shell"):
                fpath = os.path.join(tmpdir, "run.sh")
                cmd = ["bash", fpath]
            else:
                return f"[UNSUPPORTED LANGUAGE: {language}]"

            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)

            try:
                # v1.1.3-fix (bug 1.7): the sandbox environment previously
                # claimed to "block network access" via env vars, but:
                #   1. PYTHONHTTPSVERIFY=0 is a NO-OP — Python only honours
                #      the value when it's "1" (to ENABLE strict verify).
                #      Setting it to "0" is the same as not setting it.
                #   2. Empty http_proxy/https_proxy only disables the
                #      proxy — direct socket.create_connection still works.
                #   3. PATH was inherited fully, giving access to all
                #      system binaries (different code path from
                #      ALLOWED_COMMANDS in _execute_command).
                # We can't fix #2 and #3 from Python (real isolation
                # needs seccomp/network namespaces/firejail), but we
                # CAN remove the misleading no-op and the comment that
                # claimed network was blocked. The empty proxy vars
                # are kept because they DO help when a proxy is set
                # globally — they just don't block direct connections.
                sandbox_env = {
                    "HOME": tmpdir,
                    "TMPDIR": tmpdir,
                    "TEMP": tmpdir,
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    # Disable proxy use (does NOT block direct connections
                    # — see comment above). Real network isolation requires
                    # OS-level sandboxing (seccomp, firejail --net=none).
                    "http_proxy": "",
                    "https_proxy": "",
                    "HTTP_PROXY": "",
                    "HTTPS_PROXY": "",
                    "NO_PROXY": "*",
                    "no_proxy": "*",
                }

                # v1.0.6: use Popen + polling so Stop button can abort
                # subprocess execution (M-RT-7). subprocess.run blocks
                # for up to RUN_TIMEOUT with no cancellation.
                # v2.1.0 (Loop 2): use configurable per-call timeout
                # instead of the global RUN_TIMEOUT constant.
                proc = subprocess.Popen(
                    cmd, shell=False,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=tmpdir, env=sandbox_env,
                )
                stdout, stderr = b"", b""
                deadline = time.time() + timeout
                try:
                    # Poll for completion, checking cancel every 0.5s
                    while proc.poll() is None:
                        if self.is_cancelled():
                            proc.kill()
                            proc.wait()
                            return f"[CANCELLED BY USER] run_code aborted"
                        if time.time() > deadline:
                            proc.kill()
                            proc.wait()
                            return f"[TIMEOUT] Exceeded {timeout}s"
                        time.sleep(0.5)
                    stdout = proc.stdout.read()
                    stderr = proc.stderr.read()
                except Exception:
                    proc.kill()
                    proc.wait()
                    raise

                parts = []
                out_text = stdout.decode("utf-8", errors="replace")
                err_text = stderr.decode("utf-8", errors="replace")
                if out_text:
                    parts.append(f"[STDOUT]\n{out_text[-self.MAX_OUTPUT//2:]}")
                if err_text:
                    parts.append(f"[STDERR]\n{err_text[-self.MAX_OUTPUT//2:]}")
                if proc.returncode != 0:
                    parts.append(f"[EXIT CODE] {proc.returncode}")
                return "\n".join(parts) if parts else "[NO OUTPUT]"
            except subprocess.TimeoutExpired:
                return f"[TIMEOUT] Exceeded {timeout}s"
            except FileNotFoundError as e:
                return f"[RUNTIME NOT FOUND] {e}"

    def _search_project(self, query: str, directory: str = ".", file_pattern: str = "*.py") -> str:
        try:
            base = self._resolve_path(directory)
        except PermissionError:
            base = self.workspace

        results: List[str] = []
        try:
            for fpath in sorted(base.rglob(file_pattern))[:50]:
                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                    for i, line in enumerate(text.splitlines(), 1):
                        if query.lower() in line.lower():
                            rel = fpath.relative_to(self.workspace) if fpath.is_relative_to(self.workspace) else fpath
                            results.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(results) >= 40:
                                break
                except Exception:
                    continue
                if len(results) >= 40:
                    break
        except Exception as e:
            return f"[SEARCH ERROR] {e}"

        if not results:
            return f"[NO RESULTS] '{query}' not found"
        return "\n".join(results)

    # ── v1.2.1-fix (review §4.4): agentic-search primitives ──────────
    # ``_search_project`` does plain substring search over a single
    # file-pattern. ``_grep`` does REGEX search (with optional case
    # sensitivity) over a glob of files, and ``_glob`` returns paths
    # matching a pattern. Together they give the agent the same
    # "search the workspace on demand" capability other coding agents'
    # agentic-search harness has — the agent decides WHAT to search
    # for based on its reasoning, instead of relying solely on
    # ContextManager's heuristic auto-attach.

    def _grep(self, pattern: str, path: str = ".",
              include: str = "*.py", max_results: int = 50,
              case_sensitive: bool = False) -> str:
        """Regex search across files matching ``include``.

        Returns one line per match in ``path:lineno: line`` format,
        capped at ``max_results``. Empty pattern → error (avoid
        accidental "match everything" footguns). Pattern is compiled
        as a Python regex; invalid regexes surface the error to the
        agent so it can retry with a corrected pattern.
        """
        if not pattern:
            return "[GREP ERROR] pattern is required (regex)"
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                rx = re.compile(pattern, flags)
            except re.error as e:
                return (
                    f"[GREP ERROR] invalid regex {pattern!r}: {e}. "
                    f"Fix the pattern and retry."
                )
            try:
                base = self._resolve_path(path)
            except PermissionError:
                base = self.workspace
            results: List[str] = []
            # Walk manually so we can skip binary files (rglob + read_text
            # would crash on them). 200-file cap to stay responsive on
            # large monorepos.
            walked = 0
            for fpath in sorted(base.rglob(include)):
                walked += 1
                if walked > 500:
                    break
                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        try:
                            rel = fpath.relative_to(self.workspace) if fpath.is_relative_to(self.workspace) else fpath
                        except ValueError:
                            rel = fpath
                        results.append(f"{rel}:{i}: {line.rstrip()[:240]}")
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
            if not results:
                return f"[GREP NO RESULTS] pattern {pattern!r} not found in {include} under {path}"
            header = f"[GREP] {len(results)} match(es) for {pattern!r} (include={include}):"
            return header + "\n" + "\n".join(results)
        except Exception as e:
            return f"[GREP ERROR] {e}"

    def _glob(self, pattern: str, path: str = ".",
              max_results: int = 100) -> str:
        """Return workspace-relative paths matching ``pattern``.

        Uses ``Path.glob`` semantics (``**`` for recursive). Caps at
        ``max_results`` to avoid blowing the observation budget on
        huge monorepos.
        """
        if not pattern:
            return "[GLOB ERROR] pattern is required (e.g. '**/*.py')"
        try:
            try:
                base = self._resolve_path(path)
            except PermissionError:
                base = self.workspace
            results: List[str] = []
            for fpath in sorted(base.glob(pattern)):
                try:
                    rel = fpath.relative_to(self.workspace) if fpath.is_relative_to(self.workspace) else fpath
                except ValueError:
                    rel = fpath
                results.append(str(rel))
                if len(results) >= max_results:
                    break
            if not results:
                return f"[GLOB NO RESULTS] no files match {pattern!r} under {path}"
            header = f"[GLOB] {len(results)} file(s) matching {pattern!r}:"
            return header + "\n" + "\n".join(results)
        except Exception as e:
            return f"[GLOB ERROR] {e}"

    def _list_files(self, directory: str = ".", pattern: str = "*") -> str:
        try:
            base = self._resolve_path(directory)
        except PermissionError:
            base = self.workspace
        try:
            files = sorted(base.rglob(pattern))[:100]
            lines = [str(f.relative_to(self.workspace)) if f.is_relative_to(self.workspace) else str(f) for f in files]
            return "\n".join(lines) if lines else "[NO FILES FOUND]"
        except Exception as e:
            return f"[LIST ERROR] {e}"

    def _apply_diff(self, path: str, diff: str) -> str:
        try:
            p = self._resolve_path(path)
            if not p.exists():
                return f"[FILE NOT FOUND] {path}"
            if not self._request_confirmation("apply_diff", f"Patch: {path}"):
                return f"[REJECTED BY USER] {path} — apply_diff cancelled"

            # v1.0.6: multi-file diff support (M-RT-3). If the diff
            # contains --- a/ / +++ b/ headers for MULTIPLE files, split
            # and apply each file's hunks separately. Otherwise apply
            # as a single-file diff (backward compat).
            files_diffs = _split_multi_file_diff(diff)
            if len(files_diffs) == 1:
                # Single-file diff (or no file headers at all)
                original = p.read_text(encoding="utf-8")
                patched = _apply_unified_diff(original, diff)
                if p.exists() and p.is_file():
                    _backup_file_func(self._backup_dir, self._MAX_BACKUPS, p)
                p.write_text(patched, encoding="utf-8")
                return f"[PATCHED] {path}"
            else:
                # Multi-file diff: apply each file's hunks to its own file
                results = []
                for file_path, file_diff in files_diffs:
                    try:
                        target = self._resolve_path(file_path)
                    except PermissionError:
                        results.append(f"[SECURITY ERROR] path outside workspace: {file_path}")
                        continue
                    if not target.exists():
                        # New file — just write it
                        patched = _apply_unified_diff("", file_diff)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(patched, encoding="utf-8")
                        results.append(f"[CREATED] {file_path}")
                    else:
                        original = target.read_text(encoding="utf-8")
                        patched = _apply_unified_diff(original, file_diff)
                        _backup_file_func(self._backup_dir, self._MAX_BACKUPS, target)
                        target.write_text(patched, encoding="utf-8")
                        results.append(f"[PATCHED] {file_path}")
                return "\n".join(results)
        except Exception as e:
            return f"[DIFF ERROR] {e}"

    # Commands whose arguments are file/directory paths that MUST be
    # validated against the workspace sandbox before we run them. A bare
    # binary whitelist (ALLOWED_COMMANDS) is not enough on its own: "rm",
    # "mv", "cp", and "find -exec" are all whitelisted (agents legitimately
    # need them), but without checking the paths they're given, the model
    # (or content it read that contains a prompt injection) could do
    # `rm -rf /some/path/outside/the/project` and the whitelist alone
    # would happily let it through.
    # v1.0.6-security: expanded to cover all commands that take file
    # paths as arguments (C-RT-3). Without this, `cat /etc/passwd`,
    # `head ~/.ssh/id_rsa`, `grep -r secret /` were all allowed.
    _PATH_ARG_COMMANDS = {
        "rm", "mv", "cp", "find", "cat", "head", "tail",
        "grep", "mkdir", "touch", "git",
    }

    def _validate_command_paths(self, args: List[str]) -> Optional[str]:
        """For commands that take file/dir paths as arguments, make sure
        every path-like argument resolves inside the workspace sandbox.
        Returns an error string if a path escapes the sandbox, else None.

        v1.0.6-security: also validates `git -C <path>` which bypasses
        the normal arg-based check since `-C` starts with `-` and the
        path follows it (C-RT-3).
        """
        base_cmd = os.path.basename(args[0])
        if base_cmd not in self._PATH_ARG_COMMANDS:
            return None
        # v1.0.6-security: git -C <path> changes the working directory
        # to <path> which is not the workspace — validate it.
        # v2.4.0-security: also validate `--git-dir=<path>` /
        # `--work-tree=<path>` (both the `--flag=path` and `--flag path`
        # forms). These override where git reads its repository / work
        # tree from, so an unvalidated `git --git-dir=/home/user/.git log`
        # would let the model read git history from anywhere on disk, and
        # `git --work-tree=/etc add -A` would stage arbitrary files —
        # both bypass the workspace sandbox even though the flags start
        # with '-'. Fail closed: any path that doesn't resolve inside the
        # workspace blocks the command.
        if base_cmd == "git":
            for i, arg in enumerate(args[1:], 1):
                # Two-arg forms: -C <path> / --git-dir <path> / --work-tree <path>.
                if arg in ("-C", "--git-dir", "--work-tree") and i + 1 < len(args):
                    target = args[i + 1]
                    try:
                        self._resolve_path(target)
                    except PermissionError:
                        return (
                            f"[SECURITY ERROR] 'git {arg}' argument escapes the "
                            f"workspace sandbox: {target!r}"
                        )
                # Inline forms: --git-dir=<path> / --work-tree=<path>.
                for flag in ("--git-dir", "--work-tree"):
                    if arg.startswith(flag + "="):
                        target = arg[len(flag) + 1:]
                        try:
                            self._resolve_path(target)
                        except PermissionError:
                            return (
                                f"[SECURITY ERROR] 'git {flag}' argument escapes the "
                                f"workspace sandbox: {target!r}"
                            )
        for arg in args[1:]:
            # Skip flags (-rf, -name, etc.) and find's non-path predicates.
            if arg.startswith("-"):
                continue
            # find's "{}" placeholder and bare "." / "./" refer to the
            # search root or the current match — "." is fine (== workspace).
            if arg in ("{}", ".", "./"):
                continue
            try:
                self._resolve_path(arg)
            except PermissionError:
                return (
                    f"[SECURITY ERROR] '{base_cmd}' argument escapes the "
                    f"workspace sandbox: {arg!r}"
                )
        return None

    def _execute_command(self, command: str, timeout: int = 180) -> str:
        """Execute command with shell=False security.

        v2.1.0 (Loop 2): timeout is now configurable per-call (default
        180s, max 3600s, min 1s). The old global RUN_TIMEOUT=15 was too
        short for npm install, pytest, cargo build, docker build.
        """
        args, is_safe = _sanitize_command(command, project_root=str(self.workspace) if self.workspace else None)
        if not is_safe:
            return f"[SECURITY ERROR] Command blocked: {command}"

        path_error = self._validate_command_paths(args)
        if path_error:
            logger.warning("[security] %s (full command: %s)", path_error, command)
            return path_error

        if not self._request_confirmation("execute_command", f"Run: {command}"):
            return f"[REJECTED BY USER] command cancelled: {command}"

        # v2.1.0 (Loop 2): configurable per-call timeout with bounds.
        timeout = max(self.MIN_TIMEOUT, min(timeout, self.MAX_TIMEOUT))

        try:
            # v1.0.6: use Popen + polling so Stop can abort the
            # subprocess (M-RT-7). subprocess.run blocks up to
            # timeout with no cancellation path.
            proc = subprocess.Popen(
                args, shell=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.workspace,
            )
            stdout, stderr = b"", b""
            deadline = time.time() + timeout
            try:
                while proc.poll() is None:
                    if self.is_cancelled():
                        proc.kill()
                        proc.wait()
                        return "[CANCELLED BY USER] command aborted"
                    if time.time() > deadline:
                        proc.kill()
                        proc.wait()
                        return f"[TIMEOUT] Command exceeded {timeout}s"
                    time.sleep(0.25)
                stdout = proc.stdout.read()
                stderr = proc.stderr.read()
            except Exception:
                proc.kill()
                proc.wait()
                raise

            out_text = stdout.decode("utf-8", errors="replace")
            err_text = stderr.decode("utf-8", errors="replace")
            parts = []
            if out_text:
                parts.append(out_text[:self.MAX_OUTPUT])
            if err_text:
                parts.append(err_text[:self.MAX_OUTPUT])
            if proc.returncode != 0:
                parts.append(f"[EXIT CODE] {proc.returncode}")
            # v1.1.5-fix (tera_pilot_bug_report.md bug #9): if the agent just
            # ran `git init` / `git clone` (or any other command that
            # creates/removes a .git directory), invalidate the
            # GitService class-level cache so the next status poll
            # re-detects the repo instead of serving a stale "not a repo"
            # result. We do a cheap substring match on the first argv
            # rather than parsing the full command line.
            try:
                if args and isinstance(args, list) and len(args) >= 2:
                    bin_name = os.path.basename(str(args[0])).lower()
                    sub_cmd = str(args[1]).lower() if len(args) > 1 else ""
                    if bin_name == "git" and sub_cmd in {"init", "clone"}:
                        try:
                            from .git_service import GitService
                            GitService.invalidate_cache(str(self.workspace))
                        except Exception:
                            pass
            except Exception:
                pass
            return "\n".join(parts) if parts else "[NO OUTPUT]"
        except Exception as e:
            return f"[COMMAND ERROR] {e}"

    def _get_project_structure(self, directory: str = ".") -> str:
        try:
            base = self._resolve_path(directory)
            lines = []
            for root, dirs, files in os.walk(base):
                level = root.replace(str(base), "").count(os.sep)
                indent = "  " * level
                lines.append(f"{indent}{os.path.basename(root)}/")
                subindent = "  " * (level + 1)
                for file in sorted(files)[:20]:
                    lines.append(f"{subindent}{file}")
            return "\n".join(lines)
        except Exception as e:
            return f"[STRUCTURE ERROR] {e}"

    # ── v2.1.0 (G18): Web search / fetch ─────────────────────────────
    # ``_web_search`` is a thin wrapper that routes through MCPManager
    # (so it reuses MCPManager's process lifecycle, config loading from
    # ~/.tera_pilot/mcp.json, and catalog logic instead of a new HTTP client).
    # It uses an ordered-fallback pattern: if the primary MCP search
    # server is unavailable, it falls back to the next configured
    # backend; whichever backend actually served the request is
    # recorded in the result so the user/agent can see which one won.
    #
    # ``_web_fetch`` is implemented directly with stdlib urllib (no new
    # dependency — requests is already in requirements.txt but we
    # prefer urllib for the no-deps path) + a minimal HTML-to-text
    # extractor. It still goes through URL validation and output-size
    # discipline (max_chars) so a 5MB HTML page doesn't blow the
    # agent's context window.
    #
    # Both tools wrap their output through ``build_fragment()`` from
    # ``agent/context_fragments.py`` using a new fragment type
    # (``web_search`` / ``web_page``), so large fetched pages
    # tombstone-compact the same way old file reads already do, AND
    # so the result is structurally tagged as untrusted external
    # content (instructions appearing inside fetched web content are
    # data, not commands from the user — see Guardian's web-fetch
    # risk rule in ``agent/guardian.py``).

    def _web_search(self, query: str, num_results: int = 5) -> str:
        """Search the web via the configured MCP search backend.

        Routes through ``MCPManager`` so it reuses the existing
        process lifecycle + ``~/.tera_pilot/mcp.json`` config. If the
        primary search backend is unavailable, falls back to the next
        configured backend (ordered-fallback pattern). Whichever
        backend actually served the request is recorded in the
        result string so the agent + audit trail can see it.

        Args:
            query: the search query string. Required — empty query
                returns an error (avoid accidental "search for
                nothing" footguns, same rule as _grep).
            num_results: how many results to ask the backend for.
                Default 5; capped at 20 to keep the result bounded.

        Returns:
            A string starting with ``[WEB_SEARCH ...]`` containing
            the results, wrapped in a ``<context_fragment>`` so the
            output participates in tombstone-compaction and is tagged
            as untrusted external content. On any error, returns
            ``[WEB_SEARCH ERROR] ...`` instead (never raises).
        """
        if not query or not query.strip():
            return "[WEB_SEARCH ERROR] query is required"
        num_results = max(1, min(int(num_results or 5), 20))
        try:
            from tera_pilot.web_search_backend import (
                run_web_search, get_websearch_status,
            )
            results, served_by = run_web_search(
                query=query.strip(),
                num_results=num_results,
            )
            if not results:
                return (
                    f"[WEB_SEARCH NO RESULTS] query={query!r} "
                    f"(backend={served_by or 'none'}). Configure a "
                    f"search MCP server in ~/.tera_pilot/mcp.json or via "
                    f"the /websearch command."
                )
            # Format results as a compact list.
            lines = [
                f"[WEB_SEARCH] {len(results)} result(s) for {query!r} "
                f"(served_by={served_by or 'unknown'})",
                "",
            ]
            for i, r in enumerate(results, 1):
                title = (r.get("title") or "").strip()[:120]
                url = (r.get("url") or "").strip()[:300]
                snippet = (r.get("snippet") or "").strip()[:300]
                lines.append(f"{i}. {title}")
                lines.append(f"   URL: {url}")
                if snippet:
                    lines.append(f"   {snippet}")
                lines.append("")
            body = "\n".join(lines)
            # Wrap in a context fragment so it tombstone-compacts and
            # is tagged as untrusted external content.
            from tera_pilot.agent.context_fragments import build_fragment, stable_id
            fid = stable_id("web_search", query.strip())
            fragment = build_fragment("web_search", fid, body)
            return fragment
        except Exception as e:
            logger.warning("[web_search] failed: %s", e)
            return f"[WEB_SEARCH ERROR] {e}"

    def _web_fetch(self, url: str, max_chars: int = 8000) -> str:
        """Fetch a URL and return its content as plain text.

        Direct HTTP GET with stdlib urllib (no new dependency) +
        minimal HTML-to-text extraction. The result is truncated to
        ``max_chars`` and wrapped in a ``<context_fragment>`` so it
        participates in tombstone-compaction AND is tagged as
        untrusted external content (instructions appearing inside
        fetched web content are data, not commands from the user).

        URL validation rejects:
        - non-http(s) schemes (file://, ftp://, etc. — prevents the
          agent from reading local files via a URL bypass).
        - obviously-malicious URLs (long base64-like query params,
          secret-shaped strings) — these are flagged by Guardian as
          at-least-medium risk; here we just refuse to fetch them.

        Args:
            url: the URL to fetch. Required.
            max_chars: cap on the returned text length. Default 8000.

        Returns:
            A string starting with ``[WEB_FETCH ...]`` containing the
            extracted text, wrapped in a ``<context_fragment>``. On
            any error, returns ``[WEB_FETCH ERROR] ...`` instead.
        """
        if not url or not url.strip():
            return "[WEB_FETCH ERROR] url is required"
        url = url.strip()
        max_chars = max(100, min(int(max_chars or 8000), 50000))
        # Validate scheme — only http/https allowed (no file://, ftp://).
        if not (url.startswith("http://") or url.startswith("https://")):
            return (
                f"[WEB_FETCH ERROR] only http(s) URLs are allowed "
                f"(got {url[:80]!r})"
            )
        # Heuristic: reject URLs with very long base64-like query
        # params — these are often exfiltration attempts (secret
        # embedded as a query param) or prompt-injection vectors.
        # Guardian also flags these, but we double-check here so the
        # fetch never happens even if Guardian is disabled.
        suspicious_reason = _check_suspicious_url(url)
        if suspicious_reason:
            return (
                f"[WEB_FETCH REJECTED] URL looks suspicious: "
                f"{suspicious_reason}. If this is a false positive, "
                f"the user can fetch the URL manually and paste the "
                f"content into the chat."
            )
        try:
            from tera_pilot.web_search_backend import fetch_url_as_text
            text, status, final_url = fetch_url_as_text(url, max_chars=max_chars)
            if not text:
                return (
                    f"[WEB_FETCH EMPTY] {url} returned no text content "
                    f"(HTTP {status})"
                )
            body = (
                f"url: {final_url}\n"
                f"http_status: {status}\n"
                f"chars: {len(text)}\n"
                f"\n--- content ---\n{text}"
            )
            from tera_pilot.agent.context_fragments import build_fragment, stable_id
            fid = stable_id("web_page", url)
            fragment = build_fragment("web_page", fid, body)
            return f"[WEB_FETCH] {final_url} (HTTP {status}, {len(text)} chars)\n{fragment}"
        except Exception as e:
            logger.warning("[web_fetch] failed for %s: %s", url, e)
            return f"[WEB_FETCH ERROR] {url}: {e}"


def _check_suspicious_url(url: str) -> Optional[str]:
    """Return a reason string if the URL looks suspicious, else None.

    Heuristic checks:
    - Query params with long base64-like values (>=80 chars of
      [A-Za-z0-9+/=]) — often exfiltration or injection vectors.
    - URL containing obvious secret-shaped strings (api_key=,
      token=, password=, secret= followed by a long value).
    - Extremely long URL (>2000 chars) — almost always malicious.
    """
    if len(url) > 2000:
        return "URL is unusually long (>2000 chars)"
    # Look at the query string.
    if "?" in url:
        query = url.split("?", 1)[1]
        for param in query.split("&"):
            if "=" not in param:
                continue
            key, _, value = param.partition("=")
            key_lower = key.lower()
            # Secret-shaped keys.
            if key_lower in ("api_key", "apikey", "token", "access_token",
                              "password", "passwd", "secret", "client_secret"):
                if len(value) >= 16:
                    return f"URL contains secret-shaped param {key!r}"
            # Long base64-like values.
            if len(value) >= 80 and re.fullmatch(r"[A-Za-z0-9+/=_\-]+", value):
                return f"URL param {key!r} has a long base64-like value (>=80 chars)"
    return None
