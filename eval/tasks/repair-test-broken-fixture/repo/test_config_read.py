"""Tests for config_read.load_config.

BUG: the fixture file is never created, so the test fails.
"""

from config_read import load_config


def test_loads_config(tmp_path):
    p = tmp_path / "config.json"
    assert load_config(str(p)) == {"mode": "fast"}
