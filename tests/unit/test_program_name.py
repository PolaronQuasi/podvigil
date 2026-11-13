"""The advice in the closing lines names the command the user ran (F4): managed_run.py in a checkout,
python -m podvigil, or the installed podvigil command, so a line can be pasted as it is. The watchdog,
started by the controller, says the controller's command in its own log."""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import subprocess
import sys
import tempfile
import unittest

from podvigil import NAME, settings
from podvigil.controller import watchdog as watchdog_module
from tests.support import runner as run
from tests.support.jobs import fixture, ledger
from tests.support.paths import ROOT

QUIET = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class ProgramNameTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-program-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "ledger"
        # The lines advise recover when Pod deletion is unverified.
        ledger(self.folder, fixture())

    def lines(self, program):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            run.run_cli(["status", str(self.folder)], program=program)
        return err.getvalue()

    def test_the_closing_lines_name_the_command_the_user_ran(self):
        for program, expected in (
            (None, NAME),
            ("managed_run.py", "managed_run.py"),
            ("python -m " + NAME, "python -m " + NAME),
        ):
            with self.subTest(program=program):
                self.assertIn(
                    "Run: " + expected + " recover " + str(self.folder), self.lines(program)
                )
        self.assertEqual(settings.program(), NAME)  # the choice applied to that command only

    def test_each_entry_point_names_itself(self):
        for argv, cwd, expected in (
            ([str(ROOT / "managed_run.py")], None, "managed_run.py"),
            (["-m", NAME], ROOT / "src", "python -m " + NAME),
        ):
            with self.subTest(entry=expected):
                done = subprocess.run(
                    [sys.executable, "-B", *argv, "status", str(self.folder)],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    **QUIET,
                )
                self.assertIn("Run: " + expected + " recover " + str(self.folder), done.stderr)

    def test_the_watchdog_says_the_controllers_command(self):
        settings.configure(program="managed_run.py")
        self.addCleanup(settings.configure)
        self.assertIn("program='managed_run.py'", watchdog_module.watchdog_bootstrap())


if __name__ == "__main__":
    unittest.main()
