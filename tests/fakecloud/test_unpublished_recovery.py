"""A released job that never published anything (F1): after a cancel, a Ctrl+C or a hard Pod death.

The workload's outputs stay on the Pod's own disk until the Pod publishes them, so here they are gone with the
Pod, and waiting for a manifest kept the volume billed for ever. recover now deletes such a volume, but only on
evidence: the Pod's deletion is verified, the manifest is missing, the Pod's own status record is on the volume,
and the audit finds only the pristine inputs, the input manifest and the exact records the release and this job's
Pod leave, each checked. The outcome is outputs_lost, never success or not_started. Anything that could be user
output keeps the volume exactly as before.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest

from podvigil.controller import request
from podvigil.controller import watchdog as watchdog_module
from podvigil.podrt.records import read, save
from podvigil.storage import RELEASED_RECORDS, Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture, ledger
from tests.support.runner import patch_run
from unittest.mock import patch

INPUT = b"VALUE = 1\n"


class Released:
    """The ledger of a released job whose Pod is gone, and its volume as the Pod left it."""

    def __init__(self, test):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-unpublished-")
        test.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        local = root / "lib.py"
        local.write_bytes(INPUT)
        self.folder = root / "ledger"
        job = fixture()
        # The workload ran past its start-up window.
        job["startup_deadline_epoch"] = time.time() - 60
        job["config"]["inputs"] = [{"local": str(local), "remote": "lib.py"}]
        job["inputs"] = [
            {"remote": "lib.py", "size": len(INPUT), "sha256": hashlib.sha256(INPUT).hexdigest()}
        ]
        self.prefix = "jobs/" + job["job_id"] + "/"
        raw, record = request.input_manifest(job["job_id"], job["inputs"])
        job["input_manifest"] = {**record, "key": self.prefix + "control/inputs.json"}
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
        save(self.folder / "training-released.json", {"utc": "2026-11-12T22:00:00+00:00"})
        self.api = FakeApi(job)
        self.s3 = FakeS3(api=self.api)
        self.storage = Storage("EU-RO-1", self.s3)
        self.api.storage = self.storage
        self.api.pods.clear()  # the Pod is gone: deleted by the watchdog, or dead
        self.put("managed-owner.json", {"job_id": job["job_id"]})
        self.s3.objects[("testvolume", "inputs/lib.py")] = INPUT
        self.s3.objects[("testvolume", job["input_manifest"]["key"])] = raw
        self.put(self.prefix + "start.json", {"job_id": job["job_id"], "pod_id": "testpod"})
        self.put(self.prefix + "started-once.json", {"started_utc": "2026-11-12T22:00:01+00:00"})
        self.s3.objects[("testvolume", self.prefix + "start.lock")] = b"0"
        self.record(
            "agent.json",
            {"phase": "training", "heartbeat_epoch": time.time()},
            actor="pod_supervisor",
        )
        self.record("control/identity.json", {"source": "RUNPOD_POD_ID"}, actor="pod_supervisor")

    def put(self, key, value):
        self.s3.objects[("testvolume", key)] = json.dumps(value).encode()

    def record(self, name, fields, actor="pod_guard", **ids):
        self.put(
            self.prefix + name,
            {
                **fields,
                "job_id": ids.get("job_id", self.job["job_id"]),
                "pod_id": ids.get("pod_id", "testpod"),
                "actor": actor,
                "updated_utc": "2026-11-12T22:05:00+00:00",
            },
        )

    def recover(self):
        return run.recover(self.folder, self.api, self.storage)


class OutputsLostTests(unittest.TestCase):
    def test_only_inputs_and_the_pods_records_delete_the_volume_and_name_the_outputs_lost(self):
        job = Released(self)
        job.record(
            "control/guard-failure.json", {"error_type": "TimeoutError", "message": "stopped"}
        )
        job.record("control/self-delete.json", {"delete_accepted": True, "delete_http_status": 204})
        job.record(
            "control/finalization-incomplete.json",
            {"complete": False, "stage": "quiesce", "result": {"outcome": "interrupted"}},
            actor="pod_supervisor",
        )
        summary = job.recover()
        self.assertEqual(
            (summary["training_outcome"], summary["outputs_lost"]), ("outputs_lost", True)
        )
        self.assertIn("never published its outputs", summary["explanation"])
        self.assertTrue(summary["pod_termination_verified"] and summary["volume_deletion_verified"])
        self.assertFalse(job.api.volumes)
        audit = read(job.folder / "volume-audit.json")
        self.assertEqual(audit["mode"], "released_unpublished")
        kinds = {
            item["key"][len(job.prefix) :]
            if item["key"].startswith(job.prefix)
            else item["key"]: item["kind"]
            for item in audit["objects"]
        }
        self.assertEqual(kinds["inputs/lib.py"], "input")
        self.assertEqual(kinds["control/inputs.json"], "input_manifest")
        # The Pod's records were copied to the ledger before the volume went.
        copies = read(Path(audit["control_backup_directory"]) / "objects.json")["objects"]
        copied = {item["key"] for item in copies if "local_path" in item}
        self.assertLessEqual(
            {job.prefix + name for name in ("agent.json", "control/guard-failure.json")}, copied
        )
        self.assertEqual(job.recover(), summary)  # a second recovery keeps the record

    def test_the_command_line_names_the_outcome_with_its_own_code(self):
        job = Released(self)
        out, err = io.StringIO(), io.StringIO()
        with (
            patch_run("account_api", return_value=job.api),
            patch_run("Storage", return_value=job.storage),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = run.run_cli(["recover", str(job.folder)])
        result = json.loads(out.getvalue())
        self.assertEqual((code, result["outcome"], result["exit_code"]), (12, "outputs_lost", 12))
        self.assertIn("OUTPUTS LOST", err.getvalue())
        self.assertNotIn(code, (0, 2))

    def test_anything_that_could_be_output_or_does_not_match_keeps_the_volume(self):
        bundle = "output-bundle/" + "a" * 32 + "/archive.tar"
        cases = {
            "the early result": lambda job: job.put(
                job.prefix + "artifacts/result.json", {"outcome": "interrupted"}
            ),
            "a partial salvage": lambda job: (
                job.s3.objects.__setitem__(
                    ("testvolume", job.prefix + "artifacts/model.bin"), b"weights"
                ),
                job.record(
                    "control/finalization-incomplete.json",
                    {"complete": False, "salvaged_files": 1},
                    actor="pod_supervisor",
                ),
            ),
            "a salvage record alone": lambda job: job.record(
                "control/finalization-incomplete.json", {"complete": False, "salvaged_files": 3}
            ),
            "a bundle part": lambda job: job.s3.objects.__setitem__(
                ("testvolume", job.prefix + bundle), b"tar"
            ),
            "an incomplete manifest": lambda job: job.put(
                job.prefix + "artifact-manifest.json",
                {"job_id": job.job["job_id"], "complete": False, "files": []},
            ),
            "a link index": lambda job: job.put(
                job.prefix + "control/output-links.json", {"version": 1}
            ),
            "a checkpoint": lambda job: job.s3.objects.__setitem__(
                ("testvolume", job.prefix + "checkpoints/1.bin"), b"x"
            ),
            "an unknown object": lambda job: job.s3.objects.__setitem__(
                ("testvolume", "notes.txt"), b"x"
            ),
            "a changed input": lambda job: job.s3.objects.__setitem__(
                ("testvolume", "inputs/lib.py"), b"VALUE = 2\n"
            ),
            "a changed input manifest": lambda job: job.s3.objects.__setitem__(
                ("testvolume", job.job["input_manifest"]["key"]), b'{"inputs":[],"job_id":"x"}'
            ),
            "a record of another Pod": lambda job: job.record(
                "control/identity.json", {}, pod_id="otherpod"
            ),
            "a record of another job": lambda job: job.record(
                "control/guard-failure.json", {}, job_id="c" * 32
            ),
            "a release for another Pod": lambda job: job.put(
                job.prefix + "start.json", {"job_id": job.job["job_id"], "pod_id": "otherpod"}
            ),
            "a lock with content": lambda job: job.s3.objects.__setitem__(
                ("testvolume", job.prefix + "start.lock"), b"!"
            ),
            "no status record of the Pod": lambda job: job.s3.objects.pop(
                ("testvolume", job.prefix + "agent.json")
            ),
            "a status record of another actor": lambda job: job.record(
                "agent.json", {"phase": "training"}, actor="workload"
            ),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                job = Released(self)
                change(job)
                with self.assertRaisesRegex(RuntimeError, "No valid complete artifact manifest"):
                    job.recover()
                self.assertIn("testvolume", job.api.volumes)
                self.assertFalse((job.folder / "summary.json").exists())
                self.assertTrue(
                    (job.folder / "volume-kept.json").exists()
                    or name == "no status record of the Pod"
                )

    def test_it_needs_the_release_and_a_verified_pod_deletion(self):
        job = Released(self)
        # A workload that was never released follows the start-up path.
        (job.folder / "training-released.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "No valid complete artifact manifest"):
            job.recover()
        self.assertIn("testvolume", job.api.volumes)
        job = Released(self)
        job.api.pods["testpod"] = {
            "id": "testpod",
            "name": job.job["pod_name"],
            "status": "RUNNING",
            "env": {run.ENV_PREFIX + "_JOB_ID": job.job["job_id"]},
        }
        # Simulate delete_verified giving up after its retries while the Pod still exists.
        with (
            patch_run(
                "delete_verified",
                side_effect=RuntimeError("Cleanup NOT verified: resource is still present"),
            ),
            self.assertRaisesRegex(RuntimeError, "NOT verified"),
        ):
            job.recover()
        self.assertIn("testvolume", job.api.volumes)
        self.assertFalse((job.folder / "summary.json").exists())

    def test_the_exact_record_set(self):
        self.assertEqual(
            RELEASED_RECORDS,
            {
                "start.json",
                "cancel.json",
                "started-once.json",
                "start.lock",
                "agent.json",
                "control/identity.json",
                "control/self-delete.json",
                "control/guard-failure.json",
                "control/finalization-incomplete.json",
            },
        )


class CancelledWorkloadTests(unittest.TestCase):
    """cancel of a running job whose Pod publishes nothing: the watchdog deletes the Pod, and recovery now deletes
    the volume too, naming the outputs lost, instead of keeping it billed for ever."""

    def test_cancel_ends_with_nothing_billed_and_says_the_outputs_are_lost(self):
        # The Pod never answers.
        grace = patch.object(watchdog_module, "cancel_grace_seconds", return_value=0.5)
        grace.start()
        self.addCleanup(grace.stop)
        job = Released(self)
        job.api.pods["testpod"] = {
            "id": "testpod",
            "name": job.job["pod_name"],
            "status": "RUNNING",
            "cloud": "SECURE",
            "env": {run.ENV_PREFIX + "_JOB_ID": job.job["job_id"]},
            "cost": 0.5,
            "dataCenterId": "EU-RO-1",
            "gpu": {"id": job.job["config"]["gpu_id"], "count": 1},
        }
        thread = threading.Thread(
            target=run.watchdog,
            args=(job.folder, job.api, job.storage),
            kwargs={"pause": lambda _: time.sleep(0.05)},
            daemon=True,
        )
        thread.start()
        end = time.monotonic() + 10
        while not (job.folder / "watchdog.json").exists() and time.monotonic() < end:
            time.sleep(0.02)
        with patch_run("account_api", side_effect=AssertionError("cancel must not call the API")):
            result = run.cancel(job.folder, wait=20, pause=lambda _: time.sleep(0.05))
        thread.join(15)
        self.assertEqual(
            (result["state"], result["training_outcome"]), ("compute_stopped", "outputs_lost")
        )
        self.assertEqual((job.api.pods, job.api.volumes), ({}, {}))
        self.assertEqual(run.of_result("cancel", result), "outputs_lost")


if __name__ == "__main__":
    unittest.main()
