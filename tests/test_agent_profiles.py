"""Tests for tera_pilot.agent_profiles (Agent Profile registry)."""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tera_pilot.agent_profiles import (
    ACTIVE_PROFILE_CONFIG_KEY,
    PRESET_PROFILES,
    SECURITY_MAP,
    VALID_SECURITY,
    AgentProfileManager,
    get_active_profile_id,
    set_active_profile_id,
)


class AgentProfilesTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name
        from tera_pilot import agent_profiles as ap

        self._ap = ap
        # Reset singleton so it re-reads under the temp HOME
        self._old_manager = ap._MANAGER
        ap._MANAGER = None

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._ap._MANAGER = self._old_manager
        self._tmp.cleanup()

    def test_list_profiles_contains_builtin_presets(self):
        mgr = AgentProfileManager()
        profiles = mgr.list_profiles()
        ids = [p["id"] for p in profiles]
        self.assertIn("code", ids)
        self.assertIn("video", ids)
        self.assertIn("reviewer", ids)
        for p in profiles:
            self.assertTrue(p.get("builtin"))

    def test_apply_profile_sets_fragment_and_security(self):
        from tera_pilot.agent_profiles import apply_profile_to_runtime

        class FakeTools:
            def __init__(self):
                self.autonomy = None
                self._guardian_config = None
                self._confirm_callback = None

        class FakeRuntime:
            def __init__(self):
                self.fragment = None
                self.tools = FakeTools()

            def set_system_prompt_fragment(self, frag):
                self.fragment = frag

            def set_autonomy(self, level):
                self.tools.autonomy = level

        rt = FakeRuntime()
        profile = dict(PRESET_PROFILES[1])  # video → controlled
        apply_profile_to_runtime(rt, profile)
        self.assertEqual(rt.fragment, profile["system_prompt"])
        self.assertEqual(rt.tools.autonomy, "always_ask")
        self.assertIsNotNone(rt.tools._guardian_config)
        self.assertEqual(rt.tools._guardian_config.level, "dangerous_only")

        # Clearing: empty prompt → fragment None
        rt2 = FakeRuntime()
        apply_profile_to_runtime(rt2, {"security": "free", "system_prompt": ""})
        self.assertIsNone(rt2.fragment)
        self.assertEqual(rt2.tools.autonomy, "new_files_only")

    def test_upsert_creates_user_profile(self):
        mgr = AgentProfileManager()
        r = mgr.upsert_profile(
            "my-video",
            name="My Video Agent",
            description="custom",
            system_prompt="You are a video agent.",
            security="controlled",
            section="general",
        )
        self.assertTrue(r["ok"], r)
        got = mgr.get_profile("my-video")
        self.assertIsNotNone(got)
        self.assertEqual(got["name"], "My Video Agent")
        self.assertEqual(got["security"], "controlled")
        self.assertFalse(got["builtin"])

    def test_upsert_rejects_bad_id(self):
        mgr = AgentProfileManager()
        r = mgr.upsert_profile("../evil")
        self.assertFalse(r["ok"])

    def test_upsert_rejects_oversized_prompt(self):
        mgr = AgentProfileManager()
        r = mgr.upsert_profile("big", system_prompt="x" * 9000)
        self.assertFalse(r["ok"])

    def test_delete_builtin_refused(self):
        mgr = AgentProfileManager()
        r = mgr.delete_profile("code")
        self.assertFalse(r["ok"])

    def test_delete_user_profile(self):
        mgr = AgentProfileManager()
        mgr.upsert_profile("temp1", name="t")
        r = mgr.delete_profile("temp1")
        self.assertTrue(r["ok"])
        self.assertIsNone(mgr.get_profile("temp1"))

    def test_security_map_covers_all_levels(self):
        for s in VALID_SECURITY:
            autonomy, guardian = SECURITY_MAP[s]
            self.assertIn(autonomy, ("always_ask", "new_files_only", "never_ask"))
            self.assertIn(guardian, ("off", "dangerous_only", "all"))

    def test_active_profile_roundtrip(self):
        r = set_active_profile_id("video")
        self.assertTrue(r["ok"], r)
        self.assertEqual(get_active_profile_id(), "video")
        r = set_active_profile_id("")
        self.assertTrue(r["ok"])
        self.assertEqual(get_active_profile_id(), "")

    def test_corrupt_profile_file_skipped(self):
        mgr = AgentProfileManager()
        mgr.upsert_profile("broken", name="b")
        path = mgr._profile_path("broken")
        path.write_text("{not json", encoding="utf-8")
        # Must not raise; broken profile just disappears from the list
        profiles = mgr.list_profiles()
        self.assertNotIn("broken", [p["id"] for p in profiles])


if __name__ == "__main__":
    unittest.main()
