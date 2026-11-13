"""A controller and a watchdog that see different folders at the same nominal path.

Found in a live launch on 2026-11-12, from a shell inside a packaged Windows app: there %LOCALAPPDATA% leads to
the app's private copy (...\\Packages\\<app>\\LocalCache\\Local\\...), so the controller wrote the ledger there,
while the watchdog, started outside the controller's process tree, looked in the literal folder, found no
job.json and never armed. The fix hands the watchdog every path by its real location.

The model: the nominal state folder is a link (a junction on Windows) to the folder where the controller's writes
really land, and the watchdog reads its command line through a view that maps the nominal path to an empty
folder, which is what a process outside the app sees there. Both sides are real processes; the cloud is never
called (the ledger is handed off, so the watchdog exits at once).
"""

from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from unittest.mock import patch
import copy
import io
import os
import tempfile
import threading
import time
import unittest
import uuid

from podvigil import settings
from podvigil.controller import watchdog as watchdog_module
from podvigil.controller import watchdog_spawn
from podvigil.podrt.records import read, save
from podvigil.storage import Storage
from tests.integration.test_watchdog_credentials import CONTROLLER, wait_for_watchdogs
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture
from tests.support.paths import ROOT
from tests.support.runner import patch_run


def link_folder(target, link):
    """`link` leads to `target` for every process: a junction on Windows, a symbolic link elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def unlink_folder(link):
    if os.name == "nt":
        os.rmdir(link)  # removes the junction, never what it leads to
    else:
        os.unlink(link)


def watchdog_view(mapping):
    """Code run first in the watchdog: its command line seen through `mapping` (path prefix -> what it sees
    there), and the controller's dummy credentials, so the credential proof matches without a real key."""
    return (
        "import os,sys;_view=" + repr([(str(a), str(b)) for a, b in mapping]) + ";"
        "sys.argv=[next((b+x[len(a):] for a,b in _view if x==a or x.startswith(a+os.sep)),x) for x in sys.argv];"
        "sys.path.insert(0," + repr(str(ROOT)) + ");from tests.support import runner as _runner;"
        "sys.modules[_runner.credential_proof.__module__].resolve_credentials=lambda:"
        + repr(CONTROLLER)
        + ";"
    )


def cleanup_watchdog(folder, seconds=15):
    """Wait for this fixture's watchdog and release its log before directory cleanup."""
    end = time.monotonic() + seconds
    if not wait_for_watchdogs(folder, seconds=seconds):
        raise AssertionError(f"Fixture watchdog did not exit within {seconds} seconds: {folder}")
    # A Windows launcher can hold inherited stdout after the recorded watchdog PID exits.
    log = folder / "watchdog.log"
    while True:
        try:
            log.unlink(missing_ok=True)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (32, 33) or time.monotonic() >= end:
                raise
            time.sleep(0.05)


class SeparateViewsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-views-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.private = root / "app-private"  # where the controller's writes really land
        # What a process outside the app sees at the nominal path.
        self.outside = root / "outside-view"
        self.outside_too = root / "outside-view-2"
        for folder in (self.private, self.outside, self.outside_too):
            folder.mkdir()
        self.nominal = root / "state"
        link_folder(self.private, self.nominal)
        self.addCleanup(unlink_folder, self.nominal)  # runs before the folder's own cleanup
        self.real = settings.real_path(self.private)
        (self.nominal / "credentials.env").write_text(
            "RUNPOD_API_KEY=dummy-views-key\n", encoding="utf-8"
        )
        self.job = fixture()
        self.job["job_id"] = uuid.uuid4().hex
        # The nominal path where the controller created the ledger.
        self.ledger = self.nominal / "jobs" / self.job["job_id"]
        save(self.ledger / "job.json", self.job)
        save(
            self.ledger / "operator-handoff.json",
            {
                "job_id": self.job["job_id"],
                "active": True,
                "billing_unverified_acknowledged": True,
                "cleanup_verified": False,
            },
        )
        self.real_ledger = self.real / "jobs" / self.job["job_id"]
        self.addCleanup(cleanup_watchdog, self.real_ledger)

    def controller(self, mapping, *extra):
        """The controller's view: the nominal state folder and credentials file, unresolved as the per-user
        defaults are; the watchdog starts with `mapping` as its view."""
        original = watchdog_module.watchdog_bootstrap()
        stack = ExitStack()
        stack.enter_context(patch.object(settings, "state_dir", lambda: self.nominal))
        stack.enter_context(
            patch.object(settings, "env_file", lambda: self.nominal / "credentials.env")
        )
        stack.enter_context(patch_run("resolve_credentials", return_value=CONTROLLER))
        stack.enter_context(
            patch.object(
                watchdog_module, "watchdog_bootstrap", lambda: watchdog_view(mapping) + original
            )
        )
        for item in extra:
            stack.enter_context(item)
        return stack

    def test_the_watchdog_gets_real_paths_and_arms(self):
        with self.controller([(self.nominal, self.outside)]):
            monitor = run.spawn_watchdog(self.ledger)
        self.assertEqual(monitor.credentials, {"checked": True, "same": True, "different": []})
        self.assertTrue(wait_for_watchdogs(self.real_ledger))
        self.assertTrue((self.real_ledger / "watchdog-isolation.json").is_file())
        self.assertEqual(read(self.real_ledger / "watchdog.json")["state"], "operator_took_over")
        spec = read(next(self.real_ledger.glob("watchdog-arm-*.launch.json")))
        command = spec["command"]
        self.assertEqual(
            command[command.index("watchdog") + 1 :],
            [
                "--state-dir",
                str(self.real),
                "--env-file",
                str(self.real / "credentials.env"),
                str(self.real_ledger),
            ],
        )
        self.assertEqual(spec["folder"], str(self.real_ledger))
        # Nothing was created where the watchdog would have looked.
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_cleanup_waits_for_an_inherited_log_handle_to_close(self):
        log = self.real_ledger / "watchdog.log"
        sharing_violation = threading.Event()
        release = threading.Event()
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            try:
                return original_unlink(path, *args, **kwargs)
            except PermissionError as exc:
                if path == log and getattr(exc, "winerror", None) == 32:
                    sharing_violation.set()
                    release.set()
                raise

        with log.open("ab") as stream:

            def close_log():
                release.wait(5)
                stream.close()

            holder = threading.Thread(target=close_log)
            holder.start()
            try:
                with patch.object(Path, "unlink", unlink):
                    cleanup_watchdog(self.real_ledger)
                self.assertFalse(log.exists())
                if os.name == "nt":
                    self.assertTrue(sharing_violation.is_set())
                    self.assertTrue(stream.closed)
            finally:
                release.set()
                holder.join(5)
            self.assertFalse(holder.is_alive())

    def test_cleanup_reports_a_log_that_stays_locked(self):
        locked = PermissionError("Fixture launcher still holds watchdog.log")
        locked.winerror = 32
        with (
            patch.object(Path, "unlink", side_effect=locked),
            self.assertRaises(PermissionError) as caught,
        ):
            cleanup_watchdog(self.real_ledger, seconds=0)
        self.assertIs(caught.exception, locked)

    def test_a_watchdog_that_still_cannot_see_the_ledger_names_both_paths(self):
        started = time.monotonic()
        with self.controller([(self.nominal, self.outside), (self.real, self.outside_too)]):
            with self.assertRaises(RuntimeError) as caught:
                run.spawn_watchdog(self.ledger)
        message = str(caught.exception)
        self.assertLess(
            time.monotonic() - started, 12, "the watchdog's report must end the wait early"
        )
        self.assertIn("no new paid resource may be created", message)
        self.assertIn("The watchdog cannot see the job ledger.", message)
        self.assertIn(str(self.ledger) + " (on disk: " + str(self.real_ledger) + ")", message)
        self.assertIn(
            "looked for it at " + str(self.outside_too / "jobs" / self.job["job_id"]), message
        )
        self.assertFalse((self.real_ledger / "watchdog-isolation.json").exists())
        self.assertFalse(list(self.real_ledger.glob("watchdog-arm-*.accepted")))
        self.assertEqual((list(self.outside.iterdir()), list(self.outside_too.iterdir())), ([], []))

    def test_a_starter_that_cannot_read_the_arming_record_names_both_paths(self):
        # The broker is the first process outside the controller's tree. Simulate the real path
        # being hidden from it and return the exit code the real broker uses in that case.
        unseen = (
            "import time;time.sleep(1.5);raise SystemExit(%d)" % watchdog_spawn.LEDGER_UNSEEN_EXIT
        )
        with self.controller([], patch.object(watchdog_spawn, "relaunch_code", lambda: unseen)):
            with self.assertRaises(RuntimeError) as caught:
                run.spawn_watchdog(self.ledger, nominal=self.ledger)
        message = str(caught.exception)
        self.assertIn("The watchdog cannot see the job ledger.", message)
        self.assertIn(str(self.ledger) + " (on disk: " + str(self.real_ledger) + ")", message)
        self.assertIn("cannot read it there", message)

    def test_the_real_broker_refuses_when_it_cannot_read_the_arming_record(self):
        with patch.object(watchdog_spawn.time, "sleep"), self.assertRaises(SystemExit) as caught:
            watchdog_spawn.broker(str(self.outside / "watchdog-arm-0.launch.json"))
        self.assertEqual(caught.exception.code, watchdog_spawn.LEDGER_UNSEEN_EXIT)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_a_watchdog_outside_arming_reports_nothing(self):
        with patch.dict(
            os.environ, {"WONDOMON_ARM_TOKEN": "", watchdog_spawn.ARM_FOLDER: str(self.real_ledger)}
        ):
            watchdog_spawn.report_unseen_ledger(self.outside / "missing")
        self.assertFalse(list(self.real_ledger.glob("*.unseen.json")))

    def test_launch_prints_the_real_ledger_and_spawn_resolves_the_one_it_was_made_under(self):
        seen = {}

        def refuse(folder, **options):
            seen.update(folder=folder, **options)
            raise RuntimeError("stopped once the ledger exists")

        output = io.StringIO()
        with (
            patch_run("Api", return_value=FakeApi()),
            patch_run("Storage", return_value=Storage("EU-RO-1", FakeS3())),
            patch_run("load_key", return_value="dummy-views-key"),
            patch_run("spawn_watchdog", side_effect=refuse),
            patch_run("jobs_dir", return_value=self.nominal / "jobs"),
            redirect_stderr(output),
        ):
            with self.assertRaisesRegex(RuntimeError, "stopped once the ledger exists"):
                run.launch(copy.deepcopy(fixture()["config"]))
        # spawn_watchdog resolves this path, as test_the_watchdog_gets_real_paths_and_arms checks.
        made_under = seen["folder"]
        self.assertEqual(made_under.parent, self.nominal / "jobs")
        folder = self.real / "jobs" / made_under.name
        self.assertEqual(settings.real_path(made_under), folder)
        # Any process can use the printed ledger path.
        self.assertIn("Job ledger: " + str(folder) + "\n", output.getvalue())
        self.assertEqual(read(folder / "launch-failure.json")["phase"], "watchdog_arming")


if __name__ == "__main__":
    unittest.main()
