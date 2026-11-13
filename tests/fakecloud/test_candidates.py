"""Candidate fallback: the next GPU and data center only after a definite "no capacity" answer and only after
the previous attempt's volume deletion is verified. Any ambiguous answer stops the launch; nothing is retried.

The first user's wrapper (`launch_first_available.sh`) grepped message text to do this by hand.
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
from podvigil.podrt.records import read
from tests.support import runner as run
from tests.support.runner import patch_run
from podvigil.jobspec import read_job
from podvigil.podrt.api import ApiError
from tests.support.fakes import NO_CAPACITY_DETAIL, FakeApi, FakeS3
from tests.support.paths import ROOT

FIRST, SECOND, THIRD = (
    "NVIDIA GeForce RTX 3090",
    "NVIDIA RTX PRO 4500 Blackwell",
    "NVIDIA GeForce RTX 4090",
)


class Alive:
    def poll(self):
        return None


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-candidates-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = FakeApi()
        self.s3 = FakeS3(api=self.api)
        self.storage = Storage("EU-RO-1", self.s3)
        self.api.storage = self.storage
        self.jobs = self.root / "ledgers"
        self.threads = []

    def job_file(self, candidates, **changes):
        config = json.loads((ROOT / "jobs" / "smoke.example.json").read_text())
        config.pop("gpu_id")
        config.pop("data_center_id")
        config.update(
            candidates=candidates,
            self_termination_probe=False,
            poll_seconds=2,
            controller_stale_seconds=12,
            agent_stale_seconds=12,
            **changes,
        )
        path = self.root / "job.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def configs(self, count=3):
        return read_job(
            self.job_file(
                [
                    {"gpu_id": gpu, "data_center_id": "EU-RO-1"}
                    for gpu in (FIRST, SECOND, THIRD)[:count]
                ]
            )
        )

    def spawn(self, folder, **_):
        """A real watchdog loop in a thread, as the detached watchdog would run it."""
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

    def launch(self, configs, *, watchdogs=False, cleanup_wait=5):
        output = io.StringIO()
        spawn = self.spawn if watchdogs else (lambda folder, **_: Alive())
        with (
            patch_run("Api", return_value=self.api),
            patch_run("Storage", return_value=self.storage),
            patch_run("load_key", return_value="dummy-test-key"),
            patch_run("spawn_watchdog", side_effect=spawn),
            patch_run("jobs_dir", return_value=self.jobs),
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            try:
                result, error = run.launch_candidates(configs, cleanup_wait=cleanup_wait), None
            except Exception as exc:
                result, error = None, exc
        for thread in self.threads:
            thread.join(10)
        return result, error, output.getvalue()

    def pod_posts(self):
        return [path for method, path in self.api.calls if method == "POST" and path == "/pods"]

    def ledgers(self):
        return (
            sorted(self.jobs.iterdir(), key=lambda folder: read(folder / "job.json")["created_utc"])
            if self.jobs.exists()
            else []
        )

    def no_capacity_for(self, *gpus, status=400, detail=NO_CAPACITY_DETAIL):
        self.api.pod_rejection = lambda body: (
            (status, detail) if body["gpu"]["id"] in gpus else None
        )

    def test_job_file_candidates_share_everything_else(self):
        configs = read_job(
            self.job_file(
                [
                    {
                        "gpu_id": FIRST,
                        "data_center_id": "EU-CZ-1",
                        "max_compute_usd_per_hour": 0.56,
                        "max_compute_usd": 0.1,
                    },
                    {"gpu_id": SECOND, "data_center_id": "EU-RO-1"},
                ]
            )
        )
        self.assertEqual(
            [(c["gpu_id"], c["data_center_id"], c["max_compute_usd_per_hour"]) for c in configs],
            [(FIRST, "EU-CZ-1", 0.56), (SECOND, "EU-RO-1", 1.0)],
        )
        self.assertEqual(configs[0]["command"], configs[1]["command"])
        plan = run.plan_job(configs)
        self.assertEqual([c["gpu"] for c in plan["candidates"]], [FIRST, SECOND])
        self.assertIn("definite no-capacity", plan["fallback"])
        self.assertFalse(plan["paid_resources_created"])

    def test_job_file_candidate_errors(self):
        good = {"gpu_id": FIRST, "data_center_id": "EU-RO-1"}
        cases = {
            "not at the top level": ([good], {"gpu_id": FIRST}),
            "may also set only": ([{**good, "cloud": "COMMUNITY"}], {}),
            "repeats an earlier": ([good, dict(good)], {}),
            "1 to 8 entries": ([], {}),
            "documented Runpod S3 data center": (
                [{"gpu_id": FIRST, "data_center_id": "US-TX-3"}],
                {},
            ),
            "gpu_id must be nonempty": (
                [{"gpu_id": ["not", "a", "string"], "data_center_id": "EU-RO-1"}],
                {},
            ),
        }
        for expected, (candidates, changes) in cases.items():
            with self.subTest(expected=expected):
                path = self.job_file(candidates)
                if changes:
                    config = json.loads(path.read_text())
                    config.update(changes)
                    path.write_text(json.dumps(config))
                with self.assertRaisesRegex(ValueError, expected):
                    read_job(path)

    def test_catalog_no_capacity_moves_on_without_creating_anything(self):
        self.api.gpu_catalog[FIRST] = {
            "id": FIRST,
            "secure": True,
            "price": {"secure": 0.3},
            "dataCenters": [],
        }
        self.s3.on_start = lambda start: self.s3.finished(start["job_id"])
        result, error, output = self.launch(self.configs(2), watchdogs=True)
        self.assertIsNone(error, output)
        self.assertEqual(result["training_outcome"], "success")
        self.assertIn("No capacity for candidate 1 of 2 (" + FIRST + " in EU-RO-1)", output)
        self.assertIn("Nothing was created. Trying the next candidate.", output)
        self.assertEqual(len(self.ledgers()), 1)  # the first candidate never got a ledger
        self.assertEqual(read(self.ledgers()[0] / "job.json")["config"]["gpu_id"], SECOND)
        self.assertFalse(self.api.pods)
        self.assertFalse(self.api.volumes)

    def test_refused_pod_moves_on_only_after_the_volume_deletion_is_verified(self):
        self.no_capacity_for(FIRST)
        self.s3.on_start = lambda start: self.s3.finished(start["job_id"])
        result, error, output = self.launch(self.configs(2), watchdogs=True)
        self.assertIsNone(error, output)
        self.assertEqual(result["training_outcome"], "success")
        first, second = self.ledgers()
        self.assertEqual(
            run.resource_state(first, read(first / "job.json"), "pod")["state"], "rejected"
        )
        self.assertTrue(run.deletion_verified(read(first / "volume-deleted.json"), "testvolume"))
        self.assertLess(
            read(first / "volume-deleted.json")["verified_utc"],
            read(second / "job.json")["created_utc"],
        )
        self.assertIn("No Pod was created and the volume deletion was verified.", output)
        self.assertEqual(len(self.pod_posts()), 2)  # one request per candidate, never one repeated

    def test_403_no_pool_access_moves_on(self):
        self.no_capacity_for(
            FIRST, status=403, detail="Your account cannot access the requested pool."
        )
        self.s3.on_start = lambda start: self.s3.finished(start["job_id"])
        result, error, output = self.launch(self.configs(2), watchdogs=True)
        self.assertIsNone(error, output)
        self.assertEqual(result["training_outcome"], "success")
        self.assertEqual(len(self.ledgers()), 2)
        self.assertEqual(read(self.ledgers()[1] / "job.json")["config"]["gpu_id"], SECOND)

    def test_ambiguous_or_other_answers_stop_the_loop(self):
        cases = {
            "server error": (503, "Service Unavailable"),
            "network failure": (0, None),
            "rate limit": (429, "Too Many Requests"),
            "rule violation": (400, "disk must be at least 40 GB"),
            "no balance": (402, "Insufficient balance"),
        }
        for name, (status, detail) in cases.items():
            with self.subTest(case=name):
                self.setUp()
                self.no_capacity_for(FIRST, SECOND, THIRD, status=status, detail=detail)
                result, error, output = self.launch(self.configs(3))
                self.assertIsNone(result)
                self.assertIsInstance(error, ApiError)
                self.assertEqual(error.status, status)
                self.assertEqual(len(self.ledgers()), 1)  # the second candidate was never tried
                self.assertEqual(len(self.pod_posts()), 1)  # and the request was never repeated
                self.assertNotIn("Trying the next candidate", output)
                self.temp.cleanup()

    def test_unverified_volume_deletion_stops_the_loop(self):
        def refuse(body):
            # An object nobody accounts for makes the audit keep the first volume.
            self.s3.objects[("testvolume", "foreign.bin")] = b"unexpected"
            return (400, NO_CAPACITY_DETAIL) if body["gpu"]["id"] == FIRST else None

        self.api.pod_rejection = refuse
        result, error, output = self.launch(self.configs(2), cleanup_wait=0)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("volume deletion is not verified", str(error))
        self.assertEqual(len(self.ledgers()), 1)
        self.assertIn("testvolume", self.api.volumes)
        self.assertTrue(
            run.state_lines(self.ledgers()[0])[1].startswith("Volume testvolume: still exists")
        )

    def test_all_candidates_without_capacity_end_with_exit_code_four(self):
        self.no_capacity_for(FIRST, SECOND, THIRD)
        path = self.job_file(
            [{"gpu_id": gpu, "data_center_id": "EU-RO-1"} for gpu in (FIRST, SECOND, THIRD)]
        )
        output = io.StringIO()
        with (
            patch_run("Api", return_value=self.api),
            patch_run("Storage", return_value=self.storage),
            patch_run("load_key", return_value="dummy-test-key"),
            patch_run("spawn_watchdog", side_effect=lambda folder, **_: Alive()),
            patch_run("jobs_dir", return_value=self.jobs),
            redirect_stdout(io.StringIO()),
            redirect_stderr(output),
        ):
            code = run.run_cli(["launch", str(path)])
        lines = output.getvalue().splitlines()
        self.assertEqual(code, run.EXIT_NO_CAPACITY)
        self.assertTrue(
            lines[-3].startswith("RUN FAILED: None of the 3 candidates had capacity."), lines
        )
        self.assertEqual(
            lines[-2], "Pod: none was created (Runpod refused the request with HTTP 400)."
        )
        self.assertTrue(lines[-1].endswith("Nothing from this job remains."))
        self.assertEqual(len(self.ledgers()), 3)
        self.assertFalse(self.api.volumes)

    def test_a_single_candidate_keeps_its_original_error(self):
        self.no_capacity_for(FIRST)
        result, error, output = self.launch(self.configs(1))
        self.assertIsInstance(error, ApiError)
        self.assertNotIn("Trying", output)
        self.assertTrue(run.definite_no_capacity(error))

    def test_the_example_job_files_plan_offline(self):
        for name, count in (("smoke.example.json", 1), ("candidates.example.json", 3)):
            with self.subTest(job=name):
                configs = read_job(ROOT / "jobs" / name)
                self.assertEqual(len(configs), count)
                result = run.plan_job(configs)
                self.assertFalse(result["paid_resources_created"])
                self.assertEqual(len(result.get("candidates", [None])), count)

    def test_definite_no_capacity_is_narrow(self):
        self.assertTrue(run.definite_no_capacity(run.NoCapacity("catalog")))
        self.assertTrue(
            run.definite_no_capacity(
                ApiError(400, "POST", "/pods", title="Bad Request", detail=NO_CAPACITY_DETAIL)
            )
        )
        for exc in (
            ApiError(400, "POST", "/network-volumes", detail=NO_CAPACITY_DETAIL),
            ApiError(400, "GET", "/pods", detail=NO_CAPACITY_DETAIL),
            ApiError(0, "POST", "/pods", not_sent=True),
            ApiError(500, "POST", "/pods"),
            ApiError(404, "POST", "/pods"),
            RuntimeError("no longer any instances available"),
            TimeoutError(),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertFalse(run.definite_no_capacity(exc))


if __name__ == "__main__":
    unittest.main()
