"""Installed-wheel checks, deliberately outside normal discovery and its src/ path injection.

From a folder outside the checkout, use the new venv's Python:
    /path/to/venv/bin/python -I -B /checkout/tests/integration/wheel_smoke.py -v

Only the standard library and the installed wheel are used. CLI calls reject network access and
credential-file reads; the workloads are short local fixtures, with no cloud or GPU dependency.
"""

from importlib import metadata
from pathlib import Path
import ctypes
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest


CHECKOUT = Path(__file__).resolve().parents[2]
CLI = r"""
import os, runpy, sys
credential_file = os.path.abspath(os.environ['PODVIGIL_ENV_FILE'])
def offline(event, arguments):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.bind'):
        raise RuntimeError('Network access is disabled in the wheel smoke test.')
    if event == 'open' and isinstance(arguments[0], (str, bytes, os.PathLike)):
        if os.path.abspath(os.fsdecode(arguments[0])) == credential_file:
            raise RuntimeError('The wheel smoke test must not read credentials.')
sys.addaudithook(offline)
sys.argv = ['podvigil', *sys.argv[1:]]
runpy.run_module('podvigil', run_name='__main__')
"""

WORKLOAD = r"""
from pathlib import Path
import json, os, subprocess, sys, time
import helper
output = Path(os.environ['PODVIGIL_OUTPUT_DIR'])
inputs = Path(os.environ['PODVIGIL_INPUT_DIR'])
mode = os.environ['SMOKE_MODE']
child = subprocess.Popen([sys.executable, '-B', '-m', 'worker'],
                         start_new_session=os.name != 'nt',
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 5
while not (output / 'child.pid').is_file():
    if child.poll() is not None or time.monotonic() >= deadline:
        raise RuntimeError('The local smoke worker did not start.')
    time.sleep(.01)
if mode != 'missing':
    values = json.loads((inputs / 'data.json').read_text())
    (output / 'metrics.json').write_text(json.dumps({
        'value': sum(values) + helper.OFFSET,
        'cwd_is_output': Path.cwd().resolve() == output.resolve(),
        'credential_seen': any(name.startswith('RUNPOD_') for name in os.environ),
    }))
print('wheel workload ' + mode, flush=True)
if mode == 'timeout':
    time.sleep(20)
sys.exit(7 if mode == 'failure' else 0)
"""

WORKER = """
from pathlib import Path
import os, time
Path('child.pid').write_text(str(os.getpid()))
time.sleep(20)
"""


def snapshot(package):
    """Detect added/deleted files or directories, content changes, and metadata-only writes."""
    result = {}
    for path in [package, *sorted(package.rglob("*"))]:
        info = path.lstat()
        result[path.relative_to(package).as_posix()] = (
            info.st_mode,
            info.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None,
        )
    return result


def running(pid):
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE, read-only process wait.
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class InstalledWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Path.cwd().resolve().is_relative_to(CHECKOUT):
            raise AssertionError("Run the wheel smoke test outside the checkout.")
        if sys.prefix == sys.base_prefix:
            raise AssertionError("Use a fresh virtual environment containing the installed wheel.")
        cls.distribution = metadata.distribution("podvigil")
        cls.package = Path(cls.distribution.locate_file("podvigil")).resolve()
        if not cls.package.is_relative_to(Path(sys.prefix).resolve()):
            raise AssertionError("The package must come from this virtual environment.")
        cls.before = snapshot(cls.package)
        cls.addClassCleanup(cls.check_unchanged)
        files = {str(path).replace("\\", "/") for path in cls.distribution.files or ()}
        if "podvigil/localprocess.py" not in files:
            raise AssertionError("The wheel RECORD omits the local process helper.")
        helper = importlib.import_module("podvigil.localprocess")
        if Path(helper.__file__).resolve() != cls.package / "localprocess.py":
            raise AssertionError(
                "The local process helper was not imported from the installed wheel."
            )

    @classmethod
    def check_unchanged(cls):
        after = snapshot(cls.package)
        changed = sorted(
            name
            for name in cls.before.keys() | after.keys()
            if cls.before.get(name) != after.get(name)
        )
        if changed:
            raise AssertionError("The installed package changed: " + ", ".join(changed))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-wheel-smoke-", dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "unused-state"
        credentials = self.root / "dummy-credentials.env"
        credentials.write_text("RUNPOD_API_KEY=dummy-wheel-test\n", encoding="utf-8")
        self.environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("RUNPOD_", "PODVIGIL_", "WONDOMON_", "PAID_SMOKE_"))
            and name not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE")
        }
        self.environment.update(
            PODVIGIL_STATE_DIR=str(self.state),
            PODVIGIL_ENV_FILE=str(credentials),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONNOUSERSITE="1",
            AWS_EC2_METADATA_DISABLED="true",
        )
        for name, content in (
            ("train.py", WORKLOAD),
            ("worker.py", WORKER),
            ("helper.py", "OFFSET = 40\n"),
            ("data.json", "[1, 2, 3]\n"),
        ):
            (self.root / name).write_text(content, encoding="utf-8")
        self.config = {
            "cloud": "SECURE",
            "image": "python:3.13.7",
            "gpu_id": "NVIDIA GeForce RTX 4090",
            "data_center_id": "EU-RO-1",
            "gpu_count": 1,
            "min_cuda_version": "12.8",
            "max_compute_usd_per_hour": 1.0,
            "max_compute_usd": 0.20,
            "max_runtime_seconds": 600,
            "startup_timeout_seconds": 240,
            "finalize_grace_seconds": 60,
            "inputs": [
                {"local": name, "remote": name}
                for name in ("train.py", "worker.py", "helper.py", "data.json")
            ],
            "command": ["python3", "-B", "-u", "-m", "train"],
            "workload_env": {"SMOKE_MODE": "success"},
            "required_artifacts": ["metrics.json"],
        }
        self.job = self.root / "job.json"
        self.job.write_text(json.dumps(self.config), encoding="utf-8")

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-I", "-B", "-c", CLI, *arguments],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=45,
        )

    def test_installed_console_and_module_entry_points(self):
        console = Path(sys.executable).with_name("podvigil.exe" if os.name == "nt" else "podvigil")
        for command in (
            [str(console), "--version"],
            [sys.executable, "-I", "-B", "-m", "podvigil", "--version"],
        ):
            with self.subTest(command=command):
                done = subprocess.run(
                    command,
                    cwd=self.root,
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.strip(), "podvigil " + self.distribution.version)
        self.assertFalse(self.state.exists())

    def test_plan_creates_no_state(self):
        for job in (self.job, CHECKOUT / "examples" / "hello-gpu" / "job.json"):
            with self.subTest(job=job.name):
                done = self.cli("plan", str(job))
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertFalse(json.loads(done.stdout)["paid_resources_created"])
                self.assertFalse(self.state.exists())

    def test_run_local_lifecycle_and_descendant_cleanup(self):
        for mode, exit_code, outcome, workload_code in (
            ("success", 0, "success", 0),
            ("failure", 2, "training_failed", 7),
            ("timeout", 2, "time_limit", None),
            ("missing", 2, "success", 0),
        ):
            with self.subTest(mode=mode):
                self.config["workload_env"]["SMOKE_MODE"] = mode
                self.job.write_text(json.dumps(self.config), encoding="utf-8")
                work = self.root / ("run-" + mode)
                done = self.cli(
                    "run-local",
                    str(self.job),
                    "--python",
                    sys.executable,
                    "--workdir",
                    str(work),
                    "--timeout",
                    "5",
                )
                self.assertEqual(done.returncode, exit_code, done.stdout + done.stderr)
                result = json.loads(done.stdout)
                self.assertEqual(
                    result["outcome"], "success" if exit_code == 0 else "workload_failed"
                )
                self.assertEqual(result["workload_outcome"], outcome)
                if workload_code is not None:
                    self.assertEqual(result["workload_exit_code"], workload_code)
                self.assertEqual(result["inputs"]["files"], 4)
                self.assertEqual(result["cloud_calls"], 0)
                self.assertFalse(result["credentials_read"])
                self.assertFalse(self.state.exists())
                output = Path(result["output_dir"])
                self.assertTrue(output.is_relative_to(work))
                self.assertFalse(
                    running(int((output / "child.pid").read_text())),
                    "A local workload descendant survived completion.",
                )
                saved = json.loads((output / "result.json").read_text())
                self.assertEqual(saved["outcome"], outcome)
                self.assertEqual(saved["exit_code"], result["workload_exit_code"])
                self.assertIn("wheel workload " + mode, (output / "train.log").read_text())
                if mode == "missing":
                    self.assertEqual(result["required_artifacts_missing"], ["metrics.json"])
                else:
                    metrics = json.loads((output / "metrics.json").read_text())
                    self.assertEqual(
                        metrics, {"value": 46, "cwd_is_output": True, "credential_seen": False}
                    )
                    self.assertEqual(result["required_artifacts_missing"], [])
                self.assertEqual(
                    (work / "inputs" / "data.json").read_bytes(),
                    (self.root / "data.json").read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
