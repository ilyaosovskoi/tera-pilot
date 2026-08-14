"""User registry helpers."""


def get_user_name(user):
    """Return the user's display name, or 'Anonymous' when the user
    is missing or has no name."""
    if not user:
        return "Anonymous"
    return user.get("name") or "Anonymous"
