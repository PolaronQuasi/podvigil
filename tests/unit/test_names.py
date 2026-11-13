"""Neutral names: the tool's own prefix beside every WONDOMON_* name, and the CUDA floor as a setting.

New Pods get <PREFIX>_JOB_ID and <PREFIX>_JOB_SPEC; ownership and the Pod accept the old WONDOMON_ names too, so
older ledgers stay recoverable. Workloads get <PREFIX>_OUTPUT_DIR and <PREFIX>_INPUT_DIR and still the old names
(aliases). The prefix is the package's name in capitals (settings.ENV_PREFIX); the Pod code never spells it.
"""

from pathlib import Path
from unittest.mock import patch
import base64
import json
import os
import tempfile
import unittest

from podvigil import NAME, settings
from podvigil.controller import watchdog_spawn
from podvigil.jobspec import read_job, validate
from podvigil.podrt import agent
from tests.support import runner as run
from tests.support.jobs import fixture
from tests.support.paths import ROOT

PREFIX = NAME.upper()


def spec_env(job, prefix, job_id=None):
    return {
        prefix + "_JOB_SPEC": base64.b64encode(json.dumps(job).encode()).decode(),
        **({} if job_id is False else {prefix + "_JOB_ID": job_id or job["job_id"]}),
    }


class PodNameTests(unittest.TestCase):
    def test_the_pod_request_uses_the_tool_prefix(self):
        self.assertEqual(settings.ENV_PREFIX, PREFIX)
        job = fixture()
        body = run.payload(job, "testvolume")
        self.assertEqual(set(body["env"]), {PREFIX + "_JOB_ID", PREFIX + "_JOB_SPEC"})
        remote = json.loads(base64.b64decode(body["env"][PREFIX + "_JOB_SPEC"]))
        self.assertEqual(remote["env_prefixes"], [PREFIX, "PODWARDEN", "WONDOMON"])

    def test_ownership_accepts_the_new_and_the_old_name_for_the_same_job_only(self):
        job = fixture()
        pod = {"id": "testpod", "name": job["pod_name"]}
        for name in (PREFIX + "_JOB_ID", "PODWARDEN_JOB_ID", "WONDOMON_JOB_ID"):
            with self.subTest(name=name):
                self.assertTrue(run.owned_pod({**pod, "env": {name: job["job_id"]}}, job))
                self.assertFalse(run.owned_pod({**pod, "env": {name: "b" * 32}}, job))
        self.assertFalse(run.owned_pod({**pod, "env": {"OTHER_JOB_ID": job["job_id"]}}, job))
        self.assertFalse(run.owned_pod({**pod, "env": None}, job))

    def test_the_pod_finds_its_spec_under_either_prefix(self):
        job = {"job_id": "c" * 32, "config": {}}
        for prefix in (PREFIX, "PODWARDEN", "WONDOMON"):
            with (
                self.subTest(prefix=prefix),
                patch.dict(os.environ, spec_env(job, prefix), clear=True),
            ):
                self.assertEqual(agent.spec(), job)
        # The container tests start the Pod without a job ID variable.
        with patch.dict(os.environ, spec_env(job, "WONDOMON", job_id=False), clear=True):
            self.assertEqual(agent.spec(), job)

    def test_the_pod_refuses_an_ambiguous_or_mismatched_spec(self):
        job, other = {"job_id": "c" * 32}, {"job_id": "d" * 32}
        cases = {
            "two specs": {**spec_env(job, PREFIX), **spec_env(other, "WONDOMON")},
            "ID mismatch": spec_env(job, PREFIX, job_id="e" * 32),
            "none": {},
        }
        for name, environment in cases.items():
            with self.subTest(case=name), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(RuntimeError, "one job spec"):
                    agent.spec()

    def test_the_workload_gets_both_names_and_cannot_declare_either_prefix(self):
        variables = agent.child_environment(
            {"PATH": "/bin", PREFIX + "_JOB_SPEC": "private"},
            "/out",
            "/in",
            {"EXPERIMENT": "a"},
            [PREFIX, "WONDOMON"],
        )
        for prefix in (PREFIX, "WONDOMON"):
            self.assertEqual(
                (variables[prefix + "_OUTPUT_DIR"], variables[prefix + "_INPUT_DIR"]),
                ("/out", "/in"),
            )
        self.assertNotIn(PREFIX + "_JOB_SPEC", variables)
        self.assertEqual(variables["EXPERIMENT"], "a")
        for name in (PREFIX + "_MODE", "WONDOMON_MODE"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                agent.child_environment({}, "/out", "/in", {name: "x"}, [PREFIX, "WONDOMON"])
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Unsafe declared"):
                validate({**fixture()["config"], "workload_env": {name: "x"}})
        old = agent.child_environment({}, "/out", "/in")  # a spec from before the prefix list
        self.assertEqual(old["WONDOMON_OUTPUT_DIR"], "/out")

    def test_the_watchdog_takes_its_arming_nonce_under_either_name(self):
        self.assertEqual(watchdog_spawn.ARM_TOKEN, PREFIX + "_ARM_TOKEN")
        with tempfile.TemporaryDirectory(prefix="podvigil-names-") as tmp:
            for name, token in (
                (watchdog_spawn.ARM_TOKEN, "2" * 32),
                ("WONDOMON_ARM_TOKEN", "1" * 32),
            ):
                with self.subTest(name=name):
                    # The controller accepts.
                    (Path(tmp) / ("watchdog-arm-" + token + ".accepted")).write_text("{}")
                    environment = {
                        key: value
                        for key, value in os.environ.items()
                        if key not in (watchdog_spawn.ARM_TOKEN, "WONDOMON_ARM_TOKEN")
                    }
                    with patch.dict(os.environ, {**environment, name: token}, clear=True):
                        watchdog_spawn.arm(tmp)
                    record = json.loads(
                        (Path(tmp) / ("watchdog-arm-" + token + ".json")).read_text()
                    )
                    self.assertEqual(record["token"], token)


class CudaFloorTests(unittest.TestCase):
    def test_the_floor_is_a_setting_whose_default_stays(self):
        with patch.dict(os.environ, {}):
            os.environ.pop(settings.CUDA_FLOOR_VARIABLE, None)
            self.assertEqual(settings.cuda_floor(), "12.8")
        with patch.dict(os.environ, {settings.CUDA_FLOOR_VARIABLE: "12.4"}):
            self.assertEqual(settings.cuda_floor(), "12.4")
        with (
            patch.dict(os.environ, {settings.CUDA_FLOOR_VARIABLE: "twelve"}),
            self.assertRaisesRegex(ValueError, settings.CUDA_FLOOR_VARIABLE),
        ):
            settings.cuda_floor()
        self.assertEqual(settings.CUDA_FLOOR_VARIABLE, PREFIX + "_MIN_CUDA_VERSION")

    def test_new_jobs_are_held_to_the_floor_but_stored_ones_are_not(self):
        with tempfile.TemporaryDirectory(prefix="podvigil-floor-") as tmp:
            config = json.loads((ROOT / "jobs" / "smoke.example.json").read_text(encoding="utf-8"))
            config["min_cuda_version"] = "12.4"
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with (
                patch.dict(os.environ, {settings.CUDA_FLOOR_VARIABLE: "12.8"}),
                self.assertRaisesRegex(ValueError, "floor"),
            ):
                read_job(path)
            with patch.dict(os.environ, {settings.CUDA_FLOOR_VARIABLE: "12.4"}):
                self.assertEqual(read_job(path)[0]["min_cuda_version"], "12.4")
            # The watchdog and recovery validate a stored configuration.
            # A floor set later never invalidates it.
            with patch.dict(os.environ, {settings.CUDA_FLOOR_VARIABLE: "13.0"}):
                self.assertEqual(validate(config)["min_cuda_version"], "12.4")

    def test_the_survey_starts_at_the_floor(self):
        from tests.unit.test_survey import catalog

        with patch.dict(os.environ, {settings.CUDA_FLOOR_VARIABLE: "12.4"}):
            self.assertEqual(run.survey(catalog())["min_cuda_version"], "12.4")
            with self.assertRaisesRegex(ValueError, "floor"):
                run.survey(catalog(), min_cuda_version="12.2")


if __name__ == "__main__":
    unittest.main()
