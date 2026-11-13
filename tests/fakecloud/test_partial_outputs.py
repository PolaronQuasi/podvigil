"""recover --accept-partial (F3): the operator's explicit step for a volume that holds incomplete outputs.

recover keeps such a volume, billed as storage, and says why. With --accept-partial it reads every object on the
volume once, copies it into the ledger and checks the copy, then deletes the volume; the outcome is
partial_outputs. It needs the release and the Pod's verified deletion; the inputs, the input manifest and the
owner record must match; the listing must not change; and an object the link index names is never read: it
keeps the volume. The cloud is the in-process fake.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import unittest

from podvigil.podrt.records import read
from tests.fakecloud.test_unpublished_recovery import Released
from tests.support import runner as run
from tests.support.runner import patch_run


def partial(job):
    """What a Pod stopped during publishing leaves: the early result, a salvaged output and the record of it."""
    job.put(
        job.prefix + "artifacts/result.json", {"job_id": job.job["job_id"], "outcome": "cancelled"}
    )
    job.s3.objects[("testvolume", job.prefix + "artifacts/model.bin")] = b"weights"
    job.record(
        "control/finalization-incomplete.json",
        {"complete": False, "salvaged_files": 2},
        actor="pod_supervisor",
    )


class AcceptPartialTests(unittest.TestCase):
    def accept(self, job):
        return run.accept_partial(job.folder, job.api, job.storage)

    def test_every_object_is_copied_and_checked_then_the_volume_is_deleted(self):
        job = Released(self)
        partial(job)
        # recover alone keeps the volume.
        with self.assertRaisesRegex(RuntimeError, "recover --accept-partial"):
            job.recover()
        self.assertIn("testvolume", job.api.volumes)
        before = {
            key: data for (bucket, key), data in job.s3.objects.items() if bucket == "testvolume"
        }
        summary = self.accept(job)
        self.assertEqual(
            (summary["training_outcome"], summary["workload_outcome"]),
            ("partial_outputs", "cancelled"),
        )
        self.assertTrue(summary["pod_termination_verified"] and summary["volume_deletion_verified"])
        self.assertFalse(job.api.volumes)
        audit = read(job.folder / "volume-audit.json")
        self.assertEqual(audit["mode"], "partial_outputs_copied")
        copies = read(Path(summary["copy_directory"]) / "objects.json")["objects"]
        self.assertEqual({item["key"] for item in copies}, set(before))
        # The ledger holds each object's bytes, including the user's input file.
        for item in copies:
            self.assertEqual(
                Path(item["local_path"]).read_bytes(), before[item["key"]], item["key"]
            )
        kinds = {item["key"]: item["kind"] for item in copies}
        self.assertEqual(kinds[job.prefix + "artifacts/model.bin"], "partial_output")
        self.assertEqual(kinds[job.prefix + "agent.json"], "control")
        self.assertEqual(job.recover(), summary)  # a later recover keeps the record

    def test_the_command_line_names_the_outcome_with_its_own_code(self):
        job = Released(self)
        partial(job)
        out = io.StringIO()
        with (
            patch_run("account_api", return_value=job.api),
            patch_run("Storage", return_value=job.storage),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            code = run.run_cli(["recover", str(job.folder), "--accept-partial"])
        result = json.loads(out.getvalue())
        self.assertEqual(
            (code, result["outcome"], result["exit_code"]), (13, "partial_outputs", 13)
        )
        self.assertFalse(job.api.volumes)

    def test_complete_outputs_are_recovered_as_usual(self):
        job = Released(self)
        job.s3.finished(job.job["job_id"])
        summary = self.accept(job)
        self.assertEqual(summary["training_outcome"], "success")
        self.assertEqual(read(job.folder / "volume-audit.json")["mode"], "verified_backup")

    def test_without_the_release_nothing_is_deleted(self):
        job = Released(self)
        partial(job)
        (job.folder / "training-released.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "never released"):
            self.accept(job)
        self.assertIn("testvolume", job.api.volumes)

    def test_a_pod_whose_deletion_is_not_verified_keeps_the_volume(self):
        job = Released(self)
        partial(job)
        job.api.pods["testpod"] = {
            "id": "testpod",
            "name": job.job["pod_name"],
            "status": "RUNNING",
            "env": {run.ENV_PREFIX + "_JOB_ID": job.job["job_id"]},
        }
        with (
            patch_run(
                "delete_verified",
                side_effect=RuntimeError("Cleanup NOT verified: resource is still present"),
            ),
            self.assertRaisesRegex(RuntimeError, "NOT verified"),
        ):
            self.accept(job)
        self.assertIn("testvolume", job.api.volumes)
        self.assertFalse((job.folder / "summary.json").exists())

    def test_an_object_the_link_index_names_is_never_read_and_keeps_the_volume(self):
        job = Released(self)
        partial(job)
        job.put(
            job.prefix + "control/output-links.json",
            {"version": 1, "job_id": job.job["job_id"], "links": ["artifacts/link"]},
        )
        job.s3.objects[("testvolume", job.prefix + "artifacts/link")] = b"the link's target"
        reads, get_object = [], job.s3.get_object

        def recorded(Bucket, Key):
            reads.append(Key)
            return get_object(Bucket=Bucket, Key=Key)

        job.s3.get_object = recorded
        with self.assertRaisesRegex(RuntimeError, "link index"):
            self.accept(job)
        self.assertNotIn(job.prefix + "artifacts/link", reads)
        self.assertIn("testvolume", job.api.volumes)

    def test_anything_that_does_not_match_keeps_the_volume(self):
        cases = {
            "a changed input": (
                lambda job: job.s3.objects.__setitem__(
                    ("testvolume", "inputs/lib.py"), b"VALUE = 2\n"
                ),
                "differs from its original",
            ),
            "a changed input manifest": (
                lambda job: job.s3.objects.__setitem__(
                    ("testvolume", job.job["input_manifest"]["key"]), b'{"inputs":[],"job_id":"x"}'
                ),
                "differs from its original",
            ),
            "another owner": (
                lambda job: job.put("managed-owner.json", {"job_id": "c" * 32}),
                "owner",
            ),
        }
        for name, (change, message) in cases.items():
            with self.subTest(case=name):
                job = Released(self)
                partial(job)
                change(job)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.accept(job)
                self.assertIn("testvolume", job.api.volumes)
                self.assertFalse((job.folder / "summary.json").exists())

    def test_an_object_written_during_the_copy_keeps_the_volume(self):
        job = Released(self)
        partial(job)
        get_object, count = job.s3.get_object, [0]

        # Simulate an unexpected writer by adding an object on every read.
        def late_writer(Bucket, Key):
            count[0] += 1
            job.s3.objects[("testvolume", job.prefix + f"artifacts/late-{count[0]}.bin")] = b"late"
            return get_object(Bucket=Bucket, Key=Key)

        job.s3.get_object = late_writer
        with self.assertRaisesRegex(RuntimeError, "changed during its audit"):
            self.accept(job)
        self.assertIn("testvolume", job.api.volumes)


if __name__ == "__main__":
    unittest.main()
