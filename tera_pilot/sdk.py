"""
Python SDK — embed Tera Pilot in your own scripts and apps.

The TUI, daemon and CLI are all thin shells around ``AgentRuntime``;
this module is the fourth shell, for library use::

    from tera_pilot.sdk import run

    result = run("summarise auth.py", workspace="~/myproj", provider="ollama")
    print(result.output)

Or keep one agent across turns::

    from tera_pilot.sdk import TeraPilot

    with TeraPilot(workspace="~/myproj") as pilot:
        pilot.run("find the riskiest file")
        print(pilot.run("and suggest a fix").output)

Trust model: the SDK defaults to ``autonomy="never_ask"`` — the caller
*is* the operator, same as running a script. Pass
``autonomy="always_ask"`` together with ``on_confirm`` to gate
side effects, or ``headless_confirm="allow"`` only when you mean it.
Writes are still confined to ``workspace`` (+ ``extra_dirs``) by the
sandbox, and the command policy / Guardian apply unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_VALID_AUTONOMY = ("always_ask", "new_files_only", "never_ask")


@dataclass
class RunResult:
    """Outcome of one ``run()`` call."""

    output: str
    success: bool
    iterations: int = 0
    error: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0


def _build_registry(provider: Optional[str] = None, model: Optional[str] = None,
                    api_key: Optional[str] = None,
                    api_base: Optional[str] = None):
    from tera_pilot.providers import get_registry, ProviderConfig
    from tera_pilot.utils import load_config

    registry = get_registry()
    try:
        if not registry.list_providers():
            registry.register_default()
    except Exception:
        try:
            registry.register_default()
        except Exception:
            pass
    cfg = {}
    try:
        cfg = load_config() or {}
    except Exception:
        pass
    for pid, pcfg in ((cfg.get("providers") or {}).items()):
        try:
            registry.configure(pid, ProviderConfig(
                provider_id=pid,
                model=pcfg.get("model", ""),
                api_key=pcfg.get("api_key") or None,
                api_base=pcfg.get("api_base") or None,
                temperature=float(pcfg.get("temperature", 0.2)),
                max_tokens=int(pcfg.get("max_tokens", 4096))))
        except Exception:
            continue
    active = provider or cfg.get("active_provider") or "ollama"
    if model or api_key or api_base:
        try:
            existing = registry.get(active).config
        except Exception:
            existing = None
        registry.configure(active, ProviderConfig(
            provider_id=active,
            model=model or (existing.model if existing else ""),
            api_key=(api_key or os.environ.get(f"{active.upper()}_API_KEY")
                     or (existing.api_key if existing else None)),
            api_base=api_base or (existing.api_base if existing else None),
            temperature=float(getattr(existing, "temperature", 0.2) or 0.2),
            max_tokens=int(getattr(existing, "max_tokens", 4096) or 4096)))
    try:
        registry.set_active(active)
    except Exception:
        pass
    return registry


class TeraPilot:
    """A reusable agent bound to one workspace."""

    def __init__(
        self,
        workspace: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        max_iterations: int = 8,
        section: str = "general",
        autonomy: str = "never_ask",
        headless_confirm: str = "fail_closed",
        verbosity: str = "normal",
        output_style: str = "normal",
        extra_dirs: Optional[List[str]] = None,
        on_confirm: Optional[Callable[[Dict[str, Any]], bool]] = None,
        registry: Any = None,
    ) -> None:
        if autonomy not in _VALID_AUTONOMY:
            raise ValueError(f"autonomy must be one of {_VALID_AUTONOMY}")
        if section not in ("general", "heavy_code", "office"):
            raise ValueError("section must be general|heavy_code|office")
        self.workspace = str(Path(workspace or os.getcwd()).expanduser().resolve())
        self._registry = registry or _build_registry(provider, model, api_key, api_base)
        from tera_pilot.agent_runtime import AgentRuntime
        self._agent = AgentRuntime(
            registry=self._registry,
            workspace=self.workspace,
            max_iterations=max(1, min(50, int(max_iterations or 8))),
            section=section,
            verbosity=verbosity,
            output_style=output_style,
        )
        self._agent.set_autonomy(autonomy)
        self._agent.tools.headless_confirm = headless_confirm
        for extra in extra_dirs or []:
            try:
                if os.path.isdir(os.path.expanduser(extra)):
                    self._agent.tools.add_allowed_dir(os.path.expanduser(extra))
            except Exception:
                continue
        if on_confirm is not None:
            agent = self._agent

            def _confirm(info: Dict[str, Any]) -> None:
                try:
                    decision = bool(on_confirm(dict(info)))
                except Exception:
                    decision = False
                try:
                    agent.tools.respond_confirmation(decision)
                except Exception:
                    pass

            self._agent.set_confirm_callback(_confirm)

    @property
    def agent(self):
        """The underlying AgentRuntime (for tools/memory access)."""
        return self._agent

    def run(self, prompt: str) -> RunResult:
        """Run one agentic turn. Blocking."""
        if not (prompt or "").strip():
            raise ValueError("prompt must not be empty")
        raw = self._agent.run(prompt)
        calls = []

        def _one(name: Any, args: Any, error: Any) -> None:
            tool = getattr(name, "value", None) or str(name or "")
            if tool:
                calls.append({"tool": tool, "args": dict(args or {}) or {},
                              "error": error})

        try:
            for call in (getattr(raw, "tool_calls", None) or []):
                _one(getattr(call, "name", ""), getattr(call, "args", {}),
                     getattr(call, "error", None))
            if not calls:
                # Some run paths return steps without tool_calls.
                for step in (getattr(raw, "steps", None) or []):
                    action = getattr(step, "action", None)
                    if action is not None:
                        _one(getattr(action, "name", ""), getattr(action, "args", {}),
                             getattr(action, "error", None))
        except Exception:
            pass
        tokens_in = tokens_out = 0
        try:
            stats = self._agent.get_token_stats() or {}
            tokens_in = int(stats.get("tokens_in", 0) or 0)
            tokens_out = int(stats.get("tokens_out", 0) or 0)
        except Exception:
            pass
        return RunResult(
            output=getattr(raw, "output", "") or "",
            success=bool(getattr(raw, "success", False)),
            iterations=int(getattr(raw, "iterations", 0) or 0),
            error=getattr(raw, "error", None),
            tool_calls=calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def compact(self) -> Dict[str, Any]:
        """Summarise old context, keep recent. Returns status dict."""
        try:
            return dict(self._agent.compact_context())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def close(self) -> None:
        """Release background resources (REPL sessions, LSP client)."""
        try:
            for session in list(getattr(self._agent.tools, "_repl_sessions", {})):
                try:
                    self._agent.tools._repl_reset(session)
                except Exception:
                    continue
        except Exception:
            pass
        try:
            lsp = getattr(self._agent.tools, "_lsp", None)
            if lsp is not None:
                lsp.shutdown()
        except Exception:
            pass

    def __enter__(self) -> "TeraPilot":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def run(prompt: str, **kwargs: Any) -> RunResult:
    """One-shot helper: build a pilot, run once, close."""
    with TeraPilot(**kwargs) as pilot:
        return pilot.run(prompt)
