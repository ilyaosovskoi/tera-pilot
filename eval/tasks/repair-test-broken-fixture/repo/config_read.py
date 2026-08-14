"""Config loading."""

import json
from pathlib import Path


def load_config(path):
    """Return the parsed JSON config dict."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
