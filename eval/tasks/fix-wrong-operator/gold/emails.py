"""Email validation helpers."""


def is_valid_email(email):
    """Return True when ``email`` looks like user@host.tld."""
    if "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or "." not in domain:
        return False
    return True
