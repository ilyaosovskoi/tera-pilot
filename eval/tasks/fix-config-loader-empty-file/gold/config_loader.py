"""Loads a JSON config file. Empty or missing files return {}."""

import json
from pathlib import Path


def load_config(path):
    """Return the parsed config dict. Returns {} for empty/missing files."""
    try:
        content = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not content.strip():
        return {}
    return json.loads(content)
