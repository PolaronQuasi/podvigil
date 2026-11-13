"""A job-folder command given a wrong path fails without creating anything.

Found during the 2026-11-12 analysis: `recover <mistyped path>` created that folder and a lock file first.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import tempfile
import unittest

from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from podvigil.storage import Storage

COMMANDS = {
    "recover": lambda path: run.recover(path, FakeApi(), Storage("EU-RO-1", FakeS3())),
    "export-partial": lambda path: run.export_partial(
        path, FakeApi(), Storage("EU-RO-1", FakeS3())
    ),
    "export-unresolved": lambda path: run.export_unresolved(
        path, FakeApi(), Storage("EU-RO-1", FakeS3())
    ),
    "reattach": lambda path: run.reattach(path, FakeApi(), Storage("EU-RO-1", FakeS3())),
    "resolve-creation": lambda path: run.resolve_creation(path, api=FakeApi(), audit=True),
    "hand-off": lambda path: run.handoff(path, "operator note", True),
    "resume-monitoring": lambda path: run.resume_monitoring(path, timeout=1),
    "watchdog": lambda path: run.watchdog(path, FakeApi(), Storage("EU-RO-1", FakeS3())),
    "status": lambda path: run.status(path),
}


class LedgerPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-ledger-paths-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_a_missing_folder_is_never_created(self):
        for name, command in COMMANDS.items():
            with self.subTest(command=name):
                missing = self.root / ("mistyped-" + name) / "jobs" / "0123"
                with self.assertRaisesRegex(ValueError, "No job ledger"):
                    command(missing)
                self.assertFalse((self.root / ("mistyped-" + name)).exists())

    def test_a_folder_without_job_json_gets_no_lock_file(self):
        for name, command in COMMANDS.items():
            with self.subTest(command=name):
                folder = self.root / ("empty-" + name)
                folder.mkdir()
                with self.assertRaisesRegex(ValueError, "job.json is missing"):
                    command(folder)
                self.assertEqual(list(folder.iterdir()), [])

    def test_the_command_line_reports_it_and_prints_no_state_lines(self):
        missing = self.root / "no-such-ledger"
        output, people = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(people):
            code = run.run_cli(["recover", str(missing)])
        self.assertEqual(code, 1)
        message = (
            "No job ledger at "
            + str(missing.resolve())
            + " (job.json is missing). Nothing was created or changed."
        )
        self.assertEqual(people.getvalue().splitlines(), ["RUN FAILED: " + message])
        self.assertEqual(
            json.loads(output.getvalue()), {"outcome": "error", "exit_code": 1, "error": message}
        )
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
