"""run-local: a job's workload on this computer under the workload contract, with no cloud call.

The workload gets the Pod's folders and variables (built by the Pod's own code); its outcome, log, result.json
and required artifacts are checked as recovery would. It claims no isolation.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.localrun import copy_inputs
from tests.support import runner as run
from tests.support.paths import ROOT

WORKLOAD = r"""
import json, os, sys
from pathlib import Path
out, inputs = Path(os.environ["WONDOMON_OUTPUT_DIR"]), Path(os.environ["WONDOMON_INPUT_DIR"])
import helper  # from the input folder, through PYTHONPATH
seen = {"cwd_is_output": Path.cwd().resolve() == out.resolve(), "helper": helper.VALUE,
        "table": (inputs / "data" / "table.csv").read_text(), "home": os.environ["HOME"],
        "secret_seen": "RUNPOD_API_KEY" in os.environ, "declared": os.environ.get("EXPERIMENT")}
try:
    (inputs / "data" / "table.csv").write_text("changed")
    seen["input_writable"] = True
except OSError:
    seen["input_writable"] = False
(out / "metrics.json").write_text(json.dumps(seen))
print("workload done", flush=True)
sys.exit(int(os.environ.get("EXIT_CODE", "0")))
"""


def alive(pid):
    if os.name == "nt":
        from podvigil.controller.watchdog_spawn import Monitor

        try:
            monitor = Monitor(pid)
        except OSError:
            return False  # no such process any more
        try:
            return monitor.poll() is None
        finally:
            monitor.close()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if not sys.platform.startswith("linux"):
        # macOS has no /proc: a missing Linux path must not hide a surviving child.
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return bool(state) and not state.startswith("Z")
    try:  # a zombie is not alive: it only waits for its parent to collect it
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class RunLocalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-run-local-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        data = self.root / "project" / "data"
        data.mkdir(parents=True)
        (data / "table.csv").write_bytes(b"a,b\n1,2\n")
        (self.root / "project" / "helper.py").write_bytes(b"VALUE = 42\n")

    def job_file(self, code=WORKLOAD, **changes):
        config = json.loads((ROOT / "jobs" / "smoke.example.json").read_text(encoding="utf-8"))
        config.update(
            command=["python3", "-B", "-c", code],
            required_artifacts=["metrics.json"],
            workload_env={"EXPERIMENT": "local-check"},
            inputs=[
                {"directory": "project/data", "remote": "data"},
                {"local": "project/helper.py", "remote": "helper.py"},
            ],
        )
        config.update(changes)
        path = self.root / "job.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def run_local(self, path, *options):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            patch.dict(os.environ, {"RUNPOD_API_KEY": "must-not-pass"}),
        ):
            code = run.run_cli(
                [
                    "run-local",
                    str(path),
                    "--python",
                    sys.executable,
                    "--workdir",
                    str(self.root / "run"),
                    *options,
                ]
            )
        text = out.getvalue()
        return (
            code,
            json.loads(text[text.index("{") : text.rindex("}") + 1]) if "{" in text else None,
            err.getvalue(),
        )

    def test_the_workload_sees_the_pod_layout_and_succeeds(self):
        no_cloud = AssertionError("run-local must make no cloud call and read no credentials")
        # Every credential read goes through settings.env_file; every API call through Api.request.
        with (
            patch("podvigil.podrt.api.Api.request", side_effect=no_cloud),
            patch("podvigil.settings.env_file", side_effect=no_cloud),
        ):
            code, result, _ = self.run_local(self.job_file())
        self.assertEqual(
            (code, result["outcome"], result["workload_outcome"], result["workload_exit_code"]),
            (0, "success", "success", 0),
            result,
        )
        output = Path(result["output_dir"])
        seen = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (seen["cwd_is_output"], seen["helper"], seen["table"]), (True, 42, "a,b\n1,2\n")
        )
        self.assertEqual(seen["declared"], "local-check")
        # Only the Pod's passed-through variables reach the workload.
        self.assertFalse(seen["secret_seen"])
        self.assertEqual(Path(seen["home"]).parent, Path(result["workdir"]))
        self.assertIn("workload done", (output / "train.log").read_text(encoding="utf-8"))
        record = json.loads((output / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((record["outcome"], record["exit_code"]), ("success", 0))
        self.assertEqual(
            result["inputs"], {"files": 2, "bytes": len(b"a,b\n1,2\n") + len(b"VALUE = 42\n")}
        )
        self.assertEqual(result["required_artifacts_missing"], [])
        self.assertEqual(result["cloud_calls"], 0)
        self.assertIn("Not the Pod", result["note"])
        # Root ignores file modes. The Pod runs the workload under a different user.
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            self.assertFalse(
                seen["input_writable"], "inputs are read-only for the workload, as in the Pod"
            )
        self.assertIn("WONDOMON_OUTPUT_DIR", result["contract"])
        self.assertEqual(Path(result["contract"]["PYTHONPATH"]), Path(result["input_dir"]))

    def test_a_failing_workload_and_a_missing_artifact_exit_as_workload_failed(self):
        code, result, _ = self.run_local(self.job_file(workload_env={"EXIT_CODE": "3"}))
        self.assertEqual(
            (code, result["outcome"], result["workload_outcome"], result["workload_exit_code"]),
            (run.EXIT_WORKLOAD_FAILED, "workload_failed", "training_failed", 3),
        )
        code, result, _ = self.run_local(
            self.job_file(required_artifacts=["metrics.json", "model.bin"]),
            "--workdir",
            str(self.root / "run-2"),
        )
        self.assertEqual((code, result["workload_outcome"]), (run.EXIT_WORKLOAD_FAILED, "success"))
        self.assertEqual(result["required_artifacts_missing"], ["model.bin"])

    def test_the_time_limit_stops_the_workload_and_what_it_started(self):
        code = (
            "import os,pathlib,subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            "(pathlib.Path(os.environ['WONDOMON_OUTPUT_DIR'])/'child.pid').write_text(str(p.pid));time.sleep(60)"
        )
        started = time.monotonic()
        exit_code, result, _ = self.run_local(
            self.job_file(code, required_artifacts=[]), "--timeout", "3"
        )
        self.assertLess(time.monotonic() - started, 40)
        self.assertEqual(
            (exit_code, result["workload_outcome"]), (run.EXIT_WORKLOAD_FAILED, "time_limit")
        )
        child = int((Path(result["output_dir"]) / "child.pid").read_text())
        end = time.monotonic() + 10
        while alive(child) and time.monotonic() < end:
            time.sleep(0.1)
        self.assertFalse(alive(child), "the workload's own child process outlived the time limit")

    def test_parent_exit_stops_remaining_descendants_on_success_and_failure(self):
        for status in (0, 7):
            with self.subTest(status=status):
                code = (
                    "import os,pathlib,subprocess,sys;"
                    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                    "(pathlib.Path(os.environ['WONDOMON_OUTPUT_DIR'])/'child.pid').write_text(str(p.pid));"
                    f"sys.exit({status})"
                )
                exit_code, result, _ = self.run_local(
                    self.job_file(code, required_artifacts=[]),
                    "--workdir",
                    str(self.root / f"exit-{status}"),
                )
                self.assertEqual(result["workload_exit_code"], status)
                self.assertEqual(exit_code, 0 if status == 0 else run.EXIT_WORKLOAD_FAILED)
                child = int((Path(result["output_dir"]) / "child.pid").read_text())
                end = time.monotonic() + 5
                while alive(child) and time.monotonic() < end:
                    time.sleep(0.02)
                self.assertFalse(alive(child), "a descendant survived the workload's exit")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper")
    def test_a_descendant_in_its_own_session_is_reaped(self):
        code = (
            "import os,pathlib,subprocess,sys;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True);"
            "(pathlib.Path(os.environ['WONDOMON_OUTPUT_DIR'])/'child.pid').write_text(str(p.pid))"
        )
        exit_code, result, _ = self.run_local(self.job_file(code, required_artifacts=[]))
        self.assertEqual(exit_code, 0, result)
        child = int((Path(result["output_dir"]) / "child.pid").read_text())
        self.assertFalse(alive(child), "an escaped session survived its supervisor")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux termination signals")
    def test_timeout_allows_sigterm_checkpoint_and_preserves_its_exit_code(self):
        code = """import pathlib, signal, sys, time
def finish(*_):
    pathlib.Path('checkpoint.bin').write_bytes(b'saved on SIGTERM')
    sys.exit(23)
signal.signal(signal.SIGTERM, finish)
time.sleep(60)
"""
        exit_code, result, _ = self.run_local(
            self.job_file(code, required_artifacts=[]), "--timeout", "3"
        )
        self.assertEqual(
            (exit_code, result["workload_outcome"], result["workload_exit_code"]),
            (run.EXIT_WORKLOAD_FAILED, "time_limit", 23),
        )
        self.assertEqual(
            (Path(result["output_dir"]) / "checkpoint.bin").read_bytes(), b"saved on SIGTERM"
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux termination signals")
    def test_signal_exit_code_survives_descendant_reaping(self):
        code = (
            "import os,pathlib,signal,subprocess,sys;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True);"
            "pathlib.Path('child.pid').write_text(str(p.pid));os.kill(os.getpid(),signal.SIGTERM)"
        )
        exit_code, result, _ = self.run_local(self.job_file(code, required_artifacts=[]))
        self.assertEqual((exit_code, result["workload_exit_code"]), (run.EXIT_WORKLOAD_FAILED, -15))
        self.assertFalse(alive(int((Path(result["output_dir"]) / "child.pid").read_text())))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper setup")
    def test_subreaper_setup_failure_cannot_start_the_workload(self):
        from podvigil import localprocess

        marker = self.root / "should-not-run"
        bootstrap = (
            "import ctypes,runpy,types;"
            "ctypes.CDLL=lambda *a,**k:types.SimpleNamespace(prctl=lambda *a:-1);"
            f"runpy.run_path({localprocess.__file__!r},run_name='__main__')"
        )
        command = [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"]
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", bootstrap],
            input=json.dumps(command) + "\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Cannot supervise local workload descendants", done.stderr)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "nt", "Windows job object")
    def test_job_assignment_failure_cannot_start_the_workload(self):
        from podvigil.localprocess import WindowsJob

        marker = self.root / "should-not-run"
        job = self.job_file(
            f"from pathlib import Path;Path({str(marker)!r}).touch()", required_artifacts=[]
        )
        with patch.object(WindowsJob, "assign", side_effect=OSError("test assignment failure")):
            exit_code, _, _ = self.run_local(job)
        self.assertNotEqual(exit_code, 0)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "nt", "Windows filename aliases")
    def test_case_aliases_are_rejected_before_any_input_is_copied(self):
        destination = self.root / "copy"
        destination.mkdir()
        config = {
            "inputs": [
                {"local": str(self.root / "project/helper.py"), "remote": name}
                for name in ("Data.txt", "data.txt")
            ]
        }
        with self.assertRaises(ValueError):
            copy_inputs(config, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_directory_aliases_are_rejected_on_case_insensitive_posix_volumes(self):
        from types import SimpleNamespace
        from podvigil import localrun

        probe = self.root / "CaseProbe"
        probe.mkdir()
        if not (self.root / "caseprobe").exists():
            self.skipTest("Requires a case-insensitive filesystem")
        destination = self.root / "copy"
        destination.mkdir()
        config = {
            "inputs": [
                {"local": str(self.root / "project/helper.py"), "remote": name}
                for name in ("Data/first.py", "data/second.py")
            ]
        }
        # Exercise a case-insensitive volume without the Windows-only name preflight.
        with (
            patch.object(localrun, "os", SimpleNamespace(name="posix")),
            self.assertRaises(ValueError),
        ):
            copy_inputs(config, destination)
        self.assertFalse((destination / "Data/second.py").exists())

    def test_input_copy_never_overwrites_an_existing_target(self):
        destination = self.root / "copy"
        destination.mkdir()
        target = destination / "helper.py"
        target.write_bytes(b"must stay")
        config = {
            "inputs": [{"local": str(self.root / "project/helper.py"), "remote": "helper.py"}]
        }
        with self.assertRaises(FileExistsError):
            copy_inputs(config, destination)
        self.assertEqual(target.read_bytes(), b"must stay")

    def test_a_missing_command_runs_nothing_and_leaves_no_folder(self):
        path = self.job_file()
        config = json.loads(path.read_text())
        config["command"] = ["no-such-program-podvigil", "--version"]
        path.write_text(json.dumps(config))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run.run_cli(["run-local", str(path), "--workdir", str(self.root / "run")])
        self.assertEqual(code, 1)
        self.assertIn("not on this computer's PATH", out.getvalue() + err.getvalue())
        self.assertFalse((self.root / "run").exists())

    def test_a_relative_python_starts_where_the_command_was_given(self):
        relative = os.path.relpath(sys.executable)
        argv, substituted = run.resolve_command(
            ["python3", "-c", "pass"], {"PATH": ""}, self.root / "elsewhere", python=relative
        )
        self.assertEqual(argv[0], str(Path(sys.executable).resolve()))
        self.assertEqual(substituted, {"from": "python3", "to": argv[0]})
        argv, substituted = run.resolve_command(
            [sys.executable, "-c", "pass"], {"PATH": ""}, self.root, python=relative
        )
        # Only python3 and python are replaced.
        self.assertEqual((argv[0], substituted), (sys.executable, None))

    def test_a_workdir_that_holds_files_is_refused(self):
        (self.root / "run").mkdir()
        (self.root / "run" / "old.txt").write_text("x")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = run.run_cli(
                ["run-local", str(self.job_file()), "--workdir", str(self.root / "run")]
            )
        self.assertEqual(code, 1)
        self.assertEqual(sorted(path.name for path in (self.root / "run").iterdir()), ["old.txt"])


if __name__ == "__main__":
    unittest.main()
