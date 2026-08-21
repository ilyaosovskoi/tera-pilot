"""Regression tests for RequestQueue stream serialization (v2.3.4-fix).

``wrap_provider``'s stream wrapper used to submit a generator-returning
callable through ``submit_sync``, which released the semaphore the
moment the generator OBJECT was created — so the queue's concurrency
cap never applied to the actual streaming. Two concurrent streams could
run simultaneously even with ``max_concurrency=1``.

``serialized_stream`` now holds the queue slot for the entire iteration.
"""

import threading
import time

from tera_pilot.request_queue import QueueConfig, RequestQueue, wrap_provider


def _tracked_provider():
    """A provider whose stream reports peak concurrency across iterations."""

    class Tracked:
        provider_id = "tracked"

        def __init__(self):
            self.lock = threading.Lock()
            self.current = 0
            self.peak = 0

        def generate(self, messages, model=None):
            return "generate"

        def stream(self, messages):
            with self.lock:
                self.current += 1
                self.peak = max(self.peak, self.current)
            try:
                yield "a"
                time.sleep(0.1)
                yield "b"
            finally:
                with self.lock:
                    self.current -= 1

    return Tracked()


def test_serialized_stream_holds_slot_for_whole_iteration():
    """Two streams under max_concurrency=1 must never overlap in time."""
    p = _tracked_provider()
    q = wrap_provider(p, QueueConfig(max_concurrency=1))

    results = []

    def consume():
        results.append("".join(p.stream([])))

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results == ["ab", "ab"]
    assert p.peak == 1, f"streams overlapped — peak concurrency was {p.peak}"


def test_stream_generator_creation_does_not_block():
    """Creating several stream generators before consuming any must not
    deadlock — the slot is acquired lazily on first next()."""
    q = RequestQueue(QueueConfig(max_concurrency=1), name="t")

    def gen():
        yield "x"

    g1 = q.serialized_stream(gen)
    g2 = q.serialized_stream(gen)
    # Both generators exist without blocking; consuming them works.
    assert list(g1) == ["x"]
    assert list(g2) == ["x"]


def test_stream_releases_slot_after_early_close():
    """Abandoning a stream early must release the slot (generator close)."""
    q = RequestQueue(QueueConfig(max_concurrency=1), name="t")

    def gen():
        yield "a"
        yield "b"
        yield "c"

    g = q.serialized_stream(gen)
    it = iter(g)
    assert next(it) == "a"
    it.close()  # consumer gives up early — slot must be released
    # The slot is free again: a second stream can complete.
    assert list(q.serialized_stream(gen)) == ["a", "b", "c"]
