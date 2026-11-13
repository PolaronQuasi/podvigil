"""Read-only views of jobs (list, logs, cost) and naming a job by ID prefix or `latest` in every job command.

None of them takes a lock, keeps a ledger file open or creates anything, so they are safe while a job runs.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import datetime
import io
import json
import tempfile
import unittest

from podvigil import settings
from podvigil.podrt.records import durable_replace, save
from tests.support import runner as run
from tests.support.jobs import fixture, ledger
from tests.support.runner import patch_run


def snapshot(folder):
    return {
        path.relative_to(folder).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in Path(folder).rglob("*")
    }


def utc(minutes_ago=0):
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    ).isoformat()


class StateFolder(unittest.TestCase):
    """A temporary state folder holding job ledgers."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-views-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        settings.configure(state_dir=self.state)
        self.addCleanup(settings.configure)

    def make(self, job_id, created_minutes_ago=0, **changes):
        job = {**fixture(), "job_id": job_id, "created_utc": utc(created_minutes_ago), **changes}
        folder = self.state / "jobs" / job_id
        ledger(folder, job)
        return folder.resolve()

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run.run_cli(["--state-dir", str(self.state), *arguments])
        settings.configure(state_dir=self.state)  # run_cli resets the options after its command
        return code, out.getvalue(), err.getvalue()


class JobNamingTests(StateFolder):
    def test_a_path_still_names_its_ledger(self):
        folder = self.make("a" * 32)
        self.assertEqual(run.find_job(str(folder)), folder)

    def test_latest_is_the_newest_by_creation_time(self):
        self.make("1" * 32, created_minutes_ago=5)
        newest = self.make("2" * 32, created_minutes_ago=1)
        self.make("3" * 32, created_minutes_ago=9)
        self.assertEqual(run.find_job("latest"), newest)

    def test_a_unique_start_of_the_id(self):
        wanted = self.make("abcd" + "0" * 28)
        self.make("abce" + "0" * 28)
        self.assertEqual(run.find_job("abcd"), wanted)
        self.assertEqual(run.find_job("ABCD"), wanted)

    def test_an_ambiguous_start_is_refused_and_names_the_matches(self):
        self.make("abcd1" + "0" * 27)
        self.make("abcd2" + "0" * 27)
        with self.assertRaisesRegex(ValueError, "2 jobs .* start with abcd") as caught:
            run.find_job("abcd")
        self.assertIn("abcd1", str(caught.exception))

    def test_unknown_names_are_refused_and_nothing_is_created(self):
        for name in ("ffff", "latest"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                run.find_job(name)
        self.assertFalse(self.state.exists())
        with self.assertRaisesRegex(ValueError, "No job ledger at"):
            run.find_job("abc")  # three digits are too few for an ID: a path

    def test_every_job_command_takes_the_short_forms(self):
        folder = self.make("abcd" + "1" * 28)
        seen = []
        with patch_run("recover", side_effect=lambda path: seen.append(path) or {"job_id": "x"}):
            code, out, err = self.cli("recover", "abcd")
        self.assertEqual((code, seen), (0, [folder]))
        self.assertIn("Job " + folder.name + ": " + str(folder), err)
        code, out, _ = self.cli("status", "latest")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out[: out.rindex("}") + 1])["job_id"], folder.name)


class ListTests(StateFolder):
    def test_lists_jobs_newest_first_with_their_state(self):
        old = self.make("1" * 32, created_minutes_ago=30)
        save(old / "summary.json", {"job_id": "1" * 32, "training_outcome": "success"})
        save(old / "pod-deleted.json", {"id": "testpod", "evidence": {"verified": True}})
        new = self.make("2" * 32, created_minutes_ago=1)
        save(new / "training-released.json", {"utc": utc()})
        rows = run.job_list()
        self.assertEqual([row["job_id"] for row in rows], ["2" * 32, "1" * 32])
        self.assertEqual(
            (rows[0]["phase"], rows[0]["pod"], rows[0]["volume"]), ("running", "exists", "exists")
        )
        self.assertEqual((rows[1]["phase"], rows[1]["training_outcome"]), ("finished", "success"))
        code, out, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertIn("2" * 12, out.splitlines()[2])
        code, out, _ = self.cli("list", "--json")
        self.assertEqual([row["job_id"] for row in json.loads(out)["jobs"]], ["2" * 32, "1" * 32])

    def test_an_empty_state_folder_is_reported_and_not_created(self):
        code, out, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertIn("No jobs in", out)
        self.assertFalse(self.state.exists())


class LogsTests(StateFolder):
    def setUp(self):
        super().setUp()
        self.job_id = "c" * 32
        self.folder = self.make(self.job_id)

    def test_shows_the_watchdog_log_the_failure_records_and_the_workload_log(self):
        (self.folder / "watchdog.log").write_text(
            "".join(f"line {n}\n" for n in range(500)), encoding="utf-8"
        )
        save(self.folder / "launch-failure.json", {"phase": "pod_create", "error_type": "ApiError"})
        audit = self.folder / "downloads" / "volume-audit-x"
        save(
            audit / "00000002.bin",
            {"error_type": "RuntimeError", "message": "guard stopped at start-up"},
        )
        save(
            audit / "objects.json",
            {
                "objects": [
                    {
                        "key": "jobs/" + self.job_id + "/control/guard-failure.json",
                        "kind": "control",
                        "local_path": str(audit / "00000002.bin"),
                    }
                ]
            },
        )
        artifacts = self.folder / "downloads" / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "train.log").write_text("epoch 1\nepoch 2\n", encoding="utf-8")
        result = run.logs(self.folder, lines=10)
        self.assertEqual(result["watchdog_log"]["lines"], [f"line {n}" for n in range(490, 500)])
        self.assertTrue(result["watchdog_log"]["truncated"])
        self.assertEqual(result["launch_failure"]["phase"], "pod_create")
        self.assertEqual(
            result["pod_records"]["control/guard-failure.json"]["message"],
            "guard stopped at start-up",
        )
        self.assertEqual(result["workload_log"]["lines"], ["epoch 1", "epoch 2"])
        self.assertEqual(result["not_yet"], [])
        code, out, _ = self.cli("logs", str(self.folder), "--tail", "3")
        self.assertEqual(code, 0)
        self.assertIn("line 499", out)
        self.assertNotIn("line 496", out)
        self.assertIn("guard stopped at start-up", out)
        self.assertIn("epoch 2", out)

    def test_says_what_is_not_available_yet(self):
        result = run.logs(self.folder)
        self.assertIsNone(result["workload_log"])
        self.assertTrue(any("stays on the Pod" in note for note in result["not_yet"]))

    def test_accepted_partial_audit_exposes_workload_log_and_failure_records(self):
        audit = self.folder / "partial-downloads" / "volume-audit-accepted"
        audit.mkdir(parents=True)
        log = audit / "00000000.bin"
        log.write_text("epoch 1\nepoch 2\n", encoding="utf-8")
        failure = audit / "00000001.bin"
        save(failure, {"complete": False, "stage": "copy", "salvaged_files": 1})
        save(
            audit / "objects.json",
            {
                "job_id": self.job_id,
                "objects": [
                    {"key": "jobs/" + self.job_id + "/artifacts/train.log", "local_path": str(log)},
                    {
                        "key": "jobs/" + self.job_id + "/control/finalization-incomplete.json",
                        "local_path": str(failure),
                    },
                ],
            },
        )
        before = snapshot(self.folder)
        result = run.logs(self.folder)
        self.assertEqual(result["workload_log"]["lines"], ["epoch 1", "epoch 2"])
        self.assertEqual(
            result["pod_records"]["control/finalization-incomplete.json"]["stage"], "copy"
        )
        self.assertFalse(any("stays on the Pod" in note for note in result["not_yet"]))
        self.assertEqual(snapshot(self.folder), before)

    def test_partial_audit_cannot_point_logs_outside_the_ledger(self):
        outside = self.state / "outside.log"
        outside.write_text("not a job log\n", encoding="utf-8")
        audit = self.folder / "partial-downloads" / "volume-audit-unsafe"
        save(
            audit / "objects.json",
            {
                "objects": [
                    {
                        "key": "jobs/" + self.job_id + "/artifacts/train.log",
                        "local_path": str(outside),
                    }
                ]
            },
        )
        self.assertIsNone(run.logs(self.folder)["workload_log"])

    def test_a_large_log_is_read_from_its_end_only(self):
        with (self.folder / "watchdog.log").open("w", encoding="utf-8") as stream:
            stream.write("x" * (3 << 20) + "\nlast line\n")
        result = run.logs(self.folder, lines=0)
        self.assertEqual(result["watchdog_log"]["lines"][-1], "last line")
        self.assertLessEqual(
            sum(len(line) + 1 for line in result["watchdog_log"]["lines"]), 1 << 20
        )

    def test_reads_only_and_leaves_no_file_open(self):
        (self.folder / "watchdog.log").write_text("armed\n", encoding="utf-8")
        before = snapshot(self.folder)
        for arguments in (
            ("logs", str(self.folder)),
            ("logs", str(self.folder), "--json"),
            ("cost", str(self.folder)),
            ("list",),
        ):
            self.assertEqual(self.cli(*arguments)[0], 0)
        self.assertEqual(snapshot(self.folder), before)
        # Windows refuses to replace a file another handle keeps open.
        # Verify that every record can still be replaced.
        for path in sorted(self.folder.glob("*.json")) + [self.folder / "watchdog.log"]:
            temporary = path.with_name(path.name + ".new")
            temporary.write_bytes(path.read_bytes())
            durable_replace(temporary, path)


class CostTests(StateFolder):
    def setUp(self):
        super().setUp()
        self.folder = self.make(
            "d" * 32, quote={"checked_utc": utc(31), "cloud": "SECURE", "compute_usd_per_hour": 0.6}
        )
        save(
            self.folder / "pod-receipt.json",
            {
                "id": "testpod",
                "job_id": "d" * 32,
                "kind": "pod",
                "established_by": "create_response",
                "recorded_utc": utc(30),
            },
        )

    def test_a_running_job_is_estimated_until_now(self):
        result = run.cost(self.folder)
        self.assertTrue(result["estimate"])
        self.assertTrue(result["running"])
        self.assertAlmostEqual(result["estimated_compute_usd"], 0.3, delta=0.01)
        self.assertAlmostEqual(result["run_seconds"], 1800, delta=30)
        self.assertIn("not a bill", result["note"])

    def test_a_finished_job_is_estimated_until_the_verified_deletion(self):
        save(
            self.folder / "pod-deleted.json",
            {"id": "testpod", "verified_utc": utc(10), "evidence": {"verified": True}},
        )
        result = run.cost(self.folder)
        self.assertFalse(result["running"])
        self.assertAlmostEqual(result["estimated_compute_usd"], 0.2, delta=0.01)
        code, out, _ = self.cli("cost", str(self.folder))
        self.assertEqual(code, 0)
        self.assertIn("(an estimate, not a bill)", out)
        self.assertIn("0.2000 USD", out)

    def test_no_pod_costs_nothing_and_an_unknown_creation_has_no_estimate(self):
        folder = self.make("e" * 32, quote={"compute_usd_per_hour": 0.6})
        for name in ("pod-receipt.json", "pod-dispatch.json", "pod-intent.json"):
            (folder / name).unlink(missing_ok=True)
        self.assertEqual(run.cost(folder)["estimated_compute_usd"], 0.0)
        save(folder / "pod-dispatch.json", {"state": "dispatching"})
        result = run.cost(folder)
        self.assertIsNone(result["estimated_compute_usd"])
        self.assertIn("may exist and bill", result["explanation"])


if __name__ == "__main__":
    unittest.main()
