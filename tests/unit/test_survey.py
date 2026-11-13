"""Read-only capacity survey: which GPU types can be launched now, and where. It creates nothing.

The first user needed a separate script for this (`runpod_survey2.py`); the requests and response fields here
follow that script, which ran against the live API, and the archived contract.
"""

from contextlib import redirect_stdout
from pathlib import Path
import io
import json
import tempfile
import unittest
import urllib.parse

from tests.support import runner as run
from tests.support.runner import patch_run
from tests.support.fakes import FakeApi


def entry(gpu_id, price, centers, *, secure=True, memory=24, vcpu=8, ram=32):
    return {
        "id": gpu_id,
        "secure": secure,
        "price": {"secure": price, "community": price / 2},
        "memory": memory,
        "minVcpuCount": vcpu,
        "minRam": ram,
        "maxCount": {"secure": 8, "community": 4},
        "dataCenters": [
            {"id": center, "name": center, "availability": level} for center, level in centers
        ],
    }


def catalog():
    api = FakeApi()
    api.datacenter_volumes.update({"US-KS-2": [], "US-GA-2": ["HIGH_PERFORMANCE"], "US-MO-2": None})
    api.gpu_catalog.update(
        {
            "NVIDIA RTX PRO 4500 Blackwell": entry(
                "NVIDIA RTX PRO 4500 Blackwell",
                0.72,
                [("EU-RO-1", "LOW"), ("US-KS-2", "HIGH")],
                memory=32,
                vcpu=12,
                ram=62,
            ),
            "NVIDIA GeForce RTX 3090": entry(
                "NVIDIA GeForce RTX 3090", 0.5, [("EU-CZ-1", "MEDIUM"), ("US-NC-1", "HIGH")]
            ),
            "Community only": entry("Community only", 0.1, [("EU-RO-1", "HIGH")], secure=False),
            "No capacity": entry("No capacity", 0.2, [("EU-RO-1", "NONE")]),
            "Unlisted center": entry("Unlisted center", 0.3, [("US-TX-3", "HIGH")]),
            "Only without volumes": entry(
                "Only without volumes", 0.4, [("US-KS-2", "HIGH"), ("US-GA-2", "HIGH")]
            ),
            "No price": {
                **entry("No price", 0.1, [("EU-RO-1", "HIGH")]),
                "price": {"secure": None},
            },
        }
    )
    return api


class SurveyTests(unittest.TestCase):
    def test_lists_only_launchable_combinations_sorted_by_price(self):
        api = catalog()
        result = run.survey(api)
        self.assertEqual(
            [row["gpu_id"] for row in result["gpus"]],
            ["NVIDIA GeForce RTX 3090", "NVIDIA RTX PRO 4500 Blackwell"],
        )
        cheap, pro = result["gpus"]
        self.assertEqual(cheap["data_centers"], {"EU-CZ-1": "MEDIUM", "US-NC-1": "HIGH"})
        self.assertEqual(pro["data_centers"], {"EU-RO-1": "LOW"})  # US-KS-2 has no STANDARD volumes
        self.assertEqual(
            (pro["usd_per_hour"], pro["vcpu"], pro["ram_gb"], pro["vram_gb"]), (0.72, 12, 62, 32)
        )
        self.assertNotIn("US-KS-2", result["usable_data_centers"])
        self.assertEqual(result["without_standard_volumes"], ["US-GA-2", "US-KS-2"])
        self.assertEqual(result["not_in_catalog"], ["US-MO-2"])
        self.assertFalse(result["paid_resources_created"])

    def test_reads_only_and_asks_for_secure_capacity_at_the_cuda_floor(self):
        api = catalog()
        run.survey(api, gpu_count=2)
        self.assertEqual({method for method, _ in api.calls}, {"GET"})
        listing = [path for _, path in api.calls if path.startswith("/catalog/gpus?")]
        self.assertEqual(len(listing), 1)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(listing[0]).query))
        self.assertEqual(
            query,
            {
                "include": "AVAILABILITY",
                "product": "POD",
                "cloud": "SECURE",
                "count": "2",
                "minCudaVersion": run.MIN_CUDA_VERSION,
            },
        )
        self.assertFalse(api.pods or api.volumes)

    def test_price_scales_with_the_gpu_count(self):
        row = next(
            row
            for row in run.survey(catalog(), gpu_count=4)["gpus"]
            if row["gpu_id"].endswith("3090")
        )
        self.assertEqual((row["usd_per_gpu_hour"], row["usd_per_hour"]), (0.5, 2.0))

    def test_refuses_a_cuda_floor_below_the_runner_and_bad_counts(self):
        for arguments in (
            {"min_cuda_version": "12.4"},
            {"min_cuda_version": "12"},
            {"gpu_count": 0},
            {"gpu_count": 9},
            {"gpu_count": True},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                run.survey(FakeApi(), **arguments)

    def test_a_malformed_catalog_answer_is_an_error_not_an_empty_survey(self):
        api = catalog()
        original = api.request
        api.request = lambda method, path, body=None: (
            {"gpus": "broken"}
            if path.startswith("/catalog/gpus?")
            else original(method, path, body)
        )
        with self.assertRaisesRegex(RuntimeError, "Malformed GPU catalog"):
            run.survey(api)

    def test_table_shows_price_cpu_and_memory(self):
        lines = run.survey_lines(run.survey(catalog()))
        row = next(line for line in lines if "RTX PRO 4500" in line)
        self.assertEqual(row.split()[-6:], ["32", "0.72", "12", "62", "EU-RO-1", "LOW"])
        self.assertTrue(lines[-1].endswith("Nothing was created."))

    def test_command_line_prints_a_table_or_json_and_creates_nothing(self):
        with tempfile.TemporaryDirectory(prefix="wondomon-survey-") as tmp:
            jobs = Path(tmp) / "jobs"
            for arguments in (["survey"], ["survey", "--json", "--gpu-count", "2"]):
                api, output = catalog(), io.StringIO()
                with (
                    patch_run("configured_api", return_value=api),
                    patch_run("load_key", return_value="dummy"),
                    patch_run("jobs_dir", return_value=jobs),
                    redirect_stdout(output),
                ):
                    code = run.run_cli(arguments)
                self.assertEqual(code, 0)
                text = output.getvalue()
                if "--json" in arguments:
                    self.assertEqual(json.loads(text)["gpu_count"], 2)
                else:
                    self.assertIn("NVIDIA GeForce RTX 3090", text)
                    self.assertNotIn("Pod:", text)  # no job, so no state lines
                self.assertFalse(jobs.exists())


if __name__ == "__main__":
    unittest.main()
