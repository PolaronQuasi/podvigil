"""The Pod supervisor's workload outcomes with its isolation and IPC replaced.

Portable: real UID, capability, PID-1 and IPC isolation run in the container lane.
"""

from pathlib import Path
from unittest.mock import patch
import os
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.podrt.records import VOLUME_MOUNT, read, save
from tests.support.fakes import FakeApi
from tests.support.jobs import fixture
from podvigil.podrt import agent as pod_agent


class RemoteSupervisorTests(unittest.TestCase):
    def run_agent(self, command, manifest_failure=False, timeout=False):
        with tempfile.TemporaryDirectory(prefix="wondomon-agent-test-") as tmp:
            root = Path(tmp)
            workspace, scratch, volume = root / "workspace", root / "tmp", root / "volume"
            workspace.mkdir()
            scratch.mkdir()
            volume.mkdir()
            job = fixture()
            job["config"]["command"] = command
            job["config"]["required_artifacts"] = []
            if timeout:
                job["deadline_epoch"] = time.time() + 1
                job["config"]["finalize_grace_seconds"] = 0
            folder = workspace / "jobs" / job["job_id"]
            # The release marker is on the network volume; the workspace is the Pod's own disk.
            save(
                volume / "jobs" / job["job_id"] / "start.json",
                {"job_id": job["job_id"], "pod_id": "testpod"},
            )
            real_path = Path

            class Channel:
                closed = False

                def settimeout(self, value):
                    pass

                def sendall(self, value):
                    pass

                def close(self):
                    self.closed = True

            channel = Channel()

            def mapped_path(value):
                return {"/workspace": workspace, "/tmp": scratch, VOLUME_MOUNT: volume}.get(
                    value
                ) or real_path(value)

            def stop_child(child):
                if child and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)

            original_manifest = pod_agent.make_manifest

            def manifest(*args, **kwargs):
                if manifest_failure:
                    raise OSError("simulated storage full")
                return original_manifest(*args, **kwargs)

            def prepare_isolation(root, job_folder, code_dir, *, volume):
                (root / "inputs").mkdir(exist_ok=True)
                return {
                    "output": str(job_folder / "artifacts"),
                    "inputs": str(root / "inputs"),
                    "home": str(scratch),
                }

            def launch_isolated(argv, layout, environment, stdout, *, timeout, authorize):
                authorize()
                return subprocess.Popen(
                    argv,
                    cwd=layout["output"],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
                )

            with (
                patch("podvigil.podrt.agent.Path", side_effect=mapped_path),
                patch("podvigil.podrt.agent.volume_mounted", return_value=True),
                patch("podvigil.podrt.agent.exchange"),
                patch("podvigil.podrt.agent.signal.signal"),
                patch("podvigil.podrt.agent.isolation.prepare", side_effect=prepare_isolation),
                patch("podvigil.podrt.agent.isolation.launch", side_effect=launch_isolated),
                patch("podvigil.podrt.agent.stop_child", side_effect=stop_child),
                patch("podvigil.podrt.agent.make_manifest", side_effect=manifest),
            ):
                # Portable workload-outcome checks only. Real UID/capability,
                # PID-1 and IPC isolation run separately in Linux containers.
                pod_agent.execute(job, FakeApi(job), "testpod", channel)
                self.assertTrue(channel.closed)
            # Let the heartbeat thread see its stop flag before removing the fixture.
            time.sleep(0.05)
            return read(folder / "artifacts/result.json")

    def test_foreground_workload_success(self):
        result = self.run_agent([sys.executable, "-c", "print('ok')"])
        self.assertEqual(result["outcome"], "success")

    def test_foreground_workload_failure_returns_control_to_guard(self):
        result = self.run_agent([sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual((result["outcome"], result["exit_code"]), ("training_failed", 7))

    def test_timeout_returns_control_to_guard(self):
        result = self.run_agent([sys.executable, "-c", "import time;time.sleep(30)"], timeout=True)
        self.assertEqual(result["outcome"], "time_limit")

    def test_artifact_error_cannot_skip_guard_handoff(self):
        result = self.run_agent([sys.executable, "-c", "print('ok')"], manifest_failure=True)
        self.assertEqual(result["outcome"], "success")
