# Code Review: counter.py

## Findings

1. **Race condition (high)** — `record_hit` does `_hits += 1`, which is a
   read-modify-write that is not atomic. Two threads can read the same
   value and both write it back, losing increments under concurrency.
   Guard the increment with a `threading.Lock` (or use an atomic
   counter like `itertools.count` with a lock).
