"""Unit tests for the ToolEngine guardian-wait mechanics (v2.3.9-fix).

The TUI's Guardian modal fix routes every verdict through
``answer_guardian_verdict()`` → ``respond_guardian()``. These tests pin
the engine-side contract that fix depends on:

  * ``respond_confirmation()`` (used by ``answer_confirmation()``) only
    wakes the *_confirm* wait — it must NOT unblock a thread that is
    waiting inside a Guardian MODIFY review (which waits on
    ``_guardian_event``).
  * ``respond_guardian()`` wakes the guardian wait and records the exact
    decision (approve / reject / use_fix).
  * An invalid decision is coerced to ``reject`` (fail closed).

No network, no LLM, no subprocess.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.agent_runtime.tool_engine import ToolEngine  # noqa: E402


def _new_engine() -> ToolEngine:
    engine = ToolEngine(workspace=".")
    return engine


def _wait_on_guardian(engine: ToolEngine) -> None:
    """Block like the engine's guardian flow does, then record the
    decision the engine would apply."""
    ok = engine._wait_interruptible(engine._guardian_event, timeout=5)
    engine._wait_result = engine._guardian_decision if ok else "reject"


def test_answer_confirmation_does_not_wake_guardian_wait():
    """The bug's premise: poking the confirm wait must NOT unblock a
    guardian review. If it did, the old routing (answer_confirmation for
    'approve'/'reject') would have worked and the TUI fix would be moot."""
    engine = _new_engine()
    engine._guardian_event.clear()
    engine._guardian_decision = None

    thread = threading.Thread(target=_wait_on_guardian, args=(engine,), daemon=True)
    thread.start()

    # Give the thread time to reach the wait.
    time.sleep(0.2)
    assert thread.is_alive(), "guardian waiter should still be blocked"

    # Old TUI behaviour: answer_confirmation pokes the *confirm* wait.
    engine.respond_confirmation(True)
    time.sleep(0.3)
    assert thread.is_alive(), (
        "respond_confirmation() must NOT unblock the guardian wait — "
        "the TUI routing fix depends on these two waits being separate"
    )

    # Clean up so the test never hangs: the real answer arrives via
    # respond_guardian.
    engine.respond_guardian("approve")
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert engine._wait_result == "approve"


def test_respond_guardian_approve_wakes_and_records():
    engine = _new_engine()
    engine._guardian_event.clear()
    engine._guardian_decision = None

    thread = threading.Thread(target=_wait_on_guardian, args=(engine,), daemon=True)
    thread.start()
    time.sleep(0.2)

    engine.respond_guardian("approve")
    thread.join(timeout=3)
    assert not thread.is_alive(), "guardian wait must unblock on respond_guardian"
    assert engine._wait_result == "approve"


def test_respond_guardian_use_fix_wakes_and_records():
    engine = _new_engine()
    engine._guardian_event.clear()
    engine._guardian_decision = None

    thread = threading.Thread(target=_wait_on_guardian, args=(engine,), daemon=True)
    thread.start()
    time.sleep(0.2)

    engine.respond_guardian("use_fix")
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert engine._wait_result == "use_fix"


def test_respond_guardian_reject_wakes_and_records():
    engine = _new_engine()
    engine._guardian_event.clear()
    engine._guardian_decision = None

    thread = threading.Thread(target=_wait_on_guardian, args=(engine,), daemon=True)
    thread.start()
    time.sleep(0.2)

    engine.respond_guardian("reject")
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert engine._wait_result == "reject"


def test_respond_guardian_invalid_decision_coerced_to_reject():
    """An unknown decision must fail closed to 'reject', never approve."""
    engine = _new_engine()
    engine._guardian_event.clear()
    engine._guardian_decision = None

    thread = threading.Thread(target=_wait_on_guardian, args=(engine,), daemon=True)
    thread.start()
    time.sleep(0.2)

    engine.respond_guardian("maybe")
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert engine._wait_result == "reject"
