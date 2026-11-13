"""Tests for the Pod-side fixes made during the first paid use (2026-11-12).

1. On Runpod the container command is not PID 1 (an init is injected). The guard
   then becomes the reaper of its own descendants and drains only those.
2. A guard failure leaves its reason on the volume (control/guard-failure.json),
   with a probe of what the filesystems do with ownership and modes.

Portable cases fake the Linux calls. The real-process cases need Linux and root
(the network-disabled Linux container lane); they skip elsewhere.
"""

from pathlib import Path
from unittest.mock import patch
import base64
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

from podvigil.podrt import agent
from podvigil.podrt.records import read
from tests.support.linux_review_api import RunpodVolumeModel
from tests.support.paths import ROOT
from tests.support.pod import job

LINUX_ROOT = sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0
ACTUAL_DELETE = agent.delete_verified


def quick_cleanup():
    """The real deletion proof without its one-second read separation."""
    return patch(
        "podvigil.podrt.agent.delete_verified",
        side_effect=lambda *args, **kwargs: ACTUAL_DELETE(*args, sleep=lambda _: None, **kwargs),
    )


class LocalApi:
    """Own-Pod GET/DELETE only; DELETE is checked against what is already on the volume."""

    def __init__(self, on_delete=None):
        self.pod = {"id": "testpod", "cloud": "SECURE", "name": "x", "env": {}}
        self.calls, self.last_response, self.on_delete = [], None, on_delete

    def get_or_none(self, path):
        self.calls.append(("GET", path))
        self.last_response = {
            "method": "GET",
            "path": path,
            "status": 404 if self.pod is None else 200,
        }
        return self.pod

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        if method != "DELETE":
            raise AssertionError(method)
        if self.on_delete:
            self.on_delete()
        self.pod = None
        self.last_response = {"method": "DELETE", "path": path, "status": 204}


class PortableGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pod-hotfix-")
        self.addCleanup(temp.cleanup)
        self.volume = Path(temp.name) / "volume"
        self.volume.mkdir()
        self.job = job()
        self.armed = [False]
        for mock in (
            patch("podvigil.podrt.agent.volume_folder", return_value=self.volume),
            patch("podvigil.podrt.agent.volume_mounted", return_value=True),
            patch("podvigil.podrt.agent.SUBREAPER", self.armed),  # Never leaks into other tests.
            patch("podvigil.podrt.agent.sys.platform", "linux"),
            quick_cleanup(),
            patch.dict(os.environ, {"RUNPOD_POD_ID": "testpod"}),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def failure(self):
        return read(self.volume / "control/guard-failure.json")

    def test_guard_not_pid1_becomes_reaper_of_descendants_then_drains_them(self):
        api = LocalApi()
        with (
            patch("podvigil.podrt.agent.os.getpid", return_value=4321),
            patch("podvigil.podrt.agent.isolation.become_subreaper") as reaper,
            patch(
                "podvigil.podrt.agent.isolation.protect_code", side_effect=RuntimeError("stop here")
            ),
            patch("podvigil.podrt.agent.drain_processes") as drain,
        ):
            agent.guard(self.job, api, "testpod")
        reaper.assert_called_once_with()
        self.assertEqual(self.armed, [True])
        drain.assert_called_once_with(None, include_supervisor=True)
        self.assertIsNone(api.pod)  # The guard still deletes its Pod.

    def test_guard_as_pid1_does_not_ask_to_become_a_reaper(self):
        api = LocalApi()
        with (
            patch("podvigil.podrt.agent.os.getpid", return_value=1),
            patch("podvigil.podrt.agent.isolation.become_subreaper") as reaper,
            patch(
                "podvigil.podrt.agent.isolation.protect_code", side_effect=RuntimeError("stop here")
            ),
            patch("podvigil.podrt.agent.drain_processes") as drain,
        ):
            agent.guard(self.job, api, "testpod")
        reaper.assert_not_called()
        self.assertEqual(self.armed, [False])
        drain.assert_called_once_with(None, include_supervisor=True)
        self.assertIsNone(api.pod)

    def test_reaper_refusal_stops_the_guard_records_why_and_still_deletes(self):
        api = LocalApi(
            on_delete=lambda: self.assertEqual(self.failure()["error_type"], "RuntimeError")
        )
        with (
            patch("podvigil.podrt.agent.os.getpid", return_value=4321),
            patch(
                "podvigil.podrt.agent.isolation.become_subreaper",
                side_effect=RuntimeError("no subreaper"),
            ),
            patch("podvigil.podrt.agent.drain_processes") as drain,
            patch("podvigil.podrt.agent.subprocess.Popen") as spawn,
        ):
            agent.guard(self.job, api, "testpod")
        spawn.assert_not_called()
        drain.assert_not_called()  # Neither PID 1 nor an armed reaper: it touches no process.
        self.assertEqual(self.armed, [False])
        self.assertEqual(self.failure()["message"], "no subreaper")
        self.assertIsNone(api.pod)

    def test_drain_refuses_without_pid1_or_armed_reaper(self):
        with (
            patch("podvigil.podrt.agent.os.getpid", return_value=4321),
            self.assertRaises(RuntimeError),
        ):
            agent.drain_processes(None)

    def test_guard_failure_lands_on_the_volume_before_the_delete(self):
        seen = []
        api = LocalApi(on_delete=lambda: seen.append(self.failure()))
        with (
            patch("podvigil.podrt.agent.os.getpid", return_value=1),
            patch("podvigil.podrt.agent.drain_processes"),
            patch("podvigil.podrt.agent.isolation.protect_code"),
            patch(
                "podvigil.podrt.agent.isolation.lock_volume",
                side_effect=RuntimeError("The network volume is not mounted"),
            ),
        ):
            agent.guard(self.job, api, "testpod")
        self.assertEqual(seen[0]["error_type"], "RuntimeError")
        self.assertEqual(seen[0]["message"], "The network volume is not mounted")
        self.assertEqual(
            (seen[0]["actor"], seen[0]["pod_id"], seen[0]["job_id"]),
            ("pod_guard", "testpod", self.job["job_id"]),
        )
        self.assertTrue(seen[0]["where"].startswith(("_execute_mock_call:", "guard:")))
        self.assertNotIn("volume_probe", seen[0])
        self.assertIsNone(api.pod)


class GuardFailureRecordTests(unittest.TestCase):
    def setUp(self):
        self.job = job()
        recorded = self.recorded = []
        mock = patch(
            "podvigil.podrt.agent.control_record",
            side_effect=lambda job, pod, name, value, actor="pod_guard": recorded.append(
                (name, value, actor)
            ),
        )
        mock.start()
        self.addCleanup(mock.stop)

    @staticmethod
    def raised(message):
        def failing_step():
            raise RuntimeError(message)

        try:
            failing_step()
        except RuntimeError as exc:
            return exc

    def test_records_type_message_and_failing_place(self):
        agent.guard_failure(self.job, "testpod", self.raised("x" * 900))
        name, value, actor = self.recorded[-1]
        self.assertEqual((name, actor), ("guard-failure.json", "pod_guard"))
        self.assertEqual(value["error_type"], "RuntimeError")
        self.assertEqual(value["message"], "x" * 500)
        self.assertTrue(value["where"].startswith("failing_step:"))
        self.assertNotIn("volume_probe", value)

    def test_explicit_place_and_supervisor_actor(self):
        agent.guard_failure(
            self.job, "testpod", OSError("denied"), "protect_process", actor="pod_supervisor"
        )
        name, value, actor = self.recorded[-1]
        self.assertEqual((value["where"], actor), ("protect_process", "pod_supervisor"))

    def test_ownership_mode_failure_probes_the_volume_and_the_workspace(self):
        probed = []
        with patch(
            "podvigil.podrt.agent.volume_probe",
            side_effect=lambda root=None: probed.append(root) or {"root": str(root)},
        ):
            agent.guard_failure(
                self.job,
                "testpod",
                self.raised(
                    "Filesystem does not enforce required ownership/mode. /workspace: uid 0, gid 0, mode 777"
                ),
            )
        value = self.recorded[-1][1]
        self.assertEqual(probed, [None, agent.workspace_root()])
        self.assertIn("volume_probe", value)
        self.assertIn("workspace_probe", value)

    def test_never_raises(self):
        class Broken(Exception):
            def __str__(self):
                raise RuntimeError("unprintable")

        # Cleanup follows and must still run.
        agent.guard_failure(self.job, "testpod", Broken())
        with patch("podvigil.podrt.agent.volume_probe", side_effect=MemoryError):
            agent.guard_failure(self.job, "testpod", self.raised("ownership/mode"))


class ProtectProcessFailureTests(unittest.TestCase):
    """main(): a guard without memory protection records why, then always deletes its Pod."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pod-hotfix-main-")
        self.addCleanup(temp.cleanup)
        self.volume = Path(temp.name)
        self.job = job()
        self.api = LocalApi()
        spec = base64.b64encode(json.dumps(self.job).encode()).decode()
        for mock in (
            patch("podvigil.podrt.agent.volume_folder", return_value=self.volume),
            patch("podvigil.podrt.agent.volume_mounted", return_value=True),
            quick_cleanup(),
            patch("podvigil.podrt.agent.scoped_api", return_value=self.api),
            patch(
                "podvigil.podrt.agent.isolation.protect_process",
                side_effect=OSError(1, "prctl refused"),
            ),
            patch(
                "podvigil.podrt.agent.guard",
                side_effect=AssertionError("workload path must not start"),
            ),
            patch.object(sys, "argv", ["pod_agent.py"]),
            patch.dict(
                os.environ,
                {"RUNPOD_POD_ID": "testpod", "RUNPOD_API_KEY": "DUMMY", "WONDOMON_JOB_SPEC": spec},
            ),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def test_records_reason_then_deletes(self):
        agent.main()
        failure = read(self.volume / "control/guard-failure.json")
        self.assertEqual(
            (failure["error_type"], failure["where"]), ("PermissionError", "protect_process")
        )
        self.assertIsNone(self.api.pod)
        self.assertNotIn("RUNPOD_API_KEY", os.environ)

    def test_deletes_even_when_recording_is_interrupted(self):
        with (
            patch("podvigil.podrt.agent.guard_failure", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            agent.main()
        self.assertIsNone(self.api.pod)
        self.assertIn(("DELETE", "/pods/testpod"), self.api.calls)


class VolumeProbeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux filesystem model")
    def test_volume_model_covers_pathlib_and_os_and_restores_both(self):
        originals = {name: getattr(os, name) for name in RunpodVolumeModel.names}
        path_originals = {name: getattr(Path, name) for name in ("stat", "chmod")}
        with tempfile.TemporaryDirectory(prefix="pod-probe-api-") as tmp:
            root = Path(tmp)
            volume, outside = root / "volume", root / "outside"
            volume.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            item = volume / "file"
            item.write_bytes(b"dummy volume content")
            item.chmod(0o600)
            link = volume / "link"
            link.symlink_to("file")
            before = item.stat()
            with RunpodVolumeModel(volume), item.open("rb") as stream:
                for info in (
                    os.stat(item),
                    os.lstat(item),
                    os.fstat(stream.fileno()),
                    item.stat(),
                    item.lstat(),
                    item.stat(follow_symlinks=False),
                    volume.stat(),
                ):
                    self.assertEqual(
                        (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (0, 0, 0o777)
                    )
                self.assertTrue(stat.S_ISLNK(link.lstat().st_mode))
                self.assertTrue(stat.S_ISREG(link.stat().st_mode))
                for change in (
                    lambda: item.chmod(0o400),
                    lambda: item.lchmod(0o200),
                    lambda: os.chmod(item, 0o100),
                    lambda: os.fchmod(stream.fileno(), 0o000),
                ):
                    change()
                    self.assertEqual(stat.S_IMODE(originals["stat"](item).st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o777)
                with self.assertRaises(PermissionError):
                    os.chown(item, 61000, 61000)
                outside.chmod(0o750)
                self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o750)
                self.assertEqual(stat.S_IMODE(originals["stat"](outside).st_mode), 0o750)
            self.assertEqual(
                (item.stat().st_uid, item.stat().st_gid, item.stat().st_mode),
                (before.st_uid, before.st_gid, before.st_mode),
            )
        for name, original in originals.items():
            self.assertIs(getattr(os, name), original)
        for name, original in path_originals.items():
            self.assertIs(getattr(Path, name), original)

    def test_missing_root_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory(prefix="pod-probe-") as tmp:
            out = agent.volume_probe(Path(tmp) / "missing")
        self.assertIn("error", out)

    @unittest.skipUnless(LINUX_ROOT, "Needs Linux root to change owners")
    def test_pod_disk_keeps_owner_and_mode_and_nothing_is_left(self):
        with tempfile.TemporaryDirectory(prefix="pod-probe-") as tmp:
            out = agent.volume_probe(Path(tmp))
            self.assertEqual(os.listdir(tmp), [])
        self.assertNotIn("error", out)
        self.assertEqual(out["folder_root_700"], [0, 0, "700"])
        self.assertEqual(out["folder_user_700"], [61000, 61000, "700"])
        self.assertEqual(out["file_root_444"], [0, 0, "444"])

    @unittest.skipUnless(LINUX_ROOT, "Needs Linux root to change owners")
    def test_modeled_runpod_volume_reports_what_the_real_pod_reported(self):
        # The fourth paid Pod (2026-11-12, EU-CZ-1): uid 0, gid 0, mode 777; chmod has no
        # effect; chown to the workload user refused with "Operation not permitted".
        with tempfile.TemporaryDirectory(prefix="pod-probe-") as tmp, RunpodVolumeModel(tmp):
            out = agent.volume_probe(Path(tmp))
            self.assertEqual(os.listdir(tmp), [])
        self.assertEqual(out["root"], [0, 0, "777"])
        self.assertEqual(out["new_folder"], [0, 0, "777"])
        self.assertEqual(out["folder_root_700"], [0, 0, "777"])
        self.assertNotIn("folder_user_700", out)
        self.assertTrue(out["error"].startswith("PermissionError:"), out)
        self.assertIn("Operation not permitted", out["error"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Reads /proc/mounts")
    def test_reports_the_mount_line_of_a_mount_point(self):
        # The probe still reads the mount line when it cannot create anything there.
        out = agent.volume_probe(Path("/proc"))
        self.assertEqual(out["mount"][1], "/proc")
        self.assertIn("error", out)


REAPER_SCRIPT = textwrap.dedent(r"""
    import json, os, signal, subprocess, sys, time
    sys.path.insert(0, sys.argv[1])
    from podvigil.podrt import agent, isolation
    isolation.become_subreaper()
    agent.SUBREAPER[0] = True
    # A child that leaves an escaped grandchild (new session, double fork) behind.
    code = ("import os,sys,time\n"
            "if os.fork()==0:\n"
            "    os.setsid()\n"
            "    if os.fork()==0:\n"
            "        open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(60)\n"
            "    os._exit(0)\n"
            "os.wait(); time.sleep(60)")
    marker = sys.argv[2]
    child = subprocess.Popen([sys.executable, "-c", code, marker])
    until = time.monotonic() + 10
    while not (os.path.exists(marker) and open(marker).read()) and time.monotonic() < until:
        time.sleep(.05)
    orphan = int(open(marker).read())
    time.sleep(.3)
    parent = int(open(f"/proc/{orphan}/stat").read().rsplit(")", 1)[1].split()[1])
    found = sorted(agent.descendants(os.getpid()))
    agent.drain_processes(None, include_supervisor=True)
    print(json.dumps({"me": os.getpid(), "orphan": orphan, "orphan_parent": parent,
                      "child": child.pid, "descendants": found,
                      "after": sorted(agent.descendants(os.getpid()))}))
""")


@unittest.skipUnless(LINUX_ROOT, "Needs Linux root for the real reaper and process kills")
class RealReaperTests(unittest.TestCase):
    def test_orphans_come_to_the_guard_and_drain_kills_only_descendants(self):
        bystander = subprocess.Popen(["sleep", "30"])  # Not a descendant of the reaper.
        self.addCleanup(lambda: (bystander.kill(), bystander.wait()))
        with tempfile.TemporaryDirectory(prefix="pod-reaper-") as tmp:
            done = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    REAPER_SCRIPT,
                    str(ROOT / "src"),
                    str(Path(tmp) / "orphan"),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(out["orphan_parent"], out["me"])  # Re-parented to the reaper, not to init.
        self.assertIn(out["orphan"], out["descendants"])
        self.assertIn(out["child"], out["descendants"])
        self.assertEqual(out["after"], [])
        self.assertIsNone(bystander.poll())  # The reaper never touches other processes.
        with self.assertRaises(ProcessLookupError):
            os.kill(out["orphan"], 0)

    def test_descendants_follow_the_parent_chain_only(self):
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess,sys,time;p=subprocess.Popen(['sleep','30']);print(p.pid,flush=True);time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: (child.kill(), child.wait(), child.stdout.close()))
        grandchild = int(child.stdout.readline())
        self.addCleanup(lambda: os.kill(grandchild, signal.SIGKILL))
        found = agent.descendants(os.getpid())
        self.assertIn(child.pid, found)
        self.assertIn(grandchild, found)
        self.assertNotIn(os.getpid(), found)
        self.assertNotIn(1, found)


if __name__ == "__main__":
    unittest.main()
