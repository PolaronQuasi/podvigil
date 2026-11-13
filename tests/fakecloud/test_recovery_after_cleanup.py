"""A second recovery after a verified cleanup must not invent retained storage.

Found in the first paid use (2026-11-12): after a "no instances" answer the controller deleted the volume and wrote
a verified summary; the watchdog then ran its own recovery, failed to list the deleted volume and wrote
`storage_retained`. The fake S3 hid it because a deleted volume still listed as empty; it now fails like the real one.
"""

from pathlib import Path
import copy
import tempfile
import unittest

from podvigil.storage import Storage
from podvigil.podrt.records import read
from tests.support import runner as run
from tests.support.runner import patch_run
from podvigil.podrt.api import ApiError
from tests.support.fakes import NO_CAPACITY_DETAIL, FakeApi, FakeS3, NoSuchBucket
from tests.support.jobs import fixture, ledger


class AliveMonitor:
    def poll(self):
        return None


def rejected_launch(test, api, storage):
    """Run launch until POST /pods is refused; return the one job ledger it created."""
    jobs = Path(test.temp.name) / "jobs"
    with (
        patch_run("Api", return_value=api),
        patch_run("Storage", return_value=storage),
        patch_run("load_key", return_value="dummy-test-key"),
        patch_run("spawn_watchdog", side_effect=lambda folder, **_: AliveMonitor()),
        patch_run("jobs_dir", return_value=jobs),
    ):
        with test.assertRaises(ApiError) as caught:
            run.launch(copy.deepcopy(fixture()["config"]))
    test.assertEqual(caught.exception.status, 400)
    folders = list(jobs.iterdir())
    test.assertEqual(len(folders), 1)
    return folders[0]


class RecoveryAfterCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-recovery-after-cleanup-")
        self.addCleanup(self.temp.cleanup)

    def linked_cloud(self, job=None):
        api = FakeApi(job)
        s3 = FakeS3(api=api)
        return api, s3, Storage("EU-RO-1", s3)

    def test_deleted_volume_listing_fails_like_the_real_provider(self):
        api, s3, storage = self.linked_cloud(fixture())
        s3.objects[("testvolume", "inputs/x")] = b"x"
        api.request("DELETE", "/network-volumes/testvolume")
        with self.assertRaises(NoSuchBucket):
            s3.list_objects_v2(Bucket="testvolume", Prefix="")
        with self.assertRaisesRegex(RuntimeError, "inventory failed"):
            list(storage._objects("testvolume"))

    def test_watchdog_after_controller_cleanup_reports_no_pod_and_keeps_the_summary(self):
        api, s3, storage = self.linked_cloud()
        api.pod_rejection = (400, NO_CAPACITY_DETAIL)
        folder = rejected_launch(self, api, storage)
        self.assertFalse(api.pods)
        self.assertFalse(api.volumes)
        summary = read(folder / "summary.json")
        deleted = read(folder / "volume-deleted.json")
        self.assertTrue(summary["volume_deletion_verified"])
        self.assertIn("testvolume", s3.deleted_buckets)
        # The detached watchdog sees the stop request afterwards and runs its own recovery.
        before = len(api.calls)
        run.watchdog(folder, api, storage, pause=lambda _: None)
        state = read(folder / "watchdog.json")
        self.assertEqual(state["state"], "no_pod_created", state)
        handoff = read(folder / "recovery-handoff.json", {})
        self.assertNotEqual(handoff.get("state"), "storage_retained", handoff)
        self.assertEqual(read(folder / "summary.json"), summary)
        self.assertEqual(read(folder / "volume-deleted.json"), deleted)
        # The watchdog only reads during recovery.
        self.assertFalse([call for call in api.calls[before:] if call[0] != "GET"])

    def test_controller_recovery_after_the_watchdog_finished_first(self):
        api, s3, storage = self.linked_cloud(fixture())
        job = fixture()
        folder = Path(self.temp.name) / "ledger"
        ledger(folder, job)
        for name in ("pod-dispatch.json", "pod-receipt.json"):
            (folder / name).unlink()
        api.pods.clear()
        run.save(
            folder / "pod-creation-state.json",
            {"job_id": job["job_id"], "state": "never_dispatched"},
        )
        first = run.recover(folder, api, storage)  # the watchdog, say
        second = run.recover(folder, api, storage)  # then the controller's own attempt
        self.assertEqual(first, second)
        self.assertFalse(api.volumes)

    def test_trained_job_second_recovery_keeps_the_verified_deletion_record(self):
        job = fixture()
        api, s3, storage = self.linked_cloud(job)
        folder = Path(self.temp.name) / "ledger"
        ledger(folder, job)
        s3.finished(job["job_id"])
        first = run.recover(folder, api, storage)
        deleted = read(folder / "volume-deleted.json")
        self.assertTrue(deleted["evidence"]["verified"])
        second = run.recover(folder, api, storage)
        self.assertEqual(read(folder / "volume-deleted.json"), deleted)
        self.assertEqual(second, first)

    def test_a_present_volume_is_never_skipped_because_of_an_old_record(self):
        job = fixture()
        api, s3, storage = self.linked_cloud(job)
        folder = Path(self.temp.name) / "ledger"
        ledger(folder, job)
        for name in ("pod-dispatch.json", "pod-receipt.json"):
            (folder / name).unlink()
        api.pods.clear()
        run.save(
            folder / "pod-creation-state.json",
            {"job_id": job["job_id"], "state": "never_dispatched"},
        )
        # A record that claims deletion while the provider still shows the volume is not trusted.
        run.save(
            folder / "volume-deleted.json", {"id": "testvolume", "evidence": {"verified": True}}
        )
        run.recover(folder, api, storage)
        self.assertIn(("DELETE", "/network-volumes/testvolume"), api.calls)
        self.assertFalse(api.volumes)


if __name__ == "__main__":
    unittest.main()
