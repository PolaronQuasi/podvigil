"""Named outcomes with stable exit codes (one table, outcomes.OUTCOMES); the result JSON alone on standard output,
progress and the closing lines on standard error; events.jsonl, append-only, with one redaction point.

Runs the command line against the in-process fake cloud with the real watchdog loop in a thread, as the
golden-ledger test does.
"""

from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from pathlib import Path
import io
import json
import tempfile
import threading
import time
import unittest

from podvigil import events, outcomes
from podvigil.podrt.api import ApiError
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import NO_CAPACITY_DETAIL, FakeApi, FakeS3
from tests.support.jobs import fixture, ledger
from tests.support.runner import patch_run

KEY = "dummy-outcome-account-key-0123456789"


class Cloud:
    def __init__(self, root, watchdog=True):
        self.api = FakeApi()
        self.s3 = FakeS3(api=self.api)
        self.storage = Storage("EU-RO-1", self.s3)
        self.api.storage = self.storage
        self.jobs = Path(root) / "jobs"
        self.watchdog = watchdog
        self.threads = []

    def spawn(self, folder, **_):
        if not self.watchdog:

            class Alive:
                pid = 1

                def poll(self):
                    return None

            return Alive()
        thread = threading.Thread(
            target=run.watchdog,
            args=(folder, self.api, self.storage),
            kwargs={"pause": lambda _: time.sleep(0.05)},
            daemon=True,
        )
        thread.start()
        self.threads.append(thread)

        class Monitor:
            pid = 4242

            def poll(self):
                return None if thread.is_alive() else 0

        return Monitor()

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        patches = [
            patch_run("Api", return_value=self.api),
            patch_run("Storage", return_value=self.storage),
            patch_run("load_key", return_value=KEY),
            patch_run("spawn_watchdog", side_effect=self.spawn),
            patch_run("jobs_dir", return_value=self.jobs),
        ]
        for item in patches:
            item.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = run.run_cli(list(arguments))
        finally:
            for item in reversed(patches):
                item.stop()
        for thread in self.threads:
            thread.join(20)
        return code, out.getvalue(), err.getvalue().splitlines()

    def folder(self):
        return next(self.jobs.iterdir())


class OutcomeTableTests(unittest.TestCase):
    def test_one_table_with_unique_names_and_codes(self):
        names = [name for name, _, _ in outcomes.OUTCOMES]
        codes = [code for _, code, _ in outcomes.OUTCOMES]
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(len(set(codes)), len(codes))
        # The codes that existed before the table keep their meaning.
        self.assertEqual(
            {
                name: outcomes.EXIT_CODES[name]
                for name in (
                    "success",
                    "error",
                    "workload_failed",
                    "probe_failed",
                    "no_capacity",
                    "interrupted",
                )
            },
            {
                "success": 0,
                "error": 1,
                "workload_failed": 2,
                "probe_failed": 3,
                "no_capacity": 4,
                "interrupted": 130,
            },
        )
        self.assertEqual(run.EXIT_NO_CAPACITY, 4)
        # The usage error code is no longer shared with workload_failed.
        self.assertNotIn(2, [outcomes.EXIT_CODES["usage_error"]])
        self.assertEqual(outcomes.EXIT_CODES["wait_timeout"], 14)
        # Opting into capacity rounds keeps the established outcomes and their codes.
        self.assertEqual(outcomes.EXIT_CODES["no_capacity"], 4)
        self.assertEqual(sorted(outcomes.EXIT_CODES.values()), [*range(15), 64, 130])

    def test_a_workload_stopped_by_cancel(self):
        # F2: The Pod stops the workload and publishes the "cancelled" outcome.
        # A foreground launch reports it.
        self.assertEqual(
            outcomes.of_result("launch", {"training_outcome": "cancelled"}), "cancelled"
        )
        self.assertEqual(
            outcomes.of_result(
                "cancel", {"state": "compute_stopped", "training_outcome": "cancelled"}
            ),
            "success",
        )
        self.assertEqual(
            outcomes.of_result("launch", {"training_outcome": "time_limit"}), "workload_failed"
        )


class StreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-outcomes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.job_file = self.root / "job.json"
        self.job_file.write_text(json.dumps(fixture()["config"]), encoding="utf-8")

    def launch(self, cloud, *options):
        return cloud.cli("launch", *options, str(self.job_file))

    def test_success_prints_only_the_result_json_and_the_lines_on_stderr(self):
        cloud = Cloud(self.root)
        cloud.s3.on_start = lambda start: cloud.s3.finished(start["job_id"])
        code, out, err = self.launch(cloud)
        result = json.loads(out)  # standard output is exactly one JSON document
        self.assertEqual((code, result["outcome"], result["exit_code"]), (0, "success", 0))
        self.assertEqual(result["training_outcome"], "success")
        self.assertTrue(err[0].startswith("Job ledger: "))
        self.assertEqual(
            err[-2:],
            [
                "Pod testpod: deleted; its absence was verified.",
                "Volume testvolume: deleted; its absence was verified. Nothing from this job remains.",
            ],
        )

    def test_no_capacity_and_a_failure_document(self):
        cloud = Cloud(self.root)
        cloud.api.pod_rejection = (400, NO_CAPACITY_DETAIL)
        code, out, err = self.launch(cloud)
        result = json.loads(out)
        self.assertEqual((code, result["outcome"]), (4, "no_capacity"))
        self.assertIn("no longer any instances available", result["error"])
        self.assertEqual(
            (result["pod"]["state"], result["volume"]["state"]), ("rejected", "deleted")
        )
        self.assertTrue(err[-3].startswith("RUN FAILED: Runpod POST /pods: HTTP 400"))
        self.assertEqual(outcomes.of_ended_job(cloud.folder()), result["outcome"])
        failure = json.loads((cloud.folder() / "launch-failure.json").read_text())
        self.assertEqual(failure["outcome"], result["outcome"])

    def test_a_refusal_for_another_reason_is_rejected(self):
        cloud = Cloud(self.root)
        cloud.api.pod_rejection = (422, "The image name is not valid.")
        code, out, _ = self.launch(cloud)
        self.assertEqual((code, json.loads(out)["outcome"]), (5, "rejected"))

    def test_an_ambiguous_creation(self):
        cloud = Cloud(self.root, watchdog=False)
        original = cloud.api.request

        def request(method, path, body=None):
            if (method, path) == ("POST", "/pods"):
                raise ApiError(500, method, path)  # no answer, and no Pod visible in any listing
            return original(method, path, body)

        cloud.api.request = request
        code, out, _ = self.launch(cloud)
        result = json.loads(out)
        self.assertEqual((code, result["outcome"]), (6, "ambiguous"))
        self.assertEqual(result["pod"]["state"], "unknown")

    def test_a_pod_whose_deletion_is_not_verified(self):
        cloud = Cloud(self.root, watchdog=False)
        cloud.api.delete_failures = 100  # every DELETE is refused with a 503
        with patch_run("supervise", side_effect=RuntimeError("supervision lost")):
            code, out, _ = self.launch(cloud)
        result = json.loads(out)
        self.assertEqual((code, result["outcome"]), (7, "cleanup_unverified"))
        self.assertEqual(result["pod"]["state"], "exists")
        self.assertIn("testpod", cloud.api.pods)

    def test_usage_errors_have_their_own_code(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run.run_cli(["launch"])  # the job file is missing
        self.assertEqual(code, 64)
        self.assertEqual(json.loads(out.getvalue())["outcome"], "usage_error")
        self.assertIn("usage:", err.getvalue())

    def test_views_print_text_and_json_on_request(self):
        folder = self.root / "ledger"
        ledger(folder, fixture())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(run.run_cli(["cost", str(folder)]), 0)
        self.assertIn("no compute cost estimate", out.getvalue())
        self.assertEqual(err.getvalue().splitlines()[-2:], run.state_lines(folder))
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            run.run_cli(["status", str(folder)])
        self.assertEqual(json.loads(out.getvalue())["outcome"], "success")


class EventTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-events-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "ledger"
        ledger(self.folder, fixture())

    def test_a_launch_leaves_the_story_in_order(self):
        cloud = Cloud(self.root)
        cloud.s3.on_start = lambda start: cloud.s3.finished(start["job_id"])
        job_file = self.root / "job.json"
        job_file.write_text(json.dumps(fixture()["config"]), encoding="utf-8")
        code, _, _ = cloud.cli("launch", str(job_file))
        self.assertEqual(code, 0)
        story = events.read_events(cloud.folder(), 0)
        names = [item["event"] for item in story]
        expected = [
            "job_created",
            "watchdog_armed",
            "created",
            "inputs_uploaded",
            "workload_released",
            "pod_deletion_verified",
            "volume_deletion_verified",
            "recovered",
        ]
        positions = [names.index(name) for name in expected]  # each first seen in this order
        self.assertEqual(positions, sorted(positions), names)
        self.assertEqual(
            [item["kind"] for item in story if item["event"] == "created"], ["volume", "pod"]
        )
        self.assertTrue(all({"utc", "event", "pid", "command"} <= set(item) for item in story))
        self.assertEqual({item["command"] for item in story}, {"launch"})
        self.assertNotIn(KEY, (cloud.folder() / events.FILE).read_text(encoding="ascii"))

    def test_the_one_redaction_point(self):
        events._process["secrets"] = None
        with patch_run(
            "resolve_credentials",
            return_value={
                "RUNPOD_API_KEY": KEY,
                "RUNPOD_S3_ACCESS_KEY": None,
                "RUNPOD_S3_SECRET_KEY": "s3-secret-value-xyz",
            },
        ):
            self.assertTrue(
                events.record(
                    self.folder,
                    "test",
                    job_id="a" * 32,
                    pod_id=KEY,
                    message="failed with " + KEY + " and s3-secret-value-xyz; Bearer abc.def",
                    nested={"detail": "api_key=" + KEY},
                )
            )
        events._process["secrets"] = None
        raw = (self.folder / events.FILE).read_text(encoding="ascii")
        self.assertNotIn(KEY, raw)
        self.assertNotIn("s3-secret-value-xyz", raw)
        self.assertNotIn("abc.def", raw)
        item = events.read_events(self.folder)[-1]
        self.assertEqual(item["job_id"], "a" * 32)  # IDs stay readable
        self.assertNotEqual(item["pod_id"], KEY)  # a secret is never kept, even in an ID field
        # Names set by the code stay readable even when they are long.
        # Long tokens in free text are redacted.
        events.record(
            self.folder,
            "volume_deletion_verified",
            training_outcome="agent_preflight_failed",
            error_type="SelfCleanupUnverified",
            message="token abcdefghijklmnopqrstuvwxyz0123",
        )
        item = events.read_events(self.folder)[-1]
        self.assertEqual(
            (item["event"], item["training_outcome"], item["error_type"]),
            ("volume_deletion_verified", "agent_preflight_failed", "SelfCleanupUnverified"),
        )
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123", item["message"])

    def test_appends_from_two_writers_keep_every_line_whole(self):
        def write(name):
            for index in range(200):
                events.record(self.folder, name, index=index, message="x" * 300)

        threads = [threading.Thread(target=write, args=(name,)) for name in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        rows = (self.folder / events.FILE).read_text(encoding="ascii").splitlines()
        self.assertEqual(len(rows), 400)
        self.assertEqual(
            sorted(json.loads(row)["event"] for row in rows), ["one"] * 200 + ["two"] * 200
        )

    def test_a_passing_error_is_retried_without_writing_the_line_twice(self):
        real, calls = events._append, []

        def flaky(path, data):
            # Windows can raise a sharing violation while another program opens the file.
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(13, "The process cannot access the file")
            return real(path, data)

        with patch.object(events, "_append", flaky):
            self.assertTrue(events.record(self.folder, "retried"))
        names = [item["event"] for item in events.read_events(self.folder, 0)]
        self.assertEqual((names.count("retried"), len(calls)), (1, 2))

    def test_never_creates_a_ledger_and_never_raises(self):
        self.assertFalse(events.record(self.root / "missing", "test"))
        self.assertFalse((self.root / "missing").exists())
        # Unknown types become their name.
        self.assertTrue(events.record(self.folder, "test", value=object()))

    def test_logs_shows_the_latest_events(self):
        events.record(self.folder, "watchdog_started")
        self.assertEqual(run.logs(self.folder)["events"][-1]["event"], "watchdog_started")


if __name__ == "__main__":
    unittest.main()
