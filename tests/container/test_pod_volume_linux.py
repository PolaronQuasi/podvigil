"""The real guard on a volume that keeps no ownership or modes, in a container.

Set PODVIGIL_REVIEW_DOCKER_IMAGE to an already-installed Python image ID; images are never pulled.
"""

from pathlib import Path
import os
import shutil
import tempfile
import time
import unittest

from podvigil.storage import UNRELEASED_CONTROL_FILES
from podvigil.podrt.records import read, save
from tests.container import review_image
from tests.support.pod import Container, job
from tests.support.volume import (
    FAKE_MARKERS,
    controller_accepts,
    fill_volume,
    keys,
    with_input_manifest,
)


@unittest.skipUnless(review_image(), "Set installed image ID to enable isolated Docker regressions")
class DockerVolumeTests(unittest.TestCase):
    """The real guard in a network-less container, on a volume that keeps no ownership or modes."""

    def container(self, *args, **kwargs):
        instance = Container(review_image(), *args, **kwargs)
        self.addCleanup(instance.close)
        fill_volume(instance.workspace, instance.job, release=False)  # start.json is already there.
        return instance

    def pod_keys(self, container):
        # Nothing stray at the volume root (no probe folder, no temporary file).
        self.assertLessEqual(
            set(os.listdir(container.workspace)) - FAKE_MARKERS,
            {"managed-owner.json", "jobs", "inputs"},
        )
        return keys(container.workspace)

    def test_injected_init_runs_the_workload_publishes_and_deletes(self):
        escaped = (
            "from pathlib import Path;import os,time;"
            "Path(os.environ['WONDOMON_OUTPUT_DIR'],'ongoing.bin').write_bytes(b'checkpoint');time.sleep(60)"
        )
        command = [
            "python3",
            "-c",
            (
                "import subprocess,time;from pathlib import Path;Path('nested').mkdir();"
                "Path('nested/model.bin').write_bytes(b'weights');"
                f"subprocess.Popen(['python3','-c',{escaped!r}],start_new_session=True);time.sleep(.4)"
            ),
        ]
        instance = self.container(config=job(command), init=True)
        instance.finish(timeout=40)
        self.assertEqual(instance.result()["outcome"], "success", instance.output)
        last = instance.events()[-1]
        self.assertNotEqual(last["pid"], 1)  # The guard ran under the injected init.
        # Every descendant was drained.
        self.assertEqual(last["live_pids"], sorted({1, last["pid"]}))
        manifest = read(
            instance.workspace / "jobs" / instance.job["job_id"] / "artifact-manifest.json"
        )
        self.assertIn("artifacts/ongoing.bin", [item["path"] for item in manifest["files"]])
        self.pod_keys(instance)
        self.assertEqual(
            controller_accepts(self, instance.workspace, instance.job)[1]["outcome"], "success"
        )

    def test_three_hundred_inputs_arrive_through_the_manifest_on_the_volume(self):
        # This exceeds the Pod request's previous limit of about 235 inputs.
        # The request holds only the manifest's hash.
        inputs = {f"data/part-{index:03d}.txt": b"row %d\n" % index for index in range(300)}
        command = [
            "python3",
            "-c",
            (
                "import os,pathlib;i=pathlib.Path(os.environ['WONDOMON_INPUT_DIR']);"
                "n=sum(1 for p in i.rglob('*.txt'));(pathlib.Path(os.environ['WONDOMON_OUTPUT_DIR'])/'count')"
                ".write_text(str(n))"
            ),
        ]
        spec = job(command)
        manifest = with_input_manifest(spec, inputs)
        workspace = Path(tempfile.mkdtemp(prefix="pod-linux-volume-"))
        self.addCleanup(shutil.rmtree, workspace, True)
        # Stage all inputs before the Pod starts.
        fill_volume(workspace, spec, inputs, release=False, manifest=manifest)
        instance = Container(review_image(), config=spec, workspace=workspace, init=True)
        self.addCleanup(instance.close)
        instance.finish(timeout=90)
        self.assertEqual(instance.result()["outcome"], "success", instance.output)
        prefix = workspace / "jobs" / spec["job_id"]
        self.assertEqual((prefix / "artifacts" / "count").read_text(), "300")
        self.assertEqual(controller_accepts(self, workspace, spec, inputs)[1]["outcome"], "success")

    def test_a_manifest_that_does_not_match_the_request_stops_the_pod_before_the_workload(self):
        inputs = {"lib.py": b"VALUE = 1\n"}
        spec = job()
        manifest = with_input_manifest(spec, inputs)
        workspace = Path(tempfile.mkdtemp(prefix="pod-linux-volume-"))
        self.addCleanup(shutil.rmtree, workspace, True)
        fill_volume(
            workspace, spec, inputs, release=False, manifest=manifest.replace(b"lib.py", b"lib.pz")
        )
        instance = Container(review_image(), config=spec, workspace=workspace, init=True)
        self.addCleanup(instance.close)
        instance.finish(timeout=60)
        self.assertEqual(instance.result()["outcome"], "agent_failed", instance.output)
        failure = read(workspace / "jobs" / spec["job_id"] / "control/guard-failure.json")
        self.assertIn("input manifest", failure["message"])
        self.assertFalse((workspace / "jobs" / spec["job_id"] / "artifacts" / "run-count").exists())

    def test_a_cancel_request_makes_the_real_pod_stop_publish_and_delete_itself(self):
        # F2 covers the watchdog's cancel.json on the volume, with the real guard and supervisor
        # running under an injected init.
        command = [
            "python3",
            "-c",
            (
                "import os,pathlib,time;o=pathlib.Path(os.environ['WONDOMON_OUTPUT_DIR']);"
                "(o/'partial.bin').write_bytes(b'progress');time.sleep(150)"
            ),
        ]
        spec = job(command)
        # Set the workload deadline far enough out that cancellation ends it first.
        spec["deadline_epoch"] = time.time() + 150
        instance = self.container(config=spec, init=True)
        prefix = instance.workspace / "jobs" / spec["job_id"]
        # The workload runs: its output is on the Pod's own disk until the Pod publishes it.
        instance.wait_for_local(
            "/workspace/jobs/" + spec["job_id"] + "/artifacts/partial.bin", timeout=40
        )
        started = time.monotonic()
        save(
            prefix / "cancel.json",
            {
                "job_id": spec["job_id"],
                "pod_id": "testpod",
                "requested_utc": "2026-11-12T23:00:00+00:00",
                "grace_seconds": 90,
            },
        )
        instance.finish(timeout=60)
        self.assertLess(time.monotonic() - started, 45)
        self.assertEqual(instance.result()["outcome"], "cancelled", instance.output)
        self.assertEqual((prefix / "artifacts" / "partial.bin").read_bytes(), b"progress")
        self.assertEqual(
            controller_accepts(self, instance.workspace, spec)[1]["outcome"], "cancelled"
        )

    def test_pod_disk_that_ignores_modes_stops_the_guard_and_says_why_on_the_volume(self):
        instance = self.container("workspace_ignores_modes", init=True)
        instance.finish(timeout=40)
        prefix = "jobs/" + instance.job["job_id"] + "/"
        failure = read(instance.workspace / prefix / "control/guard-failure.json")
        self.assertIn("ownership/mode", failure["message"])
        self.assertIn("/workspace", failure["message"])
        self.assertEqual(failure["volume_probe"]["new_folder"], [0, 0, "777"])
        self.assertIn("Operation not permitted", failure["volume_probe"]["error"])
        self.assertEqual(failure["workspace_probe"]["folder_root_700"], [0, 0, "777"])
        self.assertIsNone(instance.result())
        found = self.pod_keys(instance)
        written = {name[len(prefix) :] for name in found if name.startswith(prefix)} - {
            "start.json"
        }
        # Only what the controller's unreleased-Pod cleanup accepts, so the volume can be deleted.
        self.assertLessEqual(written, set(UNRELEASED_CONTROL_FILES))
        self.assertIn("control/guard-failure.json", written)
