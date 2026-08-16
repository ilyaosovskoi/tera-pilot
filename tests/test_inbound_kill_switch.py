"""Regression test for the inbound STOP kill-switch (G21 §21a).

The old ``make_daemon_stop_callback`` read ``t.get("status")`` /
``t.get("task_id")`` from ``TaskQueue.list_tasks()`` — but
``TaskRecord.to_dict()`` emits ``state`` / ``id``, so the running-task
check never matched and the kill switch cancelled NOTHING.
"""

from tera_pilot.daemon import TaskQueue, TaskState
from tera_pilot.inbound_listener import InboundMessage, make_daemon_stop_callback


def _msg(text="STOP"):
    return InboundMessage(
        backend="telegram", chat_id="1", sender_id="2",
        sender_name="bob", text=text,
    )


def test_stop_cancels_running_task():
    q = TaskQueue()
    task = q.submit("do something", workspace=".")
    task.state = TaskState.RUNNING

    cancelled = []
    q.cancel_task = lambda tid: cancelled.append(tid)  # type: ignore[method-assign]

    make_daemon_stop_callback(q)(_msg())
    assert cancelled == [task.id], f"kill switch cancelled wrong task: {cancelled!r}"


def test_stop_with_no_running_task_is_noop():
    q = TaskQueue()
    q.submit("pending task", workspace=".")  # PENDING, not RUNNING

    cancelled = []
    q.cancel_task = lambda tid: cancelled.append(tid)  # type: ignore[method-assign]

    make_daemon_stop_callback(q)(_msg())
    assert cancelled == []
