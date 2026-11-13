"""The runner's state lives outside the source tree: --state-dir, then <NAME>_STATE_DIR, then a per-user folder.

New ledgers go to <state>/jobs and the API pacing file to <state>. The watchdog receives the same folder
on its command line. Ledgers that older versions wrote under the repository's output/jobs are read by path.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
import copy
import io
import os
import sys
import tempfile
import unittest
import uuid

import tests
from podvigil.podrt.records import read
from podvigil import NAME, settings
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture, ledger
from tests.support.paths import ROOT
from tests.support.runner import patch_run

VARIABLE = NAME.upper() + "_STATE_DIR"


class StateDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wondomon-state-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(settings.configure)

    def test_option_then_variable_then_per_user_default(self):
        with patch.dict(os.environ, {VARIABLE: str(self.root / "from-variable")}):
            self.assertEqual(settings.state_dir(), (self.root / "from-variable").resolve())
            settings.configure(state_dir=self.root / "from-option")
            self.assertEqual(settings.state_dir(), (self.root / "from-option").resolve())
            settings.configure()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(VARIABLE, None)
            self.assertEqual(settings.state_dir(), settings.default_state_dir())
        self.assertEqual(settings.jobs_dir(), settings.state_dir() / "jobs")
        self.assertEqual(settings.pacing_file(), settings.state_dir() / "api-rate-limit.json")

    def test_each_api_key_has_its_own_pacing_file_named_without_the_key(self):
        # F5: after an API key change the old key's pacing file no longer holds requests back.
        settings.configure(state_dir=self.root)
        first, second = "dummy-first-" + uuid.uuid4().hex, "dummy-second-" + uuid.uuid4().hex
        files = settings.pacing_file(first), settings.pacing_file(second)
        self.assertNotEqual(files[0], files[1])
        self.assertEqual(files[0], settings.pacing_file(first))  # the same key, the same file
        for path, key in zip(files, (first, second)):
            self.assertEqual(path.parent, settings.state_dir())
            self.assertRegex(path.name, r"^api-rate-limit-[0-9a-f]{16}\.json$")
            self.assertNotIn(key, path.name)
        from podvigil.provider.client import configured_api

        self.assertEqual(configured_api(second)._pacer.path, files[1].resolve())

    def test_per_user_default_is_outside_the_checkout(self):
        home = self.root / "home"
        with (
            patch.dict(
                os.environ,
                {
                    "LOCALAPPDATA": str(self.root / "local"),
                    "XDG_STATE_HOME": str(self.root / "xdg"),
                },
            ),
            patch.object(Path, "home", return_value=home),
        ):
            if os.name == "nt":
                expected = self.root / "local" / NAME
            elif sys.platform == "darwin":
                expected = home / "Library" / "Application Support" / NAME
            else:
                expected = self.root / "xdg" / NAME
            self.assertEqual(settings.default_state_dir(), expected)
        self.assertNotIn(ROOT, settings.default_state_dir().parents)

    def test_new_ledgers_go_to_the_state_folder(self):
        settings.configure(state_dir=self.root)
        refusal = RuntimeError("stopped once the ledger exists")
        with (
            patch_run("Api", return_value=FakeApi()),
            patch_run("Storage", return_value=Storage("EU-RO-1", FakeS3())),
            patch_run("load_key", return_value="dummy-state-key"),
            patch_run("spawn_watchdog", side_effect=refusal),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(RuntimeError, "stopped once the ledger exists"):
                run.launch(copy.deepcopy(fixture()["config"]))
        folders = list((self.root / "jobs").iterdir())
        self.assertEqual(len(folders), 1)
        self.assertEqual(read(folders[0] / "launch-failure.json")["phase"], "watchdog_arming")

    def test_the_watchdog_gets_the_same_folder(self):
        settings.configure(state_dir=self.root)
        with patch("podvigil.controller.watchdog_spawn.spawn") as spawn:
            run.spawn_watchdog(Path("ledger"))
        command = spawn.call_args.args[1]
        # The command name still comes first: [..., "watchdog", options..., ledger].
        # Every path by its real location: the watchdog runs outside this process's tree.
        after = command[command.index("watchdog") + 1 :]
        self.assertEqual(after[:2], ["--state-dir", str(settings.real_path(self.root))])
        self.assertEqual(after[-1], str(settings.real_path("ledger")))
        self.assertEqual(spawn.call_args.args[0], settings.real_path("ledger"))

    def test_the_command_line_takes_the_option_before_or_after_the_command(self):
        folder = self.root / "ledger"
        ledger(folder, fixture())
        for arguments in (
            ["--state-dir", str(self.root / "a"), "status", str(folder)],
            ["status", "--state-dir", str(self.root / "a"), str(folder)],
        ):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()):
                seen = []
                with patch_run(
                    "status", side_effect=lambda path: seen.append(settings.state_dir()) or {}
                ):
                    self.assertEqual(run.run_cli(arguments), 0)
                self.assertEqual(seen, [(self.root / "a").resolve()])
        # The CLI resets the state directory after the command.
        self.assertNotEqual(settings.state_dir(), (self.root / "a").resolve())

    def test_a_ledger_in_the_old_place_is_read_by_its_path(self):
        old = self.root / "checkout" / "output" / "jobs" / ("a" * 32)
        ledger(old, fixture())
        settings.configure(state_dir=self.root / "new-state")
        self.assertEqual(run.status(old)["ledger"], str(old.resolve()))
        self.assertFalse((self.root / "new-state").exists())  # reading creates nothing

    def test_the_tests_never_use_the_real_state_folder(self):
        self.assertIn(VARIABLE, os.environ)
        self.assertNotEqual(
            Path(os.environ[VARIABLE]).resolve(), settings.default_state_dir().resolve()
        )

    def test_only_the_manual_live_lane_undoes_the_session_guard(self):
        # The paid smoke must use the runner's real credentials and keep its ledger
        # in the real state folder.
        path_variables = {VARIABLE, settings.ENV_FILE_VARIABLE}
        s3_variables = {"RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY"}
        self.assertEqual(set(tests.REAL_ENVIRONMENT), path_variables | s3_variables)
        real = tests.real_environment()
        for name, value in tests.REAL_ENVIRONMENT.items():
            self.assertEqual(real.get(name), value)
            if value is None:
                self.assertNotIn(name, real)
            if name in path_variables:
                self.assertNotEqual(real.get(name), os.environ[name])
            else:
                self.assertNotIn(name, os.environ)
        live = (ROOT / "tests" / "live" / "test_live_smoke.py").read_text(encoding="utf-8")
        self.assertIn("env=real_environment()", live)


if __name__ == "__main__":
    unittest.main()
