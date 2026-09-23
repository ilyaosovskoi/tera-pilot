"""Tiny signup service (no validation yet)."""


def signup(username, password):
    """Register a user. Returns a user dict. No validation performed."""
    return {"username": username, "password_hash": f"hashed:{password}"}


def login(username, password):
    """Check credentials against the (empty) store."""
    return False
