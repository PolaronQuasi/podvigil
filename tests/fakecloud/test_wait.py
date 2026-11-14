"""Read-only waiting against simulated lifecycle records; no provider or credential access."""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import json
import tempfile
import unittest

from podvigil import cli, outcomes, waiting
from podvigil.podrt.records import save
from podvigil.status import status
from tests.support.clock import Clock
from tests.support.jobs import fixture, ledger
from tests.support.runner import patch_run


class WaitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = Clock()
        self.job = fixture()
        self.folder = self.root / "jobs" / self.job["job_id"]
        ledger(self.folder, self.job)
        save(self.folder / "training-released.json", {})
        self.heartbeat()

    def heartbeat(self, **changes):
        save(
            self.folder / "watchdog.json",
            {
                "state": "armed",
                "heartbeat_epoch": self.clock.time(),
                **changes,
            },
        )

    def finish(self, training="success", **fields):
        for kind, identity in (("pod", "testpod"), ("volume", "testvolume")):
            save(
                self.folder / (kind + "-deleted.json"),
                {
                    "id": identity,
                    "evidence": {"verified": True},
                },
            )
        save(self.folder / "summary.json", {"training_outcome": training, **fields})

    def invoke(self, *options, job=None):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("time.time", self.clock.time),
            patch("time.monotonic", self.clock.monotonic),
            patch("time.sleep", self.clock.sleep),
            patch_run("Api", side_effect=AssertionError("API must not be constructed")),
            patch_run("Storage", side_effect=AssertionError("S3 must not be constructed")),
            patch_run("load_key", side_effect=AssertionError("Credentials must not be read")),
            patch("podvigil.events.record", side_effect=AssertionError("No ledger writes")),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = cli.run_cli(
                [
                    "--state-dir",
                    str(self.root),
                    "wait",
                    job or str(self.folder),
                    *options,
                ]
            )
            expected_status = status(self.folder)
        result = json.loads(out.getvalue())
        if code != 64:
            self.assertEqual(
                {k: v for k, v in result.items() if k not in ("outcome", "exit_code")},
                expected_status,
            )
        self.assertEqual(result["exit_code"], code)
        return code, result, err.getvalue()

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_resolves_folder_latest_and_prefix_like_status(self):
        self.finish()
        before = self.snapshot()
        for name in (str(self.folder), "latest", self.job["job_id"][:4]):
            with self.subTest(job=name):
                self.assertEqual(self.invoke(job=name)[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.clock.sleeps, [])

    def test_foreground_workload_outcomes_and_probe(self):
        for training in (
            "success",
            "training_failed",
            "time_limit",
            "not_started",
            "cancelled",
            "outputs_lost",
            "partial_outputs",
        ):
            with self.subTest(training=training):
                self.finish(training)
                code, result, _ = self.invoke("--timeout", "0")
                expected = outcomes.of_result(
                    "launch", {"training_outcome": training}, [self.job["config"]]
                )
                self.assertEqual(
                    (code, result["outcome"]), (outcomes.EXIT_CODES[expected], expected)
                )
        self.job["config"]["self_termination_probe"] = True
        save(self.folder / "job.json", self.job)
        self.finish()
        self.assertEqual(self.invoke()[0], 3)
        self.finish(self_termination_probe={"passed": True})
        self.assertEqual(self.invoke()[0], 0)

    def test_launch_failure_takes_precedence_over_cleanup_summary(self):
        self.finish("not_started")
        for outcome in (
            "no_capacity",
            "rejected",
            "ambiguous",
            "cleanup_unverified",
            "storage_retained",
            "cancelled",
            "interrupted",
            "error",
        ):
            with self.subTest(outcome=outcome):
                save(self.folder / "launch-failure.json", {"outcome": outcome})
                self.assertEqual(self.invoke()[0], outcomes.EXIT_CODES[outcome])
        self.assertEqual(self.clock.sleeps, [])

    def test_legacy_failed_launch_returns_without_a_watchdog(self):
        self.finish("not_started")
        for error_type, code in (
            ("NoCapacity", 4),
            ("RuntimeError", 1),
            ("KeyboardInterrupt", 130),
        ):
            save(self.folder / "launch-failure.json", {"error_type": error_type})
            self.assertEqual(self.invoke()[0], code)

    def test_failure_without_summary_is_terminal(self):
        save(self.folder / "launch-failure.json", {"error_type": "RuntimeError"})
        code, result, _ = self.invoke()
        self.assertEqual((code, result["phase"], result["ended"]), (7, "failed", True))
        self.assertFalse(self.clock.sleeps)

    def test_legacy_create_failure_requires_structured_evidence(self):
        self.finish("not_started")
        (self.folder / "pod-receipt.json").unlink()
        failure = {
            "error_type": "ApiError",
            "method": "POST",
            "path": "/pods",
            "http_status": 400,
            "message": "no longer any instances available",
        }
        save(self.folder / "launch-failure.json", failure)
        self.assertEqual(self.invoke()[0], 6)
        save(
            self.folder / "pod-rejected.json",
            {
                "job_id": self.job["job_id"],
                "kind": "pod",
                "status": 400,
            },
        )
        self.assertEqual(self.invoke()[0], 4)
        save(self.folder / "launch-failure.json", {**failure, "message": "invalid request"})
        self.assertEqual(self.invoke()[0], 5)

    def test_missing_watchdog_never_waits_forever(self):
        (self.folder / "watchdog.json").unlink()
        self.assertEqual(self.invoke()[0], 7)
        self.assertFalse(self.clock.sleeps)

    def test_recovery_handoff_retaining_storage_is_terminal(self):
        save(self.folder / "pod-deleted.json", {"id": "testpod", "evidence": {"verified": True}})
        save(self.folder / "recovery-handoff.json", {"state": "storage_retained"})
        code, result, _ = self.invoke()
        self.assertEqual((code, result["phase"], result["ended"]), (8, "recovery failed", True))
        self.assertFalse(self.clock.sleeps)

    def test_complete_handoff_without_summary_never_invents_success(self):
        self.finish()
        (self.folder / "summary.json").unlink()
        save(self.folder / "recovery-handoff.json", {"state": "complete"})
        self.assertEqual(self.invoke()[0], 1)

    def test_malformed_or_nonterminal_records_do_not_end_job(self):
        for value in ([], "complete", {"state": "in_progress"}, {}):
            save(self.folder / "summary.json", [])
            save(self.folder / "launch-failure.json", [])
            save(self.folder / "recovery-handoff.json", value)
            code, result, _ = self.invoke("--timeout", "0")
            self.assertEqual(code, 14)
            self.assertFalse(result["ended"])

    def test_waits_through_recovery_and_reports_only_phase_changes(self):
        def update():
            if len(self.clock.sleeps) == 2:
                save(
                    self.folder / "pod-deleted.json",
                    {"id": "testpod", "evidence": {"verified": True}},
                )
            if len(self.clock.sleeps) == 4:
                self.finish()

        self.clock.on_sleep = update
        code, _, error = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(
            [line for line in error.splitlines() if line.startswith("Job phase:")],
            ["Job phase: running.", "Job phase: recovering results.", "Job phase: finished."],
        )
        self.assertEqual(len(self.clock.sleeps), 4)

    def test_dead_watchdog_returns_existing_resource_outcome(self):
        for watch in (
            {"heartbeat_epoch": self.clock.time() - 301},
            {"state": "complete"},
            {"heartbeat_epoch": self.clock.time() + 1},
            {"heartbeat_epoch": None},
        ):
            with self.subTest(watch=watch):
                self.heartbeat(**watch)
                before = self.snapshot()
                code, result, error = self.invoke()
                self.assertEqual(code, 7)
                self.assertEqual(result["pod"]["state"], "exists")
                self.assertIn("watchdog is no longer heard", error)
                self.assertIn("may still exist and bill", error)
                self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.clock.sleeps)

    def test_watchdog_dies_while_waiting(self):
        self.clock.on_sleep = lambda: self.heartbeat(state="observation_or_cleanup_unverified")
        self.assertEqual(self.invoke()[0], 7)
        self.assertEqual(len(self.clock.sleeps), 1)

    def test_terminal_transition_wins_over_dead_watchdog(self):
        def stopped(_):
            self.finish()
            return False

        with patch.object(waiting, "watchdog_heard", side_effect=stopped):
            self.assertEqual(self.invoke()[0], 0)

    def test_fractional_timeout_is_bounded_and_read_only(self):
        before = self.snapshot()
        code, result, error = self.invoke("--timeout", "0.01")
        self.assertEqual((code, result["outcome"]), (14, "wait_timeout"))
        self.assertAlmostEqual(self.clock.elapsed, 0.6)
        self.assertIn("job is unaffected", error)
        self.assertEqual(self.snapshot(), before)

    def test_completion_at_timeout_wins(self):
        self.clock.on_sleep = self.finish
        self.assertEqual(self.invoke("--timeout", "0.01")[0], 0)

    def test_ctrl_c_only_interrupts_wait_and_prints_status(self):
        def interrupt():
            raise KeyboardInterrupt

        self.clock.on_sleep = interrupt
        before = self.snapshot()
        code, result, error = self.invoke()
        self.assertEqual((code, result["outcome"]), (130, "interrupted"))
        self.assertIn("Waiting interrupted; the job is unaffected.", error)
        self.assertNotIn("cleanup requested", error)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_timeout_is_usage_error(self):
        before = self.snapshot()
        for value in ("-1", "nan", "inf", "1e308", "invalid"):
            with self.subTest(value=value):
                self.assertEqual(self.invoke("--timeout=" + value)[0], 64)
        self.assertEqual(self.snapshot(), before)
