"""The watchdog proves at arming that it resolved the controller's credentials; launch refuses otherwise.

Found during the 2026-11-12 analysis: on Windows the watchdog starts through WMI and does not inherit the
controller's environment, while the S3 keys may come from the environment. Only keyed hashes are compared.
"""

from pathlib import Path
from unittest.mock import patch
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.storage import Storage
from podvigil.podrt.records import read, save
from tests.support import runner as run
from tests.support.runner import patch_run
from podvigil.controller import watchdog_spawn as watchdog_launcher
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture
from tests.support.paths import ROOT

CONTROLLER = {
    "RUNPOD_API_KEY": "dummy-account-key",
    "RUNPOD_S3_ACCESS_KEY": "dummy-access-key",
    "RUNPOD_S3_SECRET_KEY": "dummy-secret-key",
}
NO_ENVIRONMENT = {**CONTROLLER, "RUNPOD_S3_ACCESS_KEY": None, "RUNPOD_S3_SECRET_KEY": None}
OPTIONS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def handed_off_ledger(folder):
    """A ledger whose watchdog exits at once: an operator has taken over (no cloud call is made)."""
    job = fixture()
    save(folder / "job.json", job)
    save(
        folder / "operator-handoff.json",
        {
            "job_id": job["job_id"],
            "active": True,
            "billing_unverified_acknowledged": True,
            "cleanup_verified": False,
        },
    )
    return job


def watch_command(folder, credentials):
    # The watchdog's path starts at the package folder. The tests package is at the checkout root.
    code = (
        "import sys;sys.path.insert(0," + repr(str(ROOT)) + ");from tests.support import runner;"
        "sys.modules[runner.credential_proof.__module__].resolve_credentials=lambda:"
        + repr(credentials)
        + ";"
        "from pathlib import Path;runner.watchdog(Path(" + repr(str(folder)) + "))"
    )
    return [sys.executable, "-B", "-c", code]


def wait_for_watchdogs(folder, seconds=15):
    """Every watchdog that wrote an arming record has exited."""
    end = time.monotonic() + seconds
    for record in folder.glob("watchdog-arm-*.json"):
        if record.name.endswith(".launch.json"):
            continue
        pid = read(record, {}).get("pid")
        if isinstance(pid, int):
            try:
                monitor = watchdog_launcher.Monitor(pid)
            except OSError:
                continue  # Windows: the process is already gone
            while monitor.poll() is None and time.monotonic() < end:
                time.sleep(0.05)
            exited = monitor.poll() is not None
            monitor.close()
            if not exited:
                return False
    return True


class CredentialProofTests(unittest.TestCase):
    def test_compare_names_only_the_credentials_that_differ(self):
        same = watchdog_launcher.compare_credentials(
            {"A": "1" * 64, "B": None}, {"A": "1" * 64, "B": None}
        )
        self.assertEqual(same, {"same": True, "different": []})
        different = watchdog_launcher.compare_credentials(
            {"A": "1" * 64, "B": None}, {"A": "2" * 64, "B": "3" * 64}
        )
        self.assertEqual(different, {"same": False, "different": ["A", "B"]})
        self.assertFalse(watchdog_launcher.compare_credentials(None, {"A": None})["same"])

    def test_proof_is_keyed_and_holds_no_value(self):
        with patch_run("resolve_credentials", return_value=CONTROLLER):
            first, second = run.credential_proof("a" * 32), run.credential_proof("b" * 32)
        self.assertEqual(set(first), set(CONTROLLER))
        self.assertNotEqual(first, second)
        text = json.dumps([first, second])
        for value in CONTROLLER.values():
            self.assertNotIn(value, text)
        with patch_run("resolve_credentials", return_value=NO_ENVIRONMENT):
            self.assertIsNone(run.credential_proof("a" * 32)["RUNPOD_S3_ACCESS_KEY"])

    def test_spawn_checks_by_default_and_resume_keeps_a_guard(self):
        with patch("podvigil.controller.watchdog_spawn.spawn") as spawn:
            run.spawn_watchdog(Path("ledger"))
        self.assertIs(spawn.call_args.kwargs["require_same_credentials"], True)
        self.assertIs(spawn.call_args.kwargs["credential_proof"], run.credential_proof)
        with tempfile.TemporaryDirectory(prefix="wondomon-resume-guard-") as tmp:
            handed_off_ledger(Path(tmp))
            with patch_run("spawn_watchdog") as spawn, self.assertRaises(RuntimeError):
                run.resume_monitoring(tmp, timeout=0.2)
            self.assertIs(spawn.call_args.kwargs["require_same_credentials"], False)

    def test_launch_creates_nothing_when_the_watchdog_differs(self):
        api = FakeApi()
        temp = tempfile.TemporaryDirectory(prefix="wondomon-credentials-")
        self.addCleanup(temp.cleanup)
        jobs = Path(temp.name) / "jobs"
        refusal = RuntimeError(
            "Independent watchdog failed to arm; no new paid resource may be created. Cause: "
            + watchdog_launcher.credential_mismatch(["RUNPOD_S3_ACCESS_KEY"])
        )
        with (
            patch_run("Api", return_value=api),
            patch_run("Storage", return_value=Storage("EU-RO-1", FakeS3())),
            patch_run("load_key", return_value="dummy-test-key"),
            patch_run("spawn_watchdog", side_effect=refusal),
            patch_run("jobs_dir", return_value=jobs),
        ):
            with self.assertRaisesRegex(RuntimeError, "RUNPOD_S3_ACCESS_KEY"):
                run.launch(copy.deepcopy(fixture()["config"]))
        self.assertNotIn("POST", {method for method, _ in api.calls})
        folder = next(jobs.iterdir())
        failure = read(folder / "launch-failure.json")
        self.assertEqual(failure["phase"], "watchdog_arming")
        self.assertIn(".env", failure["message"])
        self.assertEqual(
            run.state_lines(folder),
            run.NOTHING_CREATED[:1] + ["Volume: none was created. Nothing from this job remains."],
        )


class RealWatchdogCredentialTests(unittest.TestCase):
    """The production launcher with a real detached watchdog; each side resolves dummy credentials."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="wondomon-credential-arming-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        handed_off_ledger(self.folder)

    def test_same_credentials_arm_and_record_names_only(self):
        with patch_run("resolve_credentials", return_value=CONTROLLER):
            monitor = run.spawn_watchdog(self.folder, watch_command(self.folder, CONTROLLER))
        self.assertEqual(monitor.credentials, {"checked": True, "same": True, "different": []})
        self.assertTrue(wait_for_watchdogs(self.folder))
        isolation = (self.folder / "watchdog-isolation.json").read_text()
        self.assertEqual(json.loads(isolation)["credentials"]["same"], True)
        for value in CONTROLLER.values():
            self.assertNotIn(value, isolation)
        self.assertEqual(read(self.folder / "watchdog.json")["state"], "operator_took_over")

    def test_keys_missing_in_the_watchdog_refuse_to_arm(self):
        with patch_run("resolve_credentials", return_value=CONTROLLER):
            with self.assertRaises(RuntimeError) as caught:
                run.spawn_watchdog(self.folder, watch_command(self.folder, NO_ENVIRONMENT))
        message = str(caught.exception)
        self.assertIn("no new paid resource may be created", message)
        self.assertIn("RUNPOD_S3_ACCESS_KEY, RUNPOD_S3_SECRET_KEY", message)
        self.assertIn(".env", message)
        self.assertFalse((self.folder / "watchdog-isolation.json").exists())
        self.assertFalse(list(self.folder.glob("watchdog-arm-*.accepted")))
        self.assertTrue(list(self.folder.glob("watchdog-arm-*.cancel")))
        self.assertTrue(wait_for_watchdogs(self.folder))
        self.assertFalse((self.folder / "watchdog.json").exists())  # it never started monitoring

    def test_existing_resources_keep_a_guard_and_the_difference_is_recorded(self):
        with patch_run("resolve_credentials", return_value=CONTROLLER):
            monitor = run.spawn_watchdog(
                self.folder,
                watch_command(self.folder, NO_ENVIRONMENT),
                require_same_credentials=False,
            )
        self.assertFalse(monitor.credentials["same"])
        self.assertTrue(wait_for_watchdogs(self.folder))
        record = read(self.folder / "watchdog-isolation.json")["credentials"]
        self.assertEqual(record["different"], ["RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY"])


class EnvironmentOnlyKeysTests(unittest.TestCase):
    """Unpatched production code in a private copy: the S3 keys exist only in the controller's environment."""

    def test_environment_only_keys(self):
        with tempfile.TemporaryDirectory(prefix="wondomon-environment-keys-") as tmp:
            root = Path(tmp)
            for source in ROOT.glob("*.py"):
                shutil.copyfile(source, root / source.name)
            shutil.copytree(
                ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__")
            )
            (root / ".env").write_text("RUNPOD_API_KEY=dummy-local-test\n")
            folder = root / "ledger"
            handed_off_ledger(folder)
            code = (
                "from pathlib import Path;import sys;sys.path.insert(0,'src');from podvigil.controller.watchdog import spawn_watchdog\n"
                f"p=Path({str(folder)!r})\n"
                "try:\n    m=spawn_watchdog(p)\nexcept RuntimeError as exc:\n    print('REFUSED', exc, flush=True)\n"
                "else:\n    print('ARMED', flush=True)\n"
            )
            environment = dict(
                os.environ,
                RUNPOD_S3_ACCESS_KEY="dummy-env-access",
                RUNPOD_S3_SECRET_KEY="dummy-env-secret",
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", code],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=40,
                **OPTIONS,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(wait_for_watchdogs(folder))
            self.assertNotIn("dummy-env-secret", result.stdout + result.stderr)
            if os.name == "nt":
                # The WMI-created watchdog does not see this environment: launch must refuse.
                self.assertIn("REFUSED", result.stdout)
                self.assertIn("RUNPOD_S3_ACCESS_KEY", result.stdout)
            else:
                # A POSIX watchdog inherits the environment and resolves the same keys.
                self.assertIn("ARMED", result.stdout)


if __name__ == "__main__":
    unittest.main()
