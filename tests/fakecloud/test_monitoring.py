"""Real Windows launch isolation and non-destructive monitoring regressions."""

from pathlib import Path
from unittest.mock import patch
import tempfile
import time
import unittest

from podvigil.controller.ownership import creation_receipt
from podvigil.podrt.api import ApiError
from podvigil.podrt.records import read, save
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3, NoAgentStorage
from tests.support.jobs import fixture, ledger
from tests.support.runner import patch_run


class EndSimulation(BaseException):
    pass


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-controller-")
        self.folder = Path(self.temp.name)
        self.job = fixture()
        self.api = FakeApi(self.job)
        ledger(self.folder, self.job)

    def tearDown(self):
        self.temp.cleanup()

    def limited_watch(self, storage, count=3):
        polls = [0]

        def pause(_):
            polls[0] += 1
            if polls[0] >= count:
                raise EndSimulation()

        with self.assertRaises(EndSimulation):
            run.watchdog(self.folder, self.api, storage, pause=pause)
        self.assertIn("testpod", self.api.pods)
        self.assertFalse(any(method == "DELETE" for method, _ in self.api.calls))

    def test_unreadable_heartbeat_does_not_delete_healthy_pod(self):
        original = run.read

        def broken(path, default=None):
            if Path(path).name == "controller-heartbeat.json":
                raise PermissionError("simulated antivirus reader contention")
            return original(path, default)

        with patch_run("read", side_effect=broken):
            self.limited_watch(NoAgentStorage())

    def test_controller_loss_alone_does_not_cancel_training(self):
        save(self.folder / "controller-heartbeat.json", {"epoch": 0})
        self.limited_watch(NoAgentStorage())

    def test_monitoring_404_does_not_issue_delete(self):
        self.api.get_or_none = lambda path: (
            None if path.startswith("/pods/") else self.api.request("GET", path)
        )
        self.limited_watch(NoAgentStorage())

    def test_s3_outage_does_not_count_as_frozen_supervisor(self):
        class Outage:
            def get_json(self, *args):
                raise OSError("S3 unavailable")

        self.limited_watch(Outage())
        observed = run.AgentObservation(10)
        observed.observe({"heartbeat_epoch": 1}, 0)
        observed.unknown()
        observed.observe({"heartbeat_epoch": 1}, 10000)
        self.assertEqual(observed.stale_seconds, 0)
        observed.observe({"heartbeat_epoch": 1}, 10010)
        self.assertEqual(observed.stale_seconds, 10)
        observed.observe({"heartbeat_epoch": 1}, 20000)
        self.assertEqual(observed.stale_seconds, 0)

    def test_provisioning_nullable_fields_can_be_polled(self):
        pod = self.api.pods["testpod"]
        pod.update(
            status="PROVISIONING",
            dataCenterId=None,
            cost=None,
            gpu={"id": self.job["config"]["gpu_id"]},
        )
        run.assert_pod(pod, self.job)
        with self.assertRaises(RuntimeError):
            run.assert_pod(pod, self.job, ready=True)

    def test_missing_dispatch_files_do_not_authorize_volume_delete(self):
        (self.folder / "pod-dispatch.json").unlink()
        (self.folder / "pod-receipt.json").unlink()
        self.api.pods.clear()
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        with self.assertRaisesRegex(RuntimeError, "positive proof"):
            run.recover(self.folder, self.api, Storage("EU-RO-1", s3))
        self.assertIn("testvolume", self.api.volumes)

    def test_external_checkpoint_retains_volume_after_compute_cleanup(self):
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        s3.objects[("testvolume", "inputs/checkpoints/latest.pt")] = b"unique checkpoint"
        with self.assertRaises((ValueError, RuntimeError)):
            run.recover(self.folder, self.api, Storage("EU-RO-1", s3))
        self.assertFalse(self.api.pods)
        self.assertIn("testvolume", self.api.volumes)
        self.assertFalse((self.folder / "volume-deleted.json").exists())

    def test_heartbeat_retries_after_failed_write(self):
        class Stop:
            count = 0

            def is_set(self):
                return self.count >= 2

            def wait(self, _):
                self.count += 1

        with patch(
            "podvigil.podrt.records.save", side_effect=[PermissionError("locked"), None]
        ) as writer:
            run.heartbeat_worker(self.folder, Stop())
        self.assertEqual(writer.call_count, 2)

    def test_probe_does_not_mask_denied_self_delete_with_local_cleanup(self):
        self.job["config"]["self_termination_probe"] = True
        ledger(self.folder, self.job)
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        self.limited_watch(Storage("EU-RO-1", s3))

    def test_probe_requires_accepted_self_delete_evidence(self):
        self.job["config"]["self_termination_probe"] = True
        ledger(self.folder, self.job)
        self.api.pods.clear()
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        store = Storage("EU-RO-1", s3)

        def halt(_):
            raise EndSimulation()

        with self.assertRaises(EndSimulation):
            run.watchdog(self.folder, self.api, store, pause=halt)
        self.assertFalse((self.folder / "pod-deleted.json").exists())
        self.assertFalse((self.folder / "self-termination-probe.json").exists())
        store.put_json(
            "testvolume",
            "jobs/" + self.job["job_id"] + "/control/self-delete.json",
            {
                "job_id": self.job["job_id"],
                "pod_id": "testpod",
                "delete_accepted": True,
                "delete_http_status": 204,
            },
        )
        run.watchdog(self.folder, self.api, store, pause=lambda _: None)
        self.assertTrue(read(self.folder / "self-termination-probe.json")["passed"])
        self.assertNotIn(("DELETE", "/pods/testpod"), self.api.calls)

    def test_explicit_early_cancel_issues_delete_despite_lagging_get(self):
        save(
            self.folder / "pod-receipt.json",
            creation_receipt(self.api.pods["testpod"], self.job, "pod", "create_response"),
        )
        original = self.api.get_or_none
        self.api.get_or_none = lambda path: None if path.startswith("/pods/") else original(path)
        run.cleanup_pod(self.api, self.job, self.folder)
        self.assertIn(("DELETE", "/pods/testpod"), self.api.calls)
        self.assertFalse(self.api.pods)

    def test_early_delete_404_is_not_absence_proof(self):
        save(
            self.folder / "pod-receipt.json",
            creation_receipt(self.api.pods["testpod"], self.job, "pod", "create_response"),
        )
        original = self.api.request

        def lag(method, path, body=None):
            if method == "DELETE":
                raise ApiError(404, method, path)
            return original(method, path, body)

        self.api.request = lag
        with self.assertRaisesRegex(RuntimeError, "not accepted"):
            run.cleanup_pod(self.api, self.job, self.folder)
        self.assertFalse((self.folder / "pod-deleted.json").exists())
        self.assertIn("testpod", self.api.pods)

    def test_malformed_rejection_is_not_never_dispatched_proof(self):
        (self.folder / "pod-dispatch.json").unlink()
        (self.folder / "pod-receipt.json").unlink()
        save(self.folder / "pod-rejected.json", {"anything": True})
        self.api.pods.clear()
        with self.assertRaisesRegex(RuntimeError, "positive proof"):
            run.recover(self.folder, self.api, Storage("EU-RO-1", FakeS3()))
        self.assertIn("testvolume", self.api.volumes)

    def test_beacon_cannot_authorize_deleting_unrelated_pod(self):
        store = Storage("EU-RO-1", FakeS3())
        store.put_json(
            "testvolume",
            "jobs/" + self.job["job_id"] + "/control/identity.json",
            {"job_id": self.job["job_id"], "pod_id": "testpod"},
        )
        self.api.pods["testpod"]["name"] = "unrelated"
        with self.assertRaises(PermissionError):
            run.reconcile_beacon(self.api, self.job, self.folder, store)
        self.assertFalse(any(method == "DELETE" for method, _ in self.api.calls))

    def test_lost_response_self_deletion_receipt_grants_no_future_delete_authority(self):
        self.job["deadline_epoch"] = time.time() - 1
        owned = self.api.pods.pop("testpod")
        store = Storage("EU-RO-1", FakeS3())
        prefix = "jobs/" + self.job["job_id"] + "/control/"
        store.put_json(
            "testvolume",
            prefix + "identity.json",
            {"job_id": self.job["job_id"], "pod_id": "testpod"},
        )
        store.put_json(
            "testvolume",
            prefix + "self-delete.json",
            {
                "job_id": self.job["job_id"],
                "pod_id": "testpod",
                "delete_accepted": True,
                "delete_http_status": 204,
            },
        )
        self.assertEqual(run.reconcile_beacon(self.api, self.job, self.folder, store), "testpod")
        receipt = read(self.folder / "pod-receipt.json")
        self.assertFalse(receipt["deletion_authority"])
        self.api.pods["testpod"] = owned
        with self.assertRaises((PermissionError, RuntimeError)):
            run.cleanup_pod(self.api, self.job, self.folder)
        self.assertFalse(any(method == "DELETE" for method, _ in self.api.calls))


if __name__ == "__main__":
    unittest.main()
