from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import hashlib
import json

from podvigil.storage import MANIFEST_FILE, RECEIPT_FILE, Storage, local_secrets
from podvigil import settings
from podvigil.jobspec import validate
from tests.support.fakes import FakeS3, MissingObject
from tests.support.paths import ROOT


BUCKET = "testvolume"
JOB_ID = "b" * 32
PREFIX = f"jobs/{JOB_ID}/"


class VolumeAuditTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backup = self.root / "backup"
        self.job = {
            "job_id": JOB_ID,
            "inputs": [],
            "config": {"inputs": [], "required_artifacts": []},
        }
        self.s3 = FakeS3(page_size=2)
        self.storage = Storage("EU-RO-1", self.s3)
        credential = patch(
            "podvigil.pathsafe.credential_files", return_value=[self.root / "missing-credential"]
        )
        credential.start()
        self.addCleanup(credential.stop)

    def finish(self):
        self.s3.finished(JOB_ID)
        self.storage.collect(self.job, BUCKET, self.backup)

    def input(self, remote="training.py", content=b"original input"):
        source = self.root / f"source-{len(self.job['inputs'])}.bin"
        source.write_bytes(content)
        self.job["inputs"].append(
            {"remote": remote, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        )
        self.job["config"]["inputs"].append({"remote": remote, "local": str(source)})
        self.s3.objects[BUCKET, "inputs/" + remote] = content
        return source

    def audit(self, **kwargs):
        return self.storage.audit_for_delete(self.job, BUCKET, **kwargs)

    def test_whole_volume_checkpoint_outside_manifest_blocks_delete(self):
        self.finish()
        for key in (
            "inputs/checkpoints/epoch-9.pt",
            "runs/model.bin",
            "wandb/run.log",
            "jobs/other/artifacts/checkpoint",
            "unexpected-empty/",
        ):
            with self.subTest(key=key):
                self.s3.objects[BUCKET, key] = b"" if key.endswith("/") else b"only copy"
                before = dict(self.s3.objects)
                with self.assertRaisesRegex(RuntimeError, "Unexpected volume objects") as failure:
                    self.audit(backup_dir=self.backup)
                self.assertIn(key, str(failure.exception))
                self.assertEqual(self.s3.objects, before)
                del self.s3.objects[BUCKET, key]
        self.assertTrue(any(prefix == "" for _, prefix, _ in self.s3.list_calls))
        self.assertTrue(any(prefix == "" and token for _, prefix, token in self.s3.list_calls))

    def test_recover_retains_volume_with_checkpoint_outside_manifest(self):
        from tests.support.runner import recover
        from tests.support.fakes import FakeApi
        from tests.support.jobs import fixture, ledger

        job = fixture()
        folder = self.root / "integration"
        folder.mkdir()
        ledger(folder, job)
        api, s3 = FakeApi(job), FakeS3()
        s3.finished(job["job_id"])
        s3.objects[BUCKET, "inputs/checkpoints/epoch-9.pt"] = b"only checkpoint"
        with self.assertRaisesRegex(RuntimeError, "Unexpected volume objects"):
            recover(folder, api, Storage("EU-RO-1", s3))
        self.assertFalse(api.pods)
        self.assertIn(BUCKET, api.volumes)
        self.assertNotIn(("DELETE", "/network-volumes/" + BUCKET), api.calls)
        self.assertFalse((folder / "summary.json").exists())

    def test_same_size_overwritten_input_is_retained(self):
        source = self.input(content=b"original")
        self.finish()
        self.s3.objects[BUCKET, "inputs/training.py"] = b"modified"
        with self.assertRaisesRegex(RuntimeError, "original checksum"):
            self.audit(backup_dir=self.backup)
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual(self.s3.objects[BUCKET, "inputs/training.py"], b"modified")

    def test_remote_artifact_is_hashed_again_after_collection(self):
        self.finish()
        key = PREFIX + "artifacts/model.bin"
        original = self.s3.objects[BUCKET, key]
        self.s3.objects[BUCKET, key] = b"X" * len(original)
        with self.assertRaisesRegex(RuntimeError, "original checksum"):
            self.audit(backup_dir=self.backup)

    def test_controls_have_durable_copies_and_bound_evidence(self):
        self.input()
        self.finish()
        controls = {
            "managed-owner.json": json.dumps({"job_id": JOB_ID}).encode(),
            PREFIX + "agent.json": json.dumps({"job_id": JOB_ID, "phase": "done"}).encode(),
            PREFIX + "start.json": json.dumps({"job_id": JOB_ID, "pod_id": "dummy"}).encode(),
            PREFIX + "started-once.json": b'{"started_utc":"2026-11-10T00:00:00Z"}',
            PREFIX + "start.lock": b"0",
            PREFIX + "control/identity.json": b'{"pod_id":"dummy","source":"RUNPOD_POD_ID"}',
            PREFIX + "control/self-delete.json": b'{"status":204,"phase":"delete_sent"}',
            PREFIX + "control/finalization-incomplete.json": b'{"failure":"dummy"}',
        }
        self.s3.objects.update({(BUCKET, key): content for key, content in controls.items()})
        self.s3.objects[BUCKET, "inputs/"] = b""
        before = dict(self.s3.objects)
        evidence = self.audit(backup_dir=self.backup)
        self.assertEqual(evidence["job_id"], JOB_ID)
        self.assertEqual(evidence["volume_id"], BUCKET)
        self.assertEqual(evidence["mode"], "verified_backup")
        self.assertEqual(evidence["object_count"], len(before))
        self.assertEqual(len(evidence["inventory_sha256"]), 64)
        records = {record["key"]: record for record in evidence["objects"]}
        for key, content in controls.items():
            saved = Path(records[key]["local_path"])
            self.assertTrue(saved.is_relative_to(self.backup))
            self.assertEqual(saved.read_bytes(), content)
            self.assertEqual(records[key]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertTrue((Path(evidence["control_backup_directory"]) / "objects.json").is_file())
        self.assertEqual(self.s3.objects, before)

    def test_unknown_or_wrong_job_owner_is_retained(self):
        self.finish()
        self.s3.objects[BUCKET, "managed-owner.json"] = b'{"job_id":"other"}'
        with self.assertRaisesRegex(RuntimeError, "owner probe"):
            self.audit(backup_dir=self.backup)

    def test_missing_local_input_requires_backup_even_with_pristine_authorization(self):
        source = self.input()
        source.unlink()
        with self.assertRaisesRegex(RuntimeError, "no verified local source"):
            self.audit(allow_pristine_inputs=True)
        evidence = self.audit(backup_dir=self.backup, allow_pristine_inputs=True)
        record = evidence["objects"][0]
        self.assertTrue(Path(record["local_path"]).is_relative_to(self.backup))
        self.assertEqual(Path(record["local_path"]).read_bytes(), b"original input")
        self.assertFalse((self.backup / RECEIPT_FILE).exists())

    def test_changed_local_input_is_recovered_without_overwriting_it(self):
        source = self.input()
        source.write_bytes(b"user changed this locally")
        self.finish()
        evidence = self.audit(backup_dir=self.backup)
        entry = next(item for item in evidence["objects"] if item["kind"] == "input")
        self.assertEqual(Path(entry["local_path"]).read_bytes(), b"original input")
        self.assertEqual(source.read_bytes(), b"user changed this locally")

    def test_unuploaded_input_requires_its_original_local_copy(self):
        source = self.input()
        del self.s3.objects[BUCKET, "inputs/training.py"]
        evidence = self.audit(allow_pristine_inputs=True)
        self.assertEqual(evidence["object_count"], 0)
        self.assertEqual(len(evidence["inputs_absent_from_volume"]), 1)
        source.unlink()
        with self.assertRaisesRegex(RuntimeError, "absent from both"):
            self.audit(allow_pristine_inputs=True)

    def test_pristine_mode_requires_explicit_authorization(self):
        with self.assertRaisesRegex(ValueError, "verified backup"):
            self.audit()
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.audit(allow_pristine_inputs=1)
        evidence = self.audit(allow_pristine_inputs=True)
        self.assertEqual(evidence["mode"], "pristine_inputs")
        self.assertEqual(evidence["object_count"], 0)
        self.input()
        self.s3.objects[BUCKET, "managed-owner.json"] = json.dumps({"job_id": JOB_ID}).encode()
        self.assertEqual(self.audit(allow_pristine_inputs=True)["object_count"], 2)

    def test_pristine_mode_rejects_any_evidence_that_pod_ran(self):
        for suffix in (
            "agent.json",
            "start.json",
            "started-once.json",
            MANIFEST_FILE,
            "start.lock",
            "artifacts/checkpoint.bin",
            "control/identity.json",
            "control/self-delete.json",
        ):
            key = PREFIX + suffix
            with self.subTest(key=key):
                self.s3.objects[BUCKET, key] = b"data"
                with self.assertRaisesRegex(RuntimeError, "Unexpected volume objects"):
                    self.audit(allow_pristine_inputs=True)
                del self.s3.objects[BUCKET, key]
        self.finish()
        with self.assertRaisesRegex(RuntimeError, "Unexpected volume objects"):
            self.audit(backup_dir=self.backup, allow_pristine_inputs=True)

    def test_audit_rejects_missing_local_backup_or_wrong_volume(self):
        self.s3.finished(JOB_ID)
        with self.assertRaises(RuntimeError):
            self.audit(backup_dir=self.backup)
        self.storage.collect(self.job, BUCKET, self.backup)
        with self.assertRaisesRegex(RuntimeError, "receipt"):
            self.storage.audit_for_delete(self.job, "other", self.backup)
        self.job["volume_id"] = "other"
        with self.assertRaisesRegex(ValueError, "identity"):
            self.audit(backup_dir=self.backup)

    def test_inventory_change_after_hashing_blocks_delete(self):
        self.finish()
        original_get = self.s3.get_object

        def changing_get(Bucket, Key):
            response = original_get(Bucket, Key)
            self.s3.objects[BUCKET, "late-checkpoint"] = b"late output"
            return response

        with patch.object(self.s3, "get_object", side_effect=changing_get):
            with self.assertRaisesRegex(RuntimeError, "inventory changed"):
                self.audit(backup_dir=self.backup)

    def test_export_uses_opaque_names_for_entire_bucket(self):
        contents = {
            "inputs/checkpoints/eval:20:00": b"outside artifact folder",
            PREFIX + "artifacts/Case": b"upper",
            PREFIX + "artifacts/case": b"lower",
            PREFIX + "artifacts/trailing.": b"dot",
            PREFIX + "artifacts/NUL.txt": b"device name",
            "../escape": b"traversal",
            "a\\b": b"backslash",
            "empty/": b"",
        }
        self.s3.objects.update({(BUCKET, key): value for key, value in contents.items()})
        before = dict(self.s3.objects)
        result = self.storage.export_partial(self.job, BUCKET, self.root / "partial")
        records = [
            json.loads(line) for line in Path(result["mapping_file"]).read_text().splitlines()
        ]
        saved = {record["key"]: record for record in records if record["status"] == "saved"}
        self.assertEqual(set(saved), set(contents))
        self.assertEqual(result["file_count"], len(contents))
        self.assertFalse(result["complete"])
        self.assertTrue(result["listing_complete"])
        self.assertEqual(result["failed_count"], 0)
        export = Path(result["download_directory"])
        for key, content in contents.items():
            path = export / saved[key]["local_path"]
            self.assertTrue(path.is_relative_to(export / "objects"))
            self.assertRegex(path.name, r"^[0-9]{8}\.bin$")
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(saved[key]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertFalse((export / RECEIPT_FILE).exists())
        self.assertEqual(self.s3.objects, before)

    def test_partial_export_keeps_accessible_files_after_object_or_listing_failure(self):
        self.s3.objects[BUCKET, "a-good"] = b"saved"
        self.s3.objects[BUCKET, "b-unreadable"] = b"unavailable"
        self.s3.objects[BUCKET, "c-good"] = b"also saved"
        original_get = self.s3.get_object

        def unavailable(Bucket, Key):
            if Key == "b-unreadable":
                raise MissingObject()
            return original_get(Bucket, Key)

        with patch.object(self.s3, "get_object", side_effect=unavailable):
            result = self.storage.export_partial(self.job, BUCKET, self.root / "partial")
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertFalse(result["complete"])
        original_list = self.s3.list_objects_v2

        def later_page_fails(**kwargs):
            if kwargs.get("ContinuationToken"):
                raise RuntimeError("simulated listing outage")
            return original_list(**kwargs)

        with patch.object(self.s3, "list_objects_v2", side_effect=later_page_fails):
            result = self.storage.export_partial(self.job, BUCKET, self.root / "partial")
        self.assertFalse(result["listing_complete"])
        self.assertEqual(result["file_count"], 2)
        self.assertTrue(Path(result["mapping_file"]).is_file())

    def test_missing_env_file_allows_environment_credentials(self):
        with patch.object(settings, "env_file", return_value=self.root / "missing.env"):
            with patch.dict(
                "os.environ",
                {"RUNPOD_S3_ACCESS_KEY": "dummy-access", "RUNPOD_S3_SECRET_KEY": "dummy-secret"},
            ):
                self.assertEqual(
                    local_secrets(),
                    {
                        "RUNPOD_S3_ACCESS_KEY": "dummy-access",
                        "RUNPOD_S3_SECRET_KEY": "dummy-secret",
                    },
                )

    def test_probe_and_standard_volume_policy(self):
        path = ROOT / "jobs/smoke.example.json"
        config = json.loads(path.read_text())
        for name in ("self_termination_probe", "self_termination_grace_seconds", "volume_type"):
            config.pop(name, None)
        defaults = validate(config)
        self.assertIs(defaults["self_termination_probe"], False)
        self.assertEqual(defaults["self_termination_grace_seconds"], 60)
        self.assertEqual(defaults["volume_type"], "STANDARD")
        for changes in (
            {"self_termination_probe": 1},
            {"self_termination_probe": "false"},
            {"self_termination_grace_seconds": True},
            {"self_termination_grace_seconds": 0},
            {"self_termination_grace_seconds": 3601},
            {"volume_type": "PREMIUM"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**config, **changes})
        self.assertIs(
            validate({**config, "self_termination_probe": True})["self_termination_probe"], True
        )
