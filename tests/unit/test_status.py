"""Read-only `status` of one job ledger: phase, Pod and volume, deadline, stored price quote and outcome.

It must be safe while the job runs: no lock, no file kept open, nothing created or changed.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import datetime
import io
import json
import tempfile
import time
import unittest

from podvigil.podrt.records import save
from tests.support import runner as run
from tests.support.jobs import fixture, ledger


def snapshot(folder):
    return {
        path.relative_to(folder).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in folder.rglob("*")
    }


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-status-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "ledger"
        self.job = fixture()
        self.job["quote"] = {
            "checked_utc": "2026-11-12T10:00:00+00:00",
            "cloud": "SECURE",
            "compute_usd_per_hour": 0.6,
        }
        self.job["preparation_deadline_epoch"] = time.time() + 1800
        ledger(self.folder, self.job)
        created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
        save(
            self.folder / "pod-receipt.json",
            {
                "id": "testpod",
                "job_id": self.job["job_id"],
                "kind": "pod",
                "established_by": "create_response",
                "recorded_utc": created.isoformat(),
            },
        )

    def test_running_job(self):
        save(self.folder / "training-released.json", {"utc": "now"})
        save(
            self.folder / "watchdog.json",
            {"pid": 4321, "heartbeat_epoch": time.time() - 3, "state": "armed"},
        )
        result = run.status(self.folder)
        self.assertEqual(result["phase"], "running")
        self.assertEqual(result["pod"], {"state": "exists", "id": "testpod"})
        self.assertEqual(result["volume"], {"state": "exists", "id": "testvolume"})
        self.assertEqual(result["price_quote"]["compute_usd_per_hour"], 0.6)
        self.assertAlmostEqual(result["estimated_compute_cost"]["usd"], 0.3, delta=0.01)
        self.assertGreater(result["seconds_to_deadline"], 500)
        self.assertAlmostEqual(
            datetime.datetime.fromisoformat(result["deadline_utc"]).timestamp(),
            self.job["deadline_epoch"],
            delta=0.001,
        )
        self.assertEqual(result["watchdog"]["state"], "armed")
        self.assertLess(result["watchdog"]["seconds_since_heartbeat"], 60)
        self.assertIsNone(result["training_outcome"])

    def test_starting_stopping_and_finished(self):
        self.assertEqual(run.status(self.folder)["phase"], "starting")
        save(self.folder / "stop-request.json", {"reason": "test"})
        self.assertEqual(run.status(self.folder)["phase"], "stopping")
        finished = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
        save(
            self.folder / "pod-deleted.json",
            {"id": "testpod", "verified_utc": finished.isoformat(), "evidence": {"verified": True}},
        )
        self.assertEqual(run.status(self.folder)["phase"], "recovering results")
        save(
            self.folder / "volume-deleted.json",
            {"id": "testvolume", "evidence": {"verified": True}},
        )
        save(
            self.folder / "summary.json",
            {"job_id": self.job["job_id"], "training_outcome": "success"},
        )
        result = run.status(self.folder)
        self.assertEqual((result["phase"], result["training_outcome"]), ("finished", "success"))
        self.assertEqual(
            (result["pod"]["state"], result["volume"]["state"]), ("deleted", "deleted")
        )
        self.assertIsNone(result["seconds_to_deadline"])
        # 20 minutes at 0.6 USD per hour.
        self.assertAlmostEqual(result["estimated_compute_cost"]["usd"], 0.2, delta=0.01)

    def test_failed_launch_shows_the_stored_reason(self):
        save(
            self.folder / "launch-failure.json",
            {
                "job_id": self.job["job_id"],
                "phase": "pod_create",
                "error_type": "ApiError",
                "http_status": 400,
                "message": "no longer any instances available",
                "utc": "2026-11-12T10:00:00+00:00",
                "cleanup_error": None,
            },
        )
        result = run.status(self.folder)
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(result["failure"]["message"], "no longer any instances available")
        self.assertEqual(result["failure"]["phase"], "pod_create")

    def test_reads_only(self):
        save(
            self.folder / "watchdog.json",
            {"pid": 1, "heartbeat_epoch": time.time(), "state": "armed"},
        )
        before = snapshot(self.folder)
        run.status(self.folder)
        output, people = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(people):
            self.assertEqual(run.run_cli(["status", str(self.folder)]), 0)
        self.assertEqual(snapshot(self.folder), before)
        self.assertEqual(json.loads(output.getvalue())["phase"], "starting")
        self.assertEqual(people.getvalue().splitlines()[-2:], run.state_lines(self.folder))

    def test_a_ledger_from_before_these_fields_still_reads(self):
        old = Path(self.temp.name) / "old"
        save(
            old / "job.json",
            {
                "job_id": "b" * 32,
                "config": self.job["config"],
                "prior_pod_ids": [],
                "prior_volume_ids": [],
            },
        )
        result = run.status(old)
        self.assertEqual(result["phase"], "preparing")
        self.assertIsNone(result["estimated_compute_cost"])
        self.assertIsNone(result["deadline_utc"])


if __name__ == "__main__":
    unittest.main()
