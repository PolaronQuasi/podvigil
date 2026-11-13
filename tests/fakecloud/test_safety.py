from pathlib import Path
from unittest.mock import patch
import base64
import copy
import json
import tempfile
import threading
import time
import unittest

from podvigil.controller.ownership import owned_pod
from podvigil.jobspec import validate
from podvigil.podrt import agent as pod_agent
from podvigil.podrt.api import ApiError
from podvigil.podrt.records import read, save
from podvigil.podrt.teardown import delete_verified
from podvigil.settings import ENV_PREFIX
from podvigil.storage import Storage
from tests.support.fakes import FakeApi, FakeS3, NoAgentStorage, pod_for
from tests.support.jobs import fixture, ledger
from tests.support.runner import (
    assert_pod,
    check_catalog,
    cleanup_pod,
    create_once,
    launch,
    locate,
    patch_run,
    payload,
    recover,
    runtime_limit,
    watchdog,
)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-tests-")
        self.folder = Path(self.temp.name)
        self.job = fixture()
        self.api = FakeApi(self.job)

    def tearDown(self):
        self.temp.cleanup()

    def test_reject_community_and_unknown_cloud_alias(self):
        for update in ({"cloud": "COMMUNITY"}, {"cloud": "ALL"}, {"cloudType": "COMMUNITY"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate({**self.job["config"], **update})

    def test_atomic_ledger_write_retries_windows_reader_contention(self):
        from podvigil.podrt.records import _replace_once

        original = _replace_once
        count = [0]

        def replace(source, destination):
            count[0] += 1
            if count[0] == 1:
                raise PermissionError("simulated Windows file reader")
            original(source, destination)

        with patch("podvigil.podrt.records._replace_once", side_effect=replace):
            save(self.folder / "state.json", {"complete": True})
        self.assertEqual(count[0], 2)
        self.assertEqual(read(self.folder / "state.json"), {"complete": True})

    def test_payload_explicit_secure_no_account_key_or_unbounded_entrypoint(self):
        body = payload(self.job, "testvolume")
        self.assertEqual(body["cloud"], "SECURE")
        self.assertNotIn("RUNPOD_API_KEY", body["env"])
        self.assertEqual(body["entrypoint"], ["python3", "-B", "-u", "-c"])
        spec = json.loads(base64.b64decode(body["env"][ENV_PREFIX + "_JOB_SPEC"]))
        self.assertEqual(spec["deadline_epoch"], self.job["deadline_epoch"])
        self.assertEqual(spec["volume_id"], "testvolume")
        self.assertLess(len(json.dumps(body).encode()), 100000)
        compile(body["cmd"][0], "<bootstrap>", "exec")

    def test_payload_rechecks_policy_at_creation_boundary(self):
        self.job["config"]["cloud"] = "COMMUNITY"
        with self.assertRaises(ValueError):
            payload(self.job, "testvolume")

    def test_budget_restricts_lifetime(self):
        cfg = self.job["config"]
        cfg["max_compute_usd"] = 0.1
        self.assertEqual(runtime_limit(cfg), 360)
        cfg["max_compute_usd"] = 0.01
        with self.assertRaises(ValueError):
            runtime_limit(cfg)

    def test_reject_missing_deadline_nan_and_latest(self):
        for update in (
            {"max_runtime_seconds": 0},
            {"max_compute_usd": float("nan")},
            {"image": "runpod/pytorch:latest"},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate({**self.job["config"], **update})

    def test_reject_credential_and_path_escape_inputs(self):
        for remote in ("../secret", "/etc/file", "x\\y", ".env"):
            with self.subTest(remote=remote), self.assertRaises(ValueError):
                validate({**self.job["config"], "inputs": [{"local": "file.py", "remote": remote}]})

    def test_catalog_secure_price_and_availability(self):
        self.assertEqual(check_catalog(self.api, self.job["config"])["cloud"], "SECURE")
        self.assertIn("cloud=SECURE", self.api.calls[0][1])
        self.job["config"]["max_compute_usd_per_hour"] = 0.1
        with self.assertRaises(RuntimeError):
            check_catalog(self.api, self.job["config"])

    def test_actual_nonsecure_pod_rejected(self):
        pod = pod_for(self.job)
        pod["cloud"] = "COMMUNITY"
        with self.assertRaises(RuntimeError):
            assert_pod(pod, self.job)

    def test_delete_retries_then_verifies_absence(self):
        self.api.delete_failures = 2
        delete_verified(
            self.api, "/pods/testpod", lambda p: owned_pod(p, self.job), sleep=lambda _: None
        )
        self.assertFalse(self.api.pods)
        self.assertEqual(self.api.calls[-1], ("GET", "/pods/testpod"))
        self.assertEqual(sum(m == "DELETE" for m, p in self.api.calls), 3)

    def test_accepted_delete_without_absence_is_not_success(self):
        self.api.ignore_delete = True
        with self.assertRaisesRegex(RuntimeError, "NOT verified"):
            delete_verified(
                self.api, "/pods/testpod", lambda p: True, attempts=2, sleep=lambda _: None
            )

    def test_does_not_delete_unowned_or_preexisting_pod(self):
        self.job["prior_pod_ids"] = ["testpod"]
        with self.assertRaises(PermissionError):
            delete_verified(self.api, "/pods/testpod", lambda p: owned_pod(p, self.job))
        self.assertFalse(any(m == "DELETE" for m, p in self.api.calls))

    def test_lost_create_response_reconciles_without_second_post(self):
        save(self.folder / "job.json", self.job)
        save(self.folder / "pod-intent.json", {})
        self.api.pods.clear()
        self.api.post_error = 503
        with self.assertRaises(ApiError):
            create_once(self.api, self.folder, "pod", payload(self.job, "testvolume"))
        self.assertEqual(locate(self.api, self.job, self.folder, "pod"), "testpod")
        cleanup_pod(self.api, self.job, self.folder)
        self.assertEqual(sum(m == "POST" for m, p in self.api.calls), 1)

    def test_ambiguous_absence_not_reported_clean(self):
        self.api.pods.clear()
        save(self.folder / "pod-intent.json", {})
        save(self.folder / "pod-dispatch.json", {"state": "dispatching"})
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            cleanup_pod(self.api, self.job, self.folder)
        self.assertFalse((self.folder / "pod-deleted.json").exists())

    def test_watchdog_enforces_original_deadline_after_controller_crash(self):
        self.job["deadline_epoch"] = time.time() - 1
        ledger(self.folder, self.job, stale=True)
        watchdog(self.folder, self.api, NoAgentStorage(), pause=lambda _: None)
        self.assertFalse(self.api.pods)
        self.assertEqual(read(self.folder / "watchdog.json")["state"], "pod_deleted")

    def test_watchdog_enforces_deadline_with_live_controller(self):
        self.job["deadline_epoch"] = time.time() - 1
        ledger(self.folder, self.job)
        watchdog(self.folder, self.api, NoAgentStorage(), pause=lambda _: None)
        self.assertFalse(self.api.pods)

    def test_watchdog_terminates_bad_cloud_and_startup_failure(self):
        for bad_cloud in (True, False):
            with self.subTest(bad_cloud=bad_cloud):
                self.api = FakeApi(self.job)
                if bad_cloud:
                    self.api.pods["testpod"]["cloud"] = "COMMUNITY"
                else:
                    self.job["startup_deadline_epoch"] = time.time() - 1
                ledger(self.folder, self.job)
                watchdog(self.folder, self.api, NoAgentStorage(), pause=lambda _: None)
                self.assertFalse(self.api.pods)

    def test_backup_verified_before_volume_delete(self):
        ledger(self.folder, self.job)
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        summary = recover(self.folder, self.api, Storage("EU-RO-1", s3))
        self.assertTrue(summary["pod_termination_verified"])
        self.assertTrue(summary["volume_deletion_verified"])
        self.assertTrue((self.folder / "downloads/artifacts/model.bin").exists())
        pod_delete = self.api.calls.index(("DELETE", "/pods/testpod"))
        volume_delete = self.api.calls.index(("DELETE", "/network-volumes/testvolume"))
        self.assertLess(pod_delete, volume_delete)

    def test_corrupt_backup_retains_storage_but_terminates_gpu(self):
        ledger(self.folder, self.job)
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        s3.corrupt_download = True
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            recover(self.folder, self.api, Storage("EU-RO-1", s3))
        self.assertFalse(self.api.pods)
        self.assertIn("testvolume", self.api.volumes)
        self.assertFalse((self.folder / "summary.json").exists())

    def test_missing_manifest_retains_storage(self):
        ledger(self.folder, self.job)
        with self.assertRaisesRegex(RuntimeError, "manifest"):
            recover(self.folder, self.api, Storage("EU-RO-1", FakeS3()))
        self.assertFalse(self.api.pods)
        self.assertIn("testvolume", self.api.volumes)

    def test_download_path_traversal_rejected(self):
        s3 = FakeS3()
        s3.finished(self.job["job_id"])
        key = ("testvolume", "jobs/" + self.job["job_id"] + "/artifact-manifest.json")
        manifest = json.loads(s3.objects[key])
        manifest["files"][0]["path"] = "../escape"
        s3.objects[key] = json.dumps(manifest).encode()
        with self.assertRaises(ValueError):
            Storage("EU-RO-1", s3).collect(self.job, "testvolume", self.folder)

    def test_workload_does_not_inherit_control_credentials(self):
        env = pod_agent.child_environment(
            {
                "RUNPOD_API_KEY": "private",
                "WONDOMON_JOB_SPEC": "spec",
                "AWS_SECRET_ACCESS_KEY": "private",
                "PATH": "abc",
            },
            "/out",
        )
        self.assertEqual(env["PATH"], "abc")
        self.assertEqual(env["WONDOMON_OUTPUT_DIR"], "/out")
        self.assertNotIn("RUNPOD_API_KEY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)

    def test_launch_and_cleanup_end_to_end_with_fake_cloud(self):
        for outcome in ("success", "training_failed"):
            with self.subTest(outcome=outcome):
                api, s3 = FakeApi(), FakeS3()
                storage = Storage("EU-RO-1", s3)
                api.storage = storage

                def finish(start):
                    s3.finished(start["job_id"], outcome)
                    storage.put_json(
                        "testvolume",
                        "jobs/" + start["job_id"] + "/control/self-delete.json",
                        {
                            "job_id": start["job_id"],
                            "pod_id": "testpod",
                            "delete_accepted": True,
                            "delete_http_status": 204,
                        },
                    )
                    api.pods.clear()  # simulate the in-Pod supervisor deleting itself

                s3.on_start = finish

                def start_guard(folder):
                    thread = threading.Thread(
                        target=watchdog,
                        args=(folder, api, storage),
                        kwargs={"pause": lambda _: time.sleep(0.05)},
                        daemon=True,
                    )
                    thread.start()

                    class Monitor:
                        def poll(self):
                            return None if thread.is_alive() else 0

                    return Monitor()

                with (
                    patch_run("Api", return_value=api),
                    patch_run("Storage", return_value=storage),
                    patch_run("load_key", return_value="test"),
                    patch_run("spawn_watchdog", side_effect=start_guard),
                    patch_run("jobs_dir", return_value=self.folder),
                ):
                    result = launch(copy.deepcopy(self.job["config"]))
                self.assertEqual(result["training_outcome"], outcome)
                self.assertFalse(api.pods)
                self.assertFalse(api.volumes)


if __name__ == "__main__":
    unittest.main()
