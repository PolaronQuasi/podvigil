"""The workspace lives on the Pod's own disk; the network volume is locked away and gets copies.

The Runpod network volume keeps no ownership or modes (MooseFS through FUSE, measured on the
fourth paid Pod, 2026-11-12: everything uid 0, gid 0, mode 777; chmod has no effect; chown to
the workload user is refused even for root). RunpodVolumeModel gives a test folder exactly that
behavior. These tests show that the old layout fails on it as the real Pod did, and check the
new one: the volume's lock holds for the workload user, inputs are copied and checked, outputs
and the manifest reach the volume under the keys the controller reads, and the controller's own
collect and delete audit accept what the Pod left there.

Real ownership, UID and lock cases need Linux and root (the network-disabled Linux container
lane). The real guard on such a volume runs in tests/container/test_pod_volume_linux.py.
"""

from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from podvigil.podrt import bundle as output_bundle
from podvigil.podrt import agent
from podvigil.podrt import isolation
from podvigil.podrt.records import VOLUME_MOUNT, read, save
from tests.support.linux_review_api import RunpodVolumeModel
from tests.support.pod import job
from tests.support.volume import (
    controller_accepts,
    fill_volume,
    keys,
    ledger_inputs,
    with_input_manifest,
)

LINUX_ROOT = sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0


class Channel:
    def __init__(self):
        self.closed = False

    def settimeout(self, value):
        pass

    def sendall(self, value):
        pass

    def close(self):
        self.closed = True


class OwnPodApi:
    def __init__(self, volume_id="testvolume"):
        self.volume_id, self.last_response = volume_id, None

    def now(self):
        return time.time()

    def request(self, method, path, body=None):
        assert (method, path) == ("GET", "/pods/testpod"), (method, path)
        self.last_response = {
            "method": method,
            "path": path,
            "status": 200,
            "response_date": "Thu, 12 Nov 2026 16:00:00 GMT",
        }
        return {
            "id": "testpod",
            "cloud": "SECURE",
            "actions": ["terminate"],
            "mounts": {"network": [{"volumeId": self.volume_id, "path": VOLUME_MOUNT}]},
        }


WORKLOAD = r"""
import errno, json, os
from pathlib import Path
out, inputs = Path(os.environ["WONDOMON_OUTPUT_DIR"]), Path(os.environ["WONDOMON_INPUT_DIR"])
volume = Path(os.environ["PROBE_VOLUME"])
seen = {"cwd_is_output": Path.cwd() == out}
for name, action in (("stat", lambda: os.stat(volume)), ("list_lock", lambda: os.listdir(volume.parent)),
                     ("open", lambda: os.open(volume, os.O_RDONLY))):
    try:
        action()
        seen[name] = "allowed"
    except OSError as exc:
        seen[name] = errno.errorcode[exc.errno]
seen["inputs"] = sorted(p.relative_to(inputs).as_posix() for p in inputs.rglob("*") if p.is_file())
seen["table"] = (inputs / "data" / "table.csv").read_text()
seen["uid"] = os.getuid() if hasattr(os, "getuid") else None
counter = out / "run-count"
counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
(out / "checkpoints").mkdir()
(out / "checkpoints" / "model.bin").write_bytes(b"weights")
(out / "metrics.json").write_text(json.dumps(seen))
print("workload done", flush=True)
"""
INPUTS = {"data/table.csv": b"a,b\n1,2\n", "lib.py": b"VALUE = 1\n"}


class Supervisor:
    """pod_agent.execute end to end, with the guard's IPC replaced and both disks as temp folders.

    real=True (Linux root) uses the real isolation (UID drop, final-UID checks) and gives the
    volume the measured Runpod behavior. Otherwise the isolation is replaced, as in test_safety.
    """

    def __init__(self, test, *, real=False, inputs=INPUTS, command=None, workspace=None):
        self.test, self.real = test, real
        temp = tempfile.TemporaryDirectory(prefix="pod-volume-flow-")
        test.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        if real:
            self.base.chmod(0o711)  # The workload user must reach its output folder.
        self.workspace = workspace or self.base / "workspace"  # The Pod's own disk.
        self.volume = self.base / "guard" / "data"  # The network volume's mount point.
        self.workspace.mkdir(exist_ok=True)
        self.volume.mkdir(parents=True)
        self.job = job(command or [sys.executable, "-B", "-c", WORKLOAD])
        # The list reaches the Pod as on Runpod.
        self.manifest = with_input_manifest(self.job, inputs)
        self.job["config"]["workload_env"] = {"PROBE_VOLUME": str(self.volume)}
        self.inputs = inputs
        self.layouts = []

    def fill(self, inputs=None, release=True):
        fill_volume(self.volume, self.job, inputs, release, manifest=self.manifest)

    @property
    def local(self):
        return self.workspace / "jobs" / self.job["job_id"]

    @property
    def remote(self):
        return self.volume / "jobs" / self.job["job_id"]

    def run(self, workspace=None):
        if workspace is not None:
            self.workspace = workspace
            self.workspace.mkdir(exist_ok=True)
        patches = [
            patch("podvigil.podrt.agent.workspace_root", return_value=self.workspace),
            patch("podvigil.podrt.agent.volume_root", return_value=self.volume),
            patch("podvigil.podrt.agent.volume_mounted", return_value=True),
            patch("podvigil.podrt.agent.exchange"),
            patch("podvigil.podrt.agent.signal_handlers", return_value={}),
            patch("podvigil.podrt.agent.restore_signals"),
        ]
        if self.real:
            code = self.base / "code"
            code.mkdir(exist_ok=True)
            shutil.copyfile(isolation.__file__, code / Path(isolation.__file__).name)
            real_prepare = isolation.prepare

            def prepare(root, folder, _code, *, volume):
                layout = real_prepare(root, folder, code, volume=volume)
                layout["guard_pid"] = os.getpid()  # This process plays guard and supervisor.
                self.layouts.append(layout)
                self.test.addCleanup(shutil.rmtree, layout["home"], True)
                return layout

            patches += [
                patch("podvigil.podrt.agent.isolation.prepare", side_effect=prepare),
                patch(
                    "podvigil.podrt.isolation.mounts",
                    return_value=[("/", "0:1", "/"), (str(self.volume), "0:99", "/")],
                ),
            ]
        else:
            scratch = self.base / "home"
            scratch.mkdir(exist_ok=True)

            def prepare(root, folder, code, *, volume):
                self.test.assertEqual(volume, self.volume)
                return {
                    "output": str(folder / "artifacts"),
                    "inputs": str(root / "inputs"),
                    "home": str(scratch),
                }

            def launch(argv, layout, environment, stdout, *, timeout, authorize):
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

            def stop_child(child):
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)

            patches += [
                patch("podvigil.podrt.agent.isolation.prepare", side_effect=prepare),
                patch("podvigil.podrt.agent.isolation.launch", side_effect=launch),
                patch("podvigil.podrt.agent.stop_child", side_effect=stop_child),
            ]
        for item in patches:
            item.start()
        model = RunpodVolumeModel(self.volume).__enter__() if self.real else None
        channel = Channel()
        try:
            agent.execute(self.job, OwnPodApi(), "testpod", channel)
        finally:
            if model is not None:
                model.__exit__(None, None, None)
            for item in reversed(patches):
                item.stop()
            time.sleep(0.05)  # Let the heartbeat thread see its stop flag.
        self.test.assertTrue(channel.closed)
        return read(self.remote / "artifacts" / "result.json")


class SupervisorFlowTests(unittest.TestCase):
    def test_outputs_reach_the_volume_and_the_controller_collects_and_audits_them(self):
        pod = Supervisor(self)
        pod.fill({**INPUTS, "unlisted.txt": b"never copied"})
        result = pod.run()
        self.assertEqual(
            result["outcome"], "success", read(pod.remote / "control/guard-failure.json")
        )
        # Only the listed inputs, checked, on the Pod's disk.
        self.assertEqual(keys(pod.workspace / "inputs"), set(INPUTS))
        metrics = json.loads((pod.remote / "artifacts" / "metrics.json").read_text())
        self.assertEqual(metrics["inputs"], sorted(INPUTS))
        self.assertEqual(metrics["table"], "a,b\n1,2\n")
        self.assertTrue(metrics["cwd_is_output"])
        # The controller polls the records under their usual keys.
        # The manifest matches the local copy byte for byte.
        for name in ("agent.json", "started-once.json", "start.lock", "control/identity.json"):
            self.assertTrue((pod.remote / name).is_file(), name)
        self.assertEqual(read(pod.remote / "agent.json")["phase"], "done")
        self.assertEqual(
            (pod.remote / "artifact-manifest.json").read_bytes(),
            (pod.local / "artifact-manifest.json").read_bytes(),
        )
        self.assertFalse((pod.remote / "control/finalization-incomplete.json").exists())
        self.assertFalse((pod.remote / "control/guard-failure.json").exists())
        self.assertFalse(any(".tmp" in name for name in keys(pod.volume)))
        # The real controller uploads listed inputs only.
        (pod.volume / "inputs" / "unlisted.txt").unlink()
        manifest, collected = controller_accepts(self, pod.volume, pod.job, INPUTS)
        self.assertEqual(collected["outcome"], "success")
        self.assertIn(
            "artifacts/checkpoints/model.bin", [item["path"] for item in manifest["files"]]
        )

    def test_changed_input_stops_before_the_workload_and_the_reason_reaches_the_volume(self):
        pod = Supervisor(self)
        pod.fill({**INPUTS, "lib.py": b"VALUE = 2\n"})  # Not the recorded bytes.
        result = pod.run()
        self.assertEqual(result["outcome"], "agent_failed")
        self.assertFalse((pod.remote / "artifacts" / "run-count").exists())
        failure = read(pod.remote / "control/guard-failure.json")
        self.assertEqual(failure["actor"], "pod_supervisor")
        self.assertIn("'lib.py' was not copied and checked", failure["message"])
        self.assertFalse((pod.workspace / "inputs" / "lib.py").exists())
        # A claimed attempt still ends with a complete manifest the controller can collect. (Its
        # delete audit then keeps the volume: the input there is not what the controller uploaded.)
        self.assertEqual(
            controller_accepts(self, pod.volume, pod.job, INPUTS, audit=False)[1]["outcome"],
            "agent_failed",
        )

    def test_container_restart_wipes_the_disk_but_never_runs_the_workload_twice(self):
        pod = Supervisor(self)
        pod.fill(INPUTS)
        self.assertEqual(pod.run()["outcome"], "success")
        before = {
            name: (pod.remote / name).read_bytes()
            for name in keys(pod.remote)
            if name.startswith("artifacts/")
        }
        manifest = (pod.remote / "artifact-manifest.json").read_bytes()
        pod.run(workspace=pod.base / "fresh-disk")  # The container disk is wiped on restart.
        self.assertEqual(manifest, (pod.remote / "artifact-manifest.json").read_bytes())
        self.assertEqual(before, {name: (pod.remote / name).read_bytes() for name in before})
        self.assertEqual((pod.remote / "artifacts" / "run-count").read_text(), "1")
        self.assertFalse(
            (
                pod.base / "fresh-disk" / "jobs" / pod.job["job_id"] / "artifacts" / "run-count"
            ).exists()
        )
        self.assertIn("restart", read(pod.remote / "control/guard-failure.json")["message"])

    def test_a_cancel_request_stops_the_workload_which_publishes_cancelled_outputs(self):
        # F2: The watchdog writes cancel.json. The supervisor stops the workload
        # and publishes the outputs as it does at the time limit.
        workload = (
            "import os,pathlib,time;o=pathlib.Path(os.environ['WONDOMON_OUTPUT_DIR']);"
            "(o/'partial.bin').write_bytes(b'progress');(o/'started').write_text('1');time.sleep(60)"
        )
        pod = Supervisor(self, inputs={}, command=[sys.executable, "-B", "-c", workload])
        pod.fill({})

        def ask_to_finish():
            end = time.monotonic() + 30
            while not (pod.local / "artifacts" / "started").exists() and time.monotonic() < end:
                time.sleep(0.05)
            save(
                pod.remote / "cancel.json",
                {
                    "job_id": pod.job["job_id"],
                    "pod_id": "testpod",
                    "requested_utc": "2026-11-12T23:00:00+00:00",
                    "grace_seconds": 90,
                },
            )

        asker = threading.Thread(target=ask_to_finish)
        asker.start()
        self.addCleanup(asker.join)
        started = time.monotonic()
        result = pod.run()
        self.assertEqual(result["outcome"], "cancelled")
        self.assertLess(time.monotonic() - started, 40)  # well before the workload's own time limit
        self.assertEqual((pod.remote / "artifacts" / "partial.bin").read_bytes(), b"progress")
        self.assertEqual(
            controller_accepts(self, pod.volume, pod.job, {})[1]["outcome"], "cancelled"
        )

    def test_a_cancel_request_for_another_pod_is_ignored(self):
        pod = Supervisor(self)
        pod.fill(INPUTS)
        save(
            pod.remote / "cancel.json",
            {
                "job_id": pod.job["job_id"],
                "pod_id": "otherpod",
                "requested_utc": "2026-11-12T23:00:00+00:00",
                "grace_seconds": 90,
            },
        )
        self.assertEqual(pod.run()["outcome"], "success")

    def test_half_written_release_marker_is_waited_for(self):
        pod = Supervisor(self)
        pod.fill(INPUTS, release=False)
        marker = pod.remote / "start.json"
        marker.parent.mkdir(parents=True, exist_ok=True)  # the input manifest is there already
        marker.write_bytes(b'{"job_id": "')  # An S3 write seen before it is complete.

        def complete():
            time.sleep(0.6)
            marker.write_bytes(
                json.dumps({"job_id": pod.job["job_id"], "pod_id": "testpod"}).encode()
            )

        writer = threading.Thread(target=complete)
        writer.start()
        self.addCleanup(writer.join)
        self.assertEqual(pod.run()["outcome"], "success")

    @unittest.skipUnless(LINUX_ROOT, "Needs Linux root for the real UID drop and ownership")
    def test_real_isolation_on_a_modeled_runpod_volume(self):
        pod = Supervisor(self, real=True)
        pod.fill(INPUTS)
        result = pod.run()
        self.assertEqual(
            result["outcome"], "success", (pod.local / "artifacts" / "train.log").read_text()
        )
        metrics = json.loads((pod.remote / "artifacts" / "metrics.json").read_text())
        self.assertEqual(metrics["uid"], 61000)
        self.assertEqual((metrics["stat"], metrics["list_lock"], metrics["open"]), ("EACCES",) * 3)
        self.assertEqual(metrics["inputs"], sorted(INPUTS))
        lock = pod.volume.parent.stat()
        self.assertEqual((lock.st_uid, lock.st_gid, stat.S_IMODE(lock.st_mode)), (0, 0, 0o700))
        # Read-only and root-owned on the Pod's disk. The volume reports mode 777 for everything,
        # so the copy keeps an execute bit: read and execute, never write.
        local_input = (pod.workspace / "inputs" / "lib.py").stat()
        self.assertEqual((local_input.st_uid, stat.S_IMODE(local_input.st_mode)), (0, 0o555))
        self.assertEqual(pod.layouts[0]["volume"], str(pod.volume))
        self.assertEqual(
            controller_accepts(self, pod.volume, pod.job, INPUTS)[1]["outcome"], "success"
        )


class InputCopyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pod-volume-inputs-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / "volume" / "inputs"
        self.destination = self.base / "disk" / "inputs"
        self.destination.parent.mkdir()
        for name, data in {**INPUTS, "unlisted.txt": b"no"}.items():
            (self.source / name).parent.mkdir(parents=True, exist_ok=True)
            (self.source / name).write_bytes(data)
        self.job = {"inputs": ledger_inputs(INPUTS)}

    def test_copies_only_listed_inputs_after_checking_them(self):
        agent.copy_inputs(self.job, self.source, self.destination)
        self.assertEqual(keys(self.destination), set(INPUTS))
        for name, data in INPUTS.items():
            self.assertEqual((self.destination / name).read_bytes(), data)

    def test_changed_short_or_missing_input_is_refused_and_never_named(self):
        for name, change in (
            ("lib.py", b"VALUE = 9\n"),
            ("lib.py", b"VAL"),
            ("data/table.csv", None),
        ):
            with self.subTest(name=name, change=change):
                shutil.rmtree(self.destination, ignore_errors=True)
                original = (self.source / name).read_bytes()
                if change is None:
                    (self.source / name).unlink()
                else:
                    (self.source / name).write_bytes(change)
                try:
                    with self.assertRaises((RuntimeError, FileNotFoundError)):
                        agent.copy_inputs(self.job, self.source, self.destination)
                    self.assertFalse((self.destination / name).exists())
                    self.assertFalse(
                        [p for p in self.destination.rglob("*") if p.name.endswith(".tmp")]
                    )
                finally:
                    (self.source / name).write_bytes(original)

    def test_folder_that_already_holds_files_is_refused(self):
        self.destination.mkdir()
        agent.copy_inputs({"inputs": []}, self.source, self.destination)  # Empty is fine.
        (self.destination / "planted.py").write_bytes(b"unchecked")
        with self.assertRaisesRegex(RuntimeError, "already holds files"):
            agent.copy_inputs(self.job, self.source, self.destination)

    def test_cancellation_stops_the_copy(self):
        def cancelled():
            raise InterruptedError("Cancellation requested.")

        with self.assertRaises(InterruptedError):
            agent.copy_inputs(self.job, self.source, self.destination, cancelled)
        self.assertEqual(keys(self.destination), set())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux symlinks")
    def test_link_on_the_volume_is_never_followed(self):
        secret = self.base / "secret"
        secret.write_bytes(b"VALUE = 1\n")  # Same bytes: only the link itself may stop it.
        (self.source / "lib.py").unlink()
        (self.source / "lib.py").symlink_to(secret)
        with self.assertRaises(OSError):
            agent.copy_inputs(self.job, self.source, self.destination)
        self.assertFalse((self.destination / "lib.py").exists())


class PublishTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pod-volume-publish-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.job = job()
        self.folder = base / "disk" / "jobs" / self.job["job_id"]
        self.volume = base / "volume"
        self.remote = self.volume / "jobs" / self.job["job_id"]
        self.output = self.folder / "artifacts"
        self.output.mkdir(parents=True)
        (self.folder / "control").mkdir()
        self.remote.mkdir(parents=True)
        fill_volume(self.volume, self.job, release=False)
        save(self.output / "result.json", {"job_id": self.job["job_id"], "outcome": "success"})
        (self.output / "train.log").write_bytes(b"step 1\n")
        (self.output / "checkpoints").mkdir()
        (self.output / "checkpoints" / "model.bin").write_bytes(b"weights")

    def snapshot(self):
        return output_bundle.finalize(self.job, self.folder, quiesced=True)

    def test_portable_snapshot_keeps_the_controller_layout(self):
        manifest = self.snapshot()
        output_bundle.publish(self.job, self.folder, self.remote, manifest)
        prefix = "jobs/" + self.job["job_id"] + "/"
        self.assertEqual(
            {key for key in keys(self.volume) if key.startswith(prefix)},
            {
                prefix + name
                for name in (
                    "artifacts/result.json",
                    "artifacts/train.log",
                    "artifacts/checkpoints/model.bin",
                    "control/output-links.json",
                    "artifact-manifest.json",
                )
            },
        )
        self.assertEqual(
            (self.remote / "artifact-manifest.json").read_bytes(),
            (self.folder / "artifact-manifest.json").read_bytes(),
        )
        self.assertEqual(controller_accepts(self, self.volume, self.job)[1]["outcome"], "success")

    def test_bundle_snapshot_also_copies_the_original_files(self):
        # The hardlink requires a bundle.
        os.link(self.output / "checkpoints" / "model.bin", self.output / "alias.bin")
        manifest = self.snapshot()
        self.assertEqual(manifest["version"], 2)
        output_bundle.publish(self.job, self.folder, self.remote, manifest)
        for name in (
            "artifacts/alias.bin",
            "artifacts/checkpoints/model.bin",
            manifest["bundle"]["archive"],
            manifest["bundle"]["metadata"],
        ):
            self.assertTrue((self.remote / name).is_file(), name)
        self.assertEqual((self.remote / "artifacts/alias.bin").stat().st_nlink, 1)
        self.assertEqual(controller_accepts(self, self.volume, self.job)[1]["outcome"], "success")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux names, links and empty folders")
    def test_bundle_with_links_odd_names_and_empty_folders(self):
        (self.output / "epoch:1.bin").write_bytes(b"checkpoint")
        (self.output / "latest").symlink_to("epoch:1.bin")
        (self.output / "outside").symlink_to("/etc/hostname")
        (self.output / "empty").mkdir()
        manifest = self.snapshot()
        self.assertEqual(manifest["version"], 2)
        output_bundle.publish(self.job, self.folder, self.remote, manifest)
        self.assertEqual((self.remote / "artifacts/epoch:1.bin").read_bytes(), b"checkpoint")
        # The links are never copied or followed.
        for name in ("latest", "outside"):
            self.assertFalse(os.path.lexists(self.remote / "artifacts" / name))
        self.assertTrue((self.remote / "artifacts/empty").is_dir())
        self.assertEqual(
            read(self.remote / "control/output-links.json")["links"],
            ["artifacts/latest", "artifacts/outside"],
        )
        self.assertEqual(controller_accepts(self, self.volume, self.job)[1]["outcome"], "success")

    def test_manifest_is_last_and_outcome_and_log_go_first(self):
        manifest = self.snapshot()
        order = []
        actual = output_bundle.copy_file

        def observed(source, name, *args, **kwargs):
            order.append(name)
            return actual(source, name, *args, **kwargs)

        with patch("podvigil.podrt.bundle.copy_file", side_effect=observed):
            output_bundle.publish(self.job, self.folder, self.remote, manifest)
        self.assertEqual(order[:2], ["artifacts/result.json", "artifacts/train.log"])
        self.assertEqual(order[-1], "artifact-manifest.json")

    def test_nothing_is_replaced_but_the_early_outcome(self):
        manifest = self.snapshot()
        save(
            self.remote / "artifacts" / "result.json",
            {"job_id": self.job["job_id"], "outcome": "success"},
        )
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            output_bundle.publish(self.job, self.folder, self.remote, manifest)
        self.assertFalse((self.remote / "artifact-manifest.json").exists())
        shutil.rmtree(self.remote / "artifacts")
        save(
            self.remote / "artifacts" / "result.json",
            {"job_id": self.job["job_id"], "outcome": "early"},
        )
        output_bundle.publish(
            self.job, self.folder, self.remote, manifest, replace=("artifacts/result.json",)
        )
        self.assertEqual(
            (self.remote / "artifacts" / "result.json").read_bytes(),
            (self.output / "result.json").read_bytes(),
        )
        with self.assertRaisesRegex(RuntimeError, "already holds a manifest"):
            output_bundle.publish(self.job, self.folder, self.remote, manifest)

    def test_cutoff_or_changed_output_leaves_no_manifest(self):
        manifest = self.snapshot()
        with self.assertRaises(TimeoutError):
            output_bundle.publish(
                self.job, self.folder, self.remote, manifest, deadline=time.monotonic() - 1
            )
        self.assertFalse((self.remote / "artifact-manifest.json").exists())
        shutil.rmtree(self.remote / "artifacts", ignore_errors=True)
        # Same size, different bytes.
        (self.output / "checkpoints" / "model.bin").write_bytes(b"WEIGHTS")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            output_bundle.publish(self.job, self.folder, self.remote, manifest)
        self.assertFalse((self.remote / "artifact-manifest.json").exists())
        self.assertFalse((self.remote / "artifacts" / "checkpoints" / "model.bin").exists())
        self.assertFalse([name for name in keys(self.volume) if name.endswith(".tmp")])

    def test_salvage_copies_raw_outputs_without_a_manifest(self):
        self.assertEqual(output_bundle.salvage(self.folder, self.remote), 3)
        self.assertEqual((self.remote / "artifacts/checkpoints/model.bin").read_bytes(), b"weights")
        self.assertFalse((self.remote / "artifact-manifest.json").exists())
        self.assertEqual(output_bundle.salvage(self.folder, self.remote), 0)  # Nothing is replaced.
        self.assertEqual(
            output_bundle.salvage(self.folder, self.remote, deadline=time.monotonic() - 1), 0
        )

    def test_salvage_skips_external_hardlinks_including_reserved_paths(self):
        outside = self.volume.parent / "private.bin"
        outside.write_bytes(b"dummy content outside the output tree")
        for name in ("result.json", "train.log", "cache-alias.bin"):
            (self.output / name).unlink(missing_ok=True)
            os.link(outside, self.output / name)
        with patch.object(output_bundle, "_open_file", wraps=output_bundle._open_file) as opened:
            self.assertEqual(output_bundle.salvage(self.folder, self.remote), 1)
        self.assertEqual(
            [call.args[1] for call in opened.call_args_list], ["artifacts/checkpoints/model.bin"]
        )
        self.assertEqual((self.remote / "artifacts/checkpoints/model.bin").read_bytes(), b"weights")
        for name in ("result.json", "train.log", "cache-alias.bin"):
            self.assertFalse(os.path.lexists(self.remote / "artifacts" / name))
        self.assertFalse((self.remote / "artifact-manifest.json").exists())
        self.assertEqual(outside.read_bytes(), b"dummy content outside the output tree")

    def test_finalization_salvages_checkpoint_after_rejecting_an_external_hardlink(self):
        outside = self.volume.parent / "private.bin"
        outside.write_bytes(b"dummy outside content")
        os.link(outside, self.output / "cache-alias.bin")
        result = read(self.output / "result.json")
        with (
            patch.object(agent, "volume_mounted", return_value=True),
            patch.object(agent, "volume_folder", return_value=self.remote),
        ):
            self.assertFalse(
                agent.finalize_outputs(
                    self.job, "testpod", self.folder, result, lambda: None, volume=self.remote
                )
            )
        diagnostic = read(self.remote / "control/finalization-incomplete.json")
        self.assertEqual(diagnostic["salvaged_files"], 3)
        self.assertFalse(diagnostic["complete"])
        self.assertEqual((self.remote / "artifacts/checkpoints/model.bin").read_bytes(), b"weights")
        self.assertFalse((self.remote / "artifacts/cache-alias.bin").exists())
        self.assertFalse((self.remote / "artifact-manifest.json").exists())

    def test_salvage_expired_deadline_publishes_no_content(self):
        self.assertEqual(
            output_bundle.salvage(self.folder, self.remote, deadline=time.monotonic() - 1), 0
        )
        self.assertFalse((self.remote / "artifacts/result.json").exists())
        self.assertFalse((self.remote / "artifacts/train.log").exists())
        self.assertFalse((self.remote / "artifacts/checkpoints/model.bin").exists())
        self.assertFalse([name for name in keys(self.volume) if name.endswith(".tmp")])
        self.assertFalse((self.remote / "artifact-manifest.json").exists())

    def test_salvage_rechecks_identity_before_reading_a_replaced_file(self):
        outside = self.volume.parent / "private.bin"
        outside.write_bytes(b"private")  # Same size as the checkpoint.
        real_scan, real_open = output_bundle._salvage_files, output_bundle._open_file
        read_files = []

        def replaced(*args):
            files = real_scan(*args)
            checkpoint = self.output / "checkpoints/model.bin"
            checkpoint.unlink()
            os.link(outside, checkpoint)
            return files

        @contextmanager
        def observed(*args):
            with real_open(*args) as stream:
                # Record access only after the open and identity checks pass.
                read_files.append(args[1])
                yield stream

        with (
            patch.object(output_bundle, "_salvage_files", side_effect=replaced),
            patch.object(output_bundle, "_open_file", side_effect=observed),
        ):
            self.assertEqual(output_bundle.salvage(self.folder, self.remote), 2)
        self.assertNotIn("artifacts/checkpoints/model.bin", read_files)
        self.assertFalse((self.remote / "artifacts/checkpoints/model.bin").exists())
        self.assertFalse([name for name in keys(self.volume) if name.endswith(".tmp")])

    def test_salvage_continues_past_an_unreadable_subtree(self):
        blocked = self.output / "blocked"
        blocked.mkdir()
        (blocked / "unreadable.bin").write_bytes(b"not copied")
        original = os.scandir

        def denied(path):
            if (
                isinstance(path, int) and os.path.samestat(os.fstat(path), blocked.stat())
            ) or path == blocked:
                raise PermissionError("dummy subtree denied")
            return original(path)

        with patch.object(output_bundle.os, "scandir", side_effect=denied):
            self.assertEqual(output_bundle.salvage(self.folder, self.remote), 3)
        self.assertEqual((self.remote / "artifacts/checkpoints/model.bin").read_bytes(), b"weights")
        self.assertFalse((self.remote / "artifacts/blocked").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux special files")
    def test_salvage_skips_links_and_survives_special_files(self):
        outside = self.volume.parent / "private"
        outside.mkdir()
        (outside / "data.bin").write_bytes(b"dummy outside content")
        (self.output / "link").symlink_to(outside / "data.bin")
        (self.output / "linked-directory").symlink_to(outside, target_is_directory=True)
        os.link(outside / "data.bin", self.output / "hardlink")
        os.mkfifo(self.output / "pipe")
        with patch.object(output_bundle, "_open_file", wraps=output_bundle._open_file) as opened:
            self.assertEqual(output_bundle.salvage(self.folder, self.remote), 3)
        self.assertEqual(
            {call.args[1] for call in opened.call_args_list},
            {"artifacts/result.json", "artifacts/train.log", "artifacts/checkpoints/model.bin"},
        )
        for name in ("link", "linked-directory", "hardlink", "pipe"):
            self.assertFalse(os.path.lexists(self.remote / "artifacts" / name))
        self.assertTrue((self.remote / "artifacts/result.json").is_file())
        self.assertEqual((self.remote / "artifacts/checkpoints/model.bin").read_bytes(), b"weights")
        self.assertFalse((self.remote / "artifact-manifest.json").exists())


@unittest.skipUnless(LINUX_ROOT, "Needs Linux root for ownership, modes and a second UID")
class VolumeLockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pod-volume-lock-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.base.chmod(0o711)
        self.volume = self.base / "guard" / "data"
        self.volume.mkdir(parents=True)
        self.volume.parent.chmod(0o755)  # As a container runtime creates a mount point's parent.
        self.volume.chmod(0o777)
        self.entries = [("/", "0:1", "/"), (str(self.volume), "0:99", "/vol")]
        mounts = patch("podvigil.podrt.isolation.mounts", side_effect=lambda: list(self.entries))
        mounts.start()
        self.addCleanup(mounts.stop)

    def as_workload_user(self, code):
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            cwd="/",
            user=61000,
            group=61000,
            extra_groups=[],
            timeout=20,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_lock_makes_the_parent_root_only_and_holds_for_the_workload_user(self):
        before = self.as_workload_user(PROBE.replace("VOLUME", repr(str(self.volume))))
        self.assertEqual(before["stat"], "allowed")  # Without the lock the 777 volume is open.
        with RunpodVolumeModel(self.volume):
            self.assertEqual(
                isolation.lock_volume(self.volume),
                {"volume": str(self.volume), "volume_lock": str(self.volume.parent)},
            )
        info = self.volume.parent.stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (0, 0, 0o700))
        after = self.as_workload_user(PROBE.replace("VOLUME", repr(str(self.volume))))
        self.assertEqual(
            after, {"stat": "EACCES", "list_lock": "EACCES", "open": "EACCES", "create": "EACCES"}
        )

    def test_unmounted_volume_is_refused(self):
        self.entries = [("/", "0:1", "/")]
        with self.assertRaisesRegex(RuntimeError, "not mounted"):
            isolation.lock_volume(self.volume)
        self.assertEqual(stat.S_IMODE(self.volume.parent.stat().st_mode), 0o755)

    def test_second_mount_of_the_same_tree_is_refused(self):
        for other in (
            ("/elsewhere", "0:99", "/vol"),
            ("/elsewhere", "0:99", "/vol/jobs"),
            ("/elsewhere", "0:99", "/"),
            (str(self.volume.parent), "0:99", "/vol"),
        ):
            with self.subTest(other=other):
                self.entries = [("/", "0:1", "/"), (str(self.volume), "0:99", "/vol"), other]
                with self.assertRaisesRegex(RuntimeError, "also reachable"):
                    isolation.lock_volume(self.volume)

    def test_unrelated_mounts_of_the_same_device_and_mounts_inside_the_lock_are_fine(self):
        self.entries = [
            ("/", "0:1", "/"),
            (str(self.volume), "0:99", "/vol"),
            ("/review", "0:99", "/volume-of-someone-else"),
            ("/other", "0:98", "/vol"),
            (str(self.volume / "nested"), "0:99", "/vol/nested"),
        ]
        isolation.lock_volume(self.volume)

    def test_link_in_the_mount_path_or_a_top_level_mount_is_refused(self):
        alias = self.base / "alias"
        alias.symlink_to(self.volume.parent, target_is_directory=True)
        self.entries.append((str(alias / "data"), "0:99", "/vol"))
        with self.assertRaises(RuntimeError):
            isolation.lock_volume(alias / "data")
        with self.assertRaisesRegex(RuntimeError, "private parent"):
            isolation.lock_volume(Path("/data"))

    def test_final_uid_check_refuses_a_workload_that_could_reach_the_volume(self):
        root, code = self.base / "workspace", self.base / "code"
        root.mkdir()
        code.mkdir()
        shutil.copyfile(isolation.__file__, code / Path(isolation.__file__).name)
        folder = root / "jobs" / "test"
        layout = isolation.prepare(root, folder, code, volume=self.volume)
        layout["guard_pid"] = os.getpid()
        self.addCleanup(shutil.rmtree, layout["home"], True)
        self.volume.parent.chmod(0o755)  # The lock is lost after preparation.
        with (
            (folder / "artifacts" / "train.log").open("wb") as log,
            self.assertRaisesRegex(RuntimeError, "isolation checks did not pass"),
        ):
            isolation.launch(
                [sys.executable, "-c", "open('ran','w')"],
                layout,
                {"PATH": "/usr/bin:/bin"},
                log,
                timeout=10,
                authorize=lambda: None,
            )
        self.assertFalse((folder / "artifacts" / "ran").exists())

    def test_old_layout_fails_on_the_modeled_volume_as_the_real_pod_did(self):
        workspace = self.base / "old-workspace"  # The volume at /workspace, as before.
        workspace.mkdir()
        with RunpodVolumeModel(workspace):
            with self.assertRaisesRegex(
                RuntimeError,
                r"ownership/mode\. .*: uid 0, gid 0, mode 777; "
                r"required uid 0, gid 0, mode 711\.",
            ):
                isolation.prepare_control(workspace, workspace / "jobs" / "test")
            output = workspace / "artifacts"
            output.mkdir()
            with self.assertRaises(PermissionError):
                isolation.permissions(
                    output, isolation.WORKLOAD_UID, isolation.WORKLOAD_GID, 0o700, directory=True
                )

    def test_new_layout_prepares_on_the_pod_disk_with_the_modeled_volume(self):
        root, code = self.base / "workspace", self.base / "code"
        root.mkdir()
        code.mkdir()
        shutil.copyfile(isolation.__file__, code / Path(isolation.__file__).name)
        with RunpodVolumeModel(self.volume):
            layout = isolation.prepare(root, root / "jobs" / "test", code, volume=self.volume)
        self.addCleanup(shutil.rmtree, layout["home"], True)
        self.assertEqual(
            (layout["volume"], layout["volume_lock"]), (str(self.volume), str(self.volume.parent))
        )
        output = Path(layout["output"]).stat()
        self.assertEqual((output.st_uid, stat.S_IMODE(output.st_mode)), (61000, 0o700))
        with self.assertRaisesRegex(RuntimeError, "separate"):
            isolation.prepare(
                self.volume.parent / "workspace",
                self.volume.parent / "workspace" / "jobs" / "t",
                code,
                volume=self.volume,
            )


PROBE = r"""
import errno, json, os
volume = VOLUME
seen = {}
for name, action in (("stat", lambda: os.stat(volume)), ("list_lock", lambda: os.listdir(os.path.dirname(volume))),
                     ("open", lambda: os.close(os.open(volume, os.O_RDONLY))),
                     ("create", lambda: os.close(os.open(os.path.join(volume, "forged"), os.O_WRONLY | os.O_CREAT, 0o600)))):
    try:
        action()
        seen[name] = "allowed"
    except OSError as exc:
        seen[name] = errno.errorcode[exc.errno]
if seen["create"] == "allowed":
    os.unlink(os.path.join(volume, "forged"))
print(json.dumps(seen))
"""


class LayoutTests(unittest.TestCase):
    def test_volume_mounts_below_a_private_folder_outside_the_workspace(self):
        mount = Path(VOLUME_MOUNT)
        self.assertTrue(VOLUME_MOUNT.startswith("/") and mount.parent != Path("/"))
        self.assertEqual(mount.parent.parent.as_posix(), "/")
        self.assertNotIn(mount.parent.as_posix(), ("/workspace", "/tmp", "/root", "/home"))
        workspace = agent.workspace_root().as_posix()
        self.assertFalse(
            VOLUME_MOUNT.startswith(workspace + "/")
            or workspace.startswith(mount.parent.as_posix())
        )

    def test_records_go_to_the_volume_and_the_workspace_stays_on_the_pod_disk(self):
        value = job()
        self.assertEqual(agent.job_folder(value).as_posix(), "/workspace/jobs/" + value["job_id"])
        self.assertEqual(
            agent.volume_folder(value).as_posix(), VOLUME_MOUNT + "/jobs/" + value["job_id"]
        )
        with self.assertRaises(ValueError):
            agent.volume_folder({**value, "job_id": "../x"})

    def test_controller_mounts_the_volume_where_the_pod_looks(self):
        from tests.support.jobs import fixture
        from tests.support import runner as managed_run

        body = managed_run.payload(fixture(), "testvolume")
        self.assertEqual(
            body["mounts"]["network"], [{"volumeId": "testvolume", "path": VOLUME_MOUNT}]
        )

    def test_unmounted_volume_gets_no_records(self):
        with (
            tempfile.TemporaryDirectory(prefix="pod-volume-unmounted-") as tmp,
            patch("podvigil.podrt.agent.volume_root", return_value=Path(tmp) / "data"),
        ):
            self.assertFalse(agent.volume_mounted())
            self.assertFalse(agent.control_record(job(), "testpod", "identity.json", {}))
            self.assertFalse(agent.agent_status(job(), "testpod", "ready"))
            self.assertFalse((Path(tmp) / "data").exists())


if __name__ == "__main__":
    unittest.main()
