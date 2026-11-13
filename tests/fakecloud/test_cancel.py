"""cancel: the stop request goes to the job's own verified cleanup; cancel itself calls no API and deletes nothing.

The watchdog acts on the stop request as on a deadline: it deletes the Pod and checks its absence, then recovers
the results or keeps the volume. When no watchdog runs, cancel starts one, as resume-monitoring does. The cloud is
the in-process fake and the watchdog the real loop in a thread, as the golden-ledger test runs it.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import tempfile
import threading
import time
import unittest

from podvigil.controller import watchdog as watchdog_module
from podvigil.podrt.records import exclusive_lock, read, save
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3, volume_for
from tests.support.jobs import fixture, ledger
from tests.support.runner import patch_run
from unittest.mock import patch


def fast(_seconds):
    time.sleep(0.05)


class CancelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-cancel-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "ledger"
        self.job = fixture()
        self.threads = []
        # Keep the wait short because these Pods never answer a request to finish.
        # The response is covered in test_graceful_cancel.
        grace = patch.object(watchdog_module, "cancel_grace_seconds", return_value=0.5)
        grace.start()
        self.addCleanup(grace.stop)

    def cloud(self, pod=True):
        api = FakeApi(self.job) if pod else FakeApi()
        if not pod:
            api.volumes["testvolume"] = volume_for(self.job)
        s3 = FakeS3(api=api)
        storage = Storage("EU-RO-1", s3)
        api.storage = storage
        storage.put_json("testvolume", "managed-owner.json", {"job_id": self.job["job_id"]})
        self.api, self.storage = api, storage

    def running_job(self):
        ledger(self.folder, self.job)
        save(
            self.folder / "pod-receipt.json",
            {
                "id": "testpod",
                "job_id": self.job["job_id"],
                "kind": "pod",
                "established_by": "create_response",
                "fingerprint": {"id": "testpod"},
            },
        )
        save(
            self.folder / "volume-receipt.json",
            {
                "id": "testvolume",
                "job_id": self.job["job_id"],
                "kind": "volume",
                "established_by": "create_response",
                "fingerprint": {"id": "testvolume"},
            },
        )
        save(self.folder / "training-released.json", {"utc": "now"})
        self.cloud()

    def watchdog(self, *_args, **_options):
        thread = threading.Thread(
            target=run.watchdog,
            args=(self.folder, self.api, self.storage),
            kwargs={"pause": fast},
            daemon=True,
        )
        thread.start()
        self.threads.append(thread)
        end = time.monotonic() + 10
        while not (self.folder / "watchdog.json").exists() and time.monotonic() < end:
            time.sleep(0.02)
        return thread

    def cancel(self, **options):
        with patch_run("account_api", side_effect=AssertionError("cancel must not call the API")):
            result = run.cancel(self.folder, pause=fast, **options)
        for thread in self.threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        return result

    def test_a_running_watchdog_deletes_the_pod_and_keeps_the_unfinished_results(self):
        self.running_job()
        self.watchdog()
        with patch_run("spawn_watchdog", side_effect=AssertionError("a watchdog runs already")):
            result = self.cancel(wait=20)
        self.assertEqual(result["pod"]["state"], "deleted")
        self.assertTrue(run.deletion_verified(read(self.folder / "pod-deleted.json"), "testpod"))
        self.assertFalse(self.api.pods)
        # The workload stopped before publishing a manifest, so the volume is kept with a partial
        # export.
        self.assertEqual(
            (result["state"], result["volume"]["state"]), ("storage_retained", "exists")
        )
        self.assertEqual(read(self.folder / "recovery-handoff.json")["state"], "storage_retained")
        self.assertIn("testvolume", self.api.volumes)
        self.assertEqual(read(self.folder / "cancel-request.json")["job_id"], self.job["job_id"])
        with exclusive_lock(self.folder / "control.lock") as lock:
            self.assertEqual(lock.read(1), b"!")  # the creation fence is closed

    def test_published_results_are_recovered_and_nothing_remains(self):
        self.running_job()
        # The workload published its results, and the Pod still runs (it has not reported "done" yet).
        self.storage.client.finished(self.job["job_id"])
        self.storage.put_json(
            "testvolume",
            "jobs/" + self.job["job_id"] + "/agent.json",
            {
                "job_id": self.job["job_id"],
                "pod_id": "testpod",
                "phase": "training",
                "heartbeat_epoch": time.time(),
            },
        )
        self.watchdog()
        result = self.cancel(wait=20)
        self.assertEqual(
            (result["state"], result["pod"]["state"], result["volume"]["state"]),
            ("compute_stopped", "deleted", "deleted"),
        )
        self.assertEqual(read(self.folder / "summary.json")["training_outcome"], "success")

    def test_without_a_watchdog_cancel_starts_one(self):
        self.running_job()
        with patch_run("spawn_watchdog", side_effect=self.watchdog) as spawn:
            result = self.cancel(wait=20)
        self.assertEqual(spawn.call_args.kwargs, {"require_same_credentials": False})
        self.assertTrue(result["watchdog_started"])
        self.assertEqual(result["pod"]["state"], "deleted")
        self.assertFalse(self.api.pods)

    def test_before_any_pod_the_volume_is_deleted_and_nothing_remains(self):
        save(self.folder / "job.json", self.job)
        for kind in ("pod", "volume"):
            save(
                self.folder / (kind + "-creation-state.json"),
                {
                    "job_id": self.job["job_id"],
                    "state": "never_dispatched" if kind == "pod" else "dispatching",
                },
            )
        save(self.folder / "volume-intent.json", {"name": self.job["volume_name"]})
        save(self.folder / "volume-dispatch.json", {"state": "dispatching"})
        save(
            self.folder / "volume-receipt.json",
            {
                "id": "testvolume",
                "job_id": self.job["job_id"],
                "kind": "volume",
                "established_by": "create_response",
                "fingerprint": {"id": "testvolume"},
            },
        )
        self.cloud(pod=False)
        self.watchdog()
        result = self.cancel(wait=20)
        self.assertEqual(
            (result["state"], result["pod"]["state"], result["volume"]["state"]),
            ("compute_stopped", "not_created", "deleted"),
        )
        self.assertFalse(self.api.volumes)

    def test_a_finished_job_is_left_alone(self):
        self.running_job()
        save(
            self.folder / "summary.json",
            {"job_id": self.job["job_id"], "training_outcome": "success"},
        )
        result = self.cancel(wait=1)
        self.assertEqual(result["state"], "already_finished")
        self.assertFalse((self.folder / "stop-request.json").exists())
        self.assertFalse((self.folder / "cancel-request.json").exists())

    def test_before_any_pod_cancel_starts_missing_watchdog_to_recover_volume(self):
        save(self.folder / "job.json", self.job)
        save(
            self.folder / "pod-creation-state.json",
            {"job_id": self.job["job_id"], "state": "never_dispatched"},
        )
        save(self.folder / "volume-dispatch.json", {"state": "dispatching"})
        save(
            self.folder / "volume-receipt.json",
            {
                "id": "testvolume",
                "job_id": self.job["job_id"],
                "kind": "volume",
                "established_by": "create_response",
                "fingerprint": {"id": "testvolume"},
            },
        )
        self.cloud(pod=False)
        with patch_run("spawn_watchdog", side_effect=self.watchdog) as spawn:
            result = self.cancel(wait=20)
        spawn.assert_called_once_with(self.folder.resolve(), require_same_credentials=False)
        self.assertTrue(result["watchdog_started"])
        self.assertEqual(
            (result["state"], result["pod"]["state"], result["volume"]["state"]),
            ("compute_stopped", "not_created", "deleted"),
        )
        self.assertFalse(self.api.volumes)

    def test_cancel_does_not_repeat_preparation_recovery_that_kept_storage(self):
        save(self.folder / "job.json", self.job)
        save(self.folder / "volume-receipt.json", {"id": "testvolume"})
        save(self.folder / "recovery-handoff.json", {"state": "storage_retained"})
        with patch_run(
            "spawn_watchdog", side_effect=AssertionError("retention decision already recorded")
        ):
            result = self.cancel(wait=0)
        self.assertEqual(result["state"], "storage_retained")
        self.assertFalse(result["watchdog_started"])

    def test_a_handed_off_job_is_refused_and_left_unchanged(self):
        self.running_job()
        save(
            self.folder / "operator-handoff.json",
            {
                "job_id": self.job["job_id"],
                "active": True,
                "billing_unverified_acknowledged": True,
                "cleanup_verified": False,
            },
        )
        with self.assertRaisesRegex(RuntimeError, "resume-monitoring"):
            self.cancel(wait=1)
        self.assertFalse((self.folder / "stop-request.json").exists())

    def test_lines_of_a_guarded_running_job_point_to_cancel_not_to_recover(self):
        self.running_job()
        save(
            self.folder / "watchdog.json",
            {"pid": 1, "heartbeat_epoch": time.time(), "state": "armed"},
        )
        pod, volume = run.state_lines(self.folder)
        self.assertTrue(pod.startswith("Pod testpod: runs and bills; the watchdog guards it"), pod)
        self.assertIn("cancel " + str(self.folder), pod)
        self.assertNotIn(run.program() + " recover", pod + volume)  # recover would stop the job
        save(
            self.folder / "watchdog.json",
            {"pid": 1, "heartbeat_epoch": time.time() - 3600, "state": "armed"},
        )
        # The stale heartbeat triggers the usual cleanup warning.
        self.assertIn("NOT verified", run.state_lines(self.folder)[0])
        save(
            self.folder / "watchdog.json",
            {"pid": 1, "heartbeat_epoch": time.time(), "state": "armed"},
        )
        save(self.folder / "stop-request.json", {"reason": "test"})
        self.assertIn("NOT verified", run.state_lines(self.folder)[0])

    def test_a_wait_that_ends_while_the_watchdog_recovers_is_pending_not_storage_retained(self):
        # The Pod is gone and the watchdog is still recovering the results. The volume could be
        # deleted moments later, so cancel must wait to report storage_retained (exit code 8).
        self.running_job()
        save(self.folder / "pod-deleted.json", {"id": "testpod", "evidence": {"verified": True}})
        held, release = threading.Event(), threading.Event()

        def recovering_watchdog():  # holds the watchdog's lock, as the real one does while it recovers
            with exclusive_lock(self.folder / "watchdog.lock"):
                held.set()
                release.wait(10)

        thread = threading.Thread(target=recovering_watchdog, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        held.wait(5)
        with patch_run("account_api", side_effect=AssertionError("cancel must not call the API")):
            result = run.cancel(self.folder, wait=0, pause=fast)
            self.assertEqual(
                (result["state"], result["pod"]["state"], result["volume"]["state"]),
                ("recovering", "deleted", "exists"),
            )
            self.assertEqual(run.of_result("cancel", result), "pending")
            # The recovery ended and kept the volume: now, and only now, the volume is retained.
            save(self.folder / "recovery-handoff.json", {"state": "storage_retained"})
            self.assertEqual(
                run.cancel(self.folder, wait=0, pause=fast)["state"], "storage_retained"
            )
            (self.folder / "recovery-handoff.json").unlink()
            release.set()
            thread.join(5)
            # No watchdog runs any more and the volume is still there: retained.
            result = run.cancel(self.folder, wait=0, pause=fast)
        self.assertEqual(result["state"], "storage_retained")
        self.assertEqual(run.of_result("cancel", result), "storage_retained")
        self.assertIn("testvolume", self.api.volumes)  # cancel itself deleted nothing

    def test_the_command_line_waits_reports_and_exits_pending(self):
        self.running_job()
        held, release = threading.Event(), threading.Event()

        def busy_watchdog():  # holds the lock and does nothing: the stop is not acted on within the wait
            with exclusive_lock(self.folder / "watchdog.lock"):
                held.set()
                release.wait(10)

        thread = threading.Thread(target=busy_watchdog, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        held.wait(5)
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            patch_run("account_api", side_effect=AssertionError("cancel must not call the API")),
        ):
            code = run.run_cli(["cancel", str(self.folder), "--wait", "0"])
        self.assertEqual(code, run.EXIT_CANCEL_PENDING)
        text = out.getvalue()
        self.assertEqual(json.loads(text[: text.rindex("}") + 1])["state"], "pending")
        self.assertIn("testpod", self.api.pods)  # nothing was deleted by cancel itself
        self.assertEqual(read(self.folder / "stop-request.json")["reason"], run.CANCEL_REASON)


if __name__ == "__main__":
    unittest.main()
