#!/usr/bin/env python3
"""tera_pilot_tui/smoke_test.py — TUI smoke tests (v2.0.0).

Verifies that the TUI package imports cleanly, all widgets can be
constructed, the bridge exposes every backend capability the UI
relies on, and the slash-command list covers all v2.0 features
(Guardian, collaboration modes, request queue, persistence, context
fragments, progressive tools).

Run:
    python -m tera_pilot_tui.smoke_test
or:
    pytest tera_pilot_tui/smoke_test.py -v

The tests do NOT start the Textual event loop and do NOT require a
real LLM provider. They construct bridge/widget objects directly and
probe their public APIs. Anything that needs a live runtime is
wrapped in try/except so the smoke test stays green in CI without
API keys.
"""

from __future__ import annotations

import os
import sys
import importlib
import inspect
import tempfile
from pathlib import Path

# Make sure the project root is on sys.path so `import tera_pilot_tui` works
# when running the file directly.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Tiny test framework (no pytest dependency) ────────────────────────

class _SmokeResult:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.skipped = False
        self.detail: str = ""

    def __repr__(self) -> str:
        if self.skipped:
            return f"SKIP  {self.name}  ({self.detail})"
        return f"{'OK  ' if self.ok else 'FAIL'}  {self.name}  {self.detail}"


_RESULTS: list[_SmokeResult] = []


def register_test(name: str):
    """Decorator: register a smoke-test function."""

    def deco(fn):
        r = _SmokeResult(name)
        try:
            fn(r)
            r.ok = True
        except AssertionError as e:
            r.ok = False
            r.detail = f"assertion: {e}"
        except Exception as e:
            r.ok = False
            r.detail = f"{type(e).__name__}: {e}"
        _RESULTS.append(r)
        return fn

    return deco


def check(cond, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "condition false")


def skip(r: _SmokeResult, reason: str) -> None:
    r.skipped = True
    r.detail = reason


# ── 1. Import & module structure ──────────────────────────────────────

@register_test("tera_pilot_tui package imports cleanly")
def _t(r):
    import tera_pilot_tui
    check(hasattr(tera_pilot_tui, "TeraPilotTUIApp"), "TeraPilotTUIApp missing from tera_pilot_tui")
    check(hasattr(tera_pilot_tui, "TeraPilotBridge"), "TeraPilotBridge missing from tera_pilot_tui")


@register_test("tera_pilot_tui.app exposes TeraPilotTUIApp with bindings")
def _t(r):
    from tera_pilot_tui.app import TeraPilotTUIApp
    bindings = TeraPilotTUIApp.BINDINGS
    triggers = {b.key for b in bindings}
    for required in {"ctrl+c", "ctrl+d", "ctrl+g", "ctrl+p", "ctrl+t"}:
        check(required in triggers, f"missing binding: {required}")


@register_test("tera_pilot_tui.bridge exposes TeraPilotBridge and ProviderChoice")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge, ProviderChoice
    check(callable(TeraPilotBridge), "TeraPilotBridge not callable")
    check(hasattr(ProviderChoice, "__dataclass_fields__"),
          "ProviderChoice is not a dataclass")


@register_test("tera_pilot_tui.widgets exports all widget classes including GuardianModal")
def _t(r):
    from tera_pilot_tui import widgets
    expected = {"ApprovalModal", "ChatLog", "CommandPalette",
                "CommandSuggestions", "GuardianModal", "InputBox", "StatusBar"}
    exported = set(widgets.__all__)
    check(expected.issubset(exported),
          f"missing widgets: {expected - exported}")


# ── 2. Bridge API surface (v2.0 backend capabilities) ─────────────────

@register_test("TeraPilotBridge exposes v2.0 collaboration API")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    for method in ("list_collaboration_modes", "run_collaboration"):
        check(hasattr(TeraPilotBridge, method),
              f"TeraPilotBridge missing {method}")


@register_test("TeraPilotBridge exposes v2.0 request-queue API")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    check(hasattr(TeraPilotBridge, "get_queue_stats"),
          "TeraPilotBridge missing get_queue_stats")


@register_test("TeraPilotBridge exposes v2.0 persistence-backend API")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    for method in ("get_persistence_backend", "set_persistence_backend",
                   "list_sqlite_sessions"):
        check(hasattr(TeraPilotBridge, method),
              f"TeraPilotBridge missing {method}")


@register_test("TeraPilotBridge exposes v2.0 context-fragments API")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    check(hasattr(TeraPilotBridge, "get_compaction_stats"),
          "TeraPilotBridge missing get_compaction_stats")


@register_test("TeraPilotBridge exposes v2.0 progressive-tools catalog API")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    check(hasattr(TeraPilotBridge, "get_tool_catalog_state"),
          "TeraPilotBridge missing get_tool_catalog_state")


@register_test("TeraPilotBridge exposes Guardian level API + persistence helpers")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    for method in ("set_guardian_level", "get_guardian_level",
                   "_save_guardian_config", "_load_guardian_config"):
        check(hasattr(TeraPilotBridge, method),
              f"TeraPilotBridge missing {method}")


@register_test("TeraPilotBridge exposes the v2.0 Guardian handler hook")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    check(hasattr(TeraPilotBridge, "set_guardian_handler"),
          "TeraPilotBridge missing set_guardian_handler")
    # The dead `answer_guardian` method should have been removed.
    check(not hasattr(TeraPilotBridge, "answer_guardian"),
          "dead method answer_guardian() should be removed")
    check(hasattr(TeraPilotBridge, "answer_guardian_verdict"),
          "answer_guardian_verdict must remain")


# ── 3. Command palette covers all v2.0 commands ──────────────────────

@register_test("BUILTIN_COMMANDS includes v2.0 commands")
def _t(r):
    from tera_pilot_tui.widgets.command_palette import BUILTIN_COMMANDS
    ids = {c.id for c in BUILTIN_COMMANDS}
    required = {
        "section", "model", "chat", "cd", "usage", "files",
        "clear", "help", "planning", "gui", "guardian",
        "collab", "queue", "storage", "sessions", "context", "tools",
    }
    missing = required - ids
    check(not missing, f"missing builtin commands: {missing}")


@register_test("CommandPalette is a ModalScreen with expected bindings")
def _t(r):
    from tera_pilot_tui.widgets.command_palette import CommandPalette
    from textual.screen import ModalScreen
    check(issubclass(CommandPalette, ModalScreen),
          "CommandPalette must subclass ModalScreen")
    triggers = {b.key for b in CommandPalette.BINDINGS}
    for required in {"escape", "up", "down", "enter"}:
        check(required in triggers,
              f"CommandPalette missing binding: {required}")


@register_test("CommandPalette uses OptionList.highlighted (not legacy highlight)")
def _t(r):
    src = Path(__file__).resolve().parent / "widgets" / "command_palette.py"
    text = src.read_text() if src.exists() else ""
    check(".highlighted" in text,
          "CommandPalette should use OptionList.highlighted")
    check(".highlight = " not in text.replace(".highlighted", ""),
          "CommandPalette should not use the legacy .highlight = setter")


# ── 4. ChatLog widget — v2.0 features and bug fixes ──────────────────

@register_test("ChatLog exposes add_tool_call with optional sub_label")
def _t(r):
    from tera_pilot_tui.widgets.chat_log import ChatLog
    sig = inspect.signature(ChatLog.add_tool_call)
    check("sub_label" in sig.parameters,
          "ChatLog.add_tool_call must accept sub_label")


@register_test("ChatLog exposes reviewer-verdict and observer-warnings panels")
def _t(r):
    from tera_pilot_tui.widgets.chat_log import ChatLog
    check(hasattr(ChatLog, "add_reviewer_verdict"),
          "ChatLog must have add_reviewer_verdict")
    check(hasattr(ChatLog, "add_observer_warnings"),
          "ChatLog must have add_observer_warnings")


@register_test("ChatLog append_token_delta does not drop the first chunk")
def _t(r):
    src = Path(__file__).resolve().parent / "widgets" / "chat_log.py"
    text = src.read_text()
    # The bug was: `self._streaming_text = ""` on the first branch,
    # which dropped the first chunk. The fix assigns the chunk itself.
    check("self._streaming_text = chunk" in text,
          "append_token_delta must assign the chunk to _streaming_text on first call")


# ── 5. StatusBar exposes Guardian badge ───────────────────────────────

@register_test("StatusBar.update_status accepts guardian parameter")
def _t(r):
    from tera_pilot_tui.widgets.status_bar import StatusBar
    sig = inspect.signature(StatusBar.update_status)
    check("guardian" in sig.parameters,
          "StatusBar.update_status must accept guardian")


@register_test("StatusBar defines GUARDIAN_LABELS for off/dangerous_only/all")
def _t(r):
    from tera_pilot_tui.widgets.status_bar import GUARDIAN_LABELS
    for level in ("off", "dangerous_only", "all"):
        check(level in GUARDIAN_LABELS, f"missing Guardian level: {level}")


# ── 6. App handles iteration_end + subagent labels ───────────────────

@register_test("TeraPilotTUIApp._handle_event handles iteration_end")
def _t(r):
    from tera_pilot_tui.app import TeraPilotTUIApp
    src = inspect.getsource(TeraPilotTUIApp._handle_event)
    check("iteration_end" in src,
          "_handle_event must handle iteration_end")


@register_test("TeraPilotTUIApp._handle_event surfaces subagent labels")
def _t(r):
    from tera_pilot_tui.app import TeraPilotTUIApp
    src = inspect.getsource(TeraPilotTUIApp._handle_event)
    check("parent_label" in src or "subagent_label" in src,
          "_handle_event must surface subagent labels")


@register_test("TeraPilotTUIApp.on_mount wires set_guardian_handler")
def _t(r):
    from tera_pilot_tui.app import TeraPilotTUIApp
    src = inspect.getsource(TeraPilotTUIApp.on_mount)
    check("set_guardian_handler" in src,
          "on_mount must wire set_guardian_handler")


@register_test("TeraPilotTUIApp.action_toggle_theme calls reload_css")
def _t(r):
    from tera_pilot_tui.app import TeraPilotTUIApp
    src = inspect.getsource(TeraPilotTUIApp.action_toggle_theme)
    check("reload_css" in src,
          "action_toggle_theme must call reload_css")


# ── 7. App exposes all v2.0 slash-command exec methods ───────────────

@register_test("TeraPilotTUIApp has _exec_* methods for every v2.0 command")
def _t(r):
    from tera_pilot_tui.app import TeraPilotTUIApp
    for method in ("_exec_collab", "_exec_queue", "_exec_storage",
                   "_exec_sessions", "_exec_context", "_exec_tools",
                   "_exec_guardian"):
        check(hasattr(TeraPilotTUIApp, method),
              f"TeraPilotTUIApp missing {method}")


# ── 8. Live bridge smoke (no agent required) ─────────────────────────

@register_test("TeraPilotBridge.list_collaboration_modes returns 5 modes")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    b = TeraPilotBridge(workspace=tempfile.gettempdir())
    modes = b.list_collaboration_modes()
    check(len(modes) == 5, f"expected 5 modes, got {len(modes)}")
    ids = {m["id"] for m in modes}
    check(ids == {"single", "reviewer", "codegen", "pair", "observer"},
          f"unexpected mode ids: {ids}")


@register_test("TeraPilotBridge.get_persistence_backend returns 'json' or 'sqlite'")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    b = TeraPilotBridge(workspace=tempfile.gettempdir())
    backend = b.get_persistence_backend()
    check(backend in ("json", "sqlite"),
          f"unexpected backend: {backend}")


@register_test("TeraPilotBridge.get_queue_stats returns a dict (possibly empty)")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    b = TeraPilotBridge(workspace=tempfile.gettempdir())
    stats = b.get_queue_stats()
    check(isinstance(stats, dict),
          f"queue stats must be a dict, got {type(stats)}")


@register_test("TeraPilotBridge.get_compaction_stats returns None without a live agent")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    b = TeraPilotBridge(workspace=tempfile.gettempdir())
    stats = b.get_compaction_stats()
    # Without ensure_agent() being called, _agent is None and the method
    # should return None rather than crashing.
    check(stats is None, f"expected None, got {stats}")


@register_test("TeraPilotBridge.get_tool_catalog_state returns a dict with keys")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    b = TeraPilotBridge(workspace=tempfile.gettempdir())
    state = b.get_tool_catalog_state()
    check(isinstance(state, dict), "state must be a dict")
    for key in ("loaded", "available", "prompt_chars_saved"):
        check(key in state, f"missing key: {key}")


@register_test("TeraPilotBridge persistence backend switch round-trips")
def _t(r):
    from tera_pilot_tui.bridge import TeraPilotBridge
    # Use a fresh temp HOME so we don't pollute the real ~/.tera_pilot/config.json.
    tmp_home = tempfile.mkdtemp(prefix="tera_pilot_tui_smoke_")
    old_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = tmp_home
        b = TeraPilotBridge(workspace=tmp_home)
        # Default should be 'json' since the config file doesn't exist yet.
        check(b.get_persistence_backend() == "json",
              "default backend must be json")
        # Switch to sqlite and back.
        r1 = b.set_persistence_backend("sqlite")
        check(r1.get("ok"), f"set sqlite failed: {r1}")
        check(b.get_persistence_backend() == "sqlite",
              "backend should now be sqlite")
        r2 = b.set_persistence_backend("json")
        check(r2.get("ok"), f"set json failed: {r2}")
        check(b.get_persistence_backend() == "json",
              "backend should be json again")
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home
        else:
            os.environ.pop("HOME", None)


# ── 9. CSS files exist and are non-empty ──────────────────────────────

@register_test("styles_dark.tcss and styles_light.tcss exist and are non-empty")
def _t(r):
    base = Path(__file__).resolve().parent
    for fname in ("styles_dark.tcss", "styles_light.tcss"):
        f = base / fname
        check(f.exists(), f"missing {fname}")
        check(f.stat().st_size > 0, f"{fname} is empty")


# ── 10. ApprovalModal / GuardianModal sanity ─────────────────────────

@register_test("ApprovalModal and GuardianModal are ModalScreens")
def _t(r):
    from tera_pilot_tui.widgets.approval_modal import ApprovalModal, GuardianModal
    from textual.screen import ModalScreen
    check(issubclass(ApprovalModal, ModalScreen),
          "ApprovalModal must subclass ModalScreen")
    check(issubclass(GuardianModal, ModalScreen),
          "GuardianModal must subclass ModalScreen")


@register_test("GuardianModal returns approve/reject/use_fix")
def _t(r):
    from tera_pilot_tui.widgets.approval_modal import GuardianModal
    # The class must define three action methods corresponding to the
    # three possible verdicts the bridge accepts.
    for action in ("action_approve", "action_reject", "action_use_fix"):
        check(hasattr(GuardianModal, action),
              f"GuardianModal missing {action}")


# ── Runner ────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("tera_pilot TUI v2.3.7 — SMOKE TESTS")
    print("=" * 70)

    # Import the test module so all @test decorators have run.
    # They are registered at import time.
    # The tests themselves are already registered above.

    passed = sum(1 for r in _RESULTS if r.ok and not r.skipped)
    failed = sum(1 for r in _RESULTS if not r.ok and not r.skipped)
    skipped = sum(1 for r in _RESULTS if r.skipped)
    total = len(_RESULTS)

    for r in _RESULTS:
        print(f"  {r!r}")

    print()
    print("=" * 70)
    print(f"  Total: {total}  |  Passed: {passed}  |  Failed: {failed}  |  Skipped: {skipped}")
    print("=" * 70)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
