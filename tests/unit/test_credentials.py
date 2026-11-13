"""Credential lookup: --env-file, then <NAME>_ENV_FILE, then the per-user file, then the repository's .env.

Every case uses dummy files in a temporary folder; no test reads the owner's .env. A file named by the
option or the variable must exist and is never skipped. Whatever file the lookup could use, no input may
be that file or a link to it, whatever its name.
"""

from pathlib import Path
from unittest.mock import patch
import json
import os
import subprocess
import sys
import tempfile
import unittest

from podvigil import NAME, credentials, settings
from podvigil.pathsafe import validate_input_source
from tests.support import runner as run
from tests.support.paths import ROOT

VARIABLE = NAME.upper() + "_ENV_FILE"


class CredentialLookupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wondomon-credentials-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(settings.configure)
        self.legacy = self.write("checkout/.env", "RUNPOD_API_KEY=legacy-key\n")
        self.user = self.root / "user" / "credentials.env"
        patcher = patch.object(settings, "LEGACY_ENV_FILE", self.legacy)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(settings, "user_env_file", return_value=self.user)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in (VARIABLE, "RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY"):
            os.environ.pop(name, None)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_order_option_variable_user_file_then_legacy(self):
        self.assertEqual(credentials.load_key(), "legacy-key")
        self.write("user/credentials.env", "RUNPOD_API_KEY=user-key\n")
        self.assertEqual(credentials.load_key(), "user-key")
        os.environ[VARIABLE] = str(self.write("variable.env", "RUNPOD_API_KEY=variable-key\n"))
        self.assertEqual(credentials.load_key(), "variable-key")
        settings.configure(env_file=self.write("option.env", "RUNPOD_API_KEY=option-key\n"))
        self.assertEqual(credentials.load_key(), "option-key")
        self.assertEqual(settings.env_file(), (self.root / "option.env").resolve())

    def test_a_named_file_that_does_not_exist_is_an_error_not_a_fallback(self):
        os.environ[VARIABLE] = str(self.root / "missing.env")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            credentials.load_key()
        settings.configure(env_file=self.root / "also-missing.env")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            settings.env_file()

    def test_s3_keys_come_from_the_environment_first_then_the_file(self):
        settings.configure(
            env_file=self.write(
                "keys.env",
                "RUNPOD_API_KEY=k\nRUNPOD_S3_ACCESS_KEY=file-access\n"
                "RUNPOD_S3_SECRET_KEY=file-secret\n",
            )
        )
        self.assertEqual(
            credentials.local_secrets(),
            {"RUNPOD_S3_ACCESS_KEY": "file-access", "RUNPOD_S3_SECRET_KEY": "file-secret"},
        )
        os.environ["RUNPOD_S3_ACCESS_KEY"] = "environment-access"
        self.assertEqual(credentials.local_secrets()["RUNPOD_S3_ACCESS_KEY"], "environment-access")
        self.assertEqual(
            credentials.resolve_credentials(),
            {
                "RUNPOD_API_KEY": "k",
                "RUNPOD_S3_ACCESS_KEY": "environment-access",
                "RUNPOD_S3_SECRET_KEY": "file-secret",
            },
        )

    def test_the_watchdog_is_told_which_file(self):
        named = self.write("chosen.env", "RUNPOD_API_KEY=k\n")
        settings.configure(env_file=named)
        with patch("podvigil.controller.watchdog_spawn.spawn") as spawn:
            run.spawn_watchdog(Path("ledger"))
        command = spawn.call_args.args[1]
        self.assertEqual(Path(command[command.index("--env-file") + 1]), named.resolve())

    def test_no_input_may_be_a_credentials_file_whatever_its_name(self):
        named = self.write("chosen.env", "RUNPOD_API_KEY=k\n")
        self.write("user/credentials.env", "RUNPOD_API_KEY=user-key\n")
        settings.configure(env_file=named)
        for source in (named, self.user, self.legacy):
            alias = self.root / ("alias-of-" + source.parent.name + ".txt")
            try:
                os.link(source, alias)
            except OSError as exc:
                self.skipTest("hard links unavailable here: " + type(exc).__name__)
            with (
                self.subTest(source=source.name),
                self.assertRaisesRegex(ValueError, "Never upload"),
            ):
                validate_input_source(alias)
        plain = self.write("data.txt", "data")
        self.assertEqual(validate_input_source(plain), plain.resolve())


class SessionGuardTests(unittest.TestCase):
    def test_the_test_session_names_a_dummy_credentials_file(self):
        named = Path(os.environ[VARIABLE]).resolve()
        self.assertTrue(named.is_file())
        self.assertNotEqual(named, settings.LEGACY_ENV_FILE.resolve())
        self.assertNotEqual(named, settings.user_env_file().resolve())
        self.assertIn("dummy", named.read_text(encoding="utf-8"))

    def test_shell_s3_keys_are_removed_and_restored_only_in_the_live_environment_copy(self):
        names = ("RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY")
        code = """
import json, os
from tests import real_environment
from podvigil.credentials import local_secrets
names = ("RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY")
restored = real_environment()
print(json.dumps({
    "present": [name for name in names if name in os.environ],
    "resolved": local_secrets(),
    "restored": {name: restored[name] for name in names if name in restored},
}))
"""
        cases = (
            {},
            dict(zip(names, ("dummy-shell-access", "dummy-shell-secret"))),
            dict.fromkeys(names, ""),
            {names[0]: "dummy-shell-access"},
        )
        for supplied in cases:
            with self.subTest(supplied=supplied):
                environment = {
                    name: value for name, value in os.environ.items() if name not in names
                }
                environment.update(supplied)
                done = subprocess.run(
                    [sys.executable, "-B", "-c", code],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                observed = json.loads(done.stdout)
                self.assertEqual(observed["present"], [])
                self.assertEqual(observed["resolved"], dict.fromkeys(names))
                self.assertEqual(observed["restored"], supplied)


if __name__ == "__main__":
    unittest.main()
