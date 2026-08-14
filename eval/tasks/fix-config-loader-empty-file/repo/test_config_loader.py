"""Tests for config_loader. The empty-file and missing-file cases fail
until the bug is fixed."""

import json

from config_loader import load_config


def test_loads_valid_config(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"mode": "fast"}))
    assert load_config(str(p)) == {"mode": "fast"}


def test_empty_file_returns_empty_dict(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("")
    assert load_config(str(p)) == {}


def test_missing_file_returns_empty_dict(tmp_path):
    assert load_config(str(tmp_path / "nope.json")) == {}
