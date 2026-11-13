"""Graceful cancel (F2): a cancelled running workload gets to stop and publish before its Pod goes.

On a cancel of a released job the watchdog first writes jobs/<id>/cancel.json and waits finalize_grace_seconds
plus a margin, never past the deadline, for the Pod to stop the workload as at its own time limit, publish with
outcome "cancelled" and delete itself. Then it continues exactly as before: it deletes the Pod if it still exists,
checks its absence and recovers. Here a thread answers for the Pod as the Pod code does; the Pod code itself is
tested in tests/unit/test_pod_volume.py and in the container lane.
"""

from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
import io
import json
import tempfile
import threading
import time
import unittest

from podvigil.controller import watchdog as watchdog_module
from podvigil.events import read_events
from podvigil.podrt.records import read, save
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture, ledger
from tests.support.runner import patch_run


def fast(_seconds):
    time.sleep(0.05)


class Running:
    """A released job whose Pod runs, the watchdog loop in a thread, and a stand-in for the Pod's answer."""

    def __init__(self, test, released=True):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-graceful-")
        test.addCleanup(temporary.cleanup)
        self.test = test
        self.folder = Path(temporary.name) / "ledger"
        job = fixture()
        job["startup_deadline_epoch"] = time.time() - 60
        self.job = job
        ledger(self.folder, job)
        for kind, rid in (("pod", "testpod"), ("volume", "testvolume")):
            save(
                self.folder / (kind + "-receipt.json"),
                {
                    "id": rid,
                    "job_id": job["job_id"],
                    "kind": kind,
                    "established_by": "create_response",
                    "fingerprint": {"id": rid},
                },
            )
        if released:
            save(self.folder / "training-released.json", {"utc": "2026-11-12T22:00:00+00:00"})
        self.api = FakeApi(job)
        self.s3 = FakeS3(api=self.api)
        self.storage = Storage("EU-RO-1", self.s3)
        self.api.storage = self.storage
        self.prefix = "jobs/" + job["job_id"] + "/"
        self.storage.put_json("testvolume", "managed-owner.json", {"job_id": job["job_id"]})
        self.storage.put_json(
            "testvolume", self.prefix + "start.json", {"job_id": job["job_id"], "pod_id": "testpod"}
        )
        self.storage.put_json(
            "testvolume",
            self.prefix + "agent.json",
            {
                "job_id": job["job_id"],
                "pod_id": "testpod",
                "actor": "pod_supervisor",
                "phase": "training",
                "heartbeat_epoch": time.time(),
            },
        )
        self.threads = []

    def deletes(self):
        return [call for call in self.api.calls if call == ("DELETE", "/pods/testpod")]

    def watchdog(self):
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

    def pod_answers(self, outcome="cancelled"):
        """As the Pod code does: on cancel.json for this job and Pod, stop, publish, then delete its own Pod."""

        def answer():
            end = time.monotonic() + 20
            while time.monotonic() < end:
                request = self.s3.objects.get(("testvolume", self.prefix + "cancel.json"))
                if request and json.loads(request).get("pod_id") == "testpod":
                    self.s3.finished(self.job["job_id"], outcome)
                    self.storage.put_json(
                        "testvolume",
                        self.prefix + "control/self-delete.json",
                        {
                            "job_id": self.job["job_id"],
                            "pod_id": "testpod",
                            "delete_accepted": True,
                            "delete_http_status": 204,
                        },
                    )
                    self.api.pods.clear()
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        self.threads.append(thread)

    def cancel(self, wait=30):
        with patch_run("account_api", side_effect=AssertionError("cancel must not call the API")):
            result = run.cancel(self.folder, wait=wait, pause=fast)
        for thread in self.threads:
            thread.join(20)
            self.test.assertFalse(thread.is_alive())
        return result


class GracefulCancelTests(unittest.TestCase):
    def test_a_running_pod_is_asked_to_finish_and_its_outputs_are_kept(self):
        job = Running(self)
        job.watchdog()
        job.pod_answers()
        result = job.cancel()
        self.assertEqual(
            (result["state"], result["training_outcome"]), ("compute_stopped", "cancelled")
        )
        # The Pod deleted itself. The watchdog only checked its absence.
        self.assertEqual(job.deletes(), [])
        self.assertEqual((job.api.pods, job.api.volumes), ({}, {}))
        self.assertEqual(
            read(job.folder / "downloads" / "artifacts" / "result.json")["outcome"], "cancelled"
        )
        self.assertEqual(
            (job.folder / "downloads" / "artifacts" / "model.bin").read_bytes(), b"example weights"
        )
        grace = read(job.folder / "cancel-grace.json")
        self.assertTrue(grace["asked"])
        self.assertLessEqual(grace["until_epoch"], job.job["deadline_epoch"])
        names = [item["event"] for item in read_events(job.folder, 0)]
        self.assertLess(names.index("asked_pod_to_finish"), names.index("pod_finished"))
        self.assertEqual(run.of_result("cancel", result), "success")

    def test_a_pod_that_does_not_finish_in_time_is_deleted_as_before(self):
        job = Running(self)
        job.storage.put_json(
            "testvolume",
            job.prefix + "started-once.json",
            {"started_utc": "2026-11-12T22:00:01+00:00"},
        )
        with patch.object(watchdog_module, "cancel_grace_seconds", return_value=1):
            job.watchdog()
            result = job.cancel()
        # The watchdog deleted the Pod after the grace period.
        self.assertEqual(job.deletes(), [("DELETE", "/pods/testpod")])
        self.assertFalse(job.api.pods)
        # With no published outputs, the outputs are lost and the volume is deleted based on
        # its evidence (F1).
        self.assertEqual(
            (result["state"], result["training_outcome"]), ("compute_stopped", "outputs_lost")
        )
        self.assertEqual(job.s3.objects, {})

    def test_the_deadline_still_ends_the_grace(self):
        job = Running(self)

        def pass_the_deadline():  # once the Pod was asked, the provider's clock passes the deadline
            end = time.monotonic() + 20
            while not (job.folder / "cancel-grace.json").exists() and time.monotonic() < end:
                time.sleep(0.05)
            job.api.now = lambda: time.time() + 10_000

        clock = threading.Thread(target=pass_the_deadline, daemon=True)
        clock.start()
        job.threads.append(clock)
        job.watchdog()
        job.cancel()
        self.assertTrue(read(job.folder / "cancel-grace.json")["asked"])
        self.assertEqual(read(job.folder / "watchdog.json")["reason"], "absolute runtime limit")
        self.assertEqual(job.deletes(), [("DELETE", "/pods/testpod")])

    def test_a_restarted_watchdog_does_not_extend_the_grace(self):
        job = Running(self)
        save(
            job.folder / "cancel-grace.json",
            {
                "job_id": job.job["job_id"],
                "pod_id": "testpod",
                "until_epoch": time.time() - 1,
                "asked": True,
            },
        )
        started = time.monotonic()
        job.watchdog()
        job.cancel()
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(job.deletes(), [("DELETE", "/pods/testpod")])
        # The watchdog did not ask the Pod again.
        self.assertNotIn(("testvolume", job.prefix + "cancel.json"), job.s3.objects)

    def test_lines_while_the_pod_finishes_say_so_and_do_not_point_to_recover(self):
        job = Running(self)
        save(job.folder / "stop-request.json", {"reason": run.CANCEL_REASON})
        save(
            job.folder / "watchdog.json",
            {"pid": 1, "heartbeat_epoch": time.time(), "state": "armed"},
        )
        grace = {
            "job_id": job.job["job_id"],
            "pod_id": "testpod",
            "asked": True,
            "utc": "2026-11-12T22:00:00+00:00",
        }
        save(job.folder / "cancel-grace.json", {**grace, "until_epoch": time.time() + 120})
        pod, volume = run.state_lines(job.folder)
        self.assertIn("was asked to finish and publish its outputs", pod)
        self.assertIn("NOT verified yet", pod)
        # Recovering now would delete the Pod before it publishes.
        self.assertNotIn("recover ", pod + volume)
        self.assertIn("Follow: " + run.program() + " status " + str(job.folder), volume)
        save(job.folder / "cancel-grace.json", {**grace, "until_epoch": time.time() - 1})
        self.assertIn(
            "NOT verified. Run: " + run.program() + " recover ", run.state_lines(job.folder)[0]
        )

    def test_lines_right_after_the_cancel_do_not_point_to_recover_either(self):
        # Before the watchdog next checks for cancellation, cancel-grace.json is still missing.
        # Advising recovery then would delete the Pod before it is asked to publish.
        job = Running(self)
        save(
            job.folder / "watchdog.json",
            {"pid": 1, "heartbeat_epoch": time.time(), "state": "armed"},
        )
        run.request_cancel(job.folder, job.job, "cancel")
        self.assertFalse((job.folder / "cancel-grace.json").exists())
        pod, volume = run.state_lines(job.folder)
        self.assertIn("a cancel was requested; the watchdog is about to ask it to finish", pod)
        self.assertIn("NOT verified yet", pod)
        self.assertNotIn("recover ", pod + volume)
        self.assertIn("Follow: " + run.program() + " status " + str(job.folder), pod)
        self.assertIn("Follow: " + run.program() + " status " + str(job.folder), volume)
        # No live watchdog: nobody will ask the Pod, so the usual advice stands.
        save(
            job.folder / "watchdog.json",
            {"pid": 1, "heartbeat_epoch": time.time() - 3600, "state": "armed"},
        )
        self.assertIn(
            "NOT verified. Run: " + run.program() + " recover ", run.state_lines(job.folder)[0]
        )
        # A workload that was never released is deleted at once: the usual advice stands there too.
        save(
            job.folder / "watchdog.json",
            {"pid": 1, "heartbeat_epoch": time.time(), "state": "armed"},
        )
        (job.folder / "training-released.json").unlink()
        self.assertIn(
            "NOT verified. Run: " + run.program() + " recover ", run.state_lines(job.folder)[0]
        )

    def test_before_the_release_the_pod_is_deleted_at_once_and_not_asked(self):
        job = Running(self, released=False)
        job.watchdog()
        job.cancel()
        self.assertEqual(job.deletes(), [("DELETE", "/pods/testpod")])
        self.assertFalse((job.folder / "cancel-grace.json").exists())


class InterruptedLaunchTests(unittest.TestCase):
    """Ctrl+C in launch once the workload runs is a cancel, handled by the live watchdog."""

    def test_ctrl_c_after_the_release_asks_the_pod_to_finish(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-interrupted-")
        self.addCleanup(temporary.cleanup)
        api = FakeApi()
        s3 = FakeS3(api=api)
        storage = Storage("EU-RO-1", s3)
        api.storage = storage
        jobs = Path(temporary.name) / "jobs"
        threads = []

        def answer(job_id):
            prefix = "jobs/" + job_id + "/"
            end = time.monotonic() + 20
            while time.monotonic() < end:
                if ("testvolume", prefix + "cancel.json") in s3.objects:
                    s3.finished(job_id, "cancelled")
                    storage.put_json(
                        "testvolume",
                        prefix + "control/self-delete.json",
                        {
                            "job_id": job_id,
                            "pod_id": "testpod",
                            "delete_accepted": True,
                            "delete_http_status": 204,
                        },
                    )
                    api.pods.clear()
                    return
                time.sleep(0.05)

        def spawn(folder, **_):
            thread = threading.Thread(
                target=run.watchdog,
                args=(folder, api, storage),
                kwargs={"pause": fast},
                daemon=True,
            )
            thread.start()
            threads.append(thread)

            class Monitor:
                pid = 4242
                calls = [0]

                def poll(self):
                    # The supervising controller is interrupted (Ctrl+C) once the workload runs.
                    if (Path(folder) / "training-released.json").exists() and not self.calls[0]:
                        self.calls[0] = 1
                        raise KeyboardInterrupt()
                    return None if thread.is_alive() else 0

            return Monitor()

        def on_start(start):
            pod = threading.Thread(target=answer, args=(start["job_id"],), daemon=True)
            pod.start()
            threads.append(pod)

        s3.on_start = on_start
        with (
            patch_run("Api", return_value=api),
            patch_run("Storage", return_value=storage),
            patch_run("load_key", return_value="dummy-interrupted-key"),
            patch_run("spawn_watchdog", side_effect=spawn),
            patch_run("jobs_dir", return_value=jobs),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run.launch(fixture()["config"])
        for thread in threads:
            thread.join(30)
        folder = next(jobs.iterdir())
        self.assertEqual(read(folder / "cancel-request.json")["source"], "interrupted launch")
        self.assertTrue(read(folder / "launch-failure.json")["handed_to_watchdog"])
        self.assertNotIn(("DELETE", "/pods/testpod"), api.calls)  # the controller deleted nothing
        self.assertEqual(read(folder / "summary.json")["training_outcome"], "cancelled")
        self.assertEqual((api.pods, api.volumes), ({}, {}))

    def test_a_cancel_from_another_terminal_ends_a_foreground_launch_as_cancelled(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-foreground-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        job_file = root / "job.json"
        job_file.write_text(json.dumps(fixture()["config"]), encoding="utf-8")
        api = FakeApi()
        s3 = FakeS3(api=api)
        storage = Storage("EU-RO-1", s3)
        api.storage = storage
        jobs = root / "jobs"
        threads, cancelled = [], []

        def pod(job_id):  # as the Pod code does on cancel.json
            prefix = "jobs/" + job_id + "/"
            end = time.monotonic() + 20
            while time.monotonic() < end:
                if ("testvolume", prefix + "cancel.json") in s3.objects:
                    s3.finished(job_id, "cancelled")
                    storage.put_json(
                        "testvolume",
                        prefix + "control/self-delete.json",
                        {
                            "job_id": job_id,
                            "pod_id": "testpod",
                            "delete_accepted": True,
                            "delete_http_status": 204,
                        },
                    )
                    api.pods.clear()
                    return
                time.sleep(0.05)

        def other_terminal():
            folder = next(jobs.iterdir())
            end = time.monotonic() + 20
            while not (folder / "training-released.json").exists() and time.monotonic() < end:
                time.sleep(0.05)
            cancelled.append(run.cancel(folder, wait=30, pause=fast))

        def spawn(folder, **_):
            thread = threading.Thread(
                target=run.watchdog,
                args=(folder, api, storage),
                kwargs={"pause": fast},
                daemon=True,
            )
            thread.start()
            threads.append(thread)

            class Monitor:
                pid = 4242

                def poll(self):
                    return None if thread.is_alive() else 0

            return Monitor()

        def on_start(start):
            for target, args in ((pod, (start["job_id"],)), (other_terminal, ())):
                thread = threading.Thread(target=target, args=args, daemon=True)
                thread.start()
                threads.append(thread)

        s3.on_start = on_start
        out = io.StringIO()
        with (
            patch_run("Api", return_value=api),
            patch_run("Storage", return_value=storage),
            patch_run("load_key", return_value="dummy-foreground-key"),
            patch_run("spawn_watchdog", side_effect=spawn),
            patch_run("jobs_dir", return_value=jobs),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            code = run.run_cli(["launch", str(job_file)])
        for thread in threads:
            thread.join(30)
        result = json.loads(out.getvalue())
        self.assertEqual(
            (code, result["outcome"], result["training_outcome"]),
            (run.EXIT_CODES["cancelled"], "cancelled", "cancelled"),
        )
        self.assertEqual(cancelled[0]["state"], "compute_stopped")
        # The Pod finished and deleted itself.
        self.assertNotIn(("DELETE", "/pods/testpod"), api.calls)
        self.assertEqual((api.pods, api.volumes), ({}, {}))


if __name__ == "__main__":
    unittest.main()
