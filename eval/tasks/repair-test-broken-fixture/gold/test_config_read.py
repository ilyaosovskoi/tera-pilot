"""Tests for config_read.load_config."""

import json

from config_read import load_config


def test_loads_config(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"mode": "fast"}))
    assert load_config(str(p)) == {"mode": "fast"}
