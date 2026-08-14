"""User lookup for an internal tool."""

import sqlite3


def find_user(conn, username):
    """Return the user row for ``username``."""
    query = f"SELECT * FROM users WHERE username = '{username}'"
    return conn.execute(query).fetchone()
