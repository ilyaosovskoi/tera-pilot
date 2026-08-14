"""Loads a JSON config file. BUG: crashes on empty or missing files."""

import json
from pathlib import Path


def load_config(path):
    """Return the parsed config dict. Must return {} for empty/missing files."""
    return json.loads(Path(path).read_text())
