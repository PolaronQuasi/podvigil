"""A Pod whose guard stops before the workload is released must not leave its volume billed.

Found in the first paid use (2026-11-12): the Pod guard stopped at start-up, so no artifact manifest could
exist, and recovery kept the volume for ever ("No valid complete artifact manifest"). Recovery now deletes such
a volume, but only with positive evidence: this controller never released the workload, the guard's own status
record is on the volume, and the audit finds nothing but pristine inputs and the guard's pre-release records.
"""

from pathlib import Path
import json
import tempfile
import unittest

from podvigil.storage import Storage
from podvigil.podrt.records import save
from tests.support.runner import recover
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture, ledger


def guard_volume(job, actor="pod_guard", **extra):
    """A volume as a guard leaves it when it stops before the release, plus optional extra job objects."""
    s3, prefix = FakeS3(), "jobs/" + job["job_id"] + "/"
    put = lambda key, value: s3.objects.__setitem__(("testvolume", key), json.dumps(value).encode())
    put("managed-owner.json", {"job_id": job["job_id"]})
    put(
        prefix + "agent.json",
        {"job_id": job["job_id"], "pod_id": "testpod", "actor": actor, "phase": "done"},
    )
    put(prefix + "control/self-delete.json", {"state": "delete_accepted"})
    put(
        prefix + "control/guard-failure.json", {"error_type": "RuntimeError", "message": "start-up"}
    )
    for key, value in extra.items():
        put(prefix + key, value)
    return s3


class UnreleasedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-tests-")
        self.folder = Path(self.temp.name)
        self.job = fixture()
        self.api = FakeApi(self.job)
        ledger(self.folder, self.job)

    def tearDown(self):
        self.temp.cleanup()

    def retained(self, s3):
        with self.assertRaisesRegex(RuntimeError, "manifest"):
            recover(self.folder, self.api, Storage("EU-RO-1", s3))
        self.assertFalse(self.api.pods)  # compute is always cleaned up
        self.assertIn("testvolume", self.api.volumes)  # the volume is kept
        self.assertFalse((self.folder / "volume-deleted.json").exists())

    def test_guard_stopped_before_release_deletes_the_volume(self):
        summary = recover(self.folder, self.api, Storage("EU-RO-1", guard_volume(self.job)))
        self.assertEqual(summary["training_outcome"], "not_started")
        self.assertTrue(summary["pod_created"] and summary["volume_deletion_verified"])
        self.assertFalse(self.api.pods)
        self.assertNotIn("testvolume", self.api.volumes)
        self.assertTrue((self.folder / "volume-deleted.json").exists())
        audit = json.loads((self.folder / "volume-audit.json").read_text())
        self.assertEqual(audit["mode"], "unreleased_pod")
        kept = [
            r for r in audit["objects"] if r["kind"] == "control" and "guard-failure" in r["key"]
        ]
        # The guard's reason is saved locally.
        self.assertTrue(kept and Path(kept[0]["local_path"]).is_file())

    def test_empty_volume_without_a_guard_record_is_kept(self):
        self.retained(FakeS3())

    def test_released_workload_is_never_treated_as_unreleased(self):
        save(self.folder / "training-released.json", {"utc": "now"})
        self.retained(guard_volume(self.job))

    def test_start_marker_on_the_volume_keeps_it(self):
        self.retained(
            guard_volume(
                self.job, **{"start.json": {"job_id": self.job["job_id"], "pod_id": "testpod"}}
            )
        )

    def test_started_once_marker_keeps_it(self):
        self.retained(guard_volume(self.job, **{"started-once.json": {"started_utc": "now"}}))

    def test_an_artifact_without_a_manifest_keeps_it(self):
        self.retained(guard_volume(self.job, **{"artifacts/model.bin": {"weights": [1, 2, 3]}}))

    def test_supervisor_status_is_not_guard_evidence(self):
        self.retained(guard_volume(self.job, actor="pod_supervisor"))

    def test_unknown_object_outside_the_job_keeps_it(self):
        s3 = guard_volume(self.job)
        s3.objects[("testvolume", "someone-elses-data.bin")] = b"data"
        self.retained(s3)


if __name__ == "__main__":
    unittest.main()
