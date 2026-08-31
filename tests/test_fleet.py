"""Tests for tera_pilot.fleet (multi-agent fleet + main-terminal status)."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tera_pilot.fleet as fleet


def _wait_for(cond, timeout=10.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


class FleetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name
        import tera_pilot.fleet as f
        self.f = f

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    # ------------------------------------------------------------- mailbox
    def test_mailbox_roundtrip(self):
        r = self.f.submit_task("default", "video", "make a teaser")
        self.assertTrue(r["ok"], r)
        self.assertTrue(Path(r["mailbox"]).exists())

        r2 = self.f.submit_task("default", "video", "second task")
        self.assertTrue(r2["ok"], r2)

        first = self.f._take_task("default", "video")
        self.assertIsNotNone(first)
        self.assertEqual(first["prompt"], "make a teaser")
        second = self.f._take_task("default", "video")
        self.assertEqual(second["prompt"], "second task")
        self.assertIsNone(self.f._take_task("default", "video"))

    def test_submit_task_validates(self):
        self.assertFalse(self.f.submit_task("default", "../evil", "x")["ok"])
        self.assertFalse(self.f.submit_task("default", "video", "  ")["ok"])

    # ------------------------------------------------------------- worker
    def test_worker_runs_queued_task_with_injected_runner(self):
        calls = []

        def fake_runner(prompt, task_id):
            calls.append((prompt, task_id))
            return {"success": True, "output": "done: " + prompt}

        spec = fleet.FleetAgentSpec(
            profile="video", workspace=self._tmp.name, agent_id="video"
        )
        w = fleet.FleetWorker(
            "default", spec, runner=fake_runner, poll_interval=0.02
        )
        w.start()
        try:
            r = self.f.submit_task("default", "video", "hello video agent")
            self.assertTrue(r["ok"], r)
            ok = _wait_for(lambda: w.status["tasks_done"] == 1)
            self.assertTrue(ok, f"status: {w.status}")
            self.assertEqual(calls, [("hello video agent", r["task_id"])])
            self.assertEqual(w.status["state"], fleet.STATE_IDLE)

            # Status file is on disk and reflects the run.
            data = self.f.read_status("default", "video")
            self.assertEqual(data["tasks_done"], 1)
            self.assertTrue(data["recent_activity"])
        finally:
            w.stop()
            w.join(timeout=5)
            self.assertFalse(w.running)

    def test_worker_records_failure(self):
        def bad_runner(prompt, task_id):
            return {"success": False, "error": "boom"}

        spec = fleet.FleetAgentSpec(
            profile="code", workspace=self._tmp.name, agent_id="code"
        )
        w = fleet.FleetWorker("default", spec, runner=bad_runner, poll_interval=0.02)
        w.start()
        try:
            self.f.submit_task("default", "code", "will fail")
            ok = _wait_for(lambda: w.status["tasks_failed"] == 1)
            self.assertTrue(ok, f"status: {w.status}")
            self.assertEqual(w.status["state"], fleet.STATE_IDLE)
        finally:
            w.stop()
            w.join(timeout=5)
            self.assertFalse(w.running)

    def test_worker_stops_on_stop_file(self):
        spec = fleet.FleetAgentSpec(
            profile="code", workspace=self._tmp.name, agent_id="code"
        )
        w = fleet.FleetWorker("default", spec, poll_interval=0.02)
        w.start()
        try:
            self.assertTrue(_wait_for(lambda: w.running))
            self.f.fleet_dir("default").mkdir(parents=True, exist_ok=True)
            self.f.stop_path("default").write_text("stopped\n")
            ok = _wait_for(lambda: not w.running, timeout=5)
            self.assertTrue(ok)
            self.assertEqual(w.status["state"], fleet.STATE_STOPPED)
        finally:
            w.stop()
            w.join(timeout=5)

    # --------------------------------------------------------- supervisor
    def test_fleet_supervisor_lifecycle(self):
        ft = fleet.Fleet("fleet-test")
        r = ft.add_agent(fleet.FleetAgentSpec(profile="code", workspace=self._tmp.name))
        self.assertTrue(r["ok"], r)
        # duplicate agent id rejected (explicit agent_id collides)
        self.assertFalse(
            ft.add_agent(
                fleet.FleetAgentSpec(
                    profile="video", workspace=self._tmp.name, agent_id="code"
                )
            )["ok"]
        )
        # bad workspace rejected
        self.assertFalse(
            ft.add_agent(
                fleet.FleetAgentSpec(profile="video", workspace="/nonexistent-xyz")
            )["ok"]
        )
        r = ft.start()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["agents"], 1)
        # fleet.json metadata written
        meta_path = fleet.fleet_dir("fleet-test") / "fleet.json"
        self.assertTrue(meta_path.exists())
        with open(meta_path) as fh:
            self.assertEqual(json.load(fh)["fleet_id"], "fleet-test")
        ft.stop()
        ft.wait(timeout=5)
        # stop file present
        self.assertTrue(fleet.stop_path("fleet-test").exists())

    def test_list_fleets(self):
        ft = fleet.Fleet("fl-a")
        ft.add_agent(fleet.FleetAgentSpec(profile="code", workspace=self._tmp.name))
        ft.start()
        try:
            # The worker writes its status file asynchronously — wait for it.
            self.assertTrue(
                _wait_for(
                    lambda: any(
                        f["fleet_id"] == "fl-a" and len(f.get("agents", [])) == 1
                        for f in fleet.list_fleets()
                    ),
                    timeout=5,
                )
            )
            fleets = fleet.list_fleets()
            ids = [f["fleet_id"] for f in fleets]
            self.assertIn("fl-a", ids)
            entry = next(f for f in fleets if f["fleet_id"] == "fl-a")
            self.assertEqual(len(entry["agents"]), 1)
            self.assertEqual(entry["agents"][0]["agent_id"], "code")
            self.assertTrue(entry["running"])
        finally:
            ft.stop()
            ft.wait(timeout=5)

    def test_collect_status_returns_agent_rows(self):
        spec = fleet.FleetAgentSpec(
            profile="video", workspace=self._tmp.name, agent_id="video"
        )
        w = fleet.FleetWorker("default", spec, poll_interval=0.02)
        w.start()
        try:
            ok = _wait_for(
                lambda: self.f.collect_fleet_status("default") != [], timeout=5
            )
            self.assertTrue(ok)
            rows = self.f.collect_fleet_status("default")
            self.assertEqual(rows[0]["agent_id"], "video")
            self.assertIn("profile", rows[0])
        finally:
            w.stop()
            w.join(timeout=5)
            self.assertFalse(w.running)


if __name__ == "__main__":
    unittest.main()
