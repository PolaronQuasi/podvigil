"""What a run leaves behind for people: the failure reason in the ledger, a useful watchdog.log, and final lines
that say truthfully what exists. Found in the first paid use (2026-11-12): the reason was not stored,
watchdog.log was empty, and "POD CLEANUP NOT VERIFIED" was printed when no Pod had ever existed.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import tempfile
import threading
import time
import unittest

from podvigil.storage import Storage
from podvigil.podrt.records import read, save
from tests.support import runner as run
from tests.support.runner import patch_run
from tests.support.fakes import NO_CAPACITY_DETAIL, FakeApi, FakeS3
from tests.support.jobs import fixture, ledger
from podvigil.settings import ENV_PREFIX

KEY = "dummy-account-key-0123456789"


class AliveMonitor:
    def poll(self):
        return None


class SdkError(Exception):
    """Shaped like a botocore ClientError; its text must never be copied."""

    response = {
        "Error": {"Code": "AccessDenied", "Message": "SECRET-IN-SDK-TEXT"},
        "ResponseMetadata": {"HTTPHeaders": {"authorization": "SECRET-IN-SDK-TEXT"}},
    }

    def __str__(self):
        return "SECRET-IN-SDK-TEXT"


class Cloud:
    """Linked fake API and S3, plus the patches launch needs. Nothing leaves the process."""

    def __init__(self, test, *, watchdog_thread=False):
        self.test = test
        self.api = FakeApi()
        self.s3 = FakeS3(api=self.api)
        self.storage = Storage("EU-RO-1", self.s3)
        self.api.storage = self.storage
        self.jobs = Path(test.temp.name) / "jobs"
        self.threads = []
        self.watchdog_thread = watchdog_thread

    def spawn(self, folder, **_):
        if not self.watchdog_thread:
            return AliveMonitor()
        thread = threading.Thread(
            target=run.watchdog,
            args=(folder, self.api, self.storage),
            kwargs={"pause": lambda _: time.sleep(0.05)},
            daemon=True,
        )
        thread.start()
        self.threads.append(thread)

        class Monitor:
            def poll(self):
                return None if thread.is_alive() else 0

        return Monitor()

    def patches(self):
        return [
            patch_run("Api", return_value=self.api),
            patch_run("Storage", return_value=self.storage),
            patch_run("load_key", return_value=KEY),
            patch_run("spawn_watchdog", side_effect=self.spawn),
            patch_run("jobs_dir", return_value=self.jobs),
        ]

    def cli(self, *arguments):
        """The exit code and the lines for people (standard error); the result JSON (standard output) is
        kept in self.stdout."""
        output, people = io.StringIO(), io.StringIO()
        patches = self.patches()
        for item in patches:
            item.start()
        try:
            with redirect_stdout(output), redirect_stderr(people):
                code = run.run_cli(list(arguments))
        finally:
            for item in reversed(patches):
                item.stop()
        for thread in self.threads:
            thread.join(10)
        self.stdout = output.getvalue()
        return code, people.getvalue().splitlines()

    def folder(self):
        folders = list(self.jobs.iterdir())
        self.test.assertEqual(len(folders), 1)
        return folders[0]


class RunReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-run-reporting-")
        self.addCleanup(self.temp.cleanup)
        self.job_file = Path(self.temp.name) / "job.json"
        self.job_file.write_text(json.dumps(fixture()["config"]), encoding="utf-8")

    def rejected_run(self):
        cloud = Cloud(self)
        cloud.api.pod_rejection = (400, NO_CAPACITY_DETAIL)
        code, lines = cloud.cli("launch", str(self.job_file))
        return cloud, code, lines

    def test_no_pod_ever_existed_final_lines_say_so(self):
        cloud, code, lines = self.rejected_run()
        self.assertEqual(code, run.EXIT_NO_CAPACITY)
        text = "\n".join(lines)
        self.assertNotIn("CLEANUP NOT VERIFIED", text)
        self.assertTrue(lines[-3].startswith("RUN FAILED: Runpod POST /pods: HTTP 400"), lines)
        self.assertEqual(
            lines[-2], "Pod: none was created (Runpod refused the request with HTTP 400)."
        )
        self.assertEqual(
            lines[-1],
            "Volume testvolume: deleted; its absence was verified. Nothing from this job remains.",
        )
        self.assertFalse(cloud.api.volumes)

    def test_failure_record_keeps_the_reason_without_secrets_or_the_request(self):
        cloud, _, _ = self.rejected_run()
        folder = cloud.folder()
        failure = read(folder / "launch-failure.json")
        self.assertEqual(failure["phase"], "pod_create")
        self.assertEqual((failure["error_type"], failure["http_status"]), ("ApiError", 400))
        self.assertIn("no longer any instances available", failure["message"])
        self.assertIsNone(failure["cleanup_error"])
        raw = (folder / "launch-failure.json").read_text()
        for forbidden in (
            KEY,
            "WONDOMON_JOB_SPEC",
            ENV_PREFIX + "_JOB_SPEC",
            "bootstrap",
            "Authorization",
        ):
            self.assertNotIn(forbidden, raw)

    def test_a_credential_inside_an_error_message_is_redacted(self):
        cloud = Cloud(self)
        cloud.spawn = lambda folder, **_: (_ for _ in ()).throw(
            RuntimeError("arming failed for " + KEY)
        )
        code, lines = cloud.cli("launch", str(self.job_file))
        self.assertEqual(code, 1)
        raw = (cloud.folder() / "launch-failure.json").read_text()
        self.assertNotIn(KEY, raw)
        self.assertIn("[redacted]", json.loads(raw)["message"])
        self.assertEqual(
            lines[-2:],
            ["Pod: none was created.", "Volume: none was created. Nothing from this job remains."],
        )

    def test_sdk_errors_keep_only_their_type_and_code(self):
        record = run.public_error(SdkError(), [KEY])
        self.assertEqual(record, {"error_type": "SdkError", "provider_code": "AccessDenied"})
        self.assertEqual(run.public_message(SdkError()), "SdkError")
        wrapped = RuntimeError("Storage read failed")
        wrapped.__cause__ = SdkError()
        self.assertEqual(run.public_error(wrapped)["cause"], {"error_type": "SdkError"})

    def test_watchdog_log_tells_what_happened(self):
        cloud, _, _ = self.rejected_run()
        folder = cloud.folder()
        run.watchdog(folder, cloud.api, cloud.storage, pause=lambda _: None)
        log = (folder / "watchdog.log").read_text(encoding="utf-8")
        for expected in (
            "watchdog armed",
            "no Pod to stop",
            "recovery finished: no_pod_created",
            "watchdog stopped; last state no_pod_created",
            "Pod: none was created (Runpod refused the request with HTTP 400).",
        ):
            self.assertIn(expected, log)
        self.assertNotIn(KEY, log)

    def test_watchdog_logs_a_repeated_error_once_and_keeps_its_message(self):
        job = fixture()
        folder = Path(self.temp.name) / "ledger"
        ledger(folder, job)
        api = FakeApi(job)

        def broken(path, field):
            raise RuntimeError("inventory unavailable for " + KEY)

        api.listing = broken
        api.get_or_none = lambda path: (_ for _ in ()).throw(
            RuntimeError("inventory unavailable for " + KEY)
        )
        polls = [0]

        def pause(_):
            polls[0] += 1
            if polls[0] >= 3:
                raise KeyboardInterrupt()

        run.save(folder / "stop-request.json", {"reason": "test stop"})
        api.key = KEY
        with self.assertRaises(KeyboardInterrupt):
            run.watchdog(folder, api, Storage("EU-RO-1", FakeS3()), pause=pause)
        state = read(folder / "watchdog.json")
        self.assertEqual(state["state"], "observation_or_cleanup_unverified")
        self.assertIn("inventory unavailable", state["message"])
        log = (folder / "watchdog.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("inventory unavailable"), 1)
        self.assertNotIn(KEY, log + json.dumps(state))
        self.assertIn("testpod", api.pods)  # nothing was declared deleted

    def test_state_lines_for_resources_that_remain_or_are_unknown(self):
        job = fixture()
        folder = Path(self.temp.name) / "ledger"
        ledger(folder, job)
        pod, volume = run.state_lines(folder)
        self.assertEqual(
            pod,
            "Pod testpod: may still exist and bill; its deletion is NOT verified. "
            "Run: " + run.program() + " recover " + str(folder),
        )
        run.configure(program="managed_run.py")  # as the checkout's wrapper says it
        self.addCleanup(run.configure)
        self.assertIn("Run: managed_run.py recover ", run.state_lines(folder)[0])
        self.assertTrue(
            volume.startswith("Volume testvolume: still exists and is billed as storage")
        )
        (folder / "pod-receipt.json").unlink()
        self.assertTrue(
            run.state_lines(folder)[0].startswith("Pod: the create request's outcome is unknown")
        )
        save(folder / "pod-deleted.json", {"id": "testpod", "evidence": {"verified": True}})
        save(folder / "pod-receipt.json", {"id": "testpod"})
        self.assertEqual(
            run.state_lines(folder)[0], "Pod testpod: deleted; its absence was verified."
        )
        self.assertNotIn("Nothing from this job remains", run.state_lines(folder)[1])

    def test_failure_before_any_ledger_says_nothing_was_created(self):
        cloud = Cloud(self)
        cloud.api.gpu_catalog[fixture()["config"]["gpu_id"]] = {
            "secure": True,
            "price": {"secure": 0.5},
            "dataCenters": [],
        }
        code, lines = cloud.cli("launch", str(self.job_file))
        self.assertEqual(code, run.EXIT_NO_CAPACITY)
        self.assertTrue(
            lines[-3].startswith("RUN FAILED: No confirmed Secure Cloud capacity"), lines
        )
        self.assertEqual(lines[-2:], run.NOTHING_CREATED)
        self.assertFalse(cloud.jobs.exists())

    def test_successful_run_ends_with_both_deletions_verified(self):
        cloud = Cloud(self, watchdog_thread=True)

        def finish(start):
            cloud.s3.finished(start["job_id"])

        cloud.s3.on_start = finish
        code, lines = cloud.cli("launch", str(self.job_file))
        self.assertEqual(code, 0, lines)
        self.assertEqual(
            lines[-2:],
            [
                "Pod testpod: deleted; its absence was verified.",
                (
                    "Volume testvolume: deleted; its absence was verified. "
                    "Nothing from this job remains."
                ),
            ],
        )
        summary = json.loads(cloud.stdout)  # standard output holds the result JSON alone
        self.assertEqual((summary["outcome"], summary["exit_code"]), ("success", 0))
        self.assertEqual(summary["training_outcome"], "success")
        self.assertFalse((cloud.folder() / "launch-failure.json").exists())


if __name__ == "__main__":
    unittest.main()
