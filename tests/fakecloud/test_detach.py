"""launch --detach: the controller returns once the workload is released; the watchdog alone enforces the same
deadline and recovers the results (analysis section 3.3). The watchdog never reads the controller's heartbeat.

The cloud is the in-process fake and the watchdog the real loop in a thread, which keeps running after launch
returns, as the detached watchdog process does.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import copy
import io
import json
import tempfile
import threading
import time
import unittest

from podvigil.podrt.records import read, save
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture
from tests.support.runner import patch_run

KEY = "dummy-detach-account-key"


class Cloud:
    """A linked fake API and S3; the watchdog runs as the real loop in a thread."""

    def __init__(self, root):
        self.api = FakeApi()
        self.s3 = FakeS3(api=self.api)
        self.storage = Storage("EU-RO-1", self.s3)
        self.api.storage = self.storage
        self.jobs = Path(root) / "jobs"
        self.thread = None
        self.clock = 0.0  # seconds the provider's clock is ahead of this machine's
        self.api.now = lambda: time.time() + self.clock

    def spawn(self, folder, **_):
        self.thread = threading.Thread(
            target=run.watchdog,
            args=(folder, self.api, self.storage),
            kwargs={"pause": lambda _: time.sleep(0.05)},
            daemon=True,
        )
        self.thread.start()
        thread = self.thread

        class Monitor:
            pid = 4242

            def poll(self):
                return None if thread.is_alive() else 0

        return Monitor()

    def patches(self):
        return [
            patch_run("Api", return_value=self.api),
            patch_run("Storage", return_value=self.storage),
            patch_run("load_key", return_value=KEY),
            patch_run("spawn_watchdog", side_effect=self.spawn),
            patch_run("jobs_dir", return_value=self.jobs),
        ]

    def folder(self):
        return next(self.jobs.iterdir()).resolve()

    def finish(self, outcome="success"):
        """The workload publishes its results and the Pod guard deletes its own Pod."""
        job_id = self.folder().name
        self.s3.finished(job_id, outcome)
        self.storage.put_json(
            "testvolume",
            "jobs/" + job_id + "/control/self-delete.json",
            {
                "job_id": job_id,
                "pod_id": "testpod",
                "delete_accepted": True,
                "delete_http_status": 204,
            },
        )
        self.api.pods.clear()


class DetachTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-detach-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cloud = Cloud(self.root)
        self.cfg = copy.deepcopy(fixture()["config"])
        patches = self.cloud.patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def join(self):
        self.cloud.thread.join(20)
        self.assertFalse(self.cloud.thread.is_alive(), "the watchdog did not finish")

    def test_launch_returns_after_release_and_the_watchdog_finishes_alone(self):
        with redirect_stdout(io.StringIO()):
            result = run.launch(self.cfg, detach=True)
        folder = self.cloud.folder()
        self.assertEqual(result["outcome"], "detached")
        self.assertEqual(result["ledger"], str(folder))
        self.assertTrue((folder / "training-released.json").exists())
        self.assertFalse((folder / "pod-deleted.json").exists())
        self.assertIn("testpod", self.cloud.api.pods)  # the workload runs on
        detached = read(folder / "detached.json")
        job = read(folder / "job.json")
        self.assertEqual(detached["deadline_epoch"], job["deadline_epoch"])
        self.assertEqual(read(folder / "pod-intent.json")["deadline_epoch"], job["deadline_epoch"])
        self.assertTrue(self.cloud.thread.is_alive())
        self.cloud.finish()
        self.join()
        summary = read(folder / "summary.json")
        self.assertEqual(summary["training_outcome"], "success")
        self.assertTrue(summary["pod_termination_verified"] and summary["volume_deletion_verified"])
        self.assertEqual((self.cloud.api.pods, self.cloud.api.volumes), ({}, {}))
        self.assertEqual(
            run.state_lines(folder)[-1],
            "Volume testvolume: deleted; its absence was verified. Nothing from this job remains.",
        )

    def test_the_watchdog_alone_enforces_the_same_deadline(self):
        with redirect_stdout(io.StringIO()):
            run.launch(self.cfg, detach=True)
        folder = self.cloud.folder()
        deadline = read(folder / "job.json")["deadline_epoch"]
        self.cloud.clock = deadline - time.time() + 1  # the provider's clock passes the deadline
        self.join()
        self.assertEqual(
            read(folder / "watchdog.json"),
            {
                "pid": read(folder / "watchdog.json")["pid"],
                "state": "pod_deleted",
                "reason": "absolute runtime limit",
            },
        )
        self.assertTrue(run.deletion_verified(read(folder / "pod-deleted.json"), "testpod"))
        self.assertFalse(self.cloud.api.pods)
        self.assertEqual(read(folder / "job.json")["deadline_epoch"], deadline)

    def test_the_watchdog_must_be_alive_armed_and_recent(self):
        folder = self.root / "ledger"
        save(folder / "watchdog.json", {"pid": 1, "heartbeat_epoch": time.time(), "state": "armed"})
        cfg = {"poll_seconds": 10}

        class Alive:
            def poll(self):
                return None

        class Dead:
            def poll(self):
                return 0

        self.assertIsNone(run.watchdog_guards(folder, Alive(), cfg))
        self.assertIn("not running", run.watchdog_guards(folder, Dead(), cfg))
        self.assertIn("not running", run.watchdog_guards(folder, None, cfg))
        save(
            folder / "watchdog.json",
            {"pid": 1, "heartbeat_epoch": time.time() - 60, "state": "armed"},
        )
        self.assertIn("recently", run.watchdog_guards(folder, Alive(), cfg))
        save(
            folder / "watchdog.json",
            {
                "pid": 1,
                "heartbeat_epoch": time.time(),
                "state": "observation_or_cleanup_unverified",
            },
        )
        self.assertIn("armed", run.watchdog_guards(folder, Alive(), cfg))
        save(folder / "watchdog.json", {"pid": 1, "heartbeat_epoch": time.time(), "state": "armed"})
        save(folder / "stop-request.json", {"reason": "test"})
        self.assertIn("stop", run.watchdog_guards(folder, Alive(), cfg))

    def test_detach_refuses_the_self_termination_probe_before_anything_exists(self):
        self.cfg["self_termination_probe"] = True
        with self.assertRaisesRegex(ValueError, "self_termination_probe"):
            run.launch(self.cfg, detach=True)
        self.assertFalse(self.cloud.jobs.exists())
        self.assertFalse([call for call in self.cloud.api.calls if call[0] != "GET"])

    def test_the_command_line_returns_the_detached_code_and_says_who_guards_the_pod(self):
        job_file = self.root / "job.json"
        job_file.write_text(json.dumps(self.cfg), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run.run_cli(["launch", "--detach", str(job_file)])
        self.assertEqual(code, run.EXIT_DETACHED)
        result = json.loads(out.getvalue())  # standard output holds the result JSON alone
        self.assertEqual((result["outcome"], result["exit_code"]), ("detached", run.EXIT_DETACHED))
        lines = err.getvalue().strip().splitlines()
        self.assertTrue(
            lines[-2].startswith("Pod testpod: runs and bills; the watchdog guards it"), lines
        )
        self.assertIn("cancel", lines[-2])
        self.cloud.finish()
        self.join()


if __name__ == "__main__":
    unittest.main()
