"""bridge.py — the ONLY module in tera_pilot_tui that knows about tera_pilot internals.

Every widget talks to the agent through a TeraPilotBridge instance. If a widget
needs something new from the core, add a method here instead of importing
`tera_pilot.agent_runtime` (or friends) directly into UI code. Keeping this boundary
thin is what stops the TUI from becoming a fourth parallel agent-loop path.

The bridge drives the proven production path: a plain `AgentRuntime` created
the same way `tera_pilot/cli.py` creates it, wired via `on_event`, `set_cancel_check`
and `set_confirm_callback`. It deliberately does NOT touch
`agent_orchestrator.patch_runtime` (unused in production, raises on a vanilla
runtime) nor the AgentRuntimeV2 path.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Event kinds are the string values of tera_pilot.agent_runtime.AgentEvent
# (plan_created, iteration_start, thought, tool_called, tool_result,
#  iteration_end, done, error). The sink receives (kind, data_dict).
EventSink = Callable[[str, Dict[str, Any]], None]
ConfirmHandler = Callable[[Dict[str, Any]], None]


def _spend_pro_required_msg() -> str:
    """v2.3.4: Spend Dashboard is Pro-licensed — same wording on every surface."""
    return (
        "Spend Dashboard requires a Pro license — run: tera-pilot license activate <key> "
        "(see LICENSING.md)"
    )


@dataclass
class ProviderChoice:
    """Optional provider overrides. Anything left None falls back to the
    saved ~/.tera_pilot/config.json / environment defaults."""
    provider_id: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    api_base: Optional[str] = None


class TeraPilotBridge:
    def __init__(
        self,
        workspace: Optional[str] = None,
        provider: Optional[ProviderChoice] = None,
        section: str = "general",
        max_iterations: int = 8,
        enable_planning: bool = False,
    ) -> None:
        self.workspace = workspace or os.getcwd()
        self.section = section
        self.max_iterations = max_iterations
        self.enable_planning = enable_planning
        self._provider = provider or ProviderChoice()

        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._event_sink: Optional[EventSink] = None
        self._confirm_handler: Optional[ConfirmHandler] = None
        self._guardian_handler: Optional[Callable[[Dict[str, Any]], None]] = None

        self._agent: Any = None
        self._registry: Any = None
        self._tracker: Any = None

        # Slash command manager — loaded from .claude/commands/ .md files
        self._slash_manager: Any = None

    # ------------------------------------------------------------------ setup
    def set_event_sink(self, sink: Optional[EventSink]) -> None:
        self._event_sink = sink

    def set_confirm_handler(self, handler: Optional[ConfirmHandler]) -> None:
        self._confirm_handler = handler

    def set_guardian_handler(self, handler: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        self._guardian_handler = handler

    def _build_registry(self):
        from tera_pilot.providers import get_registry, ProviderConfig

        registry = get_registry()
        try:
            providers = registry.list_providers()
            if not providers:
                registry.register_default()
        except Exception as e:
            # If list_providers fails (import error), just register defaults
            registry.register_default()

        pid = self._provider.provider_id or registry.active_id or "ollama"
        p = self._provider
        # Only reconfigure when the caller actually overrode something,
        # otherwise trust the saved config for this provider.
        if p.model or p.api_key or p.api_base:
            cfg = ProviderConfig(
                provider_id=pid,
                model=p.model or "",
                api_key=(p.api_key or os.environ.get(f"{pid.upper()}_API_KEY") or None),
                api_base=(p.api_base or None),
            )
            registry.configure(pid, cfg)
        registry.set_active(pid)
        return registry

    def ensure_agent(self):
        if self._agent is not None:
            return self._agent

        from tera_pilot.agent_runtime import AgentRuntime
        from tera_pilot.token_tracker import get_token_tracker

        self._registry = self._build_registry()
        self._tracker = get_token_tracker()

        agent = AgentRuntime(
            registry=self._registry,
            workspace=self.workspace,
            max_iterations=self.max_iterations,
            enable_planning=self.enable_planning,
            on_event=self._on_agent_event,
            token_tracker=self._tracker,
            section=self.section,
            on_token_delta=self._on_token_delta_event,
        )
        agent.set_autonomy("always_ask")
        agent.set_confirm_callback(self._on_confirm_request)
        agent.set_cancel_check(lambda: self._stop.is_set())
        self._agent = agent

        # v2.0.0 fix: restore the saved Guardian level so the user does
        # not start every session with Guardian silently OFF.
        try:
            saved_level = self._load_guardian_config()
            if saved_level and saved_level != "off":
                self.set_guardian_level(saved_level)
        except Exception:
            pass

        return agent

    def _init_slash_manager(self) -> None:
        """Lazily initialize the slash command manager."""
        if self._slash_manager is not None:
            return
        try:
            from tera_pilot.slash_commands import SlashCommandManager
            self._slash_manager = SlashCommandManager()
            self._slash_manager.set_project_root(self.workspace)
        except Exception:
            self._slash_manager = None

    # --------------------------------------------------------------- callbacks
    def _on_agent_event(self, event: Any, data: Dict[str, Any]) -> None:
        sink = self._event_sink
        if sink is None:
            return
        kind = getattr(event, "value", str(event))
        try:
            sink(kind, dict(data))
        except Exception:
            # A UI error must never crash the agent loop, but log it.
            import logging
            logging.getLogger(__name__).warning(
                "event sink error for %s: %s", kind, data, exc_info=True
            )

    def _on_token_delta_event(self, chunk: str) -> None:
        """Relay a token delta chunk to the UI via the event sink.
        Called by AgentRuntime when streaming is enabled — each chunk
        of generated text is forwarded as a 'token_delta' event so the
        ChatLog can append it in real time."""
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink("token_delta", {"delta": chunk})
        except Exception:
            pass

    def _on_confirm_request(self, info: Dict[str, Any]) -> None:
        # Check if this is a Guardian MODIFY verdict
        if info.get("guardian_verdict") == "MODIFY" or info.get("suggested_args") is not None:
            handler = self._guardian_handler
            if handler is None:
                # No UI wired — default to approve
                self.answer_guardian_verdict("approve")
                return
            try:
                handler(dict(info))
            except Exception:
                self.answer_guardian_verdict("reject")
            return

        # Regular confirmation
        handler = self._confirm_handler
        if handler is None:
            # No UI wired — approve so we do not deadlock the loop.
            self.answer_confirmation(True)
            return
        try:
            handler(dict(info))
        except Exception:
            self.answer_confirmation(False)

    def answer_confirmation(self, accepted: bool) -> None:
        """Called by the UI after the user answers an approval modal."""
        agent = self._agent
        if agent is None:
            return
        try:
            # v2.2.4-fix: set the event FIRST so the agent wakes up,
            # then set the flag. This avoids the TOCTOU window where
            # the agent checks _confirm_accepted before it's set.
            agent.tools._confirm_accepted = bool(accepted)
            agent.tools._confirm_event.set()
        except Exception:
            pass

    def answer_guardian_verdict(self, verdict: str) -> None:
        """Called by the UI after the user answers a Guardian modal.

        verdict: "approve" | "reject" | "use_fix"
        """
        agent = self._agent
        if agent is None:
            return
        try:
            if verdict == "use_fix":
                # Apply the suggested args
                agent.tools._guardian_suggested_args = getattr(agent.tools, "_guardian_pending_args", None)
                agent.tools._confirm_accepted = True
            elif verdict == "approve":
                agent.tools._confirm_accepted = True
            else:  # reject
                agent.tools._confirm_accepted = False
            agent.tools._confirm_event.set()
        except Exception:
            pass

    # ------------------------------------------------------------------- runtime
    def run_prompt(self, prompt: str, plan_approved: bool = False,
                   plan_feedback: str | None = None):
        """Run one full agentic turn. BLOCKING — call from a worker thread.

        Returns the legacy TaskResult (success, output, iterations, ...).

        When ``plan_approved`` is True, the runtime continues from a
        pending plan instead of starting fresh.  ``plan_feedback``
        lets the user reject a plan with textual feedback.
        """
        from tera_pilot.agent_runtime import TaskType
        with self._busy:
            self._stop.clear()
            agent = self.ensure_agent()
            gen_kwargs = {}
            if plan_approved:
                gen_kwargs["plan_approved"] = True
            if plan_feedback is not None:
                gen_kwargs["plan_feedback"] = plan_feedback
            return agent.run(prompt, task_type=TaskType.AGENTIC, **gen_kwargs)

    def request_stop(self) -> None:
        """Cooperatively interrupt the running turn (Ctrl+C). The loop checks
        the cancel flag between iterations and before every tool call; we also
        release any pending confirmation so a blocked approval unwinds."""
        self._stop.set()
        agent = self._agent
        if agent is not None:
            try:
                # v2.2.4-fix: set event BEFORE flag to avoid TOCTOU window
                agent.tools._confirm_accepted = False
                agent.tools._confirm_event.set()
            except Exception:
                pass

    def is_busy(self) -> bool:
        return self._busy.locked()

    # -------------------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        provider = None
        model = None
        if self._registry is not None:
            try:
                for p in self._registry.list_providers():
                    if p.get("active"):
                        provider = p.get("id")
                        model = p.get("model") or p.get("default_model")
                        break
            except Exception:
                pass

        tokens = 0
        cost = 0.0
        if self._tracker is not None:
            try:
                s = self._tracker.stats()
                tokens = int(s.get("total_tokens", 0) or 0)
                cost = float(s.get("total_cost", 0.0) or 0.0)
            except Exception:
                pass

        return {
            "provider": provider,
            "model": model,
            "tokens": tokens,
            "cost": cost,
            "busy": self.is_busy(),
        }

    # --------------------------------------------------------- slash commands
    def list_slash_commands(self) -> List[Dict[str, Any]]:
        """Return all user-defined slash commands from .claude/commands/.
        Each entry has {id, name, description, has_arguments}."""
        self._init_slash_manager()
        if self._slash_manager is None:
            return []
        try:
            return self._slash_manager.list_commands()
        except Exception:
            return []

    def resolve_slash_command(self, text: str) -> Optional[Dict[str, Any]]:
        """Try to resolve text as a slash command. Returns
        {command, arguments, expanded, description} or None."""
        self._init_slash_manager()
        if self._slash_manager is None:
            return None
        try:
            return self._slash_manager.resolve(text)
        except Exception:
            return None

    # --------------------------------------------------------- provider/model
    def list_providers(self) -> List[Dict[str, Any]]:
        """Return all available provider metadata for the model switcher.
        Each entry: {id, label, default_model, model, active, capabilities}."""
        if self._registry is None:
            self._registry = self._build_registry()
        try:
            return self._registry.list_providers()
        except Exception:
            return []

    def set_provider(self, provider_id: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Switch the active provider and optionally the model.
        Returns {ok: bool, provider: str, model: str}."""
        if self._registry is None:
            self._registry = self._build_registry()
        try:
            from tera_pilot.providers import ProviderConfig
            # v2.3.4-fix: set_active raises ProviderError for unknown
            # provider ids. It used to be swallowed here ("Ignore registry
            # errors"), so `/model bogus` returned {"ok": True} while the
            # registry silently kept the previous provider — the UI claimed
            # a switch that never happened (silent failure). Let the error
            # propagate so the caller surfaces it to the user.
            self._registry.set_active(provider_id)

            if model:
                try:
                    cfg = ProviderConfig(
                        provider_id=provider_id,
                        model=model,
                    )
                    self._registry.configure(provider_id, cfg)
                except Exception:
                    pass  # Ignore config errors

            # Persist to config
            try:
                self._save_provider_config(provider_id, model)
            except Exception:
                pass

            # Force rebuild of the agent on next turn
            self._agent = None
            return {
                "ok": True,
                "provider": provider_id,
                "model": model or self._get_active_model(),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get_active_model(self) -> Optional[str]:
        """Get the model name of the currently active provider."""
        if self._registry is None:
            return None
        try:
            for p in self._registry.list_providers():
                if p.get("active"):
                    return p.get("model") or p.get("default_model")
        except Exception:
            pass
        return None

    def _save_provider_config(self, provider_id: str, model: Optional[str]) -> None:
        """Persist the provider selection to ~/.tera_pilot/config.json."""
        config_path = Path.home() / ".tera_pilot" / "config.json"
        try:
            if config_path.exists():
                with open(config_path, "r") as f:
                    cfg = json.load(f)
            else:
                cfg = {}
            cfg["active_provider"] = provider_id
            if model and "providers" in cfg:
                providers = cfg["providers"]
                if provider_id in providers:
                    providers[provider_id]["model"] = model
            with open(config_path, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    # --------------------------------------------------------- section
    def set_section(self, section_id: str) -> Dict[str, Any]:
        """Switch the runtime section. Forces agent rebuild on next turn."""
        valid = {"general", "heavy_code", "office"}
        if section_id not in valid:
            return {"ok": False, "error": f"Unknown section: {section_id}. Valid: {', '.join(valid)}"}
        self.section = section_id
        # Force agent rebuild with the new section
        self._agent = None
        return {"ok": True, "section": section_id}

    # --------------------------------------------------------- workspace/directory
    def change_workspace(self, new_path: str) -> Dict[str, Any]:
        """Change the workspace directory. Forces agent rebuild."""
        try:
            # v2.3.4-fix: expand ~ and ~user BEFORE resolving — Path('~')
            # resolves against CWD, so `/cd ~` used to fail with "Not a
            # directory: .../~".
            path = Path(new_path).expanduser().resolve()
            if not path.is_dir():
                return {"ok": False, "error": f"Not a directory: {path}"}
            self.workspace = str(path)
            # Force agent rebuild
            self._agent = None
            # Update slash commands root
            if self._slash_manager is not None:
                try:
                    self._slash_manager.set_project_root(self.workspace)
                except Exception:
                    pass
            return {"ok": True, "workspace": self.workspace}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_workspace_files(self) -> List[str]:
        """Return just the filenames (no paths) in the workspace root.
        For a more detailed file tree, the agent runtime's file tools
        provide that — this is a quick overview for /files command."""
        try:
            root = Path(self.workspace)
            names = []
            for item in sorted(root.iterdir()):
                if item.name.startswith(".") and item.name not in (".env", ".gitignore"):
                    continue
                suffix = "/" if item.is_dir() else ""
                names.append(f"{item.name}{suffix}")
            return names
        except Exception:
            return []

    # --------------------------------------------------------- chats
    def list_chats(self) -> List[Dict[str, Any]]:
        """List all saved chats from ~/.tera_pilot/chats/*.json.
        Each entry: {id, title, updated_at, message_count, status}."""
        chats_dir = Path.home() / ".tera_pilot" / "chats"
        if not chats_dir.exists():
            return []
        chats = []
        for path in sorted(chats_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    chat = json.load(f)
                last_status = "idle"
                messages = chat.get("messages", [])
                for m in reversed(messages):
                    if m.get("role") == "assistant":
                        last_status = "error" if (m.get("error") or m.get("success") == False) else "done"
                        break
                chats.append({
                    "id": chat.get("id", path.stem),
                    "title": chat.get("title", "Untitled"),
                    "updated_at": chat.get("updated_at", chat.get("created_at", "")),
                    "message_count": len(messages),
                    "status": last_status,
                })
            except Exception:
                continue
        return chats

    # --------------------------------------------------------- planning toggle
    def toggle_planning(self) -> Dict[str, Any]:
        """Toggle planning mode on/off. Forces agent rebuild."""
        self.enable_planning = not self.enable_planning
        self._agent = None
        return {"ok": True, "planning": self.enable_planning}

    # --------------------------------------------------------- guardian
    def set_guardian_level(self, level: str) -> Dict[str, Any]:
        """Set Guardian safety review level.

        Levels:
          - "off": Guardian disabled (default)
          - "dangerous_only": Only review tool calls flagged as high-risk
          - "all": Review all tool calls with medium+ risk

        Returns {ok: bool, level: str} or error dict."""
        valid_levels = {"off", "dangerous_only", "all"}
        if level not in valid_levels:
            return {"ok": False, "error": f"Invalid level: {level}. Valid: {', '.join(valid_levels)}"}

        agent = self.ensure_agent()
        if not hasattr(agent.tools, "_guardian_config"):
            from tera_pilot.agent.guardian import GuardianConfig
            agent.tools._guardian_config = GuardianConfig(level=level)
        else:
            from tera_pilot.agent.guardian import GuardianConfig
            # Create new config with updated level, preserving provider settings
            old = agent.tools._guardian_config
            agent.tools._guardian_config = GuardianConfig(
                level=level,
                provider_id=old.provider_id,
                model=old.model,
            )

        # Persist to config
        self._save_guardian_config(level)
        return {"ok": True, "level": level}

    def get_guardian_level(self) -> Dict[str, Any]:
        """Get current Guardian level."""
        if self._agent is not None and hasattr(self._agent.tools, "_guardian_config") and self._agent.tools._guardian_config:
            return {"ok": True, "level": self._agent.tools._guardian_config.level}
        return {"ok": True, "level": "off"}

    def _save_guardian_config(self, level: str) -> None:
        """Persist guardian level to ~/.tera_pilot/config.json."""
        config_path = Path.home() / ".tera_pilot" / "config.json"
        try:
            if config_path.exists():
                with open(config_path, "r") as f:
                    cfg = json.load(f)
            else:
                cfg = {}
            cfg["guardian_level"] = level
            with open(config_path, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def _load_guardian_config(self) -> str:
        """Load guardian level from ~/.tera_pilot/config.json."""
        config_path = Path.home() / ".tera_pilot" / "config.json"
        try:
            if config_path.exists():
                with open(config_path, "r") as f:
                    cfg = json.load(f)
                return cfg.get("guardian_level", "off")
        except Exception:
            pass
        return "off"

    # --------------------------------------------------------- usage
    def get_usage(self) -> Dict[str, Any]:
        """Return current session usage stats."""
        return self.status()

    # ── v2.0.0 — Collaboration modes ───────────────────────────────

    def list_collaboration_modes(self) -> List[Dict[str, Any]]:
        """Return the four collaboration modes supported by the backend."""
        return [
            {"id": "single", "label": "Single (no collaboration)",
             "desc": "Run a single agent on the task."},
            {"id": "reviewer", "label": "Reviewer",
             "desc": "Implementer + reviewer loop with APPROVE/REJECT/MODIFY verdicts."},
            {"id": "codegen", "label": "Codegen",
             "desc": "Planner decomposes task, N parallel implementers, concatenated output."},
            {"id": "pair", "label": "Pair",
             "desc": "Two pair-programmer agents alternate turns on the same task."},
            {"id": "observer", "label": "Observer",
             "desc": "One worker + N read-only observers; warnings collected."},
        ]

    def run_collaboration(self, mode: str, task: str) -> Dict[str, Any]:
        """Run a task in the given collaboration mode.

        Returns {ok, mode, output, iterations, metadata} on success.
        """
        try:
            from tera_pilot.collaboration import (
                CollaborationOrchestrator, CollaborationMode,
            )
            agent = self.ensure_agent()
            orch = CollaborationOrchestrator(agent)
            try:
                mode_enum = CollaborationMode(mode)
            except ValueError:
                return {"ok": False, "error": f"Unknown mode: {mode}"}
            result = orch.run(mode_enum, task)
            return {
                "ok": True,
                "mode": mode,
                "output": result.output,
                "iterations": result.iterations,
                "metadata": result.metadata or {},
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.0.0 — Request queue monitoring ──────────────────────────

    def get_queue_stats(self) -> Dict[str, Any]:
        """Return per-provider request-queue stats."""
        try:
            from tera_pilot.request_queue import get_queue_registry
            return get_queue_registry().stats()
        except Exception:
            return {}

    # ── v2.0.0 — Persistence backend selector ──────────────────────

    def get_persistence_backend(self) -> str:
        """Return the configured chat-persistence backend ('json' or 'sqlite')."""
        cfg_path = Path.home() / ".tera_pilot" / "config.json"
        try:
            if cfg_path.exists():
                with open(cfg_path, "r") as f:
                    cfg = json.load(f) or {}
                return cfg.get("persistence_backend", "json")
        except Exception:
            pass
        return "json"

    def set_persistence_backend(self, backend: str) -> Dict[str, Any]:
        """Switch chat persistence between 'json' and 'sqlite'."""
        valid = {"json", "sqlite"}
        if backend not in valid:
            return {"ok": False, "error": f"Invalid backend: {backend}"}
        try:
            cfg_path = Path.home() / ".tera_pilot" / "config.json"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg: dict = {}
            if cfg_path.exists():
                with open(cfg_path, "r") as f:
                    cfg = json.load(f) or {}
            cfg["persistence_backend"] = backend
            with open(cfg_path, "w") as f:
                json.dump(cfg, f, indent=2)
            return {"ok": True, "backend": backend}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_sqlite_sessions(self) -> List[Dict[str, Any]]:
        """List sessions stored in the SQLite backend (~/.tera_pilot/chats.sqlite3)."""
        try:
            from tera_pilot.session.sqlite_persistence import SQLitePersistence
            db_path = Path.home() / ".tera_pilot" / "chats.sqlite3"
            if not db_path.exists():
                return []
            store = SQLitePersistence(str(db_path))
            return store.list_sessions()
        except Exception:
            return []

    # ── v2.0.0 — Context fragments / compaction view ───────────────

    def get_compaction_stats(self) -> Optional[Dict[str, Any]]:
        """Return compaction stats from the most recent compaction pass."""
        try:
            agent = self._agent
            if agent is None:
                return None
            stats = getattr(agent, "_last_compaction_stats", None)
            if stats is None:
                return None
            if hasattr(stats, "to_dict"):
                return stats.to_dict()
            return dict(stats)
        except Exception:
            return None

    # ── v2.0.0 — Progressive tools catalog ─────────────────────────

    def get_tool_catalog_state(self) -> Dict[str, Any]:
        """Return the current progressive-tools catalog state."""
        try:
            from tera_pilot.progressive_tools import TOOL_CATALOG
            agent = self._agent
            if agent is None:
                return {"loaded": [], "available": list(TOOL_CATALOG.keys()),
                        "prompt_chars_saved": 0}
            engine = getattr(agent, "tools", None)
            try:
                loaded = sorted({str(t) for t in engine._tools.keys()})
            except Exception:
                loaded = []
            available = sorted(
                name for name in TOOL_CATALOG.keys() if name not in loaded
            )
            prompt_chars_saved = sum(
                len(name) + len(TOOL_CATALOG.get(name, ""))
                for name in available
            )
            return {
                "loaded": loaded,
                "available": available,
                "prompt_chars_saved": prompt_chars_saved,
            }
        except Exception as e:
            return {"loaded": [], "available": [], "prompt_chars_saved": 0,
                    "error": str(e)}

    # ── v2.0.1 (G7) — Capability catalog ───────────────────────────

    def list_capabilities(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Browse the capability catalog (built-in + user + project).

        Returns each capability's metadata (no body). Group by category
        in the UI. Use ``get_capability(id)`` to fetch the full body
        before filling placeholders.
        """
        try:
            from tera_pilot.capability_catalog import get_catalog
            catalog = get_catalog()
            if self.workspace and not catalog._project_root:
                catalog.set_project_root(self.workspace)
            return catalog.list_as_dicts(category=category, include_body=False)
        except Exception as e:
            return []

    def list_capability_categories(self) -> List[str]:
        """Distinct categories present in the catalog (for palette grouping)."""
        try:
            from tera_pilot.capability_catalog import get_catalog
            catalog = get_catalog()
            return catalog.list_categories()
        except Exception:
            return []

    def get_capability(self, cap_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the full capability (with body) by id."""
        try:
            from tera_pilot.capability_catalog import get_catalog
            catalog = get_catalog()
            cap = catalog.get(cap_id)
            if cap is None:
                return None
            return cap.to_dict(include_body=True)
        except Exception:
            return None

    def fill_capability_template(
        self,
        cap_id: str,
        values: Dict[str, str],
    ) -> Dict[str, Any]:
        """Substitute $placeholder$ values in the capability body.

        Returns {ok, prompt, capability, missing} on success,
        {ok=False, error, missing} if required placeholders are absent.
        """
        try:
            from tera_pilot.capability_catalog import get_catalog
            catalog = get_catalog()
            return catalog.fill_template(cap_id, values)
        except Exception as e:
            return {"ok": False, "error": str(e), "missing": []}

    # ── v2.0.1 (M1) — Second Opinion (Pro-gated) ───────────────────

    def is_pro_enabled(self) -> bool:
        """Return True if the ``tera_pilot_pro`` flag is on."""
        try:
            from tera_pilot.second_opinion import is_pro_enabled as _is_pro
            return _is_pro()
        except Exception:
            return False

    def set_pro_enabled(self, enabled: bool) -> Dict[str, Any]:
        """DEPRECATED (v2.3.4): Pro is now license-based.

        Flipping this toggle no longer grants access — Pro requires a
        valid signed license (``tera-pilot license activate <key>``).
        Returns the REAL current status so callers never show a false
        "Pro: ON" when no license is active.
        """
        try:
            from tera_pilot.second_opinion import (
                set_pro_enabled as _set_pro,
                is_pro_enabled as _is_pro,
            )
            _set_pro(enabled)
            actual = _is_pro()
            out = {"ok": True, "pro": actual}
            if not actual:
                out["note"] = (
                    "Pro requires a valid license — run: tera-pilot license activate <key>"
                )
            return out
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_second_opinion_config(self) -> Dict[str, Any]:
        """Return the current Second Opinion configuration."""
        try:
            from tera_pilot.second_opinion import get_second_opinion_config as _get
            cfg = _get()
            return {
                "ok": True,
                "enabled": cfg.enabled,
                "provider_id": cfg.provider_id,
                "model": cfg.model,
                "min_risk_level": cfg.min_risk_level,
                "pro_enabled": self.is_pro_enabled(),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_second_opinion_config(
        self,
        *,
        enabled: Optional[bool] = None,
        provider_id: Optional[str] = None,
        model: Optional[str] = None,
        min_risk_level: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update the Second Opinion configuration. Only non-None
        fields are changed; the rest are preserved."""
        try:
            from tera_pilot.second_opinion import (
                get_second_opinion_config as _get,
                set_second_opinion_config as _set,
                SecondOpinionConfig,
            )
            cur = _get()
            new_cfg = SecondOpinionConfig(
                enabled=cur.enabled if enabled is None else bool(enabled),
                provider_id=cur.provider_id if provider_id is None else str(provider_id),
                model=cur.model if model is None else str(model),
                min_risk_level=(cur.min_risk_level if min_risk_level is None
                                else str(min_risk_level)),
            )
            _set(new_cfg)
            return {
                "ok": True,
                "enabled": new_cfg.enabled,
                "provider_id": new_cfg.provider_id,
                "model": new_cfg.model,
                "min_risk_level": new_cfg.min_risk_level,
                "pro_enabled": self.is_pro_enabled(),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def run_second_opinion(
        self,
        tool_name: str,
        args: Dict[str, Any],
        risk_level: str,
        risk_reasons: List[str],
        recent_context: str = "",
    ) -> Dict[str, Any]:
        """Invoke a second model to review a proposed tool call.

        Returns the verdict dict (verdict, rationale, suggested_args,
        provider_id, model, elapsed_ms, error). Always returns APPROVE
        on any error so the feature fails OPEN.

        Requires Pro to be enabled. If Pro is off, returns a verdict
        with ``error='pro_required'`` so the UI can prompt the user.
        """
        try:
            from tera_pilot.licensing import is_feature_licensed
            from tera_pilot.second_opinion import (
                get_second_opinion_config,
                should_run_second_opinion,
                review_with_second_model,
            )
            if not is_feature_licensed("second_opinion"):
                return {
                    "verdict": "APPROVE",
                    "rationale": "Second Opinion requires Tera Pilot Pro (activate a license).",
                    "error": "pro_required",
                    "provider_id": "",
                    "model": "",
                    "elapsed_ms": 0.0,
                }
            cfg = get_second_opinion_config()
            # Even if the user disabled it, the explicit run_second_opinion()
            # call from the UI should still go through — we only honour the
            # auto-trigger gating in should_run_second_opinion().
            if self._registry is None:
                self._registry = self._build_registry()
            active_pid = self._registry.active_id or "ollama"
            verdict = review_with_second_model(
                config=cfg,
                tool_name=tool_name,
                args=args,
                risk_level=risk_level,
                risk_reasons=risk_reasons,
                recent_context=recent_context,
                provider_registry=self._registry,
                active_provider_id=active_pid,
            )
            return verdict.to_dict()
        except Exception as e:
            return {
                "verdict": "APPROVE",
                "rationale": f"Second Opinion error: {e}",
                "error": str(e),
                "provider_id": "",
                "model": "",
                "elapsed_ms": 0.0,
            }

    def list_second_opinion_providers(self) -> List[Dict[str, Any]]:
        """Return providers eligible to be the 'second' model.

        Same shape as ``list_providers()`` but excludes the active
        provider (a second opinion from the same provider is pointless).
        """
        try:
            all_p = self.list_providers()
            active_pid = None
            for p in all_p:
                if p.get("active"):
                    active_pid = p.get("id")
                    break
            return [p for p in all_p if p.get("id") != active_pid]
        except Exception:
            return []

    # ── v2.0.1 (G3) — Token budget / efficiency ────────────────────

    def get_token_budget(self) -> Dict[str, Any]:
        """Return the current token budget + live usage against the caps."""
        try:
            from tera_pilot.token_budget import get_token_budget, check_budget
            budget = get_token_budget()
            # Run the check against the live tracker if available
            check = check_budget(budget=budget, token_tracker=self._tracker)
            return {
                "ok": True,
                **budget.to_dict(),
                "day_cost": check.daily_used,
                "month_cost": check.monthly_used,
                "day_used_pct": check.day_used_pct,
                "month_used_pct": check.month_used_pct,
                "exceeded": check.exceeded,
                "reason": check.reason,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_token_budget(
        self,
        *,
        daily_usd: Optional[float] = None,
        monthly_usd: Optional[float] = None,
        max_tokens_per_turn: Optional[int] = None,
        max_iterations: Optional[int] = None,
        compaction_threshold_pct: Optional[int] = None,
        prompt_caching: Optional[bool] = None,
        predictable_mode: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Update token budget fields. Only non-None fields change.

        Forces an agent rebuild on next turn so settings take effect.
        """
        try:
            from tera_pilot.token_budget import set_token_budget as _set
            new_budget = _set(
                daily_usd=daily_usd,
                monthly_usd=monthly_usd,
                max_tokens_per_turn=max_tokens_per_turn,
                max_iterations=max_iterations,
                compaction_threshold_pct=compaction_threshold_pct,
                prompt_caching=prompt_caching,
                predictable_mode=predictable_mode,
            )
            # Force agent rebuild so max_iterations / max_tokens take effect
            self._agent = None
            return {"ok": True, **new_budget.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reset_token_budget(self) -> Dict[str, Any]:
        """Restore the default token budget."""
        try:
            from tera_pilot.token_budget import reset_token_budget as _reset
            budget = _reset()
            self._agent = None
            return {"ok": True, **budget.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def check_budget(self) -> Dict[str, Any]:
        """Convenience: check whether the budget has been exceeded.

        Returns {ok, exceeded, reason, daily_used, monthly_used, ...}.
        """
        try:
            from tera_pilot.token_budget import get_token_budget, check_budget
            budget = get_token_budget()
            check = check_budget(budget=budget, token_tracker=self._tracker)
            return {"ok": True, **check.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.0.1 (G4) — Cross-model verification ─────────────────────

    def verify_last_response(
        self,
        verifier_provider_id: Optional[str] = None,
        verifier_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a cross-model verification of the most recent agent output.

        Picks a verifier from a different model family than the active
        provider (unless the user pinned one), then asks it to flag
        correctness / safety / completeness issues in the last response.

        Returns {ok, verification, verifier_provider, verifier_model,
        elapsed_ms, error}.
        """
        try:
            from tera_pilot.second_opinion import (
                is_pro_enabled, get_second_opinion_config,
                resolve_second_provider,
            )
            from tera_pilot.providers import ProviderMessage

            # Capture the last response BEFORE any UI work.
            last_output = ""
            if self._agent is not None:
                try:
                    msgs = self._agent.memory.messages
                    for m in reversed(msgs):
                        if getattr(m, "role", "") == "assistant" and getattr(m, "content", ""):
                            last_output = m.content
                            break
                except Exception:
                    pass

            if not last_output:
                return {
                    "ok": False,
                    "error": "No prior assistant response to verify.",
                }

            if self._registry is None:
                self._registry = self._build_registry()

            active_pid = self._registry.active_id or "ollama"

            # Resolve verifier
            if verifier_provider_id:
                v_pid = verifier_provider_id
                v_model = verifier_model or ""
            else:
                cfg = get_second_opinion_config()
                v_pid, v_model = resolve_second_provider(active_pid, cfg)

            if not v_model:
                try:
                    cls = self._registry._classes.get(v_pid)
                    v_model = cls.default_model if cls else ""
                except Exception:
                    v_model = ""

            provider = self._registry.get(v_pid)
            if provider is None:
                return {"ok": False, "error": f"Provider '{v_pid}' not found in registry"}
            if not provider.is_loaded:
                provider.load()

            # Capture the user's last prompt for context
            last_user = ""
            if self._agent is not None:
                try:
                    for m in reversed(self._agent.memory.messages):
                        if getattr(m, "role", "") == "user" and getattr(m, "content", ""):
                            last_user = m.content
                            break
                except Exception:
                    pass

            system_prompt = (
                "You are an independent verifier reviewing another AI agent's response.\n"
                "The user asked a question; another model produced the answer below.\n"
                "Your job is to flag correctness, safety, and completeness issues — "
                "NOT to rewrite the answer.\n\n"
                "Return STRICT JSON:\n"
                "{\n"
                "  \"overall\": \"PASS\" | \"WARN\" | \"FAIL\",\n"
                "  \"correctness\": \"PASS\" | \"WARN\" | \"FAIL\",\n"
                "  \"safety\":      \"PASS\" | \"WARN\" | \"FAIL\",\n"
                "  \"completeness\": \"PASS\" | \"WARN\" | \"FAIL\",\n"
                "  \"issues\": [\"...\", ...],\n"
                "  \"suggestions\": [\"...\", ...],\n"
                "  \"summary\": \"<one or two sentences>\"\n"
                "}\n"
                "If the answer is fine, return PASS with empty issues.\n"
            )
            user_prompt = (
                f"## User's request\n{last_user[:2000]}\n\n"
                f"## Agent's response to verify\n{last_output[:6000]}\n\n"
                "Return your verdict JSON now."
            )
            messages = [
                ProviderMessage(role="system", content=system_prompt),
                ProviderMessage(role="user", content=user_prompt),
            ]

            import time as _t
            t0 = _t.time()
            resp = provider.generate(messages, model=v_model)
            raw = getattr(resp, "text", "") or ""
            elapsed = (_t.time() - t0) * 1000

            # Parse JSON
            # Extract JSON from the verifier response.
            # BUGS_REPORT fix: the non-greedy regex r"\{.*?\}" fails on
            # nested JSON like {"outer": {"inner": 1}}. Use a brace-balanced
            # scanner instead.
            def _extract_json(text: str) -> str:
                """Find the first balanced-brace JSON object in text."""
                # Try code fence first — if the LLM wrapped JSON in ```json ... ```,
                # extract the content between the fences, then use the brace-balanced
                # scanner on just that region (handles nested braces correctly).
                m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
                if m:
                    fenced_content = m.group(1).strip()
                    if fenced_content.startswith('{'):
                        # Run brace-balanced scanner on the fenced content
                        best = ""
                        for i, ch in enumerate(fenced_content):
                            if ch == '{':
                                depth = 0
                                for j in range(i, len(fenced_content)):
                                    if fenced_content[j] == '{':
                                        depth += 1
                                    elif fenced_content[j] == '}':
                                        depth -= 1
                                    if depth == 0:
                                        candidate = fenced_content[i:j+1]
                                        if len(candidate) > len(best):
                                            best = candidate
                                        break
                        if best:
                            return best
                # Brace-balanced scanner on full text
                best = ""
                for i, ch in enumerate(text):
                    if ch == '{':
                        depth = 0
                        for j in range(i, len(text)):
                            if text[j] == '{':
                                depth += 1
                            elif text[j] == '}':
                                depth -= 1
                            if depth == 0:
                                candidate = text[i:j+1]
                                if len(candidate) > len(best):
                                    best = candidate
                                break
                return best if best else text

            raw = _extract_json(raw)
            try:
                verification = json.loads(raw)
            except Exception:
                verification = {
                    "overall": "WARN",
                    "raw": raw[:2000],
                    "summary": "Verifier response was not valid JSON; raw text included.",
                }

            return {
                "ok": True,
                "verification": verification,
                "verifier_provider": v_pid,
                "verifier_model": v_model,
                "elapsed_ms": round(elapsed, 1),
                "original_chars": len(last_output),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.0.2 (G5) — Agent identity + tool-call audit ────────────

    def get_agent_identity(self) -> Dict[str, Any]:
        """Return the root agent identity for this Tera Pilot process."""
        try:
            from tera_pilot.agent_identity import get_root_identity
            ident = get_root_identity()
            return {"ok": True, **ident.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_agents(self) -> List[Dict[str, Any]]:
        """Return every agent that has acted in this process, with stats."""
        try:
            from tera_pilot.agent_identity import get_audit_trail
            trail = get_audit_trail()
            return trail.list_agents()
        except Exception:
            return []

    def get_agent_audit_summary(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Per-agent breakdown of tool calls, errors, durations.

        If ``agent_id`` is None, returns the full per-agent summary
        (keyed by agent id). Otherwise returns only that agent's row.
        """
        try:
            from tera_pilot.agent_identity import get_audit_trail
            trail = get_audit_trail()
            summary = trail.agent_summary(agent_id=agent_id)
            return {"ok": True, "summary": summary}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def filter_audit_by_agent(
        self, agent_id: str, include_children: bool = True, limit: int = 200,
    ) -> Dict[str, Any]:
        """Return audit entries attributed to ``agent_id`` (and optionally
        its descendant agents)."""
        try:
            from tera_pilot.agent_identity import get_audit_trail
            trail = get_audit_trail()
            entries = trail.filter_by_agent(
                agent_id=agent_id,
                include_children=include_children,
                limit=limit,
            )
            return {"ok": True, "entries": entries, "count": len(entries)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def export_audit_json(self, with_fingerprints: bool = True) -> Dict[str, Any]:
        """Export the full audit trail as JSON (with optional SHA-256 fingerprints)."""
        try:
            from tera_pilot.agent_identity import get_audit_trail
            trail = get_audit_trail()
            return {
                "ok": True,
                "json": trail.export_audit_json(with_fingerprints=with_fingerprints),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def export_audit_csv(self) -> Dict[str, Any]:
        """Export the audit trail as CSV (compact, no large args)."""
        try:
            from tera_pilot.agent_identity import get_audit_trail
            trail = get_audit_trail()
            return {"ok": True, "csv": trail.export_audit_csv()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.1.0 (G15) — Multi-provider consensus engine ────────────

    def get_consensus_config(self) -> Dict[str, Any]:
        """Return the current consensus engine config."""
        try:
            from tera_pilot.consensus_engine import get_consensus_config as _get
            cfg = _get()
            return {"ok": True, **cfg.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_consensus_config(self, **kwargs: Any) -> Dict[str, Any]:
        """Patch one or more consensus config fields.

        Accepts: providers (tuple/list), min_agreement (float),
        timeout_s (float), max_chars_per_response (int).
        """
        try:
            from tera_pilot.consensus_engine import (
                get_consensus_config, set_consensus_config,
                ConsensusConfig,
            )
            current = get_consensus_config()
            # Apply only the kwargs that are explicitly provided.
            providers = kwargs.get("providers", current.providers)
            if isinstance(providers, list):
                providers = tuple(providers)
            min_agreement = kwargs.get("min_agreement", current.min_agreement)
            timeout_s = kwargs.get("timeout_s", current.timeout_s)
            max_chars = kwargs.get("max_chars_per_response", current.max_chars_per_response)
            new_cfg = ConsensusConfig(
                providers=providers,
                min_agreement=float(min_agreement),
                timeout_s=float(timeout_s),
                max_chars_per_response=int(max_chars),
            )
            set_consensus_config(new_cfg)
            return {"ok": True, **new_cfg.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def run_consensus(self, prompt: str) -> Dict[str, Any]:
        """Run a prompt on 2–3 providers in parallel and return a
        structured comparison. BLOCKING — callers should run this in
        a worker thread (the TUI does this via @work).
        """
        try:
            from tera_pilot.consensus_engine import run_consensus, render_report_text
            # Determine active provider from the runtime's registry.
            # The bridge stores its AgentRuntime as self._agent (see
            # TeraPilotBridge.__init__). AgentRuntime exposes .registry.
            active_pid = ""
            registry = None
            try:
                if self._agent is not None:
                    registry = getattr(self._agent, "registry", None)
                    if registry is not None:
                        active_pid = getattr(registry, "active_id", "") or ""
            except Exception:
                pass
            report = run_consensus(
                prompt=prompt,
                registry=registry,
                active_provider_id=active_pid,
            )
            return {"ok": True, "text": render_report_text(report), "report": report.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.1.0 (G16) — Signed audit trail ─────────────────────────

    def export_audit_signed_json(self) -> Dict[str, Any]:
        """Export the current activity log as a signed + hash-chained
        JSON string (G16). Each entry gets an Ed25519 signature over
        its canonical payload + the previous entry's hash.
        """
        try:
            from tera_pilot.activity_log import get_activity_log
            log = get_activity_log()
            signed = log.export_signed_json()
            return {"ok": True, "signed_json": signed}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def verify_audit_signed_file(self, path: str) -> Dict[str, Any]:
        """Verify a signed/chained audit export file (G16)."""
        try:
            from tera_pilot.audit_signing import verify_signed_file
            report = verify_signed_file(path)
            return {"ok": True, "report": report.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.1.0 (G17) — Automatic learning loop ────────────────────

    def handle_learnings_command(self, workspace: str, arg: str) -> Dict[str, Any]:
        """Handle the /learnings slash command (G17). Delegates to
        tera_pilot.learning_loop.handle_learnings_command."""
        try:
            from tera_pilot.learning_loop import handle_learnings_command as _handle
            return _handle(workspace, arg)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.1.0 (G18) — Web search backend status ──────────────────

    def get_websearch_status(self) -> Dict[str, Any]:
        """Return web search backend health + last probe results (G18)."""
        try:
            from tera_pilot.web_search_backend import get_websearch_status as _get
            return {"ok": True, "status": _get()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def spawn_subidentity(self, role: str, name: str = "") -> Dict[str, Any]:
        """Derive a child AgentIdentity from the root (for subagent attribution).

        Returns the new identity dict (does NOT spawn an actual agent —
        the runtime is responsible for using the returned identity when
        recording subsequent tool calls).
        """
        try:
            from tera_pilot.agent_identity import get_root_identity
            ident = get_root_identity().child(role=role, name=name)
            return {"ok": True, **ident.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.0.2 (G6) — Post-task handoff (CMS / editable) ──────────

    def create_handoff(
        self,
        output: str,
        prompt: str = "",
        title: str = "",
        agent_identity: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Parse an agent output into an editable HandoffDocument and persist it.

        Returns {ok, doc} where ``doc`` is the full handoff dict
        (id, title, blocks, ...). Use ``set_handoff_block_status`` to
        edit individual blocks, and ``build_handoff_revision_prompt``
        to compile the user's edits into a follow-up agent prompt.
        """
        try:
            from tera_pilot.handoff_bridge import parse_agent_output, get_handoff_store
            # Default to the root agent identity if none given.
            if agent_identity is None:
                try:
                    from tera_pilot.agent_identity import get_root_identity
                    agent_identity = get_root_identity().to_dict()
                except Exception:
                    agent_identity = {}
            doc = parse_agent_output(
                output=output, prompt=prompt, agent=agent_identity, title=title,
            )
            get_handoff_store().save(doc)
            return {"ok": True, "doc": doc.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_handoffs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return metadata for saved handoff documents (no block contents)."""
        try:
            from tera_pilot.handoff_bridge import get_handoff_store
            return get_handoff_store().list_docs(limit=limit)
        except Exception:
            return []

    def get_handoff(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the full handoff document (with blocks) by id."""
        try:
            from tera_pilot.handoff_bridge import get_handoff_store
            doc = get_handoff_store().load(doc_id)
            return doc.to_dict() if doc else None
        except Exception:
            return None

    def set_handoff_block_status(
        self,
        doc_id: str,
        block_id: str,
        status: str,
        comment: str = "",
        replacement: str = "",
    ) -> Dict[str, Any]:
        """Update a single handoff block's status / comment / replacement."""
        try:
            from tera_pilot.handoff_bridge import get_handoff_store
            doc = get_handoff_store().set_block_status(
                doc_id=doc_id, block_id=block_id, status=status,
                comment=comment, replacement=replacement,
            )
            if doc is None:
                return {"ok": False, "error": f"Handoff {doc_id} not found"}
            return {"ok": True, "doc": doc.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def toggle_handoff_todo(self, doc_id: str, block_id: str) -> Dict[str, Any]:
        """Flip a todo block's checked state."""
        try:
            from tera_pilot.handoff_bridge import get_handoff_store
            doc = get_handoff_store().toggle_todo(doc_id, block_id)
            if doc is None:
                return {"ok": False, "error": f"Handoff or block not found"}
            return {"ok": True, "doc": doc.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reorder_handoff_blocks(
        self, doc_id: str, new_order: List[str],
    ) -> Dict[str, Any]:
        """Reorder blocks by id."""
        try:
            from tera_pilot.handoff_bridge import get_handoff_store
            doc = get_handoff_store().reorder_blocks(doc_id, new_order)
            if doc is None:
                return {"ok": False, "error": f"Handoff {doc_id} not found"}
            return {"ok": True, "doc": doc.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_handoff(self, doc_id: str) -> Dict[str, Any]:
        try:
            from tera_pilot.handoff_bridge import get_handoff_store
            ok = get_handoff_store().delete(doc_id)
            return {"ok": ok}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def build_handoff_revision_prompt(self, doc_id: str) -> Dict[str, Any]:
        """Compile the user's edits into a structured revision prompt.

        Returns {ok, prompt}. ``prompt`` is "" if there are no pending
        revisions. The caller (TUI/GUI) typically feeds this back to
        ``run_prompt`` so the agent addresses the user's edits.
        """
        try:
            from tera_pilot.handoff_bridge import get_handoff_store
            prompt = get_handoff_store().build_revision_prompt(doc_id)
            return {"ok": True, "prompt": prompt}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def export_handoff_markdown(self, doc_id: str) -> Dict[str, Any]:
        """Render a handoff document as a single Markdown string."""
        try:
            from tera_pilot.handoff_bridge import get_handoff_store
            md = get_handoff_store().export_markdown(doc_id)
            if not md:
                return {"ok": False, "error": f"Handoff {doc_id} not found"}
            return {"ok": True, "markdown": md}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.0.2 (M2) — Smart cost-aware provider routing ───────────

    def get_cost_router_config(self) -> Dict[str, Any]:
        """Return the current cost-router configuration."""
        try:
            from tera_pilot.cost_router import get_cost_router
            cfg = get_cost_router().get_config()
            return {"ok": True, **cfg.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_cost_router_config(self, **kwargs: Any) -> Dict[str, Any]:
        """Patch one or more cost-router config fields."""
        try:
            from tera_pilot.cost_router import get_cost_router
            cfg = get_cost_router().update_config(**kwargs)
            return {"ok": True, **cfg.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_cost_cap(self, complexity: str, usd: float) -> Dict[str, Any]:
        """Set the USD cap for a single complexity tier."""
        try:
            from tera_pilot.cost_router import get_cost_router
            cfg = get_cost_router().set_cap(complexity, usd)
            return {"ok": True, **cfg.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def cost_route(
        self,
        prompt: str,
        configured_providers: Optional[set] = None,
    ) -> Dict[str, Any]:
        """Run the cost-aware router on a prompt and return the decision."""
        try:
            from tera_pilot.cost_router import get_cost_router
            # Build configured_providers from the registry if not supplied.
            if configured_providers is None and self._registry is not None:
                configured_providers = {
                    p["id"] for p in self._registry.list_providers()
                    if p.get("configured") or p.get("id") in ("ollama", "lmstudio")
                }
            decision = get_cost_router().route(
                prompt=prompt, configured_providers=configured_providers,
            )
            return {"ok": True, **decision.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def apply_cost_route_decision(
        self,
        prompt: str,
        configured_providers: Optional[set] = None,
    ) -> Dict[str, Any]:
        """Run cost routing AND apply the resulting provider/model selection.

        This is what the runtime should call BEFORE dispatching a prompt
        if cost-aware routing is enabled. It sets the active provider on
        the registry and returns the decision so the UI can show it.
        """
        try:
            decision = self.cost_route(prompt, configured_providers)
            if not decision.get("ok"):
                return decision
            final = decision.get("final_pick") or {}
            pid = final.get("provider_id")
            model = final.get("model")
            if pid:
                self.set_provider(pid, model or None)
            return decision
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── v2.0.2 (M3) — Team spend dashboard ────────────────────────

    def get_user_identity(self) -> Dict[str, Any]:
        """Return the local user identity (creates a default if absent)."""
        try:
            from tera_pilot.spend_dashboard import load_identity
            ident = load_identity()
            return {"ok": True, **ident.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_user_team(self, team: str) -> Dict[str, Any]:
        """Update the local user's team and persist."""
        try:
            from tera_pilot.spend_dashboard import set_team
            ident = set_team(team)
            return {"ok": True, **ident.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_team_budget(self, team: Optional[str] = None) -> Dict[str, Any]:
        """Return the team's monthly USD budget (0 = no cap)."""
        try:
            from tera_pilot.spend_dashboard import load_team_budget
            if team is None:
                from tera_pilot.spend_dashboard import load_identity
                team = load_identity().team
            budget = load_team_budget(team)
            return {"ok": True, **budget.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_team_budget(
        self, monthly_usd: float, team: Optional[str] = None, alert_pct: float = 80.0,
    ) -> Dict[str, Any]:
        """Set the team's monthly USD budget."""
        try:
            from tera_pilot.spend_dashboard import load_team_budget, save_team_budget, load_identity
            if team is None:
                team = load_identity().team
            budget = load_team_budget(team)
            budget.monthly_usd = float(monthly_usd)
            budget.alert_pct = float(alert_pct)
            save_team_budget(budget)
            return {"ok": True, **budget.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_team_spend_report(self, days: int = 30) -> Dict[str, Any]:
        """Aggregate the local token history into a team spend report.

        v2.3.4: Spend Dashboard is Pro-licensed; without a license the
        module returns error="pro_required" and this bridge surfaces it
        as an explicit failure instead of zeros.
        """
        try:
            from tera_pilot.spend_dashboard import get_spend_dashboard
            report = get_spend_dashboard().report(days=days)
            if report.error:
                return {"ok": False, "error": _spend_pro_required_msg(), "pro_required": True}
            return {"ok": True, **report.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def add_spend_source(self, path: str) -> Dict[str, Any]:
        """Add a token_history.jsonl source (file or directory of *.jsonl)."""
        try:
            from tera_pilot.spend_dashboard import get_spend_dashboard
            from pathlib import Path as _P
            get_spend_dashboard().add_source(_P(path))
            return {"ok": True, "sources": get_spend_dashboard().list_sources()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_spend_sources(self) -> Dict[str, Any]:
        try:
            from tera_pilot.spend_dashboard import get_spend_dashboard
            return {"ok": True, "sources": get_spend_dashboard().list_sources()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def export_spend_report_json(self, days: int = 30) -> Dict[str, Any]:
        try:
            from tera_pilot.spend_dashboard import get_spend_dashboard
            report = get_spend_dashboard().report(days=days)
            if report.error:
                return {"ok": False, "error": _spend_pro_required_msg(), "pro_required": True}
            return {"ok": True, "json": get_spend_dashboard().export_report_json(days=days)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def export_spend_report_csv(self, days: int = 30) -> Dict[str, Any]:
        try:
            from tera_pilot.spend_dashboard import get_spend_dashboard
            report = get_spend_dashboard().report(days=days)
            if report.error:
                return {"ok": False, "error": _spend_pro_required_msg(), "pro_required": True}
            return {"ok": True, "csv": get_spend_dashboard().export_report_csv(days=days)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    # ── G9/G10/G11/G13 additions ─────────────────────────────

    # ── G9: Hook System ─────────────────────────────────────────────────────

    def register_hook(
        self,
        hook_type: str,
        callback_name: str,
        priority: int = 100,
        enabled: bool = True,
        description: str = "",
        source: str = "",
    ) -> Dict[str, Any]:
        """Register a named hook callback from the hook registry.

        The actual callback is looked up by name from the user's hook modules
        in ~/.tera_pilot/hooks/.  For programmatic registration, use the HookManager
        directly.
        """
        try:
            from tera_pilot.hook_system import get_hook_manager
            mgr = get_hook_manager()
            # For the bridge, we look up the callback by name from the loaded modules.
            # This is a simplified version — the actual callback must have been
            # registered via a user module or the API.
            return {"ok": True, "message": "Use ~/.tera_pilot/hooks/*.py modules to register hooks"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def list_hooks(self, hook_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return metadata for all registered hooks, optionally filtered by type."""
        try:
            from tera_pilot.hook_system import get_hook_manager
            return get_hook_manager().list_hooks(hook_type=hook_type)
        except Exception:
            return []


    def remove_hook(self, hook_id: str) -> Dict[str, Any]:
        """Remove a hook by its id."""
        try:
            from tera_pilot.hook_system import get_hook_manager
            removed = get_hook_manager().remove(hook_id)
            return {"ok": removed, "hook_id": hook_id}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def set_hook_enabled(self, hook_id: str, enabled: bool) -> Dict[str, Any]:
        """Enable or disable a hook."""
        try:
            from tera_pilot.hook_system import get_hook_manager
            found = get_hook_manager().set_enabled(hook_id, enabled)
            return {"ok": found, "hook_id": hook_id, "enabled": enabled}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def test_hook(self, hook_id: str, event_type: str, **kwargs: Any) -> Dict[str, Any]:
        """Dry-run a hook with a synthetic event."""
        try:
            from tera_pilot.hook_system import get_hook_manager
            return get_hook_manager().test_hook(hook_id, event_type, **kwargs)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def get_hook_stats(self) -> Dict[str, Any]:
        """Return hook system statistics."""
        try:
            from tera_pilot.hook_system import get_hook_manager
            return {"ok": True, **get_hook_manager().stats()}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    # ── G10: Checkpoint / Rewind ────────────────────────────────────────────

    def _checkpoint_manager(self):
        """Return the process-wide CheckpointManager, synced to THIS bridge's
        workspace.

        v2.3.4-fix: the manager is a process singleton whose workspace
        defaults to ``Path.cwd()``. When the TUI workspace differs from the
        process cwd, checkpoint backups were taken from / restored to the
        WRONG directory (silent no-op backups, or rewinds writing to cwd
        instead of the project). Re-sync on every access so the manager
        always points at the bridge's active workspace.
        """
        from tera_pilot.checkpoint import get_checkpoint_manager
        mgr = get_checkpoint_manager(session_id="default")
        try:
            if str(mgr.workspace) != str(Path(self.workspace).resolve()):
                mgr.set_workspace(self.workspace)
        except Exception:
            pass
        return mgr

    def create_checkpoint(
        self,
        message_count: int = 0,
        touched_files: Optional[List[str]] = None,
        label: str = "",
    ) -> Dict[str, Any]:
        """Create a manual checkpoint of the current state."""
        try:
            mgr = self._checkpoint_manager()
            cp = mgr.create_checkpoint(
                message_count=message_count,
                touched_files=touched_files,
                label=label,
            )
            return {"ok": True, "checkpoint": cp.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def list_checkpoints(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return metadata for all checkpoints (most recent first)."""
        try:
            return self._checkpoint_manager().list_checkpoints(limit=limit)
        except Exception:
            return []


    def get_checkpoint(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        """Return a single checkpoint's metadata."""
        try:
            return self._checkpoint_manager().get_checkpoint(checkpoint_id)
        except Exception:
            return None


    def rewind_checkpoint(self, n: int = 1) -> Dict[str, Any]:
        """Rewind to the checkpoint N steps back."""
        try:
            return self._checkpoint_manager().rewind(n)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def rewind_to_checkpoint(self, checkpoint_id: str) -> Dict[str, Any]:
        """Rewind to a specific checkpoint by id."""
        try:
            return self._checkpoint_manager().rewind_to(checkpoint_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def diff_checkpoints(self, from_id: str, to_id: str) -> Dict[str, Any]:
        """Compare two checkpoints."""
        try:
            return self._checkpoint_manager().diff_checkpoints(from_id, to_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def set_auto_checkpoint(self, enabled: bool) -> Dict[str, Any]:
        """Enable or disable auto-checkpointing."""
        try:
            self._checkpoint_manager().set_auto_checkpoint(enabled)
            return {"ok": True, "auto_checkpoint": enabled}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def get_checkpoint_stats(self) -> Dict[str, Any]:
        """Return checkpoint statistics."""
        try:
            return {"ok": True, **self._checkpoint_manager().stats()}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    # ── G11: GitHub Automation ──────────────────────────────────────────────

    def github_set_token(self, token: str) -> Dict[str, Any]:
        """Set the GitHub authentication token."""
        try:
            from tera_pilot.github_automation import get_github_automation
            get_github_automation().set_token(token)
            return {"ok": True, "message": "GitHub token set"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_set_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        """Set the GitHub repository (owner/repo)."""
        try:
            from tera_pilot.github_automation import get_github_automation
            get_github_automation().set_repo(owner, repo)
            return {"ok": True, "repo": f"{owner}/{repo}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_auto_detect_repo(self, workspace: Optional[str] = None) -> Dict[str, Any]:
        """Auto-detect the GitHub repo from git remote."""
        try:
            from tera_pilot.github_automation import get_github_automation
            repo = get_github_automation().auto_detect_repo(workspace)
            if repo:
                return {"ok": True, "repo": repo}
            return {"ok": False, "error": "No GitHub remote found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_list_prs(self, state: str = "open", limit: int = 10) -> Dict[str, Any]:
        """List pull requests."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return get_github_automation().list_prs(state=state, limit=limit)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_get_pr(self, number: int) -> Dict[str, Any]:
        """Get a single pull request."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return get_github_automation().get_pr(number)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_create_pr(self, title: str, body: str = "", head: str = "", base: str = "main") -> Dict[str, Any]:
        """Create a pull request."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return get_github_automation().create_pr(title=title, body=body, head=head, base=base)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_get_pr_context(self, number: int) -> Dict[str, Any]:
        """Get full PR context for implementing."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return get_github_automation().get_pr_context(number)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_list_issues(self, state: str = "open", limit: int = 10, labels: str = "") -> Dict[str, Any]:
        """List issues."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return get_github_automation().list_issues(state=state, limit=limit, labels=labels)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_get_issue(self, number: int) -> Dict[str, Any]:
        """Get a single issue."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return get_github_automation().get_issue(number)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_create_issue(self, title: str, body: str = "", labels: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create an issue."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return get_github_automation().create_issue(title=title, body=body, labels=labels)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_comment_on_pr(self, number: int, body: str) -> Dict[str, Any]:
        """Add a comment to a PR."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return get_github_automation().comment_on_pr(number, body)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_generate_action(self, trigger: str = "pull_request") -> Dict[str, Any]:
        """Generate a GitHub Action workflow template."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return get_github_automation().generate_action_template(trigger=trigger)
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def github_status(self) -> Dict[str, Any]:
        """Return the GitHub automation status."""
        try:
            from tera_pilot.github_automation import get_github_automation
            return {"ok": True, **get_github_automation().status()}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    # ── G13: MCP Server ─────────────────────────────────────────────────────

    def mcp_server_list_tools(self) -> Dict[str, Any]:
        """List tools available in MCP server mode."""
        try:
            from tera_pilot.mcp_server import MCPServerMode
            server = MCPServerMode(workspace=str(self._agent.workspace) if self._agent else os.getcwd())
            return {"ok": True, "tools": server.list_tools()}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def mcp_server_status(self) -> Dict[str, Any]:
        """Return MCP server status."""
        try:
            from tera_pilot.mcp_server import MCPServerMode
            server = MCPServerMode(workspace=str(self._agent.workspace) if self._agent else os.getcwd())
            return {"ok": True, **server.status()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Notifier (G18) ─────────────────────────────────────────────

    def notify_configure_backend(self, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Configure or update a notification backend."""
        try:
            from tera_pilot.notifier import get_notifier
            return get_notifier().configure_backend(name, config)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def notify_set_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        """Enable or disable a notification backend."""
        try:
            from tera_pilot.notifier import get_notifier
            return get_notifier().set_backend_enabled(name, enabled)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def notify_test(self, name: str) -> Dict[str, Any]:
        """Send a test notification to a specific backend."""
        try:
            from tera_pilot.notifier import get_notifier
            return get_notifier().test_backend(name)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def notify_test_all(self) -> Dict[str, Any]:
        """Send test notifications to all configured backends."""
        try:
            from tera_pilot.notifier import get_notifier
            return get_notifier().test_all()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def notify_list_backends(self) -> List[Dict[str, Any]]:
        """Return metadata for all configured notification backends."""
        try:
            from tera_pilot.notifier import get_notifier
            return get_notifier().list_backends()
        except Exception:
            return []

    def notify_set_events(self, name: str, events: List[str]) -> Dict[str, Any]:
        """Set which event kinds trigger notifications for a backend."""
        try:
            from tera_pilot.notifier import get_notifier
            return get_notifier().set_events(name, events)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def notify_status(self) -> Dict[str, Any]:
        """Return overall notifier status."""
        try:
            from tera_pilot.notifier import get_notifier
            return {"ok": True, **get_notifier().status()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def notify_remove_backend(self, name: str) -> Dict[str, Any]:
        """Remove a notification backend configuration."""
        try:
            from tera_pilot.notifier import get_notifier
            return get_notifier().remove_backend(name)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Daemon (G18) ────────────────────────────────────────────────

    def daemon_submit_task(self, prompt: str, workspace: str = "") -> Dict[str, Any]:
        """Submit a task to the daemon's task queue."""
        try:
            from tera_pilot.daemon import TaskQueue
            # If a daemon is running, submit to its queue
            # Otherwise return an error suggesting to start the daemon
            return {"ok": False, "error": "Daemon not running. Start with: tera-pilot-daemon"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def daemon_status(self) -> Dict[str, Any]:
        """Return daemon status information."""
        try:
            from tera_pilot.daemon import load_daemon_config
            config = load_daemon_config()
            return {"ok": True, "configured": bool(config.get("auth_token"))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── G19a — Task canvas ──────────────────────────────────────────

    def get_task_canvas(self) -> Dict[str, Any]:
        """Return the current task canvas state (nodes, counts, total).

        Used by the TUI's TaskCanvasView widget and by the GUI's
        ``get_task_canvas`` slot. Returns ``{"ok": True, "canvas": {...}}``
        on success. The canvas is a process-wide singleton, so this
        always reflects the latest state from any thread.
        """
        try:
            from tera_pilot.agent.task_canvas import get_task_canvas
            return {"ok": True, "canvas": get_task_canvas().to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reset_task_canvas(self) -> Dict[str, Any]:
        """Drop every node from the task canvas. Used at the start of a
        new top-level task so the previous turn's canvas doesn't leak."""
        try:
            from tera_pilot.agent.task_canvas import get_task_canvas
            get_task_canvas().reset()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── G19b — Persona memory ───────────────────────────────────────

    def get_persona(self) -> Dict[str, Any]:
        """Return the current persona.md content + path + char counts.

        The ``content`` field is the raw Markdown text — the TUI/GUI
        renders it as-is (no parsing, no transformation).
        """
        try:
            from tera_pilot.agent.persona_memory import get_persona_memory
            return {"ok": True, **get_persona_memory().to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_persona(self, content: str) -> Dict[str, Any]:
        """Overwrite the persona.md content. Used by ``/persona edit``.

        Enforces the hard char cap (2200) — anything larger is
        truncated with a note. Returns the new char count.
        """
        try:
            from tera_pilot.agent.persona_memory import get_persona_memory
            get_persona_memory().set(content)
            return {"ok": True, **get_persona_memory().to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reset_persona(self) -> Dict[str, Any]:
        """Delete the persona file. Used by ``/persona reset``."""
        try:
            from tera_pilot.agent.persona_memory import get_persona_memory
            get_persona_memory().reset()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def update_persona_from_session(
        self, digest_dict: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Run the cheap maintenance LLM call to update the persona.

        ``digest_dict`` is an optional dict with keys matching
        :class:`PersonaDigest` fields (summary, accepted_actions, ...).
        If omitted, an empty digest is used (the LLM will likely return
        the persona unchanged).

        Best-effort: any failure leaves the existing persona on disk
        untouched. Returns ``{"ok": True, "before_chars": N,
        "after_chars": M, ...}`` on success or ``{"ok": False,
        "error": str(e)}`` on failure.
        """
        try:
            from tera_pilot.agent.persona_memory import (
                get_persona_memory,
                PersonaDigest,
            )

            digest = PersonaDigest()
            if digest_dict:
                for k, v in digest_dict.items():
                    if hasattr(digest, k) and isinstance(v, list):
                        setattr(digest, k, list(v))
                    elif hasattr(digest, k) and isinstance(v, str):
                        setattr(digest, k, v)
            result = get_persona_memory().update_from_session(digest)
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── G20c — Decompose mode ───────────────────────────────────────

    def set_router_mode(self, mode: str) -> Dict[str, Any]:
        """Set the AutoRouter mode: ``"single"`` (default) or
        ``"decompose"`` (G20 task-decomposition router).

        ``"decompose"`` mode routes the prompt through the
        :class:`TaskDecompositionRouter` which breaks the task into
        subtasks, picks the best model for each, dispatches them as
        subagents (in parallel where possible), and merges the results.
        """
        try:
            from tera_pilot.auto_router import get_auto_router
            ar = get_auto_router()
            ar.set_mode(mode)
            return {"ok": True, "mode": ar.get_mode()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_router_mode(self) -> Dict[str, Any]:
        """Return the current AutoRouter mode (``"single"`` or
        ``"decompose"``)."""
        try:
            from tera_pilot.auto_router import get_auto_router
            return {"ok": True, "mode": get_auto_router().get_mode()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # --------------------------------------------------------- launch TUI
    def launch_tui(self) -> Dict[str, Any]:
        """Launch the Tera Pilot TUI in a new terminal window.
        This is called from the Web UI when the user clicks 'Open in Terminal'.

        v2.2.4-fix: all workspace paths are now shell-escaped with
        shlex.quote to prevent command injection (BUGS_REPORT).
        """
        try:
            import subprocess
            import sys
            import os
            import shlex

            # Get the current workspace
            workspace = self.workspace
            safe_ws = shlex.quote(workspace)

            # Determine the command to launch TUI
            # Use the same Python interpreter that's running this process
            python_exe = sys.executable

            # Try to find tera_pilot_tui entry point
            # First try: tera_pilot_tui command (if installed)
            # Second try: python -m tera_pilot_tui
            # Third try: direct module execution
            cmd = [python_exe, "-m", "tera_pilot_tui"]
            safe_cmd = " ".join(shlex.quote(c) for c in cmd)

            # On macOS, use osascript to open a new Terminal window
            if sys.platform == "darwin":
                # Build the command string for the new terminal
                # We need to activate the virtual environment if there is one
                venv_path = os.environ.get("VIRTUAL_ENV")
                if venv_path:
                    activate_cmd = f"source {shlex.quote(venv_path)}/bin/activate && "
                else:
                    activate_cmd = ""

                # Change to workspace directory and run tera_pilot_tui
                terminal_cmd = f'cd {safe_ws} && {activate_cmd}{safe_cmd}'

                # Use osascript to open a new Terminal window
                applescript = f'''
                tell application "Terminal"
                    do script {shlex.quote(terminal_cmd)}
                    activate
                end tell
                '''
                subprocess.Popen(["osascript", "-e", applescript], start_new_session=True)
            elif sys.platform == "linux":
                # Try common terminal emulators on Linux
                bash_cmd = f"cd {safe_ws} && {safe_cmd}; exec bash"
                terminals = [
                    ["gnome-terminal", "--", "bash", "-c", bash_cmd],
                    ["konsole", "-e", "bash", "-c", bash_cmd],
                    ["xterm", "-e", "bash", "-c", bash_cmd],
                    ["alacritty", "-e", "bash", "-c", bash_cmd],
                    ["kitty", "-e", "bash", "-c", bash_cmd],
                ]
                launched = False
                for term_cmd in terminals:
                    try:
                        subprocess.Popen(term_cmd, start_new_session=True)
                        launched = True
                        break
                    except FileNotFoundError:
                        continue
                if not launched:
                    return {"ok": False, "error": "No supported terminal emulator found. Please install gnome-terminal, konsole, xterm, alacritty, or kitty."}
            elif sys.platform == "win32":
                # Windows: use start cmd — escape workspace for cmd.exe
                safe_ws_win = workspace.replace('"', '""')
                subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", f"cd /d \"{safe_ws_win}\" && {safe_cmd}"], start_new_session=True)
            else:
                return {"ok": False, "error": f"Unsupported platform: {sys.platform}"}

            return {"ok": True, "message": "TUI launched in new terminal window"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
