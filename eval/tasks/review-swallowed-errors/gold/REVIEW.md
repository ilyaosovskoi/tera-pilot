# Code Review: worker.py

## Findings

1. **Swallowed errors (high)** — `process` catches every exception with a
   bare `except Exception` and reports the job as "ok". Failures are
   silently hidden, which makes debugging impossible and lets broken
   jobs look successful. Log the error (with traceback) and report a
   failure status instead.
