"""The real Windows watchdog launcher: the watchdog survives its controller's process tree and Job Object.

The cloud is the loopback fake; both processes resolve dummy credentials.
"""

from pathlib import Path
import ctypes
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.podrt.records import read, save
from tests.support.fakes import DUMMY_CREDENTIALS_CODE
from tests.support.jobs import fixture, ledger
from tests.support.loopback import LoopbackApi
from tests.support.paths import ROOT


@unittest.skipUnless(os.name == "nt", "Windows process isolation")
class RealWindowsLauncherTests(unittest.TestCase):
    def crash_case(self, mode):
        http = LoopbackApi().start()
        try:
            with tempfile.TemporaryDirectory(prefix="wondomon-tree-crash-") as tmp:
                folder = Path(tmp)
                job = fixture()
                job["deadline_epoch"] = time.time() + 18
                ledger(folder, job)
                # In the project venv, the watchdog must also see the venv's packages.
                # A fresh clone using stock Python without boto3 still checks process isolation
                # and survival.
                dependency = "import boto3;" if importlib.util.find_spec("boto3") else ""
                watch_code = (
                    DUMMY_CREDENTIALS_CODE
                    + "from pathlib import Path;from tests.support.runner import watchdog;"
                    "from podvigil.podrt.api import Api;from tests.support.fakes import NoAgentStorage;"
                    + dependency
                    + f"watchdog(Path({str(folder)!r}),Api('dummy',{http.url!r}),NoAgentStorage())"
                )
                controller_code = (
                    DUMMY_CREDENTIALS_CODE + "from pathlib import Path;import sys,time;"
                    "from tests.support.runner import spawn_watchdog;from podvigil.podrt.records import save;"
                    f"p=Path({str(folder)!r})\n"
                    "while not (p/'go').exists(): time.sleep(.05)\n"
                    f"m=spawn_watchdog(p,[sys.executable,'-B','-c',{watch_code!r}]);"
                    "save(p/'controller-armed.json',{'pid':m.pid});time.sleep(90)"
                )
                controller = subprocess.Popen(
                    [sys.executable, "-B", "-c", controller_code],
                    cwd=ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                job_handle = None
                try:
                    if mode == "job":
                        job_handle = kill_on_close_job(controller.pid)
                    (folder / "go").touch()
                    end = time.monotonic() + 12
                    while (
                        not (folder / "controller-armed.json").exists() and time.monotonic() < end
                    ):
                        time.sleep(0.05)
                    self.assertTrue(
                        (folder / "controller-armed.json").exists(),
                        (folder / "watchdog.log").read_text(errors="replace")
                        if (folder / "watchdog.log").exists()
                        else "no watchdog",
                    )
                    proof = read(folder / "watchdog-isolation.json")
                    self.assertFalse(proof["in_job"])
                    if mode == "tree":
                        killed = subprocess.run(
                            ["taskkill.exe", "/PID", str(controller.pid), "/T", "/F"],
                            capture_output=True,
                            timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        self.assertEqual(killed.returncode, 0)
                    else:
                        close_job(job_handle)
                        job_handle = None
                    controller.wait(timeout=8)
                    from podvigil.controller.watchdog_spawn import Monitor

                    monitor = Monitor(proof["pid"])
                    self.assertIsNone(monitor.poll(), "watchdog died with controller")
                    end = time.monotonic() + 25
                    while not (folder / "pod-deleted.json").exists() and time.monotonic() < end:
                        time.sleep(0.1)
                    self.assertTrue((folder / "pod-deleted.json").exists())
                    self.assertFalse(http.api.pods)
                    while monitor.poll() is None and time.monotonic() < end:
                        time.sleep(0.1)
                    self.assertIsNotNone(monitor.poll())
                    monitor.close()
                finally:
                    if job_handle:
                        close_job(job_handle)
                    if controller.poll() is None:
                        controller.kill()
                        controller.wait(timeout=5)
        finally:
            http.stop()

    def test_survives_taskkill_entire_controller_tree(self):
        self.crash_case("tree")

    def test_survives_kill_on_close_job_object(self):
        self.crash_case("job")

    def test_actual_default_launcher_arms_under_venv(self):
        # A private source copy supplies a dummy .env and no resource intents;
        # the production spawn_watchdog(folder) call is completely unpatched.
        with tempfile.TemporaryDirectory(prefix="wondomon-real-launch-") as tmp:
            root = Path(tmp)
            for source in ROOT.glob("*.py"):
                shutil.copyfile(source, root / source.name)
            shutil.copytree(
                ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__")
            )
            (root / ".env").write_text("RUNPOD_API_KEY=dummy-local-test\n")
            folder = root / "ledger"
            job = fixture()
            job["preparation_deadline_epoch"] = time.time() - 1
            save(folder / "job.json", job)
            save(
                folder / "pod-creation-state.json",
                {"job_id": job["job_id"], "state": "never_dispatched"},
            )
            code = (
                "from pathlib import Path;import sys,time;sys.path.insert(0,'src');"
                "from podvigil.controller.watchdog import spawn_watchdog;"
                f"p=Path({str(folder)!r});m=spawn_watchdog(p);print('ARMED',flush=True);"
                "end=time.monotonic()+10\nwhile m.poll() is None and time.monotonic()<end: time.sleep(.05)\n"
                "assert m.poll() is not None, 'watchdog did not finish'\n"
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", code],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=35,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("ARMED", result.stdout)
            proof = read(folder / "watchdog-isolation.json")
            self.assertFalse(proof["in_job"])
            self.assertFalse(proof["live_controller_ancestor"])
            self.assertTrue(proof["broker_exited"])
            self.assertEqual(Path(proof["executable"]).resolve(), Path(sys.executable).resolve())


def close_job(handle):
    from podvigil.controller.watchdog_spawn import _kernel

    _kernel().CloseHandle(handle)


def kill_on_close_job(pid):
    from ctypes import wintypes as w
    from podvigil.controller.watchdog_spawn import _kernel

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", w.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", w.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", w.DWORD),
            ("SchedulingClass", w.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k = _kernel()
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    handle = k.CreateJobObjectW(None, None)
    limits = Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000
    process = k.OpenProcess(0x0100 | 0x0001, False, pid)
    try:
        if (
            not handle
            or not process
            or not k.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not k.AssignProcessToJobObject(handle, process)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception:
        if handle:
            k.CloseHandle(handle)
        raise
    finally:
        if process:
            k.CloseHandle(process)
    return handle
