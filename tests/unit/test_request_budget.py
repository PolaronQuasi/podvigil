"""The Pod request, which Runpod caps at 102,400 bytes, is measured before anything is created.

Found in the first paid use (2026-11-12): about 300 input files overflowed the request, and the error came only
after the volume was created and every input uploaded, naming the wrong cause. The input list now travels as a
hashed manifest on the volume (test_input_manifest), so only the Pod code, the settings and the Pods already in
the account count. `plan` and `launch` still measure the request before anything is created.
"""

from pathlib import Path
from unittest.mock import MagicMock
import copy
import json
import tempfile
import time
import unittest

from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.runner import patch_run
from podvigil.jobspec import read_config
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture
from tests.support.paths import ROOT


def job_file(folder, count, name_length=40):
    """A job file with `count` explicit one-byte inputs."""
    config = json.loads((ROOT / "jobs" / "smoke.example.json").read_text())
    data = folder / "data"
    data.mkdir()
    config["inputs"] = []
    for index in range(count):
        name = (str(index).zfill(6) + "_" + "x" * name_length)[:name_length] + ".py"
        (data / name).write_bytes(b"x")
        config["inputs"].append({"local": "data/" + name, "remote": "code/" + name})
    path = folder / "job.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class RequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-request-budget-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_plan_reports_the_budget_of_a_job_that_fits(self):
        result = run.plan(read_config(job_file(self.root, 20)))
        budget = result["pod_request"]
        self.assertEqual(budget["input_files"], 20)
        self.assertEqual(budget["limit_bytes"], run.POD_REQUEST_LIMIT)
        self.assertLess(budget["bytes"], run.POD_REQUEST_LIMIT)
        self.assertEqual(budget["bytes"] + budget["headroom_bytes"], run.POD_REQUEST_LIMIT)
        self.assertFalse(result["paid_resources_created"])

    def test_six_hundred_inputs_fit_now(self):
        config = read_config(job_file(self.root, 600))
        budget = run.plan(config)["pod_request"]
        self.assertEqual(budget["input_files"], 600)
        # Compare the same workload settings: changing the example's name or command
        # also changes its request size, independently of the input manifest.
        without_inputs = copy.deepcopy(config)
        without_inputs["inputs"] = []
        self.assertEqual(budget["bytes"], run.request_budget(without_inputs, [])["bytes"])
        self.assertLess(budget["bytes"], run.POD_REQUEST_LIMIT)

    def test_launch_refuses_before_any_ledger_volume_or_watchdog(self):
        # The Pods already in the account still count: about 25 bytes each.
        cfg = read_config(job_file(self.root, 1))
        api = FakeApi()
        api.pods.update(
            {"crowded%06d" % index: {"id": "crowded%06d" % index} for index in range(5000)}
        )
        jobs = self.root / "jobs"
        spawn = MagicMock()
        with (
            patch_run("Api", return_value=api),
            patch_run("Storage", return_value=Storage("EU-RO-1", FakeS3())),
            patch_run("load_key", return_value="dummy-test-key"),
            patch_run("spawn_watchdog", spawn),
            patch_run("jobs_dir", return_value=jobs),
        ):
            with self.assertRaisesRegex(ValueError, "5,000 Pods already in the account") as caught:
                run.launch(cfg)
        self.assertIn("Nothing was created", str(caught.exception))
        self.assertIn("the input list is not in it", str(caught.exception))
        self.assertFalse(jobs.exists())
        spawn.assert_not_called()
        self.assertEqual({method for method, _ in api.calls}, {"GET"})

    def test_existing_pods_count_at_launch(self):
        cfg = fixture()["config"]
        inputs = [{"remote": "a.py", "size": 1}]
        empty = run.request_budget(cfg, inputs)["bytes"]
        crowded = run.request_budget(cfg, inputs, ["podid%09d" % index for index in range(100)])[
            "bytes"
        ]
        self.assertGreater(crowded - empty, 100 * 20)

    def test_budget_is_never_smaller_than_the_real_request(self):
        cfg = read_config(job_file(self.root, 150))
        inputs = [
            {
                "remote": item["remote"],
                "size": Path(item["local"]).stat().st_size,
                "sha256": "f" * 64,
            }
            for item in cfg["inputs"]
        ]
        prior = ["abcdefghij%04d" % index for index in range(30)]
        job = {
            **fixture(),
            "config": copy.deepcopy(cfg),
            "inputs": inputs,
            "prior_pod_ids": prior,
            "deadline_epoch": time.time() + 600,
            "startup_deadline_epoch": time.time() + 240,
        }
        real = len(json.dumps(run.payload(job, "abcdefghij")).encode())
        estimate = run.request_budget(cfg, inputs, prior)["bytes"]
        self.assertGreaterEqual(estimate, real)
        self.assertLess(estimate - real, 400)  # an upper bound, not a guess

    def test_creation_boundary_names_the_real_cause_too(self):
        job = fixture()
        job["inputs"] = [
            {"remote": "d/" + "y" * 120 + str(index), "size": 1, "sha256": "0" * 64}
            for index in range(500)
        ]
        run.payload(job, "testvolume")  # 500 inputs are no longer a problem
        job["prior_pod_ids"] = ["crowded%06d" % index for index in range(5000)]
        with self.assertRaisesRegex(ValueError, "5,000 Pods already in the account") as caught:
            run.payload(job, "testvolume")
        self.assertIn("The Pod was not requested", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
