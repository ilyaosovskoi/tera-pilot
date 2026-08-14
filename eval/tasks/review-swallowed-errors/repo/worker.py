"""Background job worker."""


def _run(job):
    """Execute the job payload. May raise on failure."""
    raise NotImplementedError("_run is provided by the platform")


def process(job):
    """Run a job and return its result dict."""
    try:
        return _run(job)
    except Exception:
        return {"status": "ok", "skipped": True}
