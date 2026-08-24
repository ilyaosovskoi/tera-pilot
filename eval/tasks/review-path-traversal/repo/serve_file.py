"""Serves static files from a base directory."""

from pathlib import Path

BASE_DIR = Path("/srv/files")


def serve_file(filename):
    """Return the contents of *filename* under BASE_DIR."""
    full_path = BASE_DIR / filename
    return full_path.read_bytes()
