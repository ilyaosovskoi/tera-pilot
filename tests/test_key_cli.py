"""Tests for tera_pilot.key_cli (API-key setup convenience)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tera_pilot.key_cli as kc


class KeyCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def _config_path(self) -> Path:
        return Path(self._tmp.name) / ".tera_pilot" / "config.json"

    def test_mask_never_leaks_middle(self):
        self.assertEqual(kc._mask("sk-1234567890abcdef"), "sk-1…cdef")
        self.assertEqual(kc._mask("short"), "••••")

    def test_set_saves_key_and_activates(self):
        code = kc.cmd_set("groq", "gsk_test1234567890")
        self.assertEqual(code, 0)
        cfg = json.loads(self._config_path().read_text())
        self.assertEqual(cfg["active_provider"], "groq")
        self.assertEqual(cfg["providers"]["groq"]["api_key"], "gsk_test1234567890")
        # model default filled
        self.assertTrue(cfg["providers"]["groq"].get("model"))

    def test_set_unknown_provider_rejected(self):
        code = kc.cmd_set("not-a-real-provider", "key")
        self.assertEqual(code, 1)
        self.assertFalse(self._config_path().exists())

    def test_remove_deletes_key(self):
        kc.cmd_set("gemini", "AIza-fake")
        code = kc.cmd_remove("gemini")
        self.assertEqual(code, 0)
        cfg = json.loads(self._config_path().read_text())
        self.assertEqual(cfg["providers"]["gemini"].get("api_key"), "")

    def test_list_status_masked(self):
        kc.cmd_set("openai", "sk-openai-1234567890")
        status = kc._config_status()
        entry = next(s for s in status if s["provider"] == "openai")
        self.assertTrue(entry["set"])
        self.assertNotIn("sk-openai-1234567890", entry["masked"])
        self.assertTrue(entry["active"])

    def test_run_key_cli_dispatch(self):
        self.assertEqual(kc.run_key_cli(["set", "deepseek", "ds-key"]), 0)
        self.assertEqual(kc.run_key_cli(["list"]), 0)
        self.assertEqual(kc.run_key_cli(["bogus"]), 1)


if __name__ == "__main__":
    unittest.main()
