"""Regression tests for independent-review lifecycle failures. No live cloud."""

from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import tempfile
import threading
import time
import unittest

from podvigil.controller.ownership import creation_receipt
from podvigil.podrt.api import Api
from podvigil.podrt.records import exclusive_lock, read, save
from podvigil.storage import Storage
from tests.support.fakes import FakeApi, FakeS3, NoAgentStorage
from tests.support.jobs import fixture, ledger
from tests.support.runner import (
    account_api,
    cleanup_pod,
    export_partial,
    launch,
    managed_create,
    record_stop,
    recover,
    watchdog,
)
from tests.support.runner import patch_run


class LifecycleReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-lifecycle-review-")
        self.folder = Path(self.temp.name)
        self.job = fixture()
        self.api = FakeApi(self.job)

    def tearDown(self):
        self.temp.cleanup()

    def test_status_disk_failure_does_not_prevent_delete(self):
        self.job["deadline_epoch"] = time.time() - 1
        ledger(self.folder, self.job, stale=True)
        original_save = save

        def broken_status(path, value):
            if Path(path).name in {
                "watchdog.json",
                "stop-request.json",
                "pod-deleted.json",
                "recovery-handoff.json",
            }:
                raise PermissionError("simulated persistent reader lock/disk failure")
            return original_save(path, value)

        with patch("podvigil.podrt.records.save", side_effect=broken_status):
            watchdog(self.folder, self.api, NoAgentStorage(), pause=lambda _: None)
        self.assertFalse(self.api.pods)
        self.assertIn(("DELETE", "/pods/testpod"), self.api.calls)

    def test_stop_fence_blocks_late_creation_even_without_status_file(self):
        save(self.folder / "controller-heartbeat.json", {"epoch": time.time()})
        self.assertTrue(record_stop(self.folder, "closing job"))
        (self.folder / "stop-request.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "fence"):
            managed_create(
                self.api, self.job, self.folder, "volume", {"name": self.job["volume_name"]}, {}
            )
        self.assertFalse(any(method == "POST" for method, _ in self.api.calls))

    def test_creation_and_fence_are_serialized(self):
        api = FakeApi()
        save(self.folder / "job.json", self.job)
        save(
            self.folder / "pod-creation-state.json",
            {"job_id": self.job["job_id"], "state": "never_dispatched"},
        )
        save(self.folder / "controller-heartbeat.json", {"epoch": time.time()})
        entered, release = threading.Event(), threading.Event()
        original = api.request

        def delayed(method, path, body=None):
            if method == "POST":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test timed out")
            return original(method, path, body)

        api.request = delayed
        failures = []

        def create():
            try:
                managed_create(
                    api,
                    self.job,
                    self.folder,
                    "volume",
                    {"name": self.job["volume_name"], "dataCenter": "EU-RO-1", "size": 10},
                    {"name": self.job["volume_name"]},
                )
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=create)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            # The decision to exit cannot acquire the fence while POST is in flight.
            self.assertFalse(record_stop(self.folder, "simulated controller loss"))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(failures)
        watchdog(self.folder, api, Storage("EU-RO-1", FakeS3()), pause=lambda _: None)
        self.assertFalse(api.volumes)
        self.assertEqual(read(self.folder / "watchdog.json")["state"], "no_pod_created")

    def test_preparation_deadline_stops_storage_with_fresh_heartbeat(self):
        self.job["preparation_deadline_epoch"] = time.time() - 1
        save(self.folder / "job.json", self.job)
        save(
            self.folder / "pod-creation-state.json",
            {"job_id": self.job["job_id"], "state": "never_dispatched"},
        )
        save(self.folder / "controller-heartbeat.json", {"epoch": time.time()})
        save(self.folder / "volume-intent.json", {"name": self.job["volume_name"]})
        save(self.folder / "volume-receipt.json", {"id": "testvolume"})
        self.api.pods.clear()
        watchdog(self.folder, self.api, Storage("EU-RO-1", FakeS3()), pause=lambda _: None)
        self.assertFalse(self.api.volumes)

    def test_recover_can_clean_a_volume_before_any_pod_existed(self):
        save(self.folder / "job.json", self.job)
        save(
            self.folder / "pod-creation-state.json",
            {"job_id": self.job["job_id"], "state": "never_dispatched"},
        )
        save(self.folder / "volume-intent.json", {"name": self.job["volume_name"]})
        save(self.folder / "volume-receipt.json", {"id": "testvolume"})
        self.api.pods.clear()
        result = recover(self.folder, self.api, Storage("EU-RO-1", FakeS3()))
        self.assertEqual(result["training_outcome"], "not_started")
        self.assertFalse(self.api.volumes)

    def test_controller_loss_hands_off_verified_backup_and_storage_cleanup(self):
        ledger(self.folder, self.job, stale=True)
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        watchdog(self.folder, self.api, Storage("EU-RO-1", s3), pause=lambda _: None)
        self.assertFalse(self.api.pods)
        self.assertFalse(self.api.volumes)
        self.assertEqual(read(self.folder / "recovery-handoff.json")["state"], "complete")

    def test_malformed_successful_get_is_never_absence(self):
        client = Api("dummy")
        for value in (None, {}, [], {"id": "different"}):
            with (
                self.subTest(value=value),
                patch.object(client, "request", return_value=value),
                self.assertRaises(RuntimeError),
            ):
                client.get_or_none("/pods/expected")

    def test_job_lock_excludes_second_writer(self):
        locked = self.folder / "recovery.lock"
        with exclusive_lock(locked):
            with self.assertRaises(TimeoutError):
                with exclusive_lock(locked, timeout=0.1):
                    self.fail("Second transaction acquired an already-held lock")

    def test_receipt_allows_cleanup_after_pod_and_volume_rename(self):
        ledger(self.folder, self.job)
        save(
            self.folder / "pod-receipt.json",
            creation_receipt(self.api.pods["testpod"], self.job, "pod", "create_response"),
        )
        save(
            self.folder / "volume-receipt.json",
            creation_receipt(self.api.volumes["testvolume"], self.job, "volume", "create_response"),
        )
        self.api.pods["testpod"].update(name="renamed", env={})
        self.api.volumes["testvolume"]["name"] = "renamed results"
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        result = recover(self.folder, self.api, Storage("EU-RO-1", s3))
        self.assertTrue(result["volume_deletion_verified"])
        self.assertFalse(self.api.pods)
        self.assertFalse(self.api.volumes)

    def test_prepared_but_not_dispatched_pod_does_not_leak_volume(self):
        ledger(self.folder, self.job, stale=True)
        (self.folder / "pod-dispatch.json").unlink()
        (self.folder / "pod-receipt.json").unlink()
        self.api.pods.clear()
        save(
            self.folder / "pod-creation-state.json",
            {"job_id": self.job["job_id"], "state": "never_dispatched"},
        )
        record_stop(self.folder, "cancelled before dispatch")
        watchdog(self.folder, self.api, Storage("EU-RO-1", FakeS3()), pause=lambda _: None)
        self.assertFalse(self.api.volumes)

    def test_dead_watchdog_before_pod_creation_is_cleaned_by_controller(self):
        api, s3 = FakeApi(), FakeS3()

        class DeadMonitor:
            def poll(self):
                return 1

        with (
            patch_run("Api", return_value=api),
            patch_run("load_key", return_value="dummy"),
            patch_run("Storage", return_value=Storage("EU-RO-1", s3)),
            patch_run("spawn_watchdog", return_value=DeadMonitor()),
            patch_run("jobs_dir", return_value=self.folder),
        ):
            with self.assertRaisesRegex(RuntimeError, "Watchdog exited"):
                launch(self.job["config"])
        self.assertFalse(api.pods)
        self.assertFalse(api.volumes)
        self.assertNotIn(("POST", "/pods"), api.calls)

    def test_changed_key_cannot_certify_wrong_account_absence(self):
        self.job["auth_key_fingerprint"] = hashlib.sha256(b"original-dummy-key").hexdigest()
        with (
            patch_run("load_key", return_value="different-dummy-key"),
            patch_run("Api") as constructor,
        ):
            with self.assertRaisesRegex(RuntimeError, "credential changed"):
                account_api(self.job)
            constructor.assert_not_called()

    def test_dead_watchdog_after_publication_leaves_recovery_to_controller(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                api, s3 = FakeApi(), FakeS3()
                storage = Storage("EU-RO-1", s3)
                api.storage = storage
                published = []

                def finish(start):
                    s3.finished(start["job_id"])
                    published.append(True)
                    s3.corrupt_download = corrupt

                s3.on_start = finish

                class DeadAfterPublication:
                    def poll(self):
                        return 1 if published else None

                folders = []
                with (
                    patch_run("Api", return_value=api),
                    patch_run("load_key", return_value="dummy"),
                    patch_run("Storage", return_value=storage),
                    patch_run("spawn_watchdog", return_value=DeadAfterPublication()),
                    patch_run("jobs_dir", return_value=self.folder / str(corrupt)),
                ):
                    with self.assertRaisesRegex(RuntimeError, "watchdog exited"):
                        launch(self.job["config"], folders)
                failure = json.loads((folders[0] / "launch-failure.json").read_text())
                self.assertFalse(api.pods)
                if corrupt:
                    self.assertIn("testvolume", api.volumes)
                    self.assertIsNotNone(failure["cleanup_error"])
                    self.assertFalse((folders[0] / "volume-deleted.json").exists())
                else:
                    self.assertFalse(api.volumes)
                    self.assertIsNone(failure["cleanup_error"])
                    summary = json.loads((folders[0] / "summary.json").read_text())
                    self.assertEqual(summary["training_outcome"], "success")
                    self.assertTrue(summary["volume_deletion_verified"])

    def test_explicit_cleanup_request_retries_after_transient_404(self):
        ledger(self.folder, self.job)
        save(
            self.folder / "pod-receipt.json",
            creation_receipt(self.api.pods["testpod"], self.job, "pod", "create_response"),
        )
        original = self.api.get_or_none
        seen = [False]

        def transient(path):
            if path == "/pods/testpod" and not seen[0]:
                seen[0] = True
                return None
            return original(path)

        self.api.get_or_none = transient
        cleanup_pod(self.api, self.job, self.folder)
        self.assertFalse(self.api.pods)
        self.assertIn(("DELETE", "/pods/testpod"), self.api.calls)

    def test_missing_backup_credentials_fail_before_any_cloud_client(self):
        with (
            patch(
                "podvigil.storage.local_secrets",
                return_value={"RUNPOD_S3_ACCESS_KEY": None, "RUNPOD_S3_SECRET_KEY": None},
            ),
            patch_run("Api") as api_constructor,
        ):
            with self.assertRaisesRegex(RuntimeError, "S3"):
                launch(self.job["config"])
            api_constructor.assert_not_called()

    def test_partial_export_retrieves_checkpoint_but_never_deletes_volume(self):
        ledger(self.folder, self.job)
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        del s3.objects[("testvolume", "jobs/" + self.job["job_id"] + "/artifact-manifest.json")]
        result = export_partial(self.folder, self.api, Storage("EU-RO-1", s3))
        self.assertFalse(result["complete"])
        self.assertTrue(result["storage_retained"])
        self.assertFalse(self.api.pods)
        self.assertIn("testvolume", self.api.volumes)
        mapping = Path(result["download_directory"]) / "objects.jsonl"
        records = [json.loads(line) for line in mapping.read_text().splitlines()]
        copied = next(
            r
            for r in records
            if r["key"].endswith("artifacts/model.bin") and r.get("status") == "saved"
        )
        self.assertTrue((Path(result["download_directory"]) / copied["local_path"]).exists())


if __name__ == "__main__":
    unittest.main()
