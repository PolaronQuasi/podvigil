"""Resume/cleanup integration regressions. Dummy transports, actual OS locks."""

from email.utils import formatdate
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import threading
import time
import unittest
import uuid

from podvigil.controller.ownership import creation_receipt
from podvigil.podrt.api import Api, ApiError
from podvigil.podrt.records import exclusive_lock, read, save
from podvigil.podrt.teardown import delete_verified
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture, ledger
from tests.support.runner import patch_run


class EndIteration(BaseException):
    pass


class ResumeMonitoringTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="wondomon-controller-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.job = fixture()
        self.cloud = FakeApi(self.job)
        ledger(self.folder, self.job)
        save(
            self.folder / "pod-receipt.json",
            creation_receipt(self.cloud.pods["testpod"], self.job, "pod", "create_response"),
        )
        self.storage = Storage("EU-RO-1", FakeS3())

    def test_resume_replaces_monitor_that_already_read_active_handoff(self):
        run.handoff(self.folder, "dummy operator takeover", True)
        captured, revoked, stop = threading.Event(), threading.Event(), threading.Event()
        threads, errors = [], []
        real_active, real_save = run.active_handoff, run.save

        def delayed_handoff(folder, job):
            active = real_active(folder, job)
            if threading.current_thread().name == "draining-monitor":
                captured.set()
                if not revoked.wait(3):
                    raise AssertionError("Resume did not publish its new request")
            return active  # This is the stale True that caused the original race.

        def publish(path, value):
            real_save(path, value)
            if Path(path).name == "watchdog-resume-request.json":
                revoked.set()

        def cleanup(*args, **kwargs):
            stop.wait(4)  # Replacement acknowledged before reaching cleanup.
            raise EndIteration()

        def start(name):
            def worker():
                try:
                    with exclusive_lock(self.folder / "watchdog.lock", timeout=1):
                        run.watchdog_loop(self.folder, self.cloud, self.storage)
                except EndIteration:
                    pass
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=worker, name=name)
            threads.append(thread)
            thread.start()

        with (
            patch_run("active_handoff", side_effect=delayed_handoff),
            patch_run("save", side_effect=publish),
            patch_run("cleanup_pod", side_effect=cleanup),
            patch_run(
                "spawn_watchdog", side_effect=lambda folder, **_: start("replacement")
            ) as spawn,
        ):
            try:
                start("draining-monitor")
                self.assertTrue(captured.wait(3))
                result = run.resume_monitoring(self.folder, timeout=3)
                self.assertEqual(result["state"], "monitor_armed")
                self.assertFalse(result["cleanup_verified"])
                self.assertFalse(real_active(self.folder, self.job))
                self.assertEqual(spawn.call_count, 1)
                threads[0].join(1)
                self.assertFalse(threads[0].is_alive())
                self.assertTrue(threads[1].is_alive())
                request = read(self.folder / "watchdog-resume-request.json")
                ack = read(self.folder / "watchdog-resume-ack.json")
                self.assertEqual(request["nonce"], ack["nonce"])
                self.assertEqual(result["resume_nonce"], ack["nonce"])
                self.assertEqual((self.folder / "control.lock").read_bytes()[:1], b"!")
                self.assertEqual(
                    read(self.folder / "job.json")["deadline_epoch"], self.job["deadline_epoch"]
                )
            finally:
                stop.set()
                revoked.set()
                for thread in threads:
                    thread.join(3)
            self.assertFalse(errors, repr(errors))
            self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_healthy_existing_monitor_acknowledges_new_nonce_without_replacement(self):
        at_pause, request_written, stop = threading.Event(), threading.Event(), threading.Event()
        errors = []
        real_save = run.save
        save(
            self.folder / "watchdog-resume-ack.json",
            {"job_id": self.job["job_id"], "nonce": "0" * 32, "pid": 1},
        )

        def publish(path, value):
            real_save(path, value)
            if Path(path).name == "watchdog-resume-request.json":
                request_written.set()

        def pause(_):
            at_pause.set()
            if not request_written.wait(3):
                raise EndIteration()
            if stop.wait(0.02):
                raise EndIteration()

        def worker():
            try:
                with exclusive_lock(self.folder / "watchdog.lock"):
                    run.watchdog_loop(self.folder, self.cloud, self.storage, pause=pause)
            except EndIteration:
                pass
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        with patch_run("save", side_effect=publish), patch_run("spawn_watchdog") as spawn:
            try:
                thread.start()
                self.assertTrue(at_pause.wait(3))
                result = run.resume_monitoring(self.folder, timeout=3)
                self.assertEqual(result["state"], "existing_monitor_active")
                self.assertNotEqual(result["resume_nonce"], "0" * 32)
                self.assertTrue(thread.is_alive())
                self.assertFalse((self.folder / "stop-request.json").exists())
                self.assertFalse(any(method == "DELETE" for method, _ in self.cloud.calls))
                spawn.assert_not_called()
            finally:
                stop.set()
                request_written.set()
                thread.join(3)
        self.assertFalse(errors, repr(errors))
        self.assertFalse(thread.is_alive())

    def test_locked_unresponsive_monitor_and_old_ack_cannot_claim_resumed(self):
        ready, stop = threading.Event(), threading.Event()

        def holder():
            with exclusive_lock(self.folder / "watchdog.lock"):
                ready.set()
                stop.wait(3)

        save(
            self.folder / "watchdog-resume-ack.json",
            {"job_id": self.job["job_id"], "nonce": "0" * 32, "pid": 1},
        )
        thread = threading.Thread(target=holder)
        with patch_run("spawn_watchdog") as spawn:
            try:
                thread.start()
                self.assertTrue(ready.wait(2))
                with self.assertRaisesRegex(RuntimeError, "did not acknowledge"):
                    run.resume_monitoring(self.folder, timeout=0.2)
                spawn.assert_not_called()
            finally:
                stop.set()
                thread.join(2)
        self.assertFalse((self.folder / "summary.json").exists())

    def test_new_watchdog_that_exits_without_ack_does_not_claim_armed(self):
        with patch_run("spawn_watchdog") as spawn:
            with self.assertRaisesRegex(RuntimeError, "exited before acknowledging"):
                run.resume_monitoring(self.folder, timeout=1)
        spawn.assert_called_once()
        self.assertFalse((self.folder / "summary.json").exists())

    def paced_cleanup(self, *, corrupt=False, explicit=False, missing_receipt=False, expired=True):
        if expired:
            self.job.update(
                deadline_epoch=time.time() - 60, startup_deadline_epoch=time.time() - 100
            )
        save(self.folder / "job.json", self.job)
        save(
            self.folder / "pod-intent.json",
            {name: self.job[name] for name in ("deadline_epoch", "startup_deadline_epoch")},
        )
        if explicit:
            self.assertTrue(run.record_stop(self.folder, "dummy explicit cleanup"))
        if missing_receipt:
            (self.folder / "pod-receipt.json").unlink()
        state = self.folder / "pacing.json"
        key, origin = "dummy-" + uuid.uuid4().hex, "http://127.0.0.1:1"
        old = Api(key, origin).configure_pacing(state, max_wait=1, read_retries=1)
        if corrupt:
            state.write_text("corrupt dummy coordination state")
        else:
            remaining = 8 if missing_receipt else 2
            old._record_response(
                "GET",
                "/pods",
                200,
                {
                    "RateLimit": f'"hour";r={remaining};t=3600',
                    "RateLimit-Policy": '"hour";q=3600;w=3600',
                },
            )
            # Even with >2 units available, ordinary discovery must not be gated
            # by read smoothing after an explicit cleanup request.
            if missing_receipt:
                old._pacer.transaction(
                    lambda values, tick: values.update(next_read=tick + 3600), cleanup=True
                )
        api = Api(key, origin).configure_pacing(state, max_wait=1, read_retries=1)
        self.assertIsNone(api._clock_anchor)
        calls = []

        def transport(method, path, body=None):
            calls.append((method, path))
            self.assertTrue(api._response.pacing["cleanup"])
            try:
                result = self.cloud.request(method, path, body)
            except ApiError as exc:
                api._record_response(method, path, exc.status, {"Date": formatdate(usegmt=True)})
                raise
            api._record_response(
                method, path, 204 if method == "DELETE" else 200, {"Date": formatdate(usegmt=True)}
            )
            return result

        def once(*args, **kwargs):
            # Execute the real verification checks; bound attempts/sleep in this
            # focused no-network test when actual quota prevents confirmation.
            return delete_verified(*args, **kwargs, attempts=1, sleep=lambda _: None)

        def pause(_):
            raise EndIteration()

        with (
            patch.object(api, "_request_once", side_effect=transport),
            patch_run("delete_verified", side_effect=once),
            patch_run("recovery_handoff") as recovery,
        ):
            try:
                run.watchdog_loop(self.folder, api, self.storage, pause=pause)
            except EndIteration:
                pass
        self.assertIsNotNone(api._clock_anchor)
        return calls, recovery

    def test_expired_deadline_spends_two_reserved_units_on_sample_then_delete(self):
        calls, recovery = self.paced_cleanup()
        self.assertEqual(calls, [("GET", "/pods/testpod"), ("DELETE", "/pods/testpod")])
        self.assertFalse(self.cloud.pods)
        self.assertTrue((self.folder / "stop-request.json").exists())
        self.assertEqual(read(self.folder / "pod-cleanup-attempt.json")["outcome"], "accepted")
        # The quota allowed DELETE and blocked verification.
        self.assertFalse((self.folder / "pod-deleted.json").exists())
        self.assertFalse((self.folder / "summary.json").exists())
        recovery.assert_not_called()

    def test_explicit_stop_deletes_before_any_clock_bootstrap(self):
        calls, recovery = self.paced_cleanup(explicit=True, expired=False)
        self.assertEqual(calls, [("DELETE", "/pods/testpod"), ("GET", "/pods/testpod")])
        self.assertFalse(self.cloud.pods)
        self.assertFalse((self.folder / "pod-deleted.json").exists())
        recovery.assert_not_called()

    def test_expired_deadline_uses_cleanup_fallback_when_shared_pacing_is_corrupt(self):
        calls, recovery = self.paced_cleanup(corrupt=True)
        self.assertEqual(calls[:2], [("GET", "/pods/testpod"), ("DELETE", "/pods/testpod")])
        self.assertFalse(self.cloud.pods)
        self.assertTrue(read(self.folder / "pod-deleted.json")["evidence"]["verified"])
        recovery.assert_called_once()

    def test_explicit_stop_uses_cleanup_fallback_when_shared_pacing_is_corrupt(self):
        calls, recovery = self.paced_cleanup(corrupt=True, explicit=True, expired=False)
        self.assertEqual(calls[0], ("DELETE", "/pods/testpod"))
        self.assertFalse(self.cloud.pods)
        self.assertTrue(read(self.folder / "pod-deleted.json")["evidence"]["verified"])
        recovery.assert_called_once()

    def test_explicit_stop_discovers_unknown_identity_under_cleanup_priority(self):
        calls, recovery = self.paced_cleanup(explicit=True, missing_receipt=True, expired=False)
        self.assertTrue(calls[0][1].startswith("/pods?"))
        self.assertIn(("DELETE", "/pods/testpod"), calls)
        self.assertFalse(self.cloud.pods)
        recovery.assert_called_once()

    def test_future_deadline_sample_does_not_cancel_healthy_workload(self):
        calls, recovery = self.paced_cleanup(expired=False)
        self.assertEqual(calls, [("GET", "/pods/testpod")])
        self.assertIn("testpod", self.cloud.pods)
        self.assertFalse((self.folder / "stop-request.json").exists())
        self.assertFalse((self.folder / "pod-deleted.json").exists())
        recovery.assert_not_called()
