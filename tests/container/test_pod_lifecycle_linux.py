"""Pod self-deletion, deadlines and finalization with the real guard as PID 1.

Set PODVIGIL_REVIEW_DOCKER_IMAGE to an already-installed Python image ID; images are never pulled.
"""

from pathlib import Path
import hashlib
import json
import subprocess
import tempfile
import time
import unittest

from podvigil.podrt.records import read
from tests.container import review_image
from tests.support.pod import POD_SOURCES, Container, job, linux_fake_source, stage_sources


@unittest.skipUnless(review_image(), "Set installed image ID to enable isolated Docker regressions")
class PodLifecycleLinuxTests(unittest.TestCase):
    def container(self, *args, **kwargs):
        instance = Container(review_image(), *args, fake_source=linux_fake_source(), **kwargs)
        self.addCleanup(instance.close)
        return instance

    def control(self, container, name):
        return read(container.workspace / "jobs" / container.job["job_id"] / "control" / name)

    def test_startup_visibility_lag_never_releases_guard_on_404_only(self):
        fake = (
            linux_fake_source()
            + r"""
OriginalApi = Api
class Api(OriginalApi):
    def __init__(self, key):
        super().__init__(key)
        self.lag_reads = self.lag_deletes = 0

    def request(self, method, path, body=None):
        if method == 'GET':
            self.lag_reads += 1
            if self.lag_reads <= 3:
                self.last_response = {'method': method, 'path': path, 'status': 404,
                    'response_date': 'Tue, 10 Nov 2026 20:00:00 GMT'}
                raise ApiError(404)
            (self.root / 'running-became-visible').touch()
        if method == 'DELETE':
            self.lag_deletes += 1
            if self.lag_reads <= 3:
                self.last_response = {'method': method, 'path': path, 'status': 404}
                (self.root / 'initial-delete-404').touch()
                raise ApiError(404)
            assert (self.root / 'running-became-visible').exists()
            config = json.loads(base64.b64decode(os.environ['WONDOMON_JOB_SPEC']))
            evidence = json.loads((self.root / 'jobs' / config['job_id'] / 'control/self-delete.json').read_text())
            assert not evidence['absence_verified'] and not evidence['delete_accepted']
        value = super().request(method, path, body)
        if method == 'GET':
            value['status'] = 'RUNNING'
        return value

    def get_or_none(self, path):
        if (self.root / 'deleted').exists():
            self.last_response = {'method': 'GET', 'path': path, 'status': 404}
            return None
        try:
            return self.request('GET', path)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
"""
        )
        container = Container(review_image(), fake_source=fake)
        self.addCleanup(container.close)
        container.finish(timeout=30)  # Production retry waits and separated reads.
        self.assertTrue((container.workspace / "initial-delete-404").exists())
        self.assertTrue((container.workspace / "running-became-visible").exists())
        evidence = self.control(container, "self-delete.json")
        self.assertEqual(evidence["delete_attempts"], 3)
        self.assertEqual(evidence["delete_http_status"], 204)
        self.assertEqual(evidence["actor"], "pod_guard")
        self.assertEqual(evidence["absence_basis"], "accepted_self_delete")
        self.assertTrue(evidence["absence_verified"])
        folder = container.workspace / "jobs" / container.job["job_id"]
        self.assertFalse((folder / "started-once.json").exists())
        self.assertEqual(container.events()[-1]["live_pids"], [1])

    def test_linux_portable_units_including_unsafe_reserved_paths(self):
        with tempfile.TemporaryDirectory(prefix="pod-linux-units-") as tmp:
            source = Path(tmp)
            stage_sources(
                source, *POD_SOURCES, "tests/support/pod.py", "tests/unit/test_pod_lifecycle.py"
            )
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network",
                    "none",
                    "--cpus",
                    "1",
                    "--memory",
                    "128m",
                    "--pids-limit",
                    "64",
                    "--mount",
                    f"type=bind,source={source},target=/review,readonly",
                    "--workdir",
                    "/review",
                    "--entrypoint",
                    "python3",
                    review_image(),
                    "-B",
                    "-m",
                    "unittest",
                    "tests.unit.test_pod_lifecycle.PodLifecycleTests",
                    "-v",
                ],
                capture_output=True,
                text=True,
                timeout=40,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("skipped", result.stderr)

    def test_auth_denial_stops_once_then_pid1_exits_three(self):
        for case in ("delete_denied", "both_denied"):
            with self.subTest(case=case):
                container = self.container(case)
                container.finish(expected_code=3, expect_deleted=False)
                evidence = self.control(container, "self-delete.json")
                self.assertEqual(evidence["state"], "auth_denied")
                self.assertEqual(evidence["delete_attempts"], 1)
                self.assertFalse(evidence["absence_verified"])
                self.assertTrue(evidence["stop_attempted"])
                self.assertEqual(evidence["stop_accepted"], case == "delete_denied")
                self.assertIn("SELF CLEANUP UNVERIFIED", container.output)
                status = read(container.workspace / "jobs" / container.job["job_id"] / "agent.json")
                self.assertEqual(
                    (status["phase"], status["pod_id"], status["actor"]),
                    ("done", "testpod", "pod_guard"),
                )

    def test_denied_get_still_attempts_permitted_own_delete(self):
        container = self.container("get_denied")
        container.finish()
        evidence = self.control(container, "self-delete.json")
        self.assertEqual(evidence["delete_http_status"], 204)
        self.assertTrue(evidence["absence_verified"])
        self.assertFalse(
            (
                container.workspace / "jobs" / container.job["job_id"] / "artifacts/run-count"
            ).exists()
        )

    def test_blocked_telemetry_does_not_starve_ipc_or_cancel_workload(self):
        config = job(
            [
                "python3",
                "-c",
                "import time;from pathlib import Path;time.sleep(5);Path('model.bin').write_bytes(b'ok')",
            ]
        )
        config["config"]["agent_stale_seconds"] = 2
        container = self.container("slow_telemetry", config=config)
        container.wait_for("telemetry-blocked")
        container.finish()
        self.assertEqual(container.result()["outcome"], "success")

    def test_relative_outputs_and_input_imports_are_preserved_and_key_not_in_exec_env(self):
        with tempfile.TemporaryDirectory(prefix="pod-inputs-") as tmp:
            workspace = Path(tmp)  # The network volume, as the controller filled it.
            (workspace / "inputs").mkdir()
            module = b"VALUE = 'checkpoint'\n"
            (workspace / "inputs/input_module.py").write_bytes(module)
            (workspace / "inputs/unlisted.txt").write_bytes(b"not in the job spec")
            command = [
                "python3",
                "-c",
                (
                    "import input_module,os;from pathlib import Path;"
                    "assert Path.cwd()==Path(os.environ['WONDOMON_OUTPUT_DIR']);"
                    "assert os.environ['WONDOMON_INPUT_DIR']=='/workspace/inputs';"
                    "assert os.environ['WONDOMON_OUTPUT_DIR']=='/workspace/jobs/'+'b'*32+'/artifacts';"
                    "assert input_module.__file__=='/workspace/inputs/input_module.py';"
                    "assert not Path('/workspace/inputs/unlisted.txt').exists();"
                    "assert 'RUNPOD_API_KEY' not in os.environ;"
                    "Path('checkpoints').mkdir();Path('checkpoints/model.bin').write_text(input_module.VALUE)"
                ),
            ]
            config = job(command)
            config["inputs"] = [
                {
                    "remote": "input_module.py",
                    "size": len(module),
                    "sha256": hashlib.sha256(module).hexdigest(),
                }
            ]
            container = self.container(config=config, workspace=workspace)
            container.finish()
            self.assertEqual(container.result()["outcome"], "success")
            folder = workspace / "jobs" / container.job["job_id"]
            paths = [item["path"] for item in read(folder / "artifact-manifest.json")["files"]]
            self.assertIn("artifacts/checkpoints/model.bin", paths)
            events = [
                json.loads(line)
                for line in (workspace / "api-events.jsonl").read_text().splitlines()
            ]
            self.assertTrue(any(event["pid"] != 1 for event in events))
            self.assertTrue(all(not event["scoped_key_in_environ"] for event in events))
            self.assertEqual(self.control(container, "identity.json")["pod_id"], "testpod")

    def test_real_linux_oddname_retains_success_in_portable_bundle(self):
        container = self.container(
            config=job(
                [
                    "python3",
                    "-c",
                    "from pathlib import Path;Path('epoch:1.bin').write_bytes(b'checkpoint')",
                ]
            )
        )
        container.finish()
        self.assertEqual(container.result()["outcome"], "success")
        folder = container.workspace / "jobs" / container.job["job_id"]
        self.assertEqual(read(folder / "artifact-manifest.json")["version"], 2)
        self.assertFalse((folder / "control/finalization-incomplete.json").exists())

    def test_hard_cutoff_during_hashing_leaves_committed_outcome(self):
        config = job()
        config.update(deadline_epoch=time.time() + 7, startup_deadline_epoch=time.time() + 4)
        container = self.container("hash_cutoff", config=config)
        container.wait_for("hash-started")
        container.finish(timeout=14)
        self.assertEqual(container.result()["outcome"], "success")
        marker = self.control(container, "finalization-incomplete.json")
        self.assertEqual(marker["stage"], "inventory_and_hashing")
        self.assertFalse(marker["complete"])
        self.assertTrue(self.control(container, "self-delete.json")["absence_verified"])
        self.assertEqual(container.events()[-1]["live_pids"], [1])
