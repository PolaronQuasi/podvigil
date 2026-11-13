"""Resume special recovery across deletion without inventing completion from an absent volume."""

from pathlib import Path
from unittest.mock import patch
import hashlib
import tempfile
import time
import unittest

from podvigil.controller import cleanup, recovery
from podvigil.controller.ownership import creation_receipt
from podvigil.controller.request import input_manifest
from podvigil.podrt.records import read, save
from podvigil.storage import Storage
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture, ledger


class Crash(BaseException):
    """Process death bypasses the controller's ordinary Exception handlers."""


class RecoveryCheckpointTests(unittest.TestCase):
    def cloud(self, outcome):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-recovery-checkpoint-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        folder = root / "ledger"
        job = fixture()
        job["startup_deadline_epoch"] = time.time() - 60
        source = root / "input.py"
        source.write_bytes(b"VALUE = 1\n")
        job["config"]["inputs"] = [{"local": str(source), "remote": "input.py"}]
        job["inputs"] = [
            {
                "remote": "input.py",
                "size": source.stat().st_size,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ]
        prefix = "jobs/" + job["job_id"] + "/"
        raw, record = input_manifest(job["job_id"], job["inputs"])
        job["input_manifest"] = {**record, "key": prefix + "control/inputs.json"}
        ledger(folder, job)
        api = FakeApi(job)
        s3 = FakeS3(api=api)
        storage = Storage("EU-RO-1", s3)
        api.storage = storage
        for kind, resource in (("pod", api.pods["testpod"]), ("volume", api.volumes["testvolume"])):
            save(
                folder / (kind + "-receipt.json"),
                creation_receipt(resource, job, kind, "create_response"),
            )
        api.pods.clear()  # the Pod has ended; verification still uses real cleanup logic
        storage.put_json("testvolume", "managed-owner.json", {"job_id": job["job_id"]})
        s3.objects[("testvolume", "inputs/input.py")] = source.read_bytes()
        s3.objects[("testvolume", job["input_manifest"]["key"])] = raw
        storage.put_json(
            "testvolume",
            prefix + "agent.json",
            {"job_id": job["job_id"], "pod_id": "testpod", "phase": "done", "actor": "pod_guard"},
        )
        if outcome != "not_started":
            save(folder / "training-released.json", {"utc": "2026-11-13T00:00:00+00:00"})
            storage.put_json(
                "testvolume", prefix + "start.json", {"job_id": job["job_id"], "pod_id": "testpod"}
            )
        if outcome == "partial_outputs":
            s3.objects[("testvolume", prefix + "artifacts/model.bin")] = b"partial weights"
            storage.put_json(
                "testvolume",
                prefix + "artifacts/result.json",
                {"job_id": job["job_id"], "outcome": "cancelled"},
            )
        return folder, job, api, storage

    def interrupt(self, outcome="partial_outputs", boundary="summary"):
        folder, job, api, storage = self.cloud(outcome)
        target = "summary.json" if boundary == "summary" else "volume-deleted.json"
        module = recovery if boundary == "summary" else cleanup

        def interrupted_save(path, value):
            if Path(path).name == target:
                raise Crash()
            return save(path, value)

        action = recovery.accept_partial if outcome == "partial_outputs" else recovery.recover
        with patch.object(module, "save", side_effect=interrupted_save), self.assertRaises(Crash):
            action(folder, api, storage)
        self.assertFalse(api.volumes)
        self.assertTrue((folder / recovery.CHECKPOINT_FILE).is_file())
        self.assertFalse((folder / "summary.json").exists())
        return folder, job, api, storage

    def test_all_special_outcomes_resume_at_both_deletion_boundaries(self):
        for outcome in recovery.SPECIAL_MODES:
            for boundary in ("summary", "deletion_receipt"):
                with self.subTest(outcome=outcome, boundary=boundary):
                    folder, job, api, storage = self.interrupt(outcome, boundary)
                    before = sum(method == "DELETE" for method, _ in api.calls)
                    with patch.object(
                        storage,
                        "collect",
                        side_effect=AssertionError("deleted volume must not be read"),
                    ):
                        result = recovery.recover(folder, api, storage)
                    self.assertEqual(result["training_outcome"], outcome)
                    self.assertTrue(
                        result["pod_termination_verified"] and result["volume_deletion_verified"]
                    )
                    self.assertEqual(sum(method == "DELETE" for method, _ in api.calls), before)
                    self.assertEqual(recovery.recover(folder, api, storage), result)
                    if boundary == "deletion_receipt":
                        self.assertTrue(result["volume_evidence"]["evidence"]["inventory_absent"])
                    if outcome == "partial_outputs":
                        self.assertEqual(result["workload_outcome"], "cancelled")

    def test_missing_or_changed_checkpoint_and_bound_records_refuse_completion(self):
        cases = (
            "missing_checkpoint",
            "malformed_checkpoint",
            "wrong_job",
            "wrong_pod",
            "wrong_volume",
            "wrong_outcome",
            "changed_job",
            "changed_receipt",
            "missing_release",
            "changed_audit",
            "missing_audit",
            "outside_audit",
            "missing_index",
            "changed_index",
            "missing_copy",
            "changed_copy",
            "changed_original_input",
        )
        for case in cases:
            with self.subTest(case=case):
                folder, job, api, storage = self.interrupt()
                checkpoint_path = folder / recovery.CHECKPOINT_FILE
                checkpoint = read(checkpoint_path)
                audit = read(folder / "volume-audit.json")
                index = Path(audit["control_backup_directory"]) / "objects.json"
                copy = Path(
                    next(
                        item["local_path"]
                        for item in audit["objects"]
                        if item["kind"] == "partial_output"
                    )
                )
                if case == "missing_checkpoint":
                    checkpoint_path.unlink()
                elif case == "malformed_checkpoint":
                    checkpoint_path.write_text("{broken", encoding="utf-8")
                elif case in ("wrong_job", "wrong_pod", "wrong_volume", "wrong_outcome"):
                    field = {
                        "wrong_job": "job_id",
                        "wrong_pod": "pod_id",
                        "wrong_volume": "volume_id",
                        "wrong_outcome": "training_outcome",
                    }[case]
                    checkpoint[field] = "outputs_lost" if case == "wrong_outcome" else "unrelated"
                    save(checkpoint_path, checkpoint)
                elif case == "changed_job":
                    job["config"]["command"] = ["python3", "changed.py"]
                    save(folder / "job.json", job)
                elif case == "changed_receipt":
                    receipt = read(folder / "volume-receipt.json")
                    receipt["job_id"] = "b" * 32
                    save(folder / "volume-receipt.json", receipt)
                elif case == "missing_release":
                    (folder / "training-released.json").unlink()
                elif case == "changed_audit":
                    audit["volume_id"] = "unrelated"
                    save(folder / "volume-audit.json", audit)
                elif case == "missing_audit":
                    (folder / "volume-audit.json").unlink()
                elif case == "outside_audit":
                    audit["control_backup_directory"] = str(folder.parent / "volume-audit-outside")
                    save(folder / "volume-audit.json", audit)
                elif case == "missing_index":
                    index.unlink()
                elif case == "changed_index":
                    save(index, {"job_id": job["job_id"], "volume_id": "testvolume", "objects": []})
                elif case == "missing_copy":
                    copy.unlink()
                elif case == "changed_copy":
                    copy.write_bytes(b"x" * copy.stat().st_size)  # same length, different content
                elif case == "changed_original_input":
                    Path(job["config"]["inputs"][0]["local"]).write_bytes(b"VALUE = 2\n")
                before = sum(method == "DELETE" for method, _ in api.calls)
                with self.assertRaises(RuntimeError):
                    recovery.recover(folder, api, storage)
                self.assertFalse((folder / "summary.json").exists())
                self.assertEqual(sum(method == "DELETE" for method, _ in api.calls), before)

    def test_absence_without_checkpoint_or_summary_is_not_completion(self):
        folder, job, api, storage = self.cloud("partial_outputs")
        api.request("DELETE", "/network-volumes/testvolume")
        with self.assertRaises(RuntimeError):
            recovery.recover(folder, api, storage)
        self.assertFalse((folder / "summary.json").exists())
        self.assertFalse((folder / "volume-deleted.json").exists())

    def test_checkpoint_schema_version_must_be_an_integer(self):
        folder, job, api, storage = self.interrupt()
        checkpoint = read(folder / recovery.CHECKPOINT_FILE)
        checkpoint["version"] = True  # Python compares True == 1; that is not schema version 1.
        save(folder / recovery.CHECKPOINT_FILE, checkpoint)
        with self.assertRaisesRegex(RuntimeError, "completion is not verified"):
            recovery.recover(folder, api, storage)
        self.assertFalse((folder / "summary.json").exists())

    def test_saved_checkpoint_does_not_replace_independent_absence_verification(self):
        folder, job, api, storage = self.interrupt(boundary="deletion_receipt")
        listing = api.listing
        with (
            patch.object(
                api,
                "listing",
                side_effect=lambda path, field: (
                    [{"id": "testvolume"}] if path == "/network-volumes" else listing(path, field)
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "independently confirmed"),
        ):
            recovery.recover(folder, api, storage)
        self.assertFalse((folder / "summary.json").exists())
        self.assertFalse((folder / "volume-deleted.json").exists())

    def test_checkpoint_is_not_authority_to_accept_partial_outputs_while_volume_exists(self):
        folder, job, api, storage = self.cloud("partial_outputs")
        delete = cleanup.delete_verified

        def fail_volume_delete(api, path, *args, **kwargs):
            if path.startswith("/network-volumes/"):
                raise RuntimeError("simulated delete failure")
            return delete(api, path, *args, **kwargs)

        with (
            patch.object(cleanup, "delete_verified", side_effect=fail_volume_delete),
            self.assertRaisesRegex(RuntimeError, "simulated delete failure"),
        ):
            recovery.accept_partial(folder, api, storage)
        self.assertTrue((folder / recovery.CHECKPOINT_FILE).exists())
        self.assertIn("testvolume", api.volumes)
        with self.assertRaisesRegex(RuntimeError, "recover --accept-partial"):
            recovery.recover(folder, api, storage)
        self.assertIn("testvolume", api.volumes)
        self.assertNotIn(("DELETE", "/network-volumes/testvolume"), api.calls)
        result = recovery.accept_partial(folder, api, storage)
        self.assertEqual(result["training_outcome"], "partial_outputs")
        self.assertFalse(api.volumes)

    def test_missing_pod_deletion_proof_prevents_checkpoint_and_volume_delete(self):
        folder, job, api, storage = self.cloud("partial_outputs")
        with (
            patch.object(cleanup, "best_effort_save", return_value=False),
            self.assertRaises(RuntimeError),
        ):
            recovery.accept_partial(folder, api, storage)
        self.assertIn("testvolume", api.volumes)
        self.assertFalse((folder / recovery.CHECKPOINT_FILE).exists())

    def test_checkpoint_write_failure_keeps_the_volume(self):
        folder, job, api, storage = self.cloud("partial_outputs")

        def cannot_checkpoint(path, value):
            if Path(path).name == recovery.CHECKPOINT_FILE:
                raise OSError("simulated full disk")
            return save(path, value)

        with (
            patch.object(recovery, "save", side_effect=cannot_checkpoint),
            self.assertRaises(OSError),
        ):
            recovery.accept_partial(folder, api, storage)
        self.assertIn("testvolume", api.volumes)
        self.assertNotIn(("DELETE", "/network-volumes/testvolume"), api.calls)

    def test_existing_special_summary_still_requires_intact_copies(self):
        folder, job, api, storage = self.cloud("partial_outputs")
        summary = recovery.accept_partial(folder, api, storage)
        # Completed ledgers from before checkpoints were introduced remain readable.
        (folder / recovery.CHECKPOINT_FILE).unlink()
        self.assertEqual(recovery.recover(folder, api, storage), summary)
        audit = read(folder / "volume-audit.json")
        copy = Path(
            next(
                item["local_path"] for item in audit["objects"] if item["kind"] == "partial_output"
            )
        )
        copy.write_bytes(b"damaged")
        with self.assertRaisesRegex(RuntimeError, "completion is not verified"):
            recovery.recover(folder, api, storage)


if __name__ == "__main__":
    unittest.main()
