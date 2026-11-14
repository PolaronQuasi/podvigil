"""Capacity rounds against fake catalog responses and explicit attempt ledgers; never a real cloud."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import json
import tempfile
import unittest

from podvigil import cli
from podvigil.controller import capacity, launch
from podvigil.podrt.api import ApiError, ApiPacingDeferred
from podvigil.podrt.records import save
from podvigil.provider.catalog import NoCapacity
from tests.support.clock import Clock
from tests.support.fakes import FakeApi, FakeS3, NO_CAPACITY_DETAIL
from tests.support.jobs import fixture
from tests.support.runner import patch_run
from podvigil.storage import Storage


class PacedCatalog(FakeApi):
    def __init__(self):
        super().__init__()
        self.pacing_settings = None
        self.deadlines = []

    def configure_pacing(self, path, **settings):
        self.pacing_settings = settings

    @contextmanager
    def pacing(self, *, deadline):
        self.deadlines.append(deadline)
        yield


class CapacityWaitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = Clock()
        self.api = PacedCatalog()
        self.configs = [fixture()["config"]]
        self.attempts = []
        self.folders = []
        self.behavior = lambda: {"training_outcome": "success"}

    def attempt(self, configs, ledgers, **options):
        self.attempts.append((configs, options))
        folder = self.root / ("attempt-" + str(len(self.folders) + 1))
        self.folders.append(folder)
        job = fixture()
        save(folder / "job.json", job)
        save(folder / "pod-dispatch.json", {"state": "dispatching"})
        save(
            folder / "pod-rejected.json",
            {
                "job_id": job["job_id"],
                "kind": "pod",
                "status": 400,
            },
        )
        save(folder / "volume-receipt.json", {"id": "testvolume"})
        save(folder / "volume-deleted.json", {"id": "testvolume", "evidence": {"verified": True}})
        ledgers.append(folder)
        return self.behavior()

    def refuse(self):
        raise ApiError(400, "POST", "/pods", detail=NO_CAPACITY_DETAIL)

    def catalog(self, cfg=None, *, available=False, price=0.5):
        cfg = cfg or self.configs[0]
        self.api.gpu_catalog[cfg["gpu_id"]] = {
            "id": cfg["gpu_id"],
            "secure": True,
            "price": {"secure": price},
            "dataCenters": [{"id": cfg["data_center_id"], "availability": "HIGH"}]
            if available
            else [],
        }

    def invoke(self, *options):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(capacity, "time", self.clock),
            patch.object(capacity, "launch_candidates", side_effect=self.attempt),
            patch.object(capacity, "load_key", return_value="dummy-capacity-key"),
            patch("podvigil.provider.client.Api", return_value=self.api),
            patch.object(cli, "read_job", return_value=self.configs),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = cli.run_cli(["launch", "fixture.json", *options])
        result = json.loads(out.getvalue())
        self.assertEqual(result["exit_code"], code)
        return code, result, err.getvalue()

    def round_lines(self, text):
        return [line for line in text.splitlines() if line.startswith("Capacity round ")]

    def test_default_launch_calls_original_path_without_capacity_metadata(self):
        with patch.object(
            cli, "launch_candidates", return_value={"training_outcome": "success"}
        ) as normal:
            code, result, output = self.invoke()
        self.assertEqual(code, 0)
        normal.assert_called_once_with(self.configs, [], detach=False)
        self.assertNotIn("capacity_rounds", result)
        self.assertFalse(self.round_lines(output))
        self.assertFalse(self.api.calls)

    def test_initial_success_does_not_poll_or_sleep(self):
        code, result, output = self.invoke("--wait-for-capacity", "10")
        self.assertEqual((code, result["capacity_rounds"]), (0, 1))
        self.assertEqual(result["attempt_ledgers"], [str(self.folders[0])])
        self.assertFalse(self.api.calls)
        self.assertFalse(self.clock.sleeps)
        self.assertIn("next round: none", self.round_lines(output)[0])

    def test_catalog_only_rounds_then_detach_in_foreground(self):
        self.catalog()
        self.behavior = lambda: (
            self.refuse() if len(self.attempts) == 1 else {"outcome": "detached"}
        )
        self.clock.on_sleep = lambda: self.catalog(available=self.clock.elapsed >= 150)
        code, result, output = self.invoke("--wait-for-capacity", "10", "--detach")
        self.assertEqual((code, result["capacity_rounds"]), (9, 3))
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.clock.elapsed, 150)
        self.assertTrue(all(options["detach"] for _, options in self.attempts))
        self.assertEqual(result["attempt_ledgers"], [str(p) for p in self.folders])
        self.assertTrue(all((p / "job.json").is_file() for p in self.folders))
        self.assertTrue(
            all(method == "GET" and path.startswith("/catalog/") for method, path in self.api.calls)
        )
        self.assertEqual(len(self.round_lines(output)), 3)
        self.assertTrue(
            all(
                "allowance ends:" in line and "next round:" in line
                for line in self.round_lines(output)
            )
        )
        self.assertEqual(self.api.pacing_settings, {"max_wait": 1, "read_retries": 1})
        self.assertEqual(self.api.deadlines, [600, 600])

    def test_timeout_stops_with_four_without_uploading_again(self):
        self.behavior = self.refuse
        self.catalog()
        code, result, output = self.invoke("--wait-for-capacity", "2")
        self.assertEqual(
            (code, result["outcome"], result["capacity_rounds"]), (4, "no_capacity", 2)
        )
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(self.clock.elapsed, 120)
        self.assertIn("next round: none", self.round_lines(output)[-1])

    def test_zero_allows_only_the_initial_round(self):
        self.behavior = self.refuse
        code, result, _ = self.invoke("--wait-for-capacity", "0")
        self.assertEqual((code, result["capacity_rounds"]), (4, 1))
        self.assertFalse(self.clock.sleeps)
        self.assertFalse(self.api.calls)

    def test_all_candidates_polled_in_order_only_available_ones_attempted(self):
        other = {**self.configs[0], "gpu_id": "NVIDIA GeForce RTX 3090"}
        self.configs.append(other)
        self.catalog()
        self.catalog(other, available=True)
        self.behavior = lambda: (
            self.refuse() if len(self.attempts) == 1 else {"training_outcome": "success"}
        )
        code, result, _ = self.invoke("--wait-for-capacity", "5")
        self.assertEqual(code, 0)
        self.assertEqual(self.attempts[0][0], self.configs)
        self.assertEqual(self.attempts[1][0], [other])
        self.assertEqual(result["capacity_rounds"], 2)

    def test_capacity_race_starts_fresh_verified_attempts(self):
        self.catalog(available=True)
        self.behavior = lambda: (
            self.refuse() if len(self.attempts) < 3 else {"training_outcome": "success"}
        )
        code, result, _ = self.invoke("--wait-for-capacity", "5")
        self.assertEqual((code, result["capacity_rounds"]), (0, 3))
        self.assertEqual(len(set(result["attempt_ledgers"])), 3)

    def test_ambiguous_rejected_and_rate_limited_creates_never_repeat(self):
        for status, detail in (
            (0, None),
            (503, None),
            (429, None),
            (402, "balance"),
            (400, "invalid image"),
            (422, "invalid request"),
        ):
            with self.subTest(status=status):
                self.attempts.clear()

                def fail():
                    if status in (0, 503):
                        (self.folders[-1] / "pod-rejected.json").unlink()
                    else:
                        save(
                            self.folders[-1] / "pod-rejected.json",
                            {
                                "job_id": fixture()["job_id"],
                                "kind": "pod",
                                "status": status,
                            },
                        )
                    raise ApiError(status, "POST", "/pods", detail=detail)

                self.behavior = fail
                code, result, _ = self.invoke("--wait-for-capacity", "5")
                self.assertEqual(code, 6 if status in (0, 503) else 5)
                self.assertEqual(result["capacity_rounds"], 1)
                self.assertEqual(len(self.attempts), 1)
                self.assertFalse(self.clock.sleeps)
                self.assertFalse(self.api.calls)

    def test_catalog_errors_and_price_changes_stop_without_another_create(self):
        self.behavior = self.refuse
        self.catalog(available=True, price=self.configs[0]["max_compute_usd_per_hour"] + 1)
        code, result, _ = self.invoke("--wait-for-capacity", "5")
        self.assertEqual((code, result["outcome"]), (1, "error"))
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(result["capacity_rounds"], 2)

    def test_total_compute_budget_rechecked_in_catalog_round(self):
        self.behavior = self.refuse
        self.clock.on_sleep = lambda: self.configs[0].update(max_compute_usd=0.0001)
        code, _, _ = self.invoke("--wait-for-capacity", "5")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.attempts), 1)
        self.assertFalse(self.api.calls)

    def test_pacing_refusal_is_not_treated_as_no_capacity(self):
        self.behavior = self.refuse
        with patch.object(
            self.api,
            "request",
            side_effect=ApiPacingDeferred("GET", "/catalog/gpus", 90, status=429),
        ):
            code, result, _ = self.invoke("--wait-for-capacity", "5")
        self.assertEqual((code, result["outcome"]), (1, "error"))
        self.assertEqual(len(self.attempts), 1)

    def test_last_attempt_must_prove_no_pod_and_no_volume(self):
        for kind, expected in (("volume", 8), ("unknown", 6), ("created_pod", 1)):
            with self.subTest(kind=kind):
                self.attempts.clear()

                def fail():
                    folder = self.folders[-1]
                    if kind == "volume":
                        (folder / "volume-deleted.json").unlink()
                    elif kind == "unknown":
                        (folder / "pod-rejected.json").unlink()
                    else:
                        save(folder / "pod-receipt.json", {"id": "testpod"})
                        save(
                            folder / "pod-deleted.json",
                            {"id": "testpod", "evidence": {"verified": True}},
                        )
                    self.refuse()

                self.behavior = fail
                with patch.object(
                    capacity,
                    "attempt_left_nothing",
                    side_effect=lambda p, **_: launch.attempt_left_nothing(p, 0),
                ):
                    code, result, error = self.invoke("--wait-for-capacity", "5")
                self.assertEqual(code, expected)
                self.assertIn("Capacity retry refused", error)
                self.assertEqual(result["capacity_rounds"], 1)
                self.assertEqual(len(self.attempts), 1)
                self.assertFalse(self.clock.sleeps)

    def test_ctrl_c_rechecks_ledger_before_claiming_absence(self):
        self.behavior = self.refuse

        def interrupt():
            raise KeyboardInterrupt

        self.clock.on_sleep = interrupt
        with patch.object(
            capacity, "attempt_left_nothing", wraps=launch.attempt_left_nothing
        ) as checked:
            code, result, error = self.invoke("--wait-for-capacity", "5")
        self.assertEqual((code, result["outcome"]), (130, "interrupted"))
        self.assertIn("last attempt's ledger verifies that nothing remains", error)
        self.assertNotIn("cleanup requested", error)
        self.assertEqual(checked.call_args.kwargs, {"wait_seconds": 0})

    def test_ctrl_c_does_not_claim_absence_when_ledger_changed(self):
        self.behavior = self.refuse

        def interrupt():
            (self.folders[-1] / "volume-deleted.json").unlink()
            raise KeyboardInterrupt

        self.clock.on_sleep = interrupt
        code, _, error = self.invoke("--wait-for-capacity", "5")
        self.assertEqual(code, 130)
        self.assertIn("does not verify absence", error)
        self.assertNotIn("verifies that nothing remains", error)
        self.assertIn("still exists and is billed", error)

    def test_catalog_only_initial_failure_creates_no_ledger(self):
        self.catalog()

        def unavailable(*args, **kwargs):
            raise NoCapacity("catalog has no capacity")

        with patch.object(self, "attempt", side_effect=unavailable):
            code, result, _ = self.invoke("--wait-for-capacity", "2")
        self.assertEqual((code, result["attempt_ledgers"]), (4, []))

    def test_ctrl_c_cannot_treat_a_missing_job_record_as_absence(self):
        self.behavior = self.refuse

        def interrupt():
            (self.folders[-1] / "job.json").unlink()
            raise KeyboardInterrupt

        self.clock.on_sleep = interrupt
        code, _, error = self.invoke("--wait-for-capacity", "5")
        self.assertEqual(code, 130)
        self.assertIn("does not verify absence", error)
        self.assertNotIn("verifies that nothing remains", error)

    def test_invalid_minutes_are_usage_errors_before_loading_job(self):
        for value in ("-1", "nan", "inf", "1e308", "text"):
            self.assertEqual(self.invoke("--wait-for-capacity=" + value)[0], 64)
        self.assertFalse(self.attempts)

    def test_candidate_loop_cannot_start_another_candidate_after_allowance(self):
        def expires(*args, **kwargs):
            self.clock.elapsed = 101
            raise NoCapacity("capacity changed")

        with (
            patch.object(launch.time, "monotonic", self.clock.monotonic),
            patch.object(launch, "launch", side_effect=expires) as start,
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(NoCapacity, "allowance expired"):
                launch.launch_candidates(self.configs * 2, [], capacity_deadline=100)
        self.assertEqual(start.call_count, 1)

    def test_catalog_read_finishing_after_allowance_cannot_launch(self):
        def late(api, configs, end):
            self.clock.elapsed = end
            return configs

        details = {}
        with (
            patch.object(capacity, "time", self.clock),
            patch.object(launch.time, "monotonic", self.clock.monotonic),
            patch.object(launch, "launch", side_effect=NoCapacity("none")) as start,
            patch.object(capacity, "available_candidates", side_effect=late),
            patch.object(capacity, "configured_api", return_value=self.api),
            patch.object(capacity, "load_key", return_value="dummy-key"),
        ):
            with self.assertRaisesRegex(NoCapacity, "allowance expired"):
                capacity.launch_with_capacity_wait(
                    self.configs, [], 5, details=details, progress=lambda _: None
                )
        self.assertEqual(details["capacity_rounds"], 2)
        self.assertEqual(start.call_count, 1)

    def test_unreadable_cleanup_proof_is_never_no_capacity(self):
        self.behavior = self.refuse
        with patch.object(
            capacity, "attempt_left_nothing", side_effect=PermissionError("ledger unavailable")
        ):
            code, result, _ = self.invoke("--wait-for-capacity", "5")
        self.assertEqual((code, result["outcome"]), (1, "error"))
        self.assertEqual(len(self.attempts), 1)
        self.assertFalse(self.clock.sleeps)

    def test_real_fakecloud_attempts_upload_only_after_catalog_returns_capacity(self):
        api = FakeApi()
        s3 = FakeS3(api=api)
        storage = Storage("EU-RO-1", s3)
        api.storage = storage
        source = self.root / "input.txt"
        source.write_text("fixture input", encoding="utf-8")
        cfg = {**self.configs[0], "inputs": [{"local": str(source), "remote": "input.txt"}]}
        counts = {"posts": 0, "polls": 0}
        request = api.request

        def provider(method, path, body=None):
            if method == "GET" and path.startswith("/catalog/gpus/") and counts["posts"] == 1:
                counts["polls"] += 1
                if counts["polls"] <= 2:
                    self.assertEqual(
                        sum(m == "POST" and p == "/network-volumes" for m, p in api.calls), 1
                    )
                    value = request(method, path, body)
                    value["dataCenters"] = []
                    return value
                self.clock.elapsed = 300
            return request(method, path, body)

        def reject_once(body):
            counts["posts"] += 1
            return (400, NO_CAPACITY_DETAIL) if counts["posts"] == 1 else None

        api.pod_rejection = reject_once

        class Monitor:
            def poll(self):
                return None

        def complete(folder, job, api, storage, monitor, pod_id, volume_id, **options):
            s3.finished(job["job_id"])
            launch.cleanup_pod(api, job, folder)
            return "pod_deleted"

        details, ledgers = {}, []
        with (
            patch_run("Api", return_value=api),
            patch_run("Storage", return_value=storage),
            patch_run("load_key", return_value="dummy-retry-key"),
            patch.object(capacity, "load_key", return_value="dummy-retry-key"),
            patch_run("spawn_watchdog", return_value=Monitor()),
            patch_run("jobs_dir", return_value=self.root / "real-attempts"),
            patch_run("provider_now", side_effect=lambda _: self.clock.time()),
            patch.object(launch, "supervise", side_effect=complete),
            patch.object(api, "request", side_effect=provider),
            patch.object(storage, "upload", wraps=storage.upload) as uploads,
            patch.object(capacity, "CAPACITY_POLL_SECONDS", 0.01),
            redirect_stderr(io.StringIO()),
        ):
            result = capacity.launch_with_capacity_wait(
                [cfg], ledgers, 1, details=details, progress=lambda _: None
            )
        self.assertEqual(result["training_outcome"], "success")
        self.assertEqual(details["capacity_rounds"], 4)
        self.assertEqual(len(ledgers), 2)
        self.assertEqual(uploads.call_count, 2)
        self.assertFalse(api.pods)
        self.assertFalse(api.volumes)
        jobs = [json.loads((p / "job.json").read_text()) for p in ledgers]
        self.assertNotEqual(jobs[0]["job_id"], jobs[1]["job_id"])
        self.assertEqual(jobs[1]["deadline_epoch"] - jobs[0]["deadline_epoch"], 300)
        self.assertEqual(jobs[1]["deadline_epoch"] - self.clock.time(), launch.runtime_limit(cfg))
