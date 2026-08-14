"""User registry helpers."""


def get_user_name(user):
    """Return the user's display name, or 'Anonymous' when the user
    is missing or has no name.

    BUG: a None user (or a missing 'name' key) raises instead of
    falling back to 'Anonymous'.
    """
    return user["name"]
