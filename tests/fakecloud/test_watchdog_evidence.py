"""Regressions for the last integration audit; all services are local doubles."""

from pathlib import Path
import tempfile
import time
import unittest

from podvigil.controller.ownership import creation_receipt
from podvigil.jobspec import validate
from podvigil.podrt.records import read, save
from podvigil.settings import ENV_PREFIX
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture, ledger
from tests.support.runner import patch_run


class StopSimulation(BaseException):
    pass


class FinalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-final-review-")
        self.folder = Path(self.temp.name)
        self.job = fixture()
        self.api = FakeApi(self.job)
        self.s3 = FakeS3()
        self.storage = Storage("EU-RO-1", self.s3)

    def tearDown(self):
        self.temp.cleanup()

    def test_first_stale_read_still_hands_off_backup_after_deadline(self):
        self.job["deadline_epoch"] = time.time() - 1
        ledger(self.folder, self.job, stale=True)
        self.s3.finished(self.job["job_id"])
        run.watchdog(self.folder, self.api, self.storage, pause=lambda _: None)
        self.assertFalse(self.api.pods)
        self.assertFalse(self.api.volumes)
        self.assertEqual(read(self.folder / "recovery-handoff.json")["state"], "complete")
        self.assertTrue((self.folder / "downloads/artifacts/model.bin").is_file())

    def test_unreadable_heartbeat_does_not_skip_post_termination_backup(self):
        self.job["deadline_epoch"] = time.time() - 1
        ledger(self.folder, self.job)
        self.s3.finished(self.job["job_id"])
        original = run.read

        def reading(path, default=None):
            if Path(path).name == "controller-heartbeat.json":
                raise PermissionError("simulated reader contention")
            return original(path, default)

        with patch_run("read", side_effect=reading):
            run.watchdog(self.folder, self.api, self.storage, pause=lambda _: None)
        self.assertFalse(self.api.volumes)
        self.assertEqual(read(self.folder / "recovery-handoff.json")["state"], "complete")

    def test_done_and_lagging_reads_require_an_actual_delete(self):
        ledger(self.folder, self.job)
        save(
            self.folder / "pod-receipt.json",
            creation_receipt(self.api.pods["testpod"], self.job, "pod", "create_response"),
        )
        self.s3.finished(self.job["job_id"])
        original_get, original_list = self.api.get_or_none, self.api.listing
        self.api.get_or_none = lambda path: (
            None if path.startswith("/pods/") else original_get(path)
        )
        self.api.listing = lambda path, field: [] if path == "/pods" else original_list(path, field)
        run.watchdog(self.folder, self.api, self.storage, pause=lambda _: None)
        self.assertIn(("DELETE", "/pods/testpod"), self.api.calls)
        self.assertFalse(self.api.pods)
        self.assertFalse(self.api.volumes)

    def test_probe_done_and_lagging_reads_do_not_claim_deletion(self):
        self.job["config"]["self_termination_probe"] = True
        ledger(self.folder, self.job)
        self.s3.finished(self.job["job_id"])
        original_get, original_list = self.api.get_or_none, self.api.listing
        self.api.get_or_none = lambda path: (
            None if path.startswith("/pods/") else original_get(path)
        )
        self.api.listing = lambda path, field: [] if path == "/pods" else original_list(path, field)

        def halt(_):
            raise StopSimulation()

        with self.assertRaises(StopSimulation):
            run.watchdog(self.folder, self.api, self.storage, pause=halt)
        self.assertIn("testpod", self.api.pods)
        self.assertFalse((self.folder / "pod-deleted.json").exists())
        self.assertFalse((self.folder / "terminal-observed.json").exists())
        self.assertNotIn(("DELETE", "/network-volumes/testvolume"), self.api.calls)

    def test_old_done_marker_is_not_a_deletion_proof(self):
        ledger(self.folder, self.job)
        save(
            self.folder / "terminal-observed.json",
            {
                "job_id": self.job["job_id"],
                "pod_id": "testpod",
                "source": "owned Pod tombstone or agent done status",
            },
        )
        self.api.get_or_none = lambda _: None
        self.api.listing = lambda *_: []
        with self.assertRaisesRegex(RuntimeError, "inconclusive"):
            run.cleanup_pod(self.api, self.job, self.folder)
        self.assertFalse((self.folder / "pod-deleted.json").exists())

    def test_config_accepts_safe_declared_workload_settings(self):
        cfg = validate(
            {**self.job["config"], "workload_env": {"BATCH_SIZE": "8", "OMP_NUM_THREADS": "2"}}
        )
        body = run.payload({**self.job, "config": cfg}, "testvolume")
        self.assertEqual(body["cloud"], "SECURE")
        import base64, json
        from podvigil.podrt import agent as pod_agent

        remote = json.loads(base64.b64decode(body["env"][ENV_PREFIX + "_JOB_SPEC"]))
        env = pod_agent.child_environment({}, "/out", "/inputs", remote["config"]["workload_env"])
        self.assertEqual(env["BATCH_SIZE"], "8")
        self.assertEqual(env["WONDOMON_INPUT_DIR"], "/inputs")

    def test_config_rejects_control_credentials_and_loader_overrides(self):
        for value in (
            [],
            {"RUNPOD_API_KEY": "dummy"},
            {"AWS_REGION": "dummy"},
            {"WONDOMON_OUTPUT_DIR": "/other"},
            {"LD_PRELOAD": "x"},
            {"HF_TOKEN": "dummy"},
            {"N": 3},
            {"BAD": "a\0b"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate({**self.job["config"], "workload_env": value})


if __name__ == "__main__":
    unittest.main()
