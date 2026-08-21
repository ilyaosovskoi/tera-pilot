"""
AgentRuntime — the legacy ReAct agent loop.

Public API:
- run(), write(), edit(), refactor(), analyze(), generate_test(),
  debug(), chat() — high-level task entry points.
- run_stream() — streaming variant that yields AgentEvent
  updates for the UI.
- get_status(), get_history(), clear_history(), set_workspace()
  — introspection / control.

The runtime owns a ContextMemory and a ToolEngine. Each turn:
1. build a prompt (PromptBuilder),
2. call the provider (with retry + streaming),
3. parse the response (OutputParser),
4. dispatch any tool call (ToolEngine),
5. emit AgentEvent updates for the UI.

v2 wraps this in tera_pilot.agent.AgentRuntimeV2 to add interjection,
compaction v2, circuit breaker, sub-agent v2, and sandbox.
"""

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from tera_pilot.providers import ProviderRegistry, ProviderMessage, ProviderResponse
from tera_pilot.project_context import get_project_context
from tera_pilot.context_manager import get_context_manager
from tera_pilot.activity_log import CATEGORY_INFO, STATUS_OK, STATUS_ERROR
from tera_pilot.skill_loader import load_all_skills_with_builtins, build_skill_catalog
from .types import AgentEvent, AgentStep, Task, TaskResult, TaskType, ToolCall, ToolName
from .context_memory import ContextMemory, _estimate_tokens
from .tool_engine import ToolEngine
from .prompts import (
    PromptBuilder,
    TOOL_SCHEMA,
    SYSTEM_PROMPT,
    GENERAL_SYSTEM_SUFFIX,
    HEAVY_CODE_SYSTEM_SUFFIX,
)
from .parser import OutputParser, _warn_unknown_tools

EventCallback = Callable[[AgentEvent, Dict[str, Any]], None]

logger = logging.getLogger(__name__)


class AgentRuntime:
    """
    ReAct-style autonomous agent with tool-use loop.
    Thread-safe operations, JSON tool calling, secure command execution.

    v1.0.3: Uses ProviderRegistry instead of ModelEngine.
    Accepts a ProviderRegistry at init and calls provider.generate()
    with proper ProviderMessage objects.
    """

    def __init__(
        self,
        registry: "ProviderRegistry",
        workspace: Optional[str] = None,
        max_iterations: int = 8,
        enable_planning: bool = True,
        on_event: Optional[EventCallback] = None,
        verbose: bool = False,
        memory_persist_path: Optional[str] = None,
        token_tracker: Optional[Any] = None,
        section: str = "general",
        on_token_delta: Optional[Callable[[str], None]] = None,
        compact_prompt: Optional[bool] = None,
    ):
        self._registry = registry
        self.memory = ContextMemory(persist_path=memory_persist_path)
        self.memory.load()
        self.tools = ToolEngine(workspace)
        # v1.1.3-fix (bug 1.1/1.2): propagate runtime-level context down to
        # the ToolEngine so that _run_subagent_internal (which lives on
        # ToolEngine) can build child agents with the parent's registry,
        # event callback, token/quota trackers, and section. Without this,
        # spawn_subagent would crash with AttributeError on self._registry.
        self.tools._registry = registry
        self.tools.on_event = on_event
        self.tools._token_tracker = token_tracker
        self.tools._quota_tracker = None
        self.tools.section = section
        # Pass memory for Guardian context building
        self.tools.memory = self.memory
        # Pass provider for Guardian LLM calls
        self.tools._provider = registry.active
        self.task_history: List[Task] = []
        self.max_iterations = max_iterations
        self.enable_planning = enable_planning
        self.on_event = on_event
        self.verbose = verbose
        # v1.2.1-fix (Plan Mode gating): состояние для ожидания подтверждения плана
        self._pending_plan: Optional[Tuple[Task, str]] = None  # (task, plan_text) - ожидающий подтверждения
        # v1.0.5-correctness: token tracker for real usage accounting (H-RT-3).
        # If None, _generate_with_retry just skips the record() call.
        self._token_tracker = token_tracker
        # v1.1.1-fix: accumulate real token counts per run so the UI can
        # display them (previously _generate() discarded ProviderResponse
        # and only returned resp.text, losing tokens_in/out).
        self._run_tokens_in: int = 0
        self._run_tokens_out: int = 0
        # v1.1.0: section ("general" | "heavy_code" | "office") — controls:
        #   - which tools are advertised in the system prompt
        #     (subagent/multi-agent only in heavy_code)
        #   - which daily quota counter to bump (heavy_code = 10/day free)
        #   - the system prompt variant (Heavy Code gets a stronger one)
        self.section = section
        # v2.0.0-tui: per-token streaming callback. When set, _generate_with_retry
        # uses provider.stream() instead of provider.generate() and emits each
        # chunk through this callback + AgentEvent.TOKEN_DELTA before accumulating
        # the full text. This lets the TUI (and potentially the GUI agent-mode)
        # display text character-by-character instead of waiting for the entire
        # step to finish.  Previously the parameter existed in __init__ but was
        # never stored or used — a phantom feature (see SKILL.md §5).
        self._on_token_delta = on_token_delta
        # v2.3.5-fix (small-model support): compact-mode override. None =
        # auto-detect from the active model's size (<= ~8B → compact);
        # True/False forces it. Config knob: agent_compact_prompt.
        self._compact_override = compact_prompt
        # v1.1.0: quota tracker — lazily wired via set_quota_tracker().
        # When set, _generate_with_retry calls quota.record() and
        # _run_agent_loop checks quota.exhausted() before each LLM call.
        self._quota_tracker: Optional[Any] = None
        # v1.0.9: project context (CLAUDE.md) — loaded lazily on first use
        self._project_context = get_project_context()
        # v1.1.4-fix (bug 4.2): ContextManager was fully implemented
        # (relevance scoring + token-budgeted file selection) but never
        # instantiated anywhere. Wired in the same way as ProjectContext
        # so relevant files get auto-attached to the prompt — see
        # execute_task() for where the selection is actually injected.
        self._context_manager = get_context_manager()
        if workspace:
            self._project_context.set_root(workspace)
            self._context_manager.set_root(workspace)
        # v1.2.1-fix (review §4.3): size ContextMemory + ContextManager
        # budgets proportionally to the active provider's real context
        # window, instead of the hardcoded 8K/6K constants the review
        # flagged as an order of magnitude too small. We re-sync on every
        # _run_agent_loop entry so a provider switch picks up the new
        # window without needing a full AgentRuntime restart.
        self._sync_context_budgets()
        # v1.0.11: skills — load from project + user-global + builtins
        self._skills: List[Any] = []
        self._reload_skills()
        # G20b — provider/model override for sub-agent routing. When set
        # (via set_provider_override()), _generate_with_retry uses this
        # provider+model instead of registry.active. Used by the task-
        # decomposition router to place each subtask on a different model.
        # Both default to None (= use the registry's active provider/model,
        # preserving today's behavior). The override is per-runtime, NOT
        # per-call, so a child runtime spawned with an override uses that
        # override for its entire lifetime — clean separation, no leakage
        # back to the parent.
        self._provider_override: Optional[str] = None
        self._model_override: Optional[str] = None
        # v2.3.4-fix: provider quota cooldown — set after a 429 so the
        # next LLM call waits out the provider's retryDelay (see
        # _wait_out_provider_cooldown).
        self._provider_cooldown_until: float = 0.0

        logger.info("AgentRuntime initialized (Provider-backed)")

    # v1.2.1-fix (review §4.3): dynamically size the conversation memory
    # and the auto-attach file budget to match the active provider's
    # context window. The old constants (8K memory / 6K files) were an
    # order of magnitude smaller than what modern models (128K-1M)
    # actually support, forcing premature auto-compaction on long
    # sessions and starving the model of relevant file context.
    #
    # We leave generous headroom: memory gets ~window/4 (the rest goes
    # to system prompt, tool catalog, MCP, and the LLM's response),
    # files get ~window/8 (auto-attach is a "fast start" — the agent
    # can always read_file for more). Both are capped to avoid
    # pathological cases (a 1M-window model shouldn't try to stuff
    # 250K tokens of conversation into a single request — that would
    # blow latency and cost without benefit).
    _MEMORY_BUDGET_FRACTION: float = 0.25
    _FILE_BUDGET_FRACTION: float = 0.125
    _MEMORY_BUDGET_CAP: int = 128_000
    _FILE_BUDGET_CAP: int = 32_000
    _MEMORY_BUDGET_FLOOR: int = 4_000  # never go below the old default
    _FILE_BUDGET_FLOOR: int = 2_000

    def _sync_context_budgets(self) -> None:
        """v1.2.1-fix (review §4.3): re-size memory + file budgets to
        match the active provider's context window.

        Safe to call any time — if the registry has no active provider
        yet (early init), it silently falls back to the existing
        defaults (8K memory / 6K files) which are still set on the
        ContextMemory and ContextManager instances from their __init__.
        """
        try:
            # G20b: respect provider_override when sizing budgets — the
            # override provider may have a different context window.
            if self._provider_override:
                if not self._registry:
                    return
                provider = self._registry.get(self._provider_override)
                if provider is None:
                    return
            else:
                if not self._registry or not getattr(self._registry, "_active_id", None):
                    return
                provider = self._registry.active
            window = 8_192
            try:
                window = int(provider.get_context_window())
                if window <= 0:
                    window = 8_192
            except Exception:
                pass
            # Apply fractions + caps + floors.
            memory_budget = max(
                self._MEMORY_BUDGET_FLOOR,
                min(self._MEMORY_BUDGET_CAP, int(window * self._MEMORY_BUDGET_FRACTION)),
            )
            file_budget = max(
                self._FILE_BUDGET_FLOOR,
                min(self._FILE_BUDGET_CAP, int(window * self._FILE_BUDGET_FRACTION)),
            )
            # max_messages is sized from memory_budget — assume ~500
            # tokens per message (rough average for an exchange).
            max_messages = max(20, min(500, memory_budget // 500))
            self.memory.max_tokens = memory_budget
            self.memory.max_messages = max_messages
            # max_chars is now derived from max_tokens (4 chars/token)
            # so the two constraints stay consistent.
            self.memory.max_chars = memory_budget * 4
            self._context_manager.set_token_budget(file_budget)
            logger.info(
                "[agent] context budgets synced to provider window %d: "
                "memory=%d tokens / %d msgs, files=%d tokens",
                window, memory_budget, max_messages, file_budget,
            )
        except Exception as e:
            logger.debug("[agent] _sync_context_budgets failed: %s", e)

    def set_cancel_check(self, fn: Optional[Callable[[], bool]]) -> None:
        """Wire a zero-arg callable that returns True once the running
        task has been cancelled (Stop button). Passed straight through to
        the ToolEngine, which polls it while blocked on diff-review /
        confirmation prompts, and the agent loop polls it between
        iterations — so Stop actually halts further tool calls instead of
        just muting UI updates while the loop keeps running.
        """
        self.tools._cancel_check = fn

    # G20b — provider/model override for sub-agent routing
    def set_provider_override(
        self,
        provider_id: Optional[str],
        model: Optional[str] = None,
    ) -> None:
        """Override which provider+model this runtime uses for LLM calls.

        When ``provider_id`` is set, the runtime looks up that provider in
        its ``ProviderRegistry`` (instead of using ``registry.active``)
        on every LLM call. When ``model`` is also set, it is passed to
        ``provider.generate(messages, model=model)`` — overriding the
        provider's configured model.

        Passing ``None`` for both clears the override and reverts to the
        registry's active provider (today's behavior).

        Used by :class:`tera_pilot.task_decomposition_router.TaskDecompositionRouter`
        to place each subtask on a different model — the child runtime is
        constructed normally, then ``set_provider_override(pid, model)``
        is called before the child's first LLM call.
        """
        self._provider_override = provider_id or None
        self._model_override = model or None
        # Re-sync context budgets — the override provider may have a
        # different context window than the active one. Cheap if the
        # override matches the active provider.
        self._sync_context_budgets()

    def _get_active_provider(self) -> Any:
        """Return the provider this runtime should call.

        G20b: if ``_provider_override`` is set, look it up in the
        registry (and ``.load()`` it if needed). Otherwise fall back to
        ``registry.active`` (today's behavior).

        Raises ``RuntimeError`` if the override is set but the registry
        has no provider with that id — better to fail loudly than to
        silently fall back to a different model the user didn't ask for.
        """
        if self._provider_override:
            if not self._registry:
                raise RuntimeError(
                    f"provider_override={self._provider_override!r} set "
                    "but no registry available"
                )
            provider = self._registry.get(self._provider_override)
            if provider is None:
                raise RuntimeError(
                    f"provider_override={self._provider_override!r} not "
                    f"found in registry (configured: "
                    f"{getattr(self._registry, '_providers', {}).keys() if self._registry else []})"
                )
            if hasattr(provider, "is_loaded") and not provider.is_loaded:
                provider.load()
            return provider
        provider = self._registry.active
        if hasattr(provider, "is_loaded") and not provider.is_loaded:
            provider.load()
        return provider

    def set_token_tracker(self, tracker: Optional[Any]) -> None:
        """Attach (or detach) a token tracker for real usage accounting.

        v1.0.5-correctness: the bridge/api_server create the AgentRuntime
        before they create the TokenTracker, so we expose a setter to
        wire the tracker in after the fact. When attached, every
        successful ``provider.generate()`` call records ``tokens_in`` /
        ``tokens_out`` (H-RT-3).
        """
        self._token_tracker = tracker
        # v1.1.3-fix (bug 1.2): mirror to ToolEngine so sub-agent
        # spawning (which lives on ToolEngine) can propagate the tracker
        # to child AgentRuntime instances.
        self.tools._token_tracker = tracker

    def set_quota_tracker(self, tracker: Optional[Any]) -> None:
        """v1.1.0: attach (or detach) the daily quota tracker.

        When attached, _run_agent_loop checks ``tracker.exhausted(section)``
        before the first LLM call and raises a friendly error if the
        section's daily limit is reached. _generate_with_retry calls
        ``tracker.record(section, provider, model)`` after each
        successful LLM call.
        """
        self._quota_tracker = tracker
        # v1.1.3-fix (bug 1.2): mirror to ToolEngine so sub-agent
        # spawning can propagate the quota tracker to child agents.
        self.tools._quota_tracker = tracker

    def set_section(self, section: str) -> None:
        """v1.1.0: switch the runtime's section ("general" | "heavy_code"
        | "office"). Affects which tools are advertised and which quota
        counter is bumped."""
        if section not in ("general", "heavy_code", "office"):
            section = "general"
        self.section = section
        # Propagate to ToolEngine so _dispatch can reject section-gated
        # tools (spawn_subagent, spawn_multi_agents).
        self.tools.section = section

    def set_autonomy(self, level: str) -> None:
        """'always_ask' | 'new_files_only' | 'never_ask' — see
        ToolEngine._request_confirmation for what each level gates."""
        if level not in ("always_ask", "new_files_only", "never_ask"):
            level = "always_ask"
        self.tools.autonomy = level

    def set_confirm_callback(self, fn: Optional[Callable]) -> None:
        """Wire the UI callback used for non-diff-review confirmations
        (execute_command, delete_file, rename_file, apply_diff,
        write_binary_file, git_commit)."""
        self.tools._confirm_callback = fn

    def set_guardian_callback(self, fn: Optional[Callable]) -> None:
        """Wire the UI callback used for Guardian safety review MODIFY verdicts."""
        self.tools._guardian_callback = fn

    def get_token_stats(self) -> Dict[str, Any]:
        """Return token/cost usage for this runtime.

        v2.3.2-fix: the daemon (``daemon.py``) and the e2e harness
        (``e2e_agent_test.py``) called ``agent.get_token_stats()``, but
        the method never existed on ``AgentRuntime`` — both call sites
        degraded silently inside try/except and the daemon recorded
        zero tokens/cost for every task. When a ``TokenTracker`` is
        attached its all-time totals are used (they include sub-agent
        calls and real pricing); otherwise the per-run counters
        (``_run_tokens_in`` / ``_run_tokens_out``) are returned.
        """
        tracker = getattr(self, "_token_tracker", None)
        if tracker is not None:
            try:
                t = tracker.stats()
                total_in = int(t.get("total_tokens_in", 0) or 0)
                total_out = int(t.get("total_tokens_out", 0) or 0)
                total_cost = float(t.get("total_cost", 0.0) or 0.0)
                requests = int(t.get("request_count", 0) or 0)
            except Exception:
                total_in, total_out = self._run_tokens_in, self._run_tokens_out
                total_cost, requests = 0.0, 0
        else:
            total_in, total_out = self._run_tokens_in, self._run_tokens_out
            total_cost, requests = 0.0, 0
        return {
            "total_tokens_in": total_in,
            "total_tokens_out": total_out,
            "total_tokens": total_in + total_out,
            # Both spellings are used by callers: daemon.py reads
            # ``total_cost_usd``, TokenTracker.stats() exposes
            # ``total_cost``.
            "total_cost_usd": total_cost,
            "total_cost": total_cost,
            "request_count": requests,
        }

    def _reload_skills(self) -> None:
        """v1.0.11: (re)load the skill list from disk.

        Called on init and after set_workspace (so opening a different
        project picks up its .tera_pilot/skills/). The skill catalog is
        injected into the system prompt on the next agent run.
        """
        ws = self.tools.workspace if self.tools and self.tools.workspace else None
        self._skills = load_all_skills_with_builtins(ws)
        # Inject the skill list into the ToolEngine so _get_skill works
        self.tools.set_skills(self._skills)
        logger.info("[agent] loaded %d skills", len(self._skills))

    # ── v1.0.9: Context management ────────────────────────────────────

    def context_status(self) -> Dict[str, Any]:
        """Return a status dict for the /context command.

        Combines:
          - ContextMemory status (messages, tokens, utilization)
          - ProjectContext status (CLAUDE.md sources, char count)
          - System prompt size
        """
        mem_status = self.memory.status()
        # v1.0.9: call instructions() first so the cache is populated
        # before we read status() — otherwise status() shows no sources
        # even when CLAUDE.md exists.
        self._project_context.instructions()
        proj_status = self._project_context.status()
        sys_prompt_chars = len(self._system_prompt())
        # v1.1.4-fix (bug 4.2): surface ContextManager's file selection
        # (what's actually auto-attached to the prompt) alongside
        # conversation memory — previously invisible even though it was
        # already being computed on every task once wired in.
        try:
            file_selection = self._context_manager.select_context()
        except Exception:
            file_selection = None
        return {
            "memory": mem_status,
            "project_context": proj_status,
            "files": file_selection,
            "system_prompt_chars": sys_prompt_chars,
            "system_prompt_tokens": _estimate_tokens(self._system_prompt()),
            "workspace": str(self.tools.workspace) if self.tools.workspace else None,
        }

    def clear_context(self) -> Dict[str, Any]:
        """v1.0.9: /clear command — wipe conversation memory + compaction.

        Does NOT touch the project's CLAUDE.md — that's persistent project
        instructions, not conversation history.
        """
        self.memory.clear()
        logger.info("[agent] context cleared by /clear command")
        return {"ok": True, "message": "Context cleared. Start fresh."}

    def compact_context(self) -> Dict[str, Any]:
        """v1.0.9: /compact command — summarise old messages, keep recent.

        Uses the active provider to generate a summary of the conversation
        so far, then replaces old messages with the summary. The most
        recent 4 messages are kept verbatim so the agent has immediate
        context for the next turn.
        """
        if not self.memory.messages:
            return {"ok": True, "message": "Nothing to compact — memory is empty."}
        if len(self.memory.messages) <= 4:
            return {"ok": True, "message": "Not enough messages to compact (need > 4)."}

        # Build a compaction prompt
        history = self.memory.to_prompt_history()
        compact_prompt = (
            "Summarise the following conversation, preserving:\n"
            "- Key decisions and their rationale\n"
            "- Files that were read, created, or modified (with paths)\n"
            "- Any errors encountered and how they were resolved\n"
            "- Open questions or TODOs\n\n"
            "Be concise but complete. Output a markdown summary, no preamble.\n\n"
            f"Conversation to summarise:\n{history}"
        )
        try:
            # v1.1.3-fix (bug 1.5): route through _generate_with_retry
            # (via _generate_with_explicit_system) instead of calling
            # provider.generate() directly. A transient 429/503 during
            # auto-compaction used to fail compact_context(), which then
            # fell back to force-trimming half the history WITHOUT a
            # summary — silently losing context. With retry, transient
            # errors recover; only persistent failures fall back.
            summary, _tok_in, _tok_out = self._generate_with_explicit_system(
                system_prompt="You are a conversation summarizer. Be concise and factual.",
                user_prompt=compact_prompt,
            )
            summary = summary.strip()
            self.memory.compact(summary, keep_recent=4)
            logger.info("[agent] context compacted by /compact command")
            return {"ok": True, "message": "Context compacted.",
                    "summary_chars": len(summary),
                    "kept_messages": len(self.memory.messages)}
        except Exception as e:
            logger.error("[agent] compaction failed: %s", e)
            return {"ok": False, "error": str(e)}

    def _maybe_auto_compact(self) -> bool:
        """v1.0.9: auto-compact if context is over 85% of budget.

        Called before each LLM call in _run_agent_loop. Returns True if
        compaction happened (so the loop can log it / notify the UI).

        v1.0.5-correctness: if ``compact_context()`` fails (provider
        error, network blip), the previous code returned ``False`` and
        the loop proceeded to call the provider with context that was
        already over 85% of budget — which may exceed the context
        window and fail with a more confusing error. We now fall back
        to force-trimming the oldest non-system messages so the next
        LLM call has at least a chance of fitting (BUGS_REPORT M-RT-2).
        """
        if not self.memory.should_compact(threshold=0.85):
            return False
        logger.info("[agent] auto-compacting context (over 85%% budget)")
        result = self.compact_context()
        if result.get("ok", False):
            return True
        # Graceful degradation: force-trim oldest messages so we don't
        # blow the context window on the next LLM call. Keep the most
        # recent half of the messages (or at least 4).
        try:
            msgs = self.memory.messages
            keep = max(4, len(msgs) // 2)
            if len(msgs) > keep:
                logger.warning(
                    "[agent] auto-compact failed (%s); force-trimming to last %d messages",
                    result.get("error", "unknown"), keep,
                )
                # Replace the message list under the lock so concurrent
                # readers (to_prompt_history, save, status) don't see a
                # partially-replaced list (RuntimeError: list changed size
                # during iteration).
                with self.memory._lock:
                    self.memory.messages = msgs[-keep:]
                self.memory.save()
        except Exception as trim_err:
            logger.error("[agent] force-trim also failed: %s", trim_err)
        return False

    def _emit(self, event: AgentEvent, **data):
        if self.on_event:
            try:
                self.on_event(event, data)
            except Exception as e:
                logger.debug(f"Event callback error: {e}")
        if self.verbose:
            logger.debug(f"[EVENT] {event.value}: {data}")

    def _generate(self, prompt: str, *, include_system: bool = True) -> Tuple[str, int, int]:
        """Call the active provider's generate() with a plain prompt.

        v1.0.6: when ``include_system`` is True, the system prompt is
        sent as a SEPARATE ``role="system"`` message — not concatenated
        into the user prompt. Many providers (Groq's llama-3.3-70b in
        particular) follow agent/tool-use instructions far more reliably
        when the system role is distinct from the user role. The old
        concatenated form was getting the agent's "use JSON tool calls"
        instruction treated as user content, which led to the model
        replying with prose ("I can't write files, here's the code…")
        instead of emitting a write_file tool call.

        Returns (text, tokens_in, tokens_out).
        """
        provider = self._get_active_provider()
        if not provider.is_loaded:
            provider.load()
        messages: List[ProviderMessage] = []
        if include_system:
            messages.append(ProviderMessage(role="system", content=self._system_prompt()))
        messages.append(ProviderMessage(role="user", content=prompt))
        resp = self._generate_with_retry(provider, messages)
        return resp.text, int(getattr(resp, 'tokens_in', 0) or 0), int(getattr(resp, 'tokens_out', 0) or 0)

    def _use_compact_prompt(self) -> bool:
        """Whether to run in compact mode (lean prompt + essential tools).

        v2.3.5-fix (small-model support): the full system prompt +
        26 native tool schemas are ~8.3K tokens. Small but capable
        agent models (e.g. LFM 2.5 2.6B) lose the plot under that
        load — they degrade into repeating their first tool call.
        Compact mode cuts the prompt ~5x and advertises only the
        essential tools, which lets such models actually complete
        tasks (the security layer — command policy, sandbox, diff
        review — is unchanged and enforced in code).

        Resolution order: explicit ``compact_prompt`` override (config
        ``agent_compact_prompt``) > heuristic on the active model's
        size (models whose name contains a size <= 8B, e.g. ``2.6b``,
        ``7b`` → compact; ``27b``, ``70b`` → full prompt).
        """
        if self._compact_override is not None:
            return bool(self._compact_override)
        try:
            provider = self._registry.active
            model = (getattr(getattr(provider, "config", None), "model", None) or "").lower()
        except Exception:
            model = ""
        m = re.search(r'(\d+(?:\.\d+)?)\s*b\b', model)
        if m:
            try:
                if float(m.group(1)) <= 8.0:
                    return True
            except ValueError:
                pass
        return False

    def _system_prompt(self, section: str = "general") -> str:
        """System prompt for *section*, honoring compact mode."""
        return PromptBuilder.system(section=section, compact=self._use_compact_prompt())

    def _native_tools(self) -> Optional[List[dict]]:
        """Return the OpenAI-function-format tool schemas for this run,
        or None when the active provider can't do native tool calling.

        v2.3.4-fix (model compatibility): the legacy ReAct loop only
        advertised tools as TEXT in the system prompt. Models trained
        for NATIVE tool calling (OpenAI gpt-oss, newer checkpoints)
        respond with an API-level tool_call instead of text JSON — and
        with no ``tools`` in the request the provider rejects it (400
        "tool choice is none, but model called a tool"). Passing the
        native schemas lets those models work: their tool_calls are
        serialized back into the ``{"tool": ..., "args": ...}`` text
        format by the provider before the OutputParser runs.

        v2.3.5-fix: in compact mode only the essential tools are
        advertised (build_native_tools_schema(..., compact=True)) so
        small models aren't drowning in 26 schemas.
        """
        try:
            from tera_pilot.providers.base import ProviderCapability
            provider = self._get_active_provider()
            caps = getattr(provider, "capabilities", frozenset())
            if ProviderCapability.TOOL_CALLING not in caps:
                return None
            # A provider may declare TOOL_CALLING yet not accept the
            # ``tools`` kwarg (test fakes, some local adapters). Passing
            # it would TypeError every call — check the signature first.
            import inspect
            sig = inspect.signature(provider.generate)
            if "tools" not in sig.parameters:
                return None
            from .prompts import build_native_tools_schema
            return build_native_tools_schema(self.section, compact=self._use_compact_prompt())
        except Exception:
            # Never break the agent loop over schema plumbing.
            return None

    def _tools_to_send(self) -> Optional[List[dict]]:
        """The native tool schemas to actually SEND to the provider.

        v2.3.5-fix (LM Studio / LFM 2.5): ``_native_tools()`` decides
        whether the runtime CAN do native tool calling (capability +
        signature checks) and is used to enable the native-history loop.
        But some engines that declare TOOL_CALLING cannot SAFELY
        generate native tool calls — LM Studio hard-400s generated calls
        whose content contains quotes ("Invalid diff"). For those,
        advertise NO ``tools`` schema and parse the model's native text
        format instead.
        """
        tools = self._native_tools()
        if not tools:
            return None
        try:
            provider = self._get_active_provider()
        except Exception:
            return tools
        if not getattr(provider, "emits_native_tool_calls", True):
            return None
        return tools

    def _generate_with_explicit_system(self, system_prompt: str,
                                        user_prompt: str,
                                        tools: Optional[List[dict]] = None) -> Tuple[str, int, int]:
        """v1.0.6 — call the provider with an EXPLICIT system message.

        Used by the agent loop so the tool-use instructions land in the
        system role (where models pay attention to them) instead of
        being concatenated into the user prompt (where they get treated
        as content to respond to).

        ``tools`` — optional native tool schemas (see ``_native_tools``);
        lets native-tool-calling models work alongside text-JSON models.

        Returns (text, tokens_in, tokens_out).
        """
        provider = self._get_active_provider()
        if not provider.is_loaded:
            provider.load()
        messages: List[ProviderMessage] = [
            ProviderMessage(role="system", content=system_prompt),
            ProviderMessage(role="user", content=user_prompt),
        ]
        resp = self._generate_with_retry(provider, messages, tools=tools)
        return resp.text, int(getattr(resp, 'tokens_in', 0) or 0), int(getattr(resp, 'tokens_out', 0) or 0)

    def _generate_native_history(self, messages, tools: Optional[List[dict]] = None):
        """Generate over a NATIVE message history and return the response.

        v2.3.5-fix (small-model support): the legacy loop embeds every
        observation into one growing user prompt. Models trained on
        native tool calling (e.g. LFM 2.6B) follow their plan correctly
        only when the conversation is real OpenAI-style messages
        (assistant ``tool_calls`` + ``role="tool"`` results); with
        observations buried in prose they degenerate into a read_file
        loop. This helper calls the provider with the message list and
        returns the full ProviderResponse so the caller can read
        ``resp.tool_calls``.

        The non-streaming ``generate()`` is used so ``resp.tool_calls``
        is populated (the streaming adapter only forwards content
        deltas). If a token-delta callback is wired (TUI), the
        accumulated text is replayed through it afterwards so the UI
        still streams.
        """
        provider = self._get_active_provider()
        if not provider.is_loaded:
            provider.load()
        resp = self._generate_with_retry(provider, messages, tools=tools,
                                         force_non_streaming=True)
        if self._on_token_delta is not None and resp is not None and resp.text:
            try:
                for chunk in resp.text:
                    self._on_token_delta(chunk)
            except Exception:
                pass  # UI errors must never crash the agent loop.
        return resp

    # ── v1.0.5-correctness: provider call with retry + token tracking ──

    # H-RT-1: a single transient 429/5xx/network blip used to abort the
    # entire agent task. We now retry with exponential backoff + jitter.
    # H-RT-3: real token usage from `ProviderResponse.tokens_in/out` was
    # being discarded; the only accounting was a char-count heuristic.
    # We now record actual usage to the shared `token_tracker` so the
    # UI's cost/burn-rate/budget features are accurate.

    _RETRY_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
    _RETRY_MAX_ATTEMPTS = 5
    # v2.3.4: quota/429 errors are SOFT failures — the upstream explicitly
    # asks us to retry shortly (Retry-After). Give them a longer budget than
    # plain transient errors: a saturated free-tier shared pool (e.g.
    # OpenRouter's z-ai/glm-5.2:free → Decart) can stay 429 for 60-90s, and
    # the old 5×10s budget gave up just before it recovered.
    _RETRY_QUOTA_MAX_ATTEMPTS = 8
    _RETRY_BASE_DELAY = 1.0   # seconds
    _RETRY_MAX_DELAY = 90.0   # seconds
    # 429/quota errors are honoured with the provider's own retryDelay
    # (e.g. Gemini free tier: "Please retry in 41s"). Never hammer a
    # quota-exhausted endpoint with less than this floor.
    _RETRY_QUOTA_MIN_DELAY = 10.0

    def _is_retryable(self, exc: Exception) -> bool:
        """Return True if *exc* looks like a transient provider error."""
        msg = str(exc).lower()
        # ProviderError carries the upstream status code in its message
        # in a few well-known forms. Match the most common substrings.
        if any(s in msg for s in ("rate limit", "rate-limit", "too many requests",
                                   "service unavailable", "bad gateway",
                                   "gateway timeout", "temporarily unavailable",
                                   "connection reset", "timed out", "timeout",
                                   "read timeout", "connection aborted")):
            return True
        # v2.3.5-fix (LM Studio / LFM 2.5): generation-time validation
        # failure. LM Studio's engine validates the tool call the model
        # is currently GENERATING; a small model sometimes emits a
        # string argument with an unescaped quote (e.g. ``content`` =
        # ``query = f"SELECT ...``) and the engine rejects it with
        # ``400 Invalid diff: '…' not found at start of '…'``
        # (server_error 500 inside). This is a MODEL-OUTPUT flake, not
        # a client error — the exact same request can succeed on the
        # next sample — so retry instead of killing the run.
        if "invalid diff" in msg or "engine protocol predict stream returned an error" in msg:
            return True
        # Status code patterns like "HTTP 429" / "status 503" / "[429]"
        import re as _re
        m = _re.search(r'(?:http|status)?\s*[\[\(]?(\d{3})[\]\)]?', msg)
        if m:
            try:
                code = int(m.group(1))
                if code in self._RETRY_STATUS_CODES:
                    return True
            except ValueError:
                pass
        return False

    @staticmethod
    def _extract_retry_delay(exc: Exception) -> Optional[float]:
        """Parse the provider's suggested retry delay out of an error.

        Providers embed a retry hint in 429/503 errors, e.g.:
          - Gemini:   "Please retry in 41.343146745s."
          - JSON:     "retryDelay": "41s"  /  retryDelay: 41s

        v2.3.4-fix (real-run): we used to ignore this hint entirely and
        retry with a 1s→16s backoff — far below the provider's own
        estimate, so a free-tier quota hit (5 req/min on Gemini) was
        retried too early, failed again, and the whole agent run died
        after 3 attempts.

        Returns None when no hint is present (plain backoff applies).
        """
        import re as _re
        msg = str(exc)
        m = _re.search(r'retry\s*(?:in|delay)\D*([\d.]+)s', msg, _re.IGNORECASE)
        if not m:
            return None
        try:
            return max(float(m.group(1)), 0.0)
        except ValueError:
            return None

    def _quota_backoff_delay(self, exc: Exception) -> float:
        """Retry delay for 429/quota errors: honour the provider hint.

        Returns a delay >= ``_RETRY_QUOTA_MIN_DELAY`` so we never
        hammer an exhausted quota endpoint, and never less than the
        provider's own estimate.
        """
        hint = self._extract_retry_delay(exc)
        if hint is not None:
            return max(hint, self._RETRY_QUOTA_MIN_DELAY)
        # No explicit hint — give the sliding window time to slide.
        return self._RETRY_QUOTA_MIN_DELAY

    def _quota_exhausted_error(self, provider, last_exc: Exception,
                               attempts: int, elapsed: float) -> None:
        """Raise an actionable rate-limit error after retries run out.

        v2.3.4: the old code surfaced the raw upstream JSON blob
        (``OpenRouter HTTP 429: {...}``) — it told the user nothing about
        what to do. Extract the upstream provider name and produce a short
        message with concrete next steps (wait / switch model / switch
        provider), keeping the original error as ``__cause__`` for logs.
        """
        import re as _re
        _upstream = ""
        m = _re.search(r'"provider_name"\s*:\s*"([^"]+)"', str(last_exc))
        if m:
            _upstream = f" (upstream: {m.group(1)})"
        model_name = getattr(getattr(provider, "config", None), "model", "?")
        from tera_pilot.providers import ProviderError
        err = ProviderError(
            f"Rate limit: {provider.provider_id} model {model_name!r} is "
            f"temporarily rate-limited{_upstream} — {attempts} attempts over "
            f"{elapsed:.0f}s. Wait a minute and retry, or switch to another "
            "model/provider in Settings."
        )
        raise err from last_exc

    def _wait_out_provider_cooldown(self) -> None:
        """Pause until the provider's quota cooldown expires.

        After a 429 the provider told us to wait ~N seconds. We set
        ``self._provider_cooldown_until`` so the *next* LLM call in the
        agent loop also respects that pause (otherwise a fresh burst of
        calls right after recovery would re-trip the quota).
        Cancellation-aware: a Stop click aborts the wait.
        """
        now = time.time()
        if now >= getattr(self, "_provider_cooldown_until", 0.0):
            return
        remaining = self._provider_cooldown_until - now
        logger.info("[agent] provider quota cooldown — waiting %.1fs", remaining)
        while now < self._provider_cooldown_until:
            if self.tools.is_cancelled():
                return
            time.sleep(min(0.5, self._provider_cooldown_until - now))
            now = time.time()

    def _generate_with_retry(self, provider, messages, tools: Optional[List[dict]] = None,
                             force_non_streaming: bool = False):
        """Call provider.generate with exponential-backoff retry.

        Retries on transient errors (429, 5xx, timeouts, connection
        resets) up to ``_RETRY_MAX_ATTEMPTS`` times. Auth errors and
        other 4xx (non-transient) errors are NOT retried — they bubble
        out immediately so the caller can surface them to the user.

        Also records the actual token usage (tokens_in / tokens_out) to
        the agent's ``token_tracker`` if one is attached, so the UI's
        cost/burn-rate/budget features reflect real usage instead of
        a char-count heuristic.

        v1.0.5-hotfix: added INFO logging at call start/end so the user
        can see what's happening when a call is slow (the user reported
        "долго отвечает" — with this logging they'll see exactly which
        step is slow and how long it took).

        v2.0.0-tui: when ``self._on_token_delta`` is set, we use
        ``provider.stream()`` instead of ``provider.generate()`` so that
        each token chunk is relayed to the UI in real time. The full
        text is still accumulated and returned as a ProviderResponse so
        the agent loop can parse tool calls exactly as before.

        v2.3.5-fix: ``force_non_streaming=True`` skips the streaming
        branch even when a token-delta callback is wired — the native
        history loop needs ``resp.tool_calls``, which the streaming
        adapter (content deltas only) cannot provide.
        """
        # v2.0.0-tui: if a token-delta callback is wired, use streaming
        # (unless the native-history loop asked for non-streaming).
        if self._on_token_delta is not None and not force_non_streaming:
            return self._generate_streaming_with_retry(provider, messages, tools=tools)

        import random as _random
        last_exc: Optional[Exception] = None
        call_start = time.time()
        # v2.3.5-fix (LM Studio / LFM 2.5): the model keeps re-emitting the
        # same unescaped quote in a tool-call argument; nudge it ONCE so
        # the retry has a real chance instead of burning attempts.
        _diff_hint_added = False
        model_name = getattr(getattr(provider, "config", None), "model", "?")
        logger.info("[agent] LLM call starting — provider=%s model=%s timeout=%.0fs",
                    provider.provider_id, model_name,
                    getattr(getattr(provider, "config", None), "timeout", 0))

        # v2.0.1 (G3): enforce token budget caps BEFORE the call so a
        # blown daily/monthly cap short-circuits with a friendly error
        # instead of letting the provider fail with a confusing 429.
        try:
            from tera_pilot.token_budget import check_budget
            budget_check = check_budget(token_tracker=getattr(self, "_token_tracker", None))
            if budget_check.exceeded:
                raise RuntimeError(budget_check.reason)
        except RuntimeError:
            raise
        except Exception as _be:
            logger.debug("[agent] budget check skipped: %s", _be)

        # v2.3.4-fix: respect the provider's quota cooldown before every
        # call — a previous 429 told us to wait N seconds; firing a fresh
        # burst right after recovery re-trips the quota (Gemini free tier
        # is 5 req/min).
        self._wait_out_provider_cooldown()

        # v2.3.4: quota errors get a longer attempt budget (see
        # _RETRY_QUOTA_MAX_ATTEMPTS) so saturated free-tier pools have
        # time to recover; plain transient errors keep the old 5.
        total_attempts = self._RETRY_MAX_ATTEMPTS
        for attempt in range(1, self._RETRY_QUOTA_MAX_ATTEMPTS + 1):
            try:
                # G20b: pass model_override to provider.generate() if set.
                # consensus_engine does the same — provider.generate()
                # accepts an optional ``model`` kwarg that overrides the
                # provider's configured model for this call only.
                # v2.3.4-fix: thread native ``tools`` through so
                # native-tool-calling models are accepted by the API.
                # Only pass the kwarg when non-empty — some providers /
                # test fakes reject an explicit ``tools=None``.
                gen_kwargs = {}
                if tools:
                    gen_kwargs["tools"] = tools
                if self._model_override:
                    resp = provider.generate(messages, model=self._model_override, **gen_kwargs)
                else:
                    resp = provider.generate(messages, **gen_kwargs)
                elapsed = time.time() - call_start
                logger.info("[agent] LLM call completed in %.1fs — provider=%s model=%s tokens_in=%d tokens_out=%d",
                            elapsed, provider.provider_id, model_name,
                            int(getattr(resp, "tokens_in", 0) or 0),
                            int(getattr(resp, "tokens_out", 0) or 0))
                # v1.0.5-correctness: record real token usage (H-RT-3).
                try:
                    tracker = getattr(self, "_token_tracker", None)
                    if tracker is not None:
                        tracker.record(
                            provider=provider.provider_id,
                            model=getattr(resp, "model", None) or provider.config.model,
                            tokens_in=int(getattr(resp, "tokens_in", 0) or 0),
                            tokens_out=int(getattr(resp, "tokens_out", 0) or 0),
                        )
                except Exception as track_err:
                    logger.debug("[agent] token_tracker.record failed: %s", track_err)
                # v1.1.0: record quota usage (per-section daily counter).
                # Only count SUCCESSFUL calls — failed retries don't
                # consume the user's daily quota.
                try:
                    quota = getattr(self, "_quota_tracker", None)
                    if quota is not None:
                        quota.record(
                            section=self.section,
                            provider=provider.provider_id,
                            model=getattr(resp, "model", None) or provider.config.model,
                        )
                except Exception as quota_err:
                    logger.debug("[agent] quota.record failed: %s", quota_err)
                return resp
            except Exception as exc:
                last_exc = exc
                elapsed = time.time() - call_start
                if self._is_quota_error(exc):
                    total_attempts = self._RETRY_QUOTA_MAX_ATTEMPTS
                if attempt >= total_attempts:
                    logger.warning("[agent] LLM call FAILED after %d attempts (%.1fs): %s",
                                   attempt, elapsed, exc)
                    break
                if not self._is_retryable(exc):
                    # Non-transient (auth, bad request, etc.) — don't retry.
                    logger.warning("[agent] LLM call FAILED (%.1fs, non-retryable): %s",
                                   elapsed, exc)
                    break
                # v2.3.4-fix: quota/429 errors honour the provider's own
                # retryDelay (Gemini free tier: "Please retry in 41s")
                # instead of the old 1s→16s backoff that was far too
                # short and killed the run after 3 attempts. Also set a
                # cooldown so the NEXT agent-loop call is paced too.
                delay = min(self._RETRY_MAX_DELAY,
                            self._RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                delay = delay * (0.5 + 0.5 * _random.random())
                if self._is_quota_error(exc):
                    qdelay = self._quota_backoff_delay(exc)
                    if qdelay > delay:
                        delay = qdelay
                    cooldown_until = time.time() + qdelay
                    if cooldown_until > getattr(self, "_provider_cooldown_until", 0.0):
                        self._provider_cooldown_until = cooldown_until
                # v2.3.5-fix (LM Studio / LFM 2.5): when the engine
                # rejected the model's tool-call generation ("Invalid
                # diff"), append a ONE-TIME corrective note to the
                # message list before retrying — a small model keeps
                # emitting the same unescaped quote unless told. Only
                # the native-history path shares ``messages`` with the
                # agent loop (which is what we want: the hint persists
                # into the conversation); the legacy path passes a fresh
                # list per call, so the hint is harmless there.
                if not _diff_hint_added and "invalid diff" in str(exc).lower():
                    _diff_hint_added = True
                    try:
                        messages.append(ProviderMessage(
                            role="user",
                            content=(
                                "[SYSTEM NOTE] Your previous response was rejected: a tool-call "
                                "argument contained an unescaped double quote. When you put "
                                "code that contains double quotes into a JSON argument value "
                                "(e.g. write_file content), escape EVERY double quote as \\\" "
                                "inside the argument string."
                            ),
                        ))
                        logger.info("[agent] LM Studio invalid-diff retry: appended escape hint")
                    except Exception as _hint_err:
                        logger.debug("[agent] hint append failed: %s", _hint_err)
                logger.warning(
                    "[agent] transient provider error (attempt %d/%d, %.1fs): %s — retrying in %.1fs",
                    attempt, total_attempts, elapsed, exc, delay,
                )
                # Sleep, but check cancellation every 0.25s so a Stop
                # click can still abort the wait.
                slept = 0.0
                while slept < delay:
                    if self.tools.is_cancelled():
                        raise exc
                    step = min(0.25, delay - slept)
                    time.sleep(step)
                    slept += step
        # All retries exhausted (or non-retryable) — re-raise the last error.
        if last_exc is not None and self._is_quota_error(last_exc):
            # v2.3.4: surface an actionable message instead of raw JSON.
            self._quota_exhausted_error(
                provider, last_exc, total_attempts, time.time() - call_start,
            )
        raise last_exc if last_exc is not None else RuntimeError("generate failed")

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        """True for 429 / RESOURCE_EXHAUSTED / quota-exceeded errors."""
        msg = str(exc).lower()
        if "429" in msg or "resource_exhausted" in msg:
            return True
        return any(s in msg for s in ("quota", "rate limit", "rate-limit"))

    def _generate_streaming_with_retry(self, provider, messages,
                                        tools: Optional[List[dict]] = None):
        """Stream tokens from the provider, emitting each chunk via
        ``self._on_token_delta`` and ``AgentEvent.TOKEN_DELTA``, while
        accumulating the full text into a ``ProviderResponse`` for the
        agent loop to parse.

        Uses exponential-backoff retry identical to
        ``_generate_with_retry``. Falls back to ``provider.generate()``
        (non-streaming) if the provider does not support streaming
        (i.e. lacks ``ProviderCapability.STREAMING``).
        """
        from tera_pilot.providers.base import ProviderCapability

        # If the active provider doesn't support streaming, fall back
        # to the normal generate-with-retry path (without token deltas).
        caps = getattr(provider, "capabilities", frozenset())
        if ProviderCapability.STREAMING not in caps:
            logger.info("[agent] provider %s lacks STREAMING — falling back to generate()", provider.provider_id)
            # Temporarily disable the callback so we don't recurse.
            saved = self._on_token_delta
            self._on_token_delta = None
            try:
                return self._generate_with_retry(provider, messages, tools=tools)
            finally:
                self._on_token_delta = saved

        import random as _random
        last_exc: Optional[Exception] = None
        call_start = time.time()
        model_name = getattr(getattr(provider, "config", None), "model", "?")
        logger.info("[agent] LLM stream starting — provider=%s model=%s",
                    provider.provider_id, model_name)

        # v2.3.4-fix: same quota-cooldown pacing as _generate_with_retry.
        self._wait_out_provider_cooldown()

        # v2.3.4: same quota-aware attempt budget as _generate_with_retry.
        total_attempts = self._RETRY_MAX_ATTEMPTS
        for attempt in range(1, self._RETRY_QUOTA_MAX_ATTEMPTS + 1):
            try:
                full_text: List[str] = []
                chunk_count = 0
                # G20b: thread model_override into the streaming call
                # too. provider.stream() accepts the same ``model`` kwarg
                # as provider.generate() — verified in the providers'
                # base class (tera_pilot/providers/base.py).
                stream_kwargs = (
                    {"model": self._model_override}
                    if self._model_override
                    else {}
                )
                if tools:
                    stream_kwargs["tools"] = tools
                for chunk in provider.stream(messages, **stream_kwargs):
                    # Check cancellation between chunks so Ctrl+C still works.
                    if self.tools.is_cancelled():
                        logger.info("[agent] stream cancelled after %d chunks", chunk_count)
                        break
                    full_text.append(chunk)
                    chunk_count += 1
                    # Relay to the callback and the event system. When a
                    # dedicated on_token_delta callback is wired it is the
                    # single delivery channel (the TUI bridge forwards it to
                    # the same sink as events, so emitting TOKEN_DELTA here
                    # too would append every chunk twice). Consumers that
                    # only subscribe to events still get TOKEN_DELTA.
                    if self._on_token_delta is not None:
                        try:
                            self._on_token_delta(chunk)
                        except Exception:
                            pass  # UI errors must never crash the agent loop.
                    else:
                        self._emit(AgentEvent.TOKEN_DELTA, delta=chunk)

                elapsed = time.time() - call_start
                text = "".join(full_text)
                logger.info("[agent] LLM stream completed in %.1fs — provider=%s model=%s chunks=%d len=%d",
                            elapsed, provider.provider_id, model_name, chunk_count, len(text))

                # Build a ProviderResponse from the accumulated text.
                # Streaming providers don't always return structured
                # token counts; estimate from text length if not available.
                resp = ProviderResponse(
                    text=text,
                    model=model_name,
                    provider=provider.provider_id,
                    tokens_in=0,   # streaming doesn't expose input tokens per chunk
                    tokens_out=chunk_count,
                )

                # v1.0.5-correctness: record real token usage (H-RT-3).
                try:
                    tracker = getattr(self, "_token_tracker", None)
                    if tracker is not None:
                        tracker.record(
                            provider=provider.provider_id,
                            model=model_name,
                            tokens_in=0,
                            tokens_out=chunk_count,
                        )
                except Exception as track_err:
                    logger.debug("[agent] token_tracker.record failed: %s", track_err)
                # v1.1.0: record quota usage.
                try:
                    quota = getattr(self, "_quota_tracker", None)
                    if quota is not None:
                        quota.record(
                            section=self.section,
                            provider=provider.provider_id,
                            model=model_name,
                        )
                except Exception as quota_err:
                    logger.debug("[agent] quota.record failed: %s", quota_err)
                return resp

            except Exception as exc:
                last_exc = exc
                elapsed = time.time() - call_start
                # P0.x-fix: NEVER retry a stream that already emitted
                # chunks. The partial text was already relayed to the
                # UI / token-delta sink — restarting the stream would
                # re-deliver the SAME text (duplicated output) and the
                # final response would be garbled. Only a stream that
                # failed BEFORE its first chunk (connection refused,
                # auth, etc.) is safe to retry.
                if chunk_count > 0:
                    logger.warning(
                        "[agent] LLM stream failed after %d chunks — NOT retrying "
                        "(partial output already delivered): %s", chunk_count, exc,
                    )
                    break
                if self._is_quota_error(exc):
                    total_attempts = self._RETRY_QUOTA_MAX_ATTEMPTS
                if attempt >= total_attempts:
                    logger.warning("[agent] LLM stream FAILED after %d attempts (%.1fs): %s",
                                   attempt, elapsed, exc)
                    break
                if not self._is_retryable(exc):
                    logger.warning("[agent] LLM stream FAILED (%.1fs, non-retryable): %s",
                                   elapsed, exc)
                    break
                # v2.3.4-fix: honour provider retryDelay on quota errors.
                delay = min(self._RETRY_MAX_DELAY,
                            self._RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                delay = delay * (0.5 + 0.5 * _random.random())
                if self._is_quota_error(exc):
                    qdelay = self._quota_backoff_delay(exc)
                    if qdelay > delay:
                        delay = qdelay
                    cooldown_until = time.time() + qdelay
                    if cooldown_until > getattr(self, "_provider_cooldown_until", 0.0):
                        self._provider_cooldown_until = cooldown_until
                logger.warning(
                    "[agent] transient stream error (attempt %d/%d, %.1fs): %s — retrying in %.1fs",
                    attempt, total_attempts, elapsed, exc, delay,
                )
                slept = 0.0
                while slept < delay:
                    if self.tools.is_cancelled():
                        raise exc
                    step = min(0.25, delay - slept)
                    time.sleep(step)
                    slept += step

        if last_exc is not None and self._is_quota_error(last_exc):
            # v2.3.4: surface an actionable message instead of raw JSON.
            self._quota_exhausted_error(
                provider, last_exc, total_attempts, time.time() - call_start,
            )
        raise last_exc if last_exc is not None else RuntimeError("stream failed")

    # ── High-level API ───────────────────────────────────────────────────

    def run(self, description: str, task_type: TaskType = TaskType.AGENTIC,
            language: str = "python", context: Optional[str] = None,
            file_path: Optional[str] = None, **gen_kwargs) -> TaskResult:
        task = Task(
            type=task_type,
            description=description,
            context=context,
            file_path=file_path,
            language=language,
        )
        return self._run_agent_loop(task, **gen_kwargs)

    def write(self, description: str, language: str = "python",
              context: Optional[str] = None, file_path: Optional[str] = None,
              **gen_kwargs) -> TaskResult:
        return self.run(description, TaskType.WRITE, language, context, file_path, **gen_kwargs)

    def edit(self, code: str, instruction: str, language: str = "python",
             file_path: Optional[str] = None, **gen_kwargs) -> TaskResult:
        return self.run(instruction, TaskType.EDIT, language, code, file_path, **gen_kwargs)

    def refactor(self, code: str, goal: str = "improve quality",
                 language: str = "python", file_path: Optional[str] = None,
                 **gen_kwargs) -> TaskResult:
        return self.run(goal, TaskType.REFACTOR, language, code, file_path, **gen_kwargs)

    def analyze(self, code: str, language: str = "python", **gen_kwargs) -> TaskResult:
        return self.run("Analyze this code", TaskType.ANALYZE, language, code, **gen_kwargs)

    def generate_test(self, code: str, language: str = "python", **gen_kwargs) -> TaskResult:
        return self.run("Generate comprehensive tests", TaskType.TEST, language, code, **gen_kwargs)

    def debug(self, code: str, error_message: str, language: str = "python",
              file_path: Optional[str] = None, **gen_kwargs) -> TaskResult:
        return self.run(error_message, TaskType.DEBUG, language, code, file_path, **gen_kwargs)

    def chat(self, message: str, **gen_kwargs) -> TaskResult:
        """Non-agent chat — single LLM round-trip with conversation history.

        v1.0.5-correctness: the old implementation concatenated the
        system prompt into the user content and called ``_generate``
        with ``include_system=False``, which sent tool-use instructions
        as user content (the same bug the v1.0.6 agent-loop refactor
        fixed but never propagated to ``chat()``). Models that treat
        system-role content as authoritative (Groq's llama-3.3-70b in
        particular) would reply with prose instead of following
        instructions. We now send the system prompt as a separate
        ``role="system"`` message (BUGS_REPORT H-RT-9).

        Also: ``self.memory.add("user", message)`` previously ran
        BEFORE the generate call, so if generate raised, the user
        message was orphaned in memory with no assistant reply. We now
        add it only after a successful generate.
        """
        task = Task(type=TaskType.CHAT, description=message)
        history = self.memory.to_prompt_history()
        user_prompt = ""
        if history:
            user_prompt += f"## Conversation so far\n{history}\n\n"
        # v1.1.3-fix (bug 1.10): removed the legacy "[USER]\n{message}\n\n[TERA_PILOT]"
        # markers. They were left over from the old concatenated-prompt
        # scheme (v1.0.5) where the system prompt was inlined into the
        # user content. Since v1.0.6 the system prompt is a separate
        # role="system" message, and the markers confuse llama-3 family
        # models into echoing them back ("[USER] I'm ready [TERA_PILOT] ...").
        # The model now sees just the user's message as user content,
        # which is what it expects.
        user_prompt += message

        try:
            # v1.0.6-style: system prompt as a separate role="system" message.
            output, tok_in, tok_out = self._generate(user_prompt, include_system=True)
            # Only persist to memory after a successful generate, so a
            # failed call doesn't leave an orphaned user message.
            self.memory.add("user", message)
            self.memory.add("assistant", output)
            self.task_history.append(task)
            return TaskResult(success=True, output=output, iterations=1,
                              metadata={"total_tokens_in": tok_in, "total_tokens_out": tok_out})
        except Exception as e:
            return TaskResult(success=False, output="", error=str(e))

    # ── Agent Loop ────────────────────────────────────────────────────────

    def _is_write_or_execute_tool(self, tool_call: Optional[ToolCall]) -> bool:
        """Check if a tool call would write files or execute commands.

        v1.0.5-correctness: ``undo_write`` was missing from this set,
        so under ``autonomy="always_ask"`` the agent could silently
        roll back a file the user just edited — ``undo_write``
        overwrites the current file with a backup, which is a write
        operation (BUGS_REPORT M-RT-6).
        """
        if tool_call is None:
            return False
        write_tools = {ToolName.WRITE_FILE, ToolName.EXECUTE_COMMAND, ToolName.RUN_CODE,
                       ToolName.DELETE_FILE, ToolName.RENAME_FILE, ToolName.APPLY_DIFF,
                       ToolName.WRITE_BINARY_FILE, ToolName.MKDIR, ToolName.STR_REPLACE,
                       # v1.0.11: git stage/commit modify repo state
                       ToolName.GIT_STAGE, ToolName.GIT_COMMIT,
                       # v1.0.5-correctness: undo_write overwrites the
                       # current file with a backup — treat as a write.
                       ToolName.UNDO_WRITE,
                       # v1.1.3-fix (bug 1.3): MCP tools can have side
                       # effects (filesystem write_file, github push, etc.)
                       # so they are subject to the autonomy gate. The
                       # actual confirmation is requested inside
                       # _call_mcp_tool, but listing it here keeps the
                       # metadata consistent for the UI.
                       ToolName.CALL_MCP_TOOL}
        return tool_call.name in write_tools

    def _create_plan_with_cancel_check(self, task: Task, autonomy: str = "always_ask",
                                        plan_mode: bool = False) -> Tuple[str, bool]:
        """Create a plan and check if the user wants to cancel.
        Returns (plan, cancelled).

        v1.2.1-fix (Plan Mode gating): когда plan_mode=True и autonomy позволяет,
        реально останавливает выполнение и ждёт подтверждения пользователя.

        autonomy: 'always_ask' | 'new_files_only' | 'never_ask'
        plan_mode: если True и autonomy != 'never_ask' — ждём подтверждения
        """
        plan = self._create_plan(task)
        self._emit(AgentEvent.PLAN_CREATED, plan=plan, task=task.description)

        # Если plan_mode включён и autonomy не 'never_ask' — ждём подтверждения
        if plan_mode and autonomy != 'never_ask':
            self._pending_plan = (task, plan)
            return plan, True  # cancelled=True сигнализирует что нужно ожидать подтверждения

        # v1.2.1-fix: сбрасываем pending_plan если autonomy='never_ask' или plan_mode=False
        self._pending_plan = None
        return plan, False

    def _run_agent_loop(self, task: Task, **gen_kwargs) -> TaskResult:
        all_steps: List[AgentStep] = []
        # v2.3.4: set when the loop gives up on tool calls and accepts the
        # model's prose as the final answer (iteration 3+ with no tool call).
        # Such a run did NOT do the requested work — the UI must warn instead
        # of presenting it as a normal successful completion.
        self._degraded_prose = False
        autonomy = gen_kwargs.pop("autonomy", "always_ask")

        # v1.2.1-fix (Plan Mode gating): извлекаем параметры plan_mode
        plan_mode = gen_kwargs.pop("plan_mode", False)
        plan_approved = gen_kwargs.pop("plan_approved", None)
        plan_feedback = gen_kwargs.pop("plan_feedback", None)
        plan = ""  # default; may be overridden by approved plan below

        # v1.2.1-fix: обработка подтверждения/фидбэка по плану
        if self._pending_plan is not None:
            if plan_approved:
                # Продолжаем с сохранённого плана
                task, plan = self._pending_plan
                self._pending_plan = None
                # План будет использован ниже
            elif plan_feedback is not None:
                # Пересоздаём план с учётом фидбэка
                old_task, old_plan = self._pending_plan
                self._pending_plan = None
                # Добавляем фидбэк в задачу для контекста
                task.description = f"[PLAN FEEDBACK] {plan_feedback}\n\nOriginal task: {old_task.description}"
                # Сбрасываем план — будет создан новый ниже
                plan = ""
            else:
                # Ещё ожидаем подтверждения — возвращаем специальный результат
                return TaskResult(
                    success=False,
                    output="",
                    iterations=0,
                    error="awaiting_plan_approval",
                    metadata={"status": "awaiting_plan_approval",
                             "plan": self._pending_plan[1] if self._pending_plan else ""}
                )

        # v1.1.1-fix: reset per-run token accumulators
        self._run_tokens_in = 0
        self._run_tokens_out = 0

        # v1.2.1-fix (review §4.3): re-sync context budgets in case the
        # user switched providers since the last run. Cheap (one dict
        # lookup + a few arithmetic ops) and idempotent.
        self._sync_context_budgets()

        # v1.2.0: reset the touched-files list at the start of each run
        # so self_verify only re-reads files touched in THIS run, not
        # leftover state from a previous run. Also reset the subagent
        # watchdog state — a fresh run starts with no in-flight children.
        self.tools._touched_files = []
        self.tools._subagent_watchdog_state = []
        # v1.2.0: reset the per-run chat_id / iteration context so
        # activity log entries get the right chat_id attached. The
        # chat_id is propagated by AgentRuntime.run() via gen_kwargs.
        self.tools._current_chat_id = gen_kwargs.pop("chat_id", None)
        self.tools._current_iteration = None
        # v1.2.0: record a "run started" info entry so the Activity
        # Stream visually separates one agent run from the next.
        try:
            self.tools._activity_log.record(
                category=CATEGORY_INFO,
                kind="run_started",
                tool="",
                title=f"Agent run started · section={self.section}",
                summary=(task.description[:160] + "…") if len(task.description) > 160 else task.description,
                status=STATUS_OK,
                section=self.section,
                chat_id=self.tools._current_chat_id,
                meta={"iterations_planned": self.max_iterations},
            )
        except Exception:
            pass

        # v1.1.0: enforce daily quota BEFORE doing any LLM work. The user
        # gets a clear, friendly error instead of burning a provider call
        # they'll be billed for but can't use.
        if self._quota_tracker and self._quota_tracker.exhausted(self.section):
            remaining = self._quota_tracker.remaining(self.section)
            limit = self._quota_tracker.get_daily_limit(self.section)
            err_msg = (
                f"Daily {self.section} limit reached ({limit} requests/day). "
                f"Limit resets at 00:00 UTC. "
                f"Future versions will offer paid tiers with higher limits."
            )
            self._emit(AgentEvent.ERROR, error=err_msg, iteration=0)
            return TaskResult(
                success=False, output=err_msg, iterations=0,
                error="quota_exhausted",
                metadata={"section": self.section, "limit": limit, "remaining": remaining},
            )

        # v1.2.1-fix: if plan wasn't already set by the approved-plan
        # branch above, and planning is enabled, create a new plan.
        if self.enable_planning and task.type not in (TaskType.CHAT, TaskType.ANALYZE) and not plan:
            plan, cancelled = self._create_plan_with_cancel_check(task, autonomy, plan_mode=plan_mode)
            if cancelled:
                # v1.2.1-fix: это означает что мы ожидаем подтверждения плана
                return TaskResult(
                    success=False,
                    output="",
                    iterations=0,
                    error="awaiting_plan_approval",
                    metadata={"status": "awaiting_plan_approval", "plan": plan}
                )

        step_history: List[str] = []
        # v1.0.6: keep the SYSTEM_PROMPT and the task prompt SEPARATE.
        # The old code concatenated them into one user-prompt string and
        # called _generate with the system-inclusion flag turned OFF —
        # which meant the tool-use instructions were sent as user
        # content, not as a system message. Many providers (notably
        # Groq's llama-3.3-70b) treat "system" content as authoritative
        # instructions and "user" content as a request to respond to —
        # so the model was answering "I can't write files, here's the
        # code instead of writing them" instead of emitting a
        # write_file tool call.
        #
        # v1.0.9: append CLAUDE.md project instructions to the system
        # prompt so they're treated as authoritative project rules.
        system_prompt = self._system_prompt(self.section)
        # v1.0.9: inject CLAUDE.md project instructions
        proj_instructions = self._project_context.instructions()
        if proj_instructions:
            system_prompt = system_prompt + "\n\n" + proj_instructions
        # v1.1.4-fix (bug 4.2): auto-attach relevant project files, scored
        # by ContextManager (pinned files, recently-touched files, files
        # named in the task, config/entry files) within a token budget —
        # this is what "smart file selection" was supposed to do all
        # along; it was previously computed nowhere. Wrapped in try/except
        # since this must never break a task that has no project root yet
        # (e.g. chat-only mode with no workspace set).
        try:
            file_block = self._context_manager.build_context_block(
                query=task.description or "",
                mentioned_files=[task.file_path] if task.file_path else None,
            )
            if file_block:
                system_prompt = system_prompt + (
                    "\n\n# Relevant project files (auto-attached, "
                    "token-budgeted — not exhaustive; use read_file for "
                    "anything not shown here)\n\n" + file_block
                )
        except Exception as e:
            logger.debug("[agent] context file auto-attach failed: %s", e)
        # v1.0.11: inject skill catalog so the agent knows what skills
        # are available. Full skill bodies are NOT injected (saves
        # context tokens) — the agent calls get_skill(id) to pull the
        # full body when it decides a skill fits the task.
        if self._skills:
            skill_catalog = build_skill_catalog(self._skills)
            if skill_catalog:
                system_prompt = system_prompt + "\n\n" + skill_catalog
        # v1.1.0: inject MCP tool catalog (available in ALL sections).
        # If no MCP servers are configured/running, this is a no-op.
        # v1.2.1-fix (review §4.5): we now inject the TYPED catalog by
        # default — each MCP tool appears as ``mcp__<server>__<tool>``
        # with its full JSON Schema. The legacy ``call_mcp_tool`` meta-
        # tool still works (its schema is still in TOOL_SCHEMA, and
        # _dispatch still routes it). The typed path gives the model
        # better-typed args and avoids the (server, tool, args) tuple
        # indirection — same lazy-loading pattern mature coding agents use.
        try:
            from .mcp_manager import get_mcp_manager
            manager = get_mcp_manager()
            # Use typed catalog (paginated, with JSON Schemas). Falls
            # back to legacy catalog_prompt() if typed_catalog_prompt
            # raises (shouldn't happen, but be defensive).
            try:
                mcp_catalog = manager.typed_catalog_prompt()
            except Exception:
                mcp_catalog = manager.catalog_prompt()
            if mcp_catalog:
                system_prompt = system_prompt + "\n\n" + mcp_catalog
        except Exception as e:
            logger.debug("[agent] MCP catalog injection failed: %s", e)
        # v1.2.0: Heavy Code section's substantive system prompt
        # (slice decomposition, adversarial review, watchdog, quota
        # awareness) is now built by PromptBuilder.system() via
        # HEAVY_CODE_SYSTEM_SUFFIX. The runtime injection below stays
        # only as a short marker — it doesn't duplicate the substantive
        # guidance anymore. Keeping it as a marker lets the agent tell
        # at a glance "I'm in heavy_code mode" even if the suffix
        # scrolls out of the model's attention.
        if self.section == "heavy_code":
            system_prompt = system_prompt + "\n\n" + (
                "[MODE] You are in HEAVY CODE mode. spawn_subagent and "
                "spawn_multi_agents are available. See the "
                "HEAVY_CODE_SYSTEM_SUFFIX above for when to use them."
            )
        # v2.3.5-fix (small-model support): anchor the model to the
        # workspace root. Small models (e.g. LFM 2.6B) hallucinate
        # absolute paths from training data (``/home/user/discount.py``)
        # and then loop on the failing read. Stating the real root and
        # demanding RELATIVE paths in the system prompt fixes the most
        # common degenerate loop without weakening any enforcement.
        if self.tools.workspace:
            system_prompt = system_prompt + (
                "\n\n## Workspace\n"
                f"Root: {self.tools.workspace}\n"
                "All file paths in tool calls must be RELATIVE to this "
                "root. Never use absolute paths (e.g. /home/user/...)."
            )
        # G19a — Symbolic task canvas. Injected ONCE per turn (replace,
        # not append) via the existing fragment system so it tombstone-
        # compacts like every other tool output. Stable id means
        # re-emission each turn is idempotent — the compactor keeps only
        # the latest per-id, so the canvas never accumulates across
        # turns even though we inject it every turn. Bounded token cost
        # (~few hundred tokens max) regardless of task graph size.
        try:
            from tera_pilot.agent.task_canvas import get_task_canvas
            canvas_fragment = get_task_canvas().to_fragment()
            if canvas_fragment:
                system_prompt = system_prompt + "\n\n" + canvas_fragment
        except Exception as e:
            logger.debug("[agent] task canvas injection failed: %s", e)
        # G19b — Persona memory. Cross-session, per-user profile
        # (~/.tera_pilot/persona.md, hard-capped at ~2000 chars). Injected via
        # the same fragment discipline so it tombstone-compacts and
        # never becomes a second source of permanent bloat. Mirrors the
        # G17 learnings injection point — both are "persistent context
        # the model should always see" and belong together.
        try:
            from tera_pilot.agent.persona_memory import get_persona_memory
            persona_fragment = get_persona_memory().to_fragment()
            if persona_fragment:
                system_prompt = system_prompt + "\n\n" + persona_fragment
        except Exception as e:
            logger.debug("[agent] persona injection failed: %s", e)
        initial_user_prompt = PromptBuilder.task_prompt(
            task, plan=plan, history=self.memory.to_prompt_history()
        )
        # v2.3.5-fix (small-model support): put the workspace root in the
        # USER message too (not just the system prompt). Small models
        # pattern-match the task prompt against training data and emit
        # hallucinated absolute paths (e.g. /home/user/discount.py) —
        # stating the real root where they are looking fixes it.
        if self.tools.workspace:
            initial_user_prompt = (
                f"Workspace root: {self.tools.workspace}\n"
                "All file paths in tool calls must be RELATIVE to this "
                "root (e.g. \"discount.py\", never \"/home/user/discount.py\").\n\n"
                + initial_user_prompt
            )

        current_user_prompt = initial_user_prompt
        # v2.3.5-fix (small-model support): native-history conversation.
        # When the provider can do native tool calling we keep the
        # conversation as real OpenAI-style messages (assistant
        # tool_calls + role="tool" results) instead of embedding every
        # observation in the user prompt; models trained on native tool
        # calling (e.g. LFM 2.6B) follow their plan only in that format.
        # ``_native_history_active`` flips to True after the first
        # response that actually carries native tool_calls.
        self._native_messages: Optional[List[ProviderMessage]] = None
        self._native_history_active = False
        if self._native_tools():
            self._native_messages = [
                ProviderMessage(role="system", content=system_prompt),
                ProviderMessage(role="user", content=initial_user_prompt),
            ]
            # v2.3.5-fix (LM Studio / LFM 2.5): when the provider cannot
            # safely GENERATE native tool_calls (LM Studio 400s on
            # quote-heavy content), the conversation still uses the
            # native HISTORY format (assistant text + tool results as
            # real messages) but the model's tool calls come as its
            # native TEXT format, parsed by OutputParser. Activate the
            # native-history append from the start so observations are
            # real messages, not prose in the user prompt.
            try:
                _prov = self._get_active_provider()
                if not getattr(_prov, "emits_native_tool_calls", True):
                    self._native_history_active = True
            except Exception:
                pass

        final_output = ""
        success = True
        error_msg = None

        for iteration in range(1, self.max_iterations + 1):
            # v1.1.1: honor Stop — check BEFORE starting another LLM call /
            # tool call, so cancelling actually halts further agent
            # activity instead of just muting UI updates while the loop
            # keeps running to completion in the background.
            if self.tools.is_cancelled():
                error_msg = "Cancelled by user"
                success = False
                self._emit(AgentEvent.ERROR, error=error_msg, iteration=iteration)
                break

            # v1.1.3-fix (bug 2.3): re-check quota INSIDE the loop, not
            # just before iteration 1. If another Tera Pilot process (or a
            # recursive sub-agent, see bug 1.2) exhausts the daily limit
            # mid-run, the previous code kept making LLM calls past the
            # quota. We now bail out as soon as the limit is hit.
            if self._quota_tracker and self._quota_tracker.exhausted(self.section):
                remaining = self._quota_tracker.remaining(self.section)
                limit = self._quota_tracker.get_daily_limit(self.section)
                error_msg = (
                    f"Daily {self.section} quota exhausted mid-run "
                    f"(limit={limit}/day, remaining={remaining}). "
                    f"Resets at 00:00 UTC."
                )
                success = False
                self._emit(AgentEvent.ERROR, error=error_msg, iteration=iteration)
                break

            self._emit(AgentEvent.ITERATION_START, iteration=iteration, max=self.max_iterations)
            # v1.2.0: propagate current iteration to ToolEngine so
            # activity log entries can be tagged with the iteration
            # they occurred in. This is read by record_tool_call().
            self.tools._current_iteration = iteration

            # v1.0.9: auto-compact if context is over 85% of budget.
            # This prevents silent context loss in long conversations.
            if self._maybe_auto_compact():
                self._emit(AgentEvent.THOUGHT,
                           thought="[auto-compacted context to stay under token budget]",
                           iteration=iteration)
                # v1.1.3-fix (bug 1.9): rebuild the user prompt more
                # carefully. The previous code did
                # ``initial_user_prompt.split("## Previous Steps")[0]``
                # which corrupted the prompt if "## Previous Steps"
                # appeared in the user's task description (rare but
                # possible when discussing the agent itself). It also
                # dropped the "## Execution Plan" section on subsequent
                # iterations. We now rebuild from the structured parts:
                #   - everything before "## Previous Steps" (plan + task)
                #   - the new (compacted) history under "## Previous Steps"
                # If "## Previous Steps" is NOT in the initial prompt
                # (e.g. first iteration), we just append it.
                if "## Previous Steps" in initial_user_prompt:
                    pre_history = initial_user_prompt.split("## Previous Steps", 1)[0]
                else:
                    # No "## Previous Steps" section in the initial prompt
                    # — use the whole thing and append the section.
                    pre_history = initial_user_prompt.rstrip() + "\n\n"
                current_user_prompt = (
                    pre_history
                    + "## Previous Steps\n"
                    + self.memory.to_prompt_history()
                )

            try:
                resp = None
                if self._native_messages is not None:
                    # v2.3.5-fix (small-model support): use the native
                    # message history (assistant tool_calls + tool
                    # results) — the format small agent models were
                    # trained on. Used from the FIRST iteration so
                    # ``resp.tool_calls`` is available for activation.
                    # ``_tools_to_send()`` may return None (LM Studio) so
                    # the provider's engine never validates native calls.
                    resp = self._generate_native_history(
                        self._native_messages,
                        tools=self._tools_to_send(),
                    )
                    raw = resp.text
                    tok_in = int(getattr(resp, "tokens_in", 0) or 0)
                    tok_out = int(getattr(resp, "tokens_out", 0) or 0)
                else:
                    # v1.0.6: explicit system + user — model now treats
                    # the tool-use instructions as authoritative.
                    raw, tok_in, tok_out = self._generate_with_explicit_system(
                        system_prompt, current_user_prompt,
                        tools=self._tools_to_send(),
                    )
                # v1.1.1-fix: accumulate real token counts for the UI
                self._run_tokens_in += tok_in
                self._run_tokens_out += tok_out
            except Exception as e:
                error_msg = str(e)
                self._emit(AgentEvent.ERROR, error=error_msg, iteration=iteration)
                success = False
                break

            thought = OutputParser.extract_thought(raw)
            # v1.0.5: detect the [WRITE_FILE] token. The token is a hint
            # — it does NOT replace the JSON tool call. We surface it as
            # part of the TOOL_CALLED event so the UI can pre-load the
            # original file for diff review and highlight the target
            # path in the project tree.
            write_intent = OutputParser.extract_write_intent(raw)
            if write_intent:
                intent_path, intent_line = write_intent
                # Strip the token line so it doesn't pollute the JSON parse.
                raw_for_parse = OutputParser.strip_write_token(raw)
            else:
                intent_path, raw_for_parse = None, raw
            # v2.3.5-fix (small-model support): prefer the API's NATIVE
            # tool_calls when the provider returned them — the message
            # history then switches to the native format (assistant
            # tool_calls + tool results), which small agent models need
            # to follow their plan instead of re-reading in a loop.
            native_calls_this = (getattr(resp, "tool_calls", None)
                                 if resp is not None else None)
            if native_calls_this:
                self._native_history_active = True
                parsed_calls = OutputParser.tool_calls_from_native(native_calls_this)
            else:
                parsed_calls = OutputParser.parse_tool_calls(raw_for_parse)
            is_final = OutputParser.is_final(raw)
            final_text = OutputParser.parse_final_answer(raw)

            step = AgentStep(thought=thought, is_final=is_final)
            self._emit(AgentEvent.THOUGHT, thought=thought, iteration=iteration)

            # Sanity-check: if the model emitted [WRITE_FILE] X but the
            # first tool call targets a different path, warn (don't fail
            # — the tool call is the source of truth, the token is a hint).
            if write_intent and parsed_calls:
                tc_path = parsed_calls[0].args.get("path")
                if tc_path and intent_path and tc_path != intent_path:
                    logger.warning(
                        "[agent] [WRITE_FILE] token path %r does not match "
                        "tool call path %r — using tool call path",
                        intent_path, tc_path,
                    )

            if is_final and final_text is not None:
                final_output = final_text
                step.observation = "[DONE]"
                all_steps.append(step)
                self._emit(AgentEvent.DONE, output=final_output, iterations=iteration)
                break

            # v2.3.5-fix (small-model support): the model SIGNALED a tool
            # call (finish_reason="tool_calls") but the provider returned
            # no actual tool_calls — LM Studio can emit a truncated
            # tool-call turn (empty content + empty tool_calls), which
            # openai_compat surfaces as "[tool_calls] no content returned".
            # Without this, the runtime would accept that marker as prose
            # and finalize the run with garbage. Treat it as a failed
            # attempt and retry with a corrective reminder (bounded by
            # max_iterations, so it cannot loop forever).
            if (
                not parsed_calls
                and not is_final
                and getattr(resp, "finish_reason", None) == "tool_calls"
            ):
                logger.warning(
                    "[agent] iter %d: provider signaled tool_calls but "
                    "returned none — retrying with reminder", iteration,
                )
                self._emit(AgentEvent.THOUGHT,
                           thought="[provider returned an empty tool call — retrying]",
                           iteration=iteration,
                           note="empty_tool_calls")
                reminder = (
                    "Your previous response signaled a tool call but "
                    "contained no actual tool call. Emit ONE complete "
                    "JSON tool call now, e.g. "
                    '{\"tool\": \"read_file\", \"args\": {\"path\": \"main.py\"}}, '
                    'or {\"final_answer\": \"...\"}.'
                )
                if self._native_history_active and self._native_messages is not None:
                    self._native_messages.append(
                        ProviderMessage(role="assistant", content=raw))
                    self._native_messages.append(
                        ProviderMessage(role="user", content=reminder))
                else:
                    current_user_prompt = initial_user_prompt + "\n\n" + reminder
                all_steps.append(step)
                self._emit(AgentEvent.ITERATION_END, iteration=iteration,
                           reason="empty_tool_calls")
                continue

            if parsed_calls:
                # v2.3.5-fix (small-model support): execute EVERY tool
                # call the model emitted in this response, in order.
                # Small agent models (e.g. LFM 2.5 2.6B) emit their whole
                # plan as several calls ("read A, read B, write C"); the
                # runtime previously executed only the FIRST and dropped
                # the rest, so the model saw its other calls "not happen"
                # and repeated them forever (the degenerate read_file
                # loop). Each call still goes through the same ToolEngine
                # guards (workspace sandbox, command policy, diff review)
                # — this only changes WHICH calls get executed, not how.
                executed_steps: List[AgentStep] = []
                for tool_call in parsed_calls:
                    tool_name_str = (tool_call.name.value
                                     if isinstance(tool_call.name, ToolName)
                                     else tool_call.name)
                    # v1.0.5-security: re-check cancellation BEFORE
                    # executing each tool. The LLM call can take 30–120 s;
                    # if the user clicked Stop during that window, the LLM
                    # still returned and we would have executed the parsed
                    # tool_call (writing/deleting files, running commands)
                    # AFTER the user pressed Stop (BUGS_REPORT H-RT-7).
                    # Bail out now instead.
                    if self.tools.is_cancelled():
                        self._emit(AgentEvent.ITERATION_END,
                                   iteration=iteration, reason="user_stop_before_tool")
                        logger.info("[agent] cancelled before tool execution: %s",
                                    tool_name_str)
                        break

                    call_step = AgentStep(thought=thought, is_final=False)
                    call_step.action = tool_call
                    # v1.0.5: include write_intent in the event payload so
                    # the UI can show "[WRITE_FILE] path" before the write
                    # lands, and pre-warm the diff-review pane.
                    event_payload: Dict[str, Any] = {
                        "tool": tool_name_str,
                        "args": tool_call.args,
                    }
                    if write_intent:
                        event_payload["write_intent"] = intent_path
                    self._emit(AgentEvent.TOOL_CALLED, **event_payload)

                    observation = self.tools.execute(tool_call)

                    step_summary = (
                        f"Step {iteration}: [{tool_name_str}] → "
                        + observation[:300].replace("\n", " ")
                    )
                    step_history.append(step_summary)
                    if len(step_history) > 3:
                        step_history = step_history[-3:]

                    call_step.observation = (
                        observation[:500] + " ... [truncated]"
                        if len(observation) > 500 else observation
                    )

                    if tool_call.result and len(tool_call.result) > 500:
                        tool_call.result = tool_call.result[:500] + " ... [truncated]"

                    self._emit(AgentEvent.TOOL_RESULT, tool=tool_name_str,
                               result=observation[:200])
                    executed_steps.append(call_step)

                if not executed_steps:
                    # Cancelled before any tool ran — stop the run.
                    break

                all_steps.extend(executed_steps)

                if self._native_history_active and self._native_messages is not None:
                    # v2.3.5-fix (small-model support): native-history
                    # continuation — append the assistant's tool_calls
                    # message plus one ``role="tool"`` message per
                    # executed call, and let the next turn continue over
                    # this real message list.
                    asst_content = "" if native_calls_this else raw_for_parse
                    # v2.3.5-fix (LM Studio / LFM 2.5 native tool calls):
                    # NEVER echo the model's raw ``tool_calls`` arguments
                    # back into the conversation. A small model sometimes
                    # emits an arguments string with an unescaped quote
                    # inside ``content`` (e.g. ``query = f"SELECT ...`` in
                    # the SQL-injection review task); LM Studio's engine
                    # re-validates the assistant tool_calls message on the
                    # NEXT request and hard-400s the whole run ("Invalid
                    # diff: '…' not found at start of '…'", server_error
                    # 500). Rebuild every call's arguments from the
                    # PARSED dict (``parsed_calls``) so the conversation
                    # always carries well-formed JSON — the ids stay the
                    # same, so the ``role="tool"`` follow-ups still match.
                    # (Text-mode providers — LM Studio — never emitted
                    # native tool_calls, so no tool_calls are attached to
                    # the assistant message at all; the observations below
                    # fall back to role="user" messages.)
                    if native_calls_this:
                        _clean_native_calls = []
                        for _c in parsed_calls:
                            try:
                                _args_json = json.dumps(_c.args, ensure_ascii=False)
                            except (TypeError, ValueError):
                                _args_json = "{}"
                            _nm2 = (_c.name.value
                                    if isinstance(_c.name, ToolName) else _c.name)
                            _clean_native_calls.append({
                                "id": _c.id,
                                "type": "function",
                                "function": {
                                    "name": _nm2,
                                    "arguments": _args_json,
                                },
                            })
                        _asst_tool_calls = _clean_native_calls or None
                    else:
                        _asst_tool_calls = None
                    self._native_messages.append(ProviderMessage(
                        role="assistant",
                        content=asst_content,
                        tool_calls=_asst_tool_calls,
                    ))
                    for cs in executed_steps:
                        call = cs.action
                        obs = cs.observation or ""
                        if call is not None and call.id:
                            self._native_messages.append(ProviderMessage(
                                role="tool", tool_call_id=call.id, content=obs,
                            ))
                        else:
                            # Text-JSON call without a native id — some
                            # local servers reject ``role="tool"`` without
                            # a matching tool_call_id; surface the
                            # observation as a plain user message instead.
                            _nm = (call.name.value
                                   if isinstance(call.name, ToolName)
                                   else call.name)
                            self._native_messages.append(ProviderMessage(
                                role="user",
                                content=f"[tool result {_nm}]\n{obs}",
                            ))
                else:
                    # v1.0.6: continuation prompt is built from the
                    # INITIAL user prompt (task + plan + history) + the
                    # step observations accumulated so far. The system
                    # prompt is sent separately by
                    # _generate_with_explicit_system.
                    current_user_prompt = (
                        initial_user_prompt
                        + "\n"
                        + "\n".join(
                            PromptBuilder.continuation(s, i + 1)
                            for i, s in enumerate(step_history)
                        )
                    )

                    # v2.3.5-fix (small-model support): degenerate-loop
                    # guard. A small model that repeats the SAME failing
                    # call (e.g. read_file on a hallucinated absolute
                    # path) burns its whole iteration budget on the same
                    # error. Detect the pattern (same tool + same args
                    # twice in a row, both erroring) and inject
                    # corrective guidance so the model inspects the
                    # workspace and retries with a real path.
                    if len(executed_steps) >= 2:
                        prev, last = executed_steps[-2], executed_steps[-1]
                        if (
                            prev.action is not None and last.action is not None
                            and prev.action.name == last.action.name
                            and prev.action.args == last.action.args
                            and "[TOOL ERROR]" in (last.observation or "")
                        ):
                            _nm = (last.action.name.value
                                   if isinstance(last.action.name, ToolName)
                                   else last.action.name)
                            current_user_prompt += (
                                "\n\n[SYSTEM NOTE] You called "
                                f"{_nm} with the same arguments twice and it "
                                "ERRORED both times. STOP repeating that call. "
                                "First inspect the workspace (list_files or "
                                "get_project_structure) to find the correct "
                                "path, then call it with a RELATIVE path."
                            )
                            logger.info(
                                "[agent] degenerate-loop guard fired: %s %s repeated twice with errors",
                                _nm, last.action.args,
                            )
            else:
                # v1.0.6: model didn't emit a tool call OR a final_answer
                # marker — it just wrote prose. This is the failure mode
                # where the model says "I can't write files, here's the
                # code instead of writing them" because it didn't
                # internalise that it IS the agent.
                #
                # v1.0.5-hotfix: the old code retried ONLY on iteration 1
                # and then accepted prose on iteration 2. But the retry
                # condition was ``iteration == 1`` — so iteration 2's
                # prose was accepted as final. BUT if the model kept
                # emitting short non-JSON prose on every retry, the loop
                # would still spin to max_iterations=8 before giving up
                # (the user saw 5+ iterations of "no tool call" in the
                # logs). We now:
                #   1. Retry up to 2 times with the reminder (iterations 1-2).
                #   2. On iteration 3+, accept the prose as the final answer
                #      instead of looping to exhaustion — the model clearly
                #      isn't going to emit a tool call, and the user is
                #      waiting.
                #   3. Emit a THOUGHT event with the full raw text so the
                #      UI shows what the model actually said (the user
                #      reported the UI was stuck on "planning..." — this
                #      is because the THOUGHT event had an empty/truncated
                #      thought when the model's response was short prose).
                if iteration <= 2 and task.type == TaskType.AGENTIC:
                    logger.info(
                        "[agent] iter %d produced no tool call and no "
                        "final_answer — retrying with explicit reminder",
                        iteration,
                    )
                    # Re-emit the thought with the FULL raw text so the
                    # UI can show what the model actually said (not just
                    # the extracted thought which may be empty).
                    if not thought and raw.strip():
                        self._emit(AgentEvent.THOUGHT,
                                   thought=raw.strip()[:500],
                                   iteration=iteration,
                                   note="no_tool_call_retry")
                    reminder = (
                        "REMINDER: You are the agent. You have tools. "
                        "Do NOT write code in your reply and ask the "
                        "user to run it — call the write_file or "
                        "str_replace tool DIRECTLY. Output one JSON "
                        "tool call now, or {\"final_answer\": \"...\"} "
                        "if you truly have nothing to do."
                    )
                    if self._native_history_active and self._native_messages is not None:
                        self._native_messages.append(
                            ProviderMessage(role="assistant", content=raw))
                        self._native_messages.append(
                            ProviderMessage(role="user", content=reminder))
                    else:
                        current_user_prompt = (
                            initial_user_prompt
                            + "\n\n"
                            + reminder
                        )
                    all_steps.append(step)
                    self._emit(AgentEvent.ITERATION_END, iteration=iteration)
                    continue
                # v2.3.5-fix (overall agent quality): a model stuck in a
                # repetition loop produces a response dominated by one
                # verbatim fragment and no tool call / final answer.
                # Accepting it as the final answer would surface garbage
                # to the user. Refuse to finalize on it — nudge the model
                # back on track and keep going (bounded by
                # max_iterations, so the loop cannot spin forever; if it
                # exhausts, the run reports an honest "max iterations"
                # failure instead of succeeding with repeated text).
                from .repetition_guard import is_repetition_dominated
                if is_repetition_dominated(raw):
                    logger.warning(
                        "[agent] iter %d: response is repetition-dominated "
                        "— refusing to finalize on repeated text", iteration,
                    )
                    self._emit(AgentEvent.THOUGHT,
                               thought="[repetition detected — model is "
                                       "looping; refusing to finalize on "
                                       "repeated text]",
                               iteration=iteration,
                               note="repetition_guard")
                    note = (
                        "Your previous response was repetitive and "
                        "contained no tool call or final answer. STOP "
                        "repeating yourself. Take a concrete action: call "
                        "a tool (read_file, list_files, str_replace, "
                        "write_file, ...) or emit "
                        "{\"final_answer\": \"...\"}."
                    )
                    if self._native_history_active and self._native_messages is not None:
                        self._native_messages.append(
                            ProviderMessage(role="assistant", content=raw))
                        self._native_messages.append(
                            ProviderMessage(role="user", content=note))
                    else:
                        current_user_prompt = initial_user_prompt + "\n\n" + note
                    all_steps.append(step)
                    self._emit(AgentEvent.ITERATION_END, iteration=iteration,
                               reason="repetition_guard")
                    continue
                # Iteration 3+ with no tool call: accept the prose as
                # the final answer. The model isn't cooperating, and
                # looping further just wastes the user's time.
                # v2.3.4: mark the run as DEGRADED only when NO tool was
                # ever executed during the WHOLE run. The previous code
                # set the flag whenever iteration 3+ emitted prose — even
                # after earlier iterations had already run tools — so a
                # run that created files / executed commands was wrongly
                # flagged as "prose-without-tools" and the UI warned
                # "verify the result" over real, completed work.
                # (Reproduced deterministically with a scripted provider:
                #  write_file + read_file iterations followed by one prose
                #  iteration previously produced degraded_prose=True.)
                final_output = raw
                step.is_final = True
                all_steps.append(step)
                if not any(s.action is not None for s in all_steps):
                    self._degraded_prose = True
                    logger.warning(
                        "[agent] iter %d: accepting prose as final answer "
                        "(model not emitting tool calls) — run DEGRADED, no "
                        "tools executed", iteration,
                    )
                    self._emit(AgentEvent.DONE, output=final_output, iterations=iteration,
                               degraded=True)
                else:
                    logger.info(
                        "[agent] iter %d: no tool call this iteration, but "
                        "tools ran earlier — accepting prose as final answer",
                        iteration,
                    )
                    self._emit(AgentEvent.DONE, output=final_output, iterations=iteration)
                break

            all_steps.append(step)
            self._emit(AgentEvent.ITERATION_END, iteration=iteration)
        else:
            # for/else: loop completed without `break` — max iterations
            # exhausted. If `raw` was assigned (at least one iteration
            # ran before any potential break), use it as the final
            # output; otherwise (e.g. max_iterations=0) there's nothing
            # to surface.
            #
            # v1.0.5-correctness: previously ``success = bool(final_output)``
            # was True whenever the model emitted ANY text — but at this
            # point ``final_output`` is just the last raw LLM response
            # (a tool call or prose), NOT a final answer. Reporting
            # ``success=True`` misled the UI into thinking the task had
            # completed successfully when in fact the agent ran out of
            # steam mid-tool-call (BUGS_REPORT H-RT-10). We now report
            # ``success=False`` and surface ``error_msg`` so the caller
            # can distinguish "exhausted" from "done".
            if not final_output:
                # Use locals() instead of dir() — dir() returns the
                # module-level namespace when called at class scope,
                # which would falsely report `raw` as defined.
                final_output = locals().get("raw", "")
            error_msg = f"Max iterations ({self.max_iterations}) reached"
            success = False

        tool_calls = [s.action for s in all_steps if s.action]

        # v1.1.3-fix (bug 1.8): don't pollute ContextMemory with
        # cancelled/failed tasks. The previous code wrote
        # "[Task: ...] <description>" + empty/partial output unconditionally,
        # so the next conversation saw an orphaned user message with no
        # assistant reply. Auto-compaction would then bake that into the
        # summary, permanently corrupting the context. We now:
        #   - SKIP the memory write entirely if the task was cancelled
        #     (success=False and error_msg == "Cancelled by user")
        #   - For other failures, write the user message but mark it
        #     with metadata={"failed": True} so a future filter can
        #     skip it in to_prompt_history().
        was_cancelled = (
            not success
            and error_msg is not None
            and "cancel" in error_msg.lower()
        )
        if not was_cancelled:
            user_meta = {}
            if not success:
                user_meta["failed"] = True
                user_meta["error"] = (error_msg or "")[:200]
            self.memory.add("user", f"[Task: {task.type.value}] {task.description[:200]}", **user_meta)
            # Only write the assistant message if there's actual output.
            if final_output and final_output.strip():
                self.memory.add("assistant", final_output[:1000], failed=not success)

        task.metadata["iterations"] = len(all_steps)
        task.metadata["success"] = success
        self.task_history.append(task)

        # v1.2.0: record the run's terminal state in the activity log
        # so the Activity Stream shows a clear "run done" / "run failed"
        # / "run cancelled" boundary marker after every agent invocation.
        # This is the bookend to the "run_started" entry emitted above.
        try:
            if was_cancelled:
                done_kind, done_title, done_status = "run_cancelled", "Agent run cancelled", STATUS_ERROR
            elif success:
                done_kind, done_title, done_status = "run_done", "Agent run done", STATUS_OK
            else:
                done_kind, done_title, done_status = "run_failed", f"Agent run failed — {error_msg or 'unknown error'}", STATUS_ERROR
            self.tools._activity_log.record(
                category=CATEGORY_INFO,
                kind=done_kind,
                tool="",
                title=done_title,
                summary=(final_output[:160] + "…") if len(final_output or "") > 160 else (final_output or ""),
                status=done_status,
                section=self.section,
                chat_id=self.tools._current_chat_id,
                meta={
                    "iterations": len(all_steps),
                    "tool_calls": len(tool_calls),
                    "tokens_in": self._run_tokens_in,
                    "tokens_out": self._run_tokens_out,
                },
            )
        except Exception:
            pass

        return TaskResult(
            success=success,
            output=final_output,
            error=error_msg,
            iterations=len(all_steps),
            steps=all_steps,
            tool_calls=tool_calls,
            plan=plan,
            metadata={
                "language": task.language,
                "task_type": task.type.value,
                "total_tokens_in": self._run_tokens_in,
                "total_tokens_out": self._run_tokens_out,
                # v2.3.4: True when the run ended by accepting prose as the
                # final answer without executing any tool — the UI must not
                # present this as a completed task.
                "degraded_prose": self._degraded_prose,
            },
        )

    def _create_plan(self, task: Task) -> str:
        context = task.context[:500] if task.context else ""
        if task.file_path:
            context += f"\nFile: {task.file_path}"
        prompt = PromptBuilder.plan(task.description, context)
        import time as _time
        plan_start = _time.time()
        logger.info("[agent] planning step starting — task=%r", task.description[:80])
        try:
            plan, tok_in, tok_out = self._generate(prompt)
            self._run_tokens_in += tok_in
            self._run_tokens_out += tok_out
            plan = plan.strip()
            logger.info("[agent] planning step completed in %.1fs (%d chars)",
                        _time.time() - plan_start, len(plan))
            # v1.0.6: validate plan against available tools (M-RT-8).
            # If the plan references tools that don't exist, the agent
            # would waste iterations trying to call them.
            _warn_unknown_tools(plan)
            return plan
        except Exception as e:
            logger.warning("[agent] planning failed after %.1fs: %s",
                           _time.time() - plan_start, e)
            return ""

    def run_stream(self, description: str, task_type: TaskType = TaskType.AGENTIC,
                   language: str = "python", context: Optional[str] = None,
                   **gen_kwargs) -> Generator[str, None, None]:
        task = Task(type=task_type, description=description, context=context, language=language)
        try:
            # G20b: respect provider_override for streaming calls too.
            provider = self._get_active_provider()
            if not provider.is_loaded:
                provider.load()
            messages = [
                ProviderMessage(role="system", content=self._system_prompt()),
                ProviderMessage(role="user", content=PromptBuilder.task_prompt(task)),
            ]
            for chunk in provider.stream(messages, **gen_kwargs):
                yield chunk
        except Exception as e:
            yield f"\n[ERROR] {e}"

    def get_status(self) -> Dict[str, Any]:
        return {
            "tasks_completed": len(self.task_history),
            "memory_messages": len(self.memory.messages),
            "max_iterations": self.max_iterations,
            "planning_enabled": self.enable_planning,
            "workspace": str(self.tools.workspace),
        }

    def get_history(self) -> List[Task]:
        return self.task_history

    def clear_history(self):
        self.task_history.clear()
        self.memory.clear()
        logger.info("Agent history and memory cleared")

    def clear_pending_plan(self):
        """v1.2.1-fix (Plan Mode gating): Сбросить ожидающий подтверждения план."""
        self._pending_plan = None
        logger.debug("[agent] pending plan cleared")

    def set_workspace(self, path: str):
        # v2.3.5-fix (context isolation): switching to a DIFFERENT
        # workspace must not leak the previous task's conversation into
        # the new one. The runtime is a process-wide singleton reused
        # across HTTP requests (api_server.get_agent_runtime), and
        # ContextMemory persists to agent_memory.json — so without this,
        # a task run in workspace A would see the previous task's tool
        # observations in ``initial_user_prompt`` (memory.to_prompt_history())
        # and chase files from the old repo (observed: the fix-config-
        # loader task tried to read ``discount.py`` from an earlier task;
        # small models can't recover from that and degrade into a
        # read_file loop). Same-workspace calls keep the history (that's
        # a continuing chat); a changed workspace starts clean.
        prev_ws = str(self.tools.workspace) if self.tools.workspace else None
        self.tools.set_workspace(path)
        if prev_ws and os.path.realpath(prev_ws) != os.path.realpath(path):
            self.memory.clear()
            self.task_history.clear()
            logger.info("[agent] workspace changed — cleared conversation memory")
        # v1.0.9: update project context so TERA_PILOT.md is re-read for
        # the new project root.
        self._project_context.set_root(path)
        # v1.1.4-fix: re-index files for the new project root — without
        # this the ContextManager kept scoring files from the previous
        # project after switching folders.
        self._context_manager.set_root(path)
        # v1.0.11: reload skills for the new project root
        self._reload_skills()
        # v1.2.1-fix (review §4.6): invalidate the CommandPolicy cache
        # so the next _sanitize_command call picks up the new project's
        # .tera_pilot/commands.json (if any). Cheap (one lock + None assign).
        try:
            from .command_policy import invalidate_global_policy
            invalidate_global_policy()
        except Exception:
            pass
        logger.info(f"Agent workspace set to: {path}")


# ── AgentWorker (QThread) — Non-blocking UI ──────────────────────────────

