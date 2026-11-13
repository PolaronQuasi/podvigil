"""Ambiguous create recovery never equates missing evidence with no billing."""

from pathlib import Path
import json
import tempfile
import time
import unittest

from podvigil.storage import Storage
from podvigil.controller.creation_recovery import active_handoff, reconcile_from_audit
from podvigil.podrt.records import read, save
from tests.support import runner as run
from tests.support.runner import patch_run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture, ledger


class CreationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-creation-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.job = fixture()
        self.api = FakeApi(self.job)
        ledger(self.folder, self.job)
        for kind in ("pod", "volume"):
            (self.folder / (kind + "-receipt.json")).unlink()
            save(self.folder / (kind + "-dispatch.json"), {"state": "dispatching"})

    def audits(self, rows):
        original = self.api.listing

        def listing(path, field):
            if path.startswith("/audit-logs?"):
                self.assertEqual(field, "auditLogs")
                self.api.calls.append(("GET", path))
                return rows
            return original(path, field)

        self.api.listing = listing

    def row(self, kind="pod"):
        value = (
            self.api.pods["testpod"] if kind == "pod" else self.api.volumes["testvolume"]
        ).copy()
        return {
            "resourceType": "pod" if kind == "pod" else "networkVolume",
            "action": "create",
            "resourceId": value["id"],
            "value": json.dumps(value),
            "timestamp": "2026-11-11T00:00:00Z",
        }

    def test_audit_recovers_already_deleted_pod_identity_without_raw_secrets(self):
        row = self.row()
        value = json.loads(row["value"])
        value["env"]["OTHER_SECRET"] = "never-persist-this-secret"
        row["value"] = json.dumps(value)
        self.audits([row])
        self.api.pods.clear()
        self.assertEqual(reconcile_from_audit(self.api, self.job, self.folder, "pod"), "testpod")
        record = read(self.folder / "pod-receipt.json")
        self.assertEqual(record["evidence_source"], "authenticated_provider_create_audit")
        self.assertNotIn("never-persist", (self.folder / "pod-receipt.json").read_text())
        self.assertFalse((self.folder / "pod-deleted.json").exists())
        self.assertFalse(any(m != "GET" for m, _ in self.api.calls))

    def test_audit_empty_partial_bulk_or_prior_resource_is_inconclusive(self):
        row = self.row()
        variants = [
            [],
            [{**row, "value": json.dumps({"name": self.job["pod_name"]})}],
            [{**row, "resourceId": "one,two"}],
            [{**row, "action": "delete"}],
            [{**row, "value": "invalid"}],
            [{**row, "value": json.dumps({"env": None})}],
        ]
        for rows in variants:
            with self.subTest(rows=rows):
                self.audits(rows)
                self.assertIsNone(reconcile_from_audit(self.api, self.job, self.folder, "pod"))
                self.assertFalse((self.folder / "pod-receipt.json").exists())
        self.job["prior_pod_ids"] = ["testpod"]
        self.audits([row])
        self.assertIsNone(reconcile_from_audit(self.api, self.job, self.folder, "pod"))

    def test_audit_conflict_and_multiple_creations_refuse_authority(self):
        row = self.row()
        self.audits([row])
        self.api.pods["testpod"]["name"] = "unrelated"
        with self.assertRaises(PermissionError):
            reconcile_from_audit(self.api, self.job, self.folder, "pod")
        duplicate = json.loads(row["value"])
        duplicate["id"] = "secondpod"
        self.audits([row, {**row, "resourceId": "secondpod", "value": json.dumps(duplicate)}])
        with self.assertRaises(RuntimeError):
            reconcile_from_audit(self.api, self.job, self.folder, "pod")

    def test_volume_manual_resolution_checks_identity_and_never_rebinds(self):
        result = run.resolve_creation(self.folder, api=self.api, volume_id="testvolume")
        self.assertTrue(result["resolved"])
        self.assertFalse(result["cleanup_verified"])
        with self.assertRaises(RuntimeError):
            run.resolve_creation(self.folder, api=self.api, volume_id="different")
        with self.assertRaises(ValueError):
            run.resolve_creation(self.folder, api=self.api, pod_id="../unrelated")
        self.assertFalse(any(m != "GET" for m, _ in self.api.calls))

    def test_full_audit_command_does_not_delete_or_claim_completion(self):
        self.audits([self.row(), self.row("volume")])
        result = run.resolve_creation(self.folder, api=self.api, audit=True)
        self.assertEqual(result["identities"], {"pod": "testpod", "volume": "testvolume"})
        self.assertFalse(result["cleanup_verified"])

    def test_unresolved_export_keeps_source_and_never_certifies_compute(self):
        self.job["deadline_epoch"] = time.time() - 310
        save(self.folder / "job.json", self.job)
        cloud = FakeS3()
        storage = Storage("EU-RO-1", cloud)
        cloud.put_object(Bucket="testvolume", Key="odd:name", Body=b"checkpoint")
        result = run.export_unresolved(self.folder, self.api, storage)
        self.assertFalse(result["complete"])
        self.assertFalse(result["pod_termination_verified"])
        self.assertTrue(result["billing_unverified"])
        self.assertTrue(result["storage_retained"])
        self.assertIn("testpod", self.api.pods)
        self.assertFalse(any(m != "GET" for m, _ in self.api.calls))
        self.assertFalse((self.folder / "summary.json").exists())
        self.assertFalse(active_handoff(self.folder, self.job))

    def test_unresolved_export_refuses_before_original_deadline_margin(self):
        with self.assertRaises(RuntimeError):
            run.export_unresolved(self.folder, self.api, Storage("EU-RO-1", FakeS3()))

    def test_handoff_requires_explicit_ack_and_does_not_fabricate_receipts(self):
        with self.assertRaises(ValueError):
            run.handoff(self.folder, "Operator investigating lost response", False)
        with self.assertRaises(ValueError):
            run.handoff(self.folder, "", True)
        self.assertFalse((self.folder / "stop-request.json").exists())
        result = run.handoff(self.folder, "Operator investigating lost response", True)
        self.assertFalse(result["cleanup_verified"])
        run.watchdog_loop(self.folder, self.api, Storage("EU-RO-1", FakeS3()))
        self.assertEqual(read(self.folder / "watchdog.json")["state"], "operator_took_over")
        self.assertFalse(self.api.calls)
        self.assertFalse((self.folder / "pod-deleted.json").exists())
        self.assertFalse((self.folder / "summary.json").exists())
        with self.assertRaises(RuntimeError):
            run.stop_requested(self.folder)
        with patch_run(
            "spawn_watchdog",
            side_effect=lambda folder, **_: run.acknowledge_resume(folder, self.job),
        ) as spawn:
            resumed = run.resume_monitoring(self.folder)
        spawn.assert_called_once()
        self.assertFalse(active_handoff(self.folder, self.job))
        self.assertFalse(resumed["cleanup_verified"])
        with self.assertRaises(RuntimeError):
            run.stop_requested(self.folder)

    def test_beacon_bool_without_http_acceptance_cannot_create_a_receipt(self):
        self.job["deadline_epoch"] = time.time() - 1
        self.api.pods.clear()
        storage = Storage("EU-RO-1", FakeS3())
        prefix = "jobs/" + self.job["job_id"] + "/control/"
        storage.put_json(
            "testvolume",
            prefix + "identity.json",
            {"job_id": self.job["job_id"], "pod_id": "testpod"},
        )
        for status in (None, 403, True):
            storage.put_json(
                "testvolume",
                prefix + "self-delete.json",
                {
                    "job_id": self.job["job_id"],
                    "pod_id": "testpod",
                    "delete_accepted": True,
                    "delete_http_status": status,
                },
            )
            self.assertIsNone(run.reconcile_beacon(self.api, self.job, self.folder, storage))
            self.assertFalse((self.folder / "pod-receipt.json").exists())
