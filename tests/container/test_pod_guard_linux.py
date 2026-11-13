"""The real Pod guard as PID 1 in a Linux container with networking disabled.

Set PODVIGIL_REVIEW_DOCKER_IMAGE to an already-installed Python image ID; images are never pulled.
"""

from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from podvigil.podrt.records import read
from tests.container import review_image
from tests.support.pod import POD_SOURCES, Container, job, stage_sources


@unittest.skipUnless(review_image(), "Set installed image ID to enable isolated Docker regressions")
class LinuxContainerTests(unittest.TestCase):
    def container(self, *args, **kwargs):
        instance = Container(review_image(), *args, **kwargs)
        self.addCleanup(instance.close)
        return instance

    def test_linux_manifest_and_portable_units(self):
        # Explicit source allowlist: never mount the checkout or its credentials.
        with tempfile.TemporaryDirectory(prefix="pod-review-linux-units-") as tmp:
            source = Path(tmp)
            stage_sources(
                source, *POD_SOURCES, "tests/support/pod.py", "tests/unit/test_pod_agent.py"
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
                    "tests.unit.test_pod_agent.PodReviewTests",
                    "-v",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("skipped", result.stderr)

    def test_success_reaps_then_deletes_from_pid1(self):
        container = self.container()
        container.finish()
        self.assertEqual(container.result()["outcome"], "success")
        self.assertEqual(container.events()[-1]["live_pids"], [1])
        self.assertEqual(container.events()[-1]["pid"], 1)

    def test_supervisor_sigkill_does_not_kill_guard(self):
        # The unprivileged workload can no longer kill its supervisor. Inject
        # the crash from the container's trusted root control surface instead.
        command = [
            "python3",
            "-B",
            "-c",
            "import os,time;from pathlib import Path;Path('parent-pid').write_text(str(os.getppid()));time.sleep(60)",
        ]
        container = self.container(config=job(command))
        # Outputs stay on the Pod's disk until finalization; read the marker inside the container.
        marker = "/workspace/jobs/" + container.job["job_id"] + "/artifacts/parent-pid"
        parent_pid = int(container.wait_for_local(marker))
        injected = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "0",
                container.name,
                "python3",
                "-c",
                "import os,signal;os.kill(" + str(parent_pid) + ",signal.SIGKILL)",
            ],
            capture_output=True,
            timeout=10,
        )
        # PID 1 may reap this root fault injector together with escaped children.
        self.assertIn(injected.returncode, (0, 137))
        container.finish()
        self.assertEqual(container.events()[-1]["pid"], 1)
        self.assertEqual(container.events()[-1]["live_pids"], [1])

    def test_worker_sigkill_preserves_failure_result(self):
        container = self.container(
            config=job(["python3", "-c", "import os,signal;os.kill(os.getpid(),signal.SIGKILL)"])
        )
        container.finish()
        self.assertEqual(container.result()["outcome"], "training_failed")
        self.assertEqual(container.result()["exit_code"], -9)

    def test_worker_oom_does_not_kill_guard(self):
        command = [
            "python3",
            "-c",
            "from pathlib import Path;Path('/proc/self/oom_score_adj').write_text('1000');x=bytearray(256*1024*1024)",
        ]
        container = self.container(config=job(command))
        container.finish()
        counters = dict(
            line.split() for line in container.events()[-1]["memory_events"].splitlines()
        )
        self.assertGreater(int(counters.get("oom_kill", 0)), 0)
        self.assertEqual(container.result()["outcome"], "training_failed")
        self.assertEqual(container.result()["exit_code"], -9)
        self.assertEqual(container.events()[-1]["pid"], 1)

    def test_real_pids_exhaustion_still_deletes(self):
        container = self.container(pids=1)
        container.finish()
        self.assertFalse(
            (
                container.workspace / "jobs" / container.job["job_id"] / "artifacts/run-count"
            ).exists()
        )
        self.assertEqual(container.events()[-1]["pid"], 1)

    def test_heartbeat_write_error_does_not_cancel_healthy_workload(self):
        container = self.container("heartbeat_failure")
        container.finish()
        self.assertEqual(
            (
                container.workspace / "jobs" / container.job["job_id"] / "artifacts/run-count"
            ).read_text(),
            "1",
        )
        self.assertEqual(container.result()["outcome"], "success")

    def test_denied_readiness_never_runs_workload(self):
        container = self.container("deny_release")
        container.finish()
        self.assertFalse(
            (
                container.workspace / "jobs" / container.job["job_id"] / "artifacts/run-count"
            ).exists()
        )
        self.assertEqual(container.result()["outcome"], "agent_failed")

    def test_signals_during_delete_backoff_do_not_exit(self):
        container = self.container("retry_signals")
        # The first retry comes about 7 s after the start. Allow more time if the Docker host is
        # busy with other containers.
        container.wait_for("retry-sleep", timeout=30)
        for signum in ("TERM", "INT", "TERM"):
            subprocess.run(
                ["docker", "kill", "--signal", signum, container.name],
                capture_output=True,
                check=True,
                timeout=5,
            )
        self.assertIsNone(container.process.poll())
        (container.workspace / "allow-delete").touch()
        container.finish()
        self.assertGreaterEqual(len(container.events()), 2)

    def test_renamed_own_pod_is_deleted(self):
        container = self.container("mutated")
        container.finish()
        self.assertEqual(container.result()["outcome"], "success")

    def test_wrong_cloud_own_pod_deleted_without_workload(self):
        container = self.container("bad_cloud")
        container.finish()
        self.assertFalse(
            (
                container.workspace / "jobs" / container.job["job_id"] / "artifacts/run-count"
            ).exists()
        )
        self.assertEqual(container.events()[-1]["pid"], 1)

    def test_hung_worker_cannot_outlive_hard_deadline(self):
        config = job(
            [
                "python3",
                "-c",
                "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)",
            ]
        )
        config["deadline_epoch"] = time.time() + 5
        config["startup_deadline_epoch"] = time.time() + 3
        container = self.container(config=config)
        began = time.monotonic()
        container.finish(timeout=12)
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(container.events()[-1]["live_pids"], [1])

    def test_escaped_session_is_reaped_before_manifest(self):
        escaped = "from pathlib import Path;import os,time;p=Path(os.environ['WONDOMON_OUTPUT_DIR'])/'ongoing.bin';p.write_bytes(b'checkpoint');time.sleep(60)"
        command = [
            "python3",
            "-c",
            f"import subprocess,time;subprocess.Popen(['python3','-c',{escaped!r}],start_new_session=True);time.sleep(.4)",
        ]
        container = self.container(config=job(command))
        container.finish()
        self.assertEqual(container.events()[-1]["live_pids"], [1])
        manifest = read(
            container.workspace / "jobs" / container.job["job_id"] / "artifact-manifest.json"
        )
        self.assertIn("artifacts/ongoing.bin", [item["path"] for item in manifest["files"]])

    def test_container_restart_preserves_single_execution_and_artifacts(self):
        first = self.container()
        first.finish()
        folder = first.workspace / "jobs" / first.job["job_id"]
        before = {
            str(p.relative_to(folder)): p.read_bytes() for p in (folder / "artifacts").iterdir()
        }
        manifest = (folder / "artifact-manifest.json").read_bytes()
        (first.workspace / "deleted").unlink()
        second = self.container(config=first.job, workspace=first.workspace)
        second.finish()
        self.assertEqual((folder / "artifacts/run-count").read_text(), "1")
        self.assertEqual(manifest, (folder / "artifact-manifest.json").read_bytes())
        self.assertEqual(
            before,
            {str(p.relative_to(folder)): p.read_bytes() for p in (folder / "artifacts").iterdir()},
        )
