# Code Review: tree.py

## Findings

1. **Unbounded recursion (high)** — `fibonacci` has no base case, so any
   call recurses forever until Python raises `RecursionError`. Add base
   cases for `n == 0` and `n == 1` (and consider the exponential cost of
   naive recursion for large `n`).
