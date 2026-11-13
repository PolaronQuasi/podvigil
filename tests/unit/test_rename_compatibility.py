"""The public rename must not hide chosen credentials or invalidate existing job identity."""

from pathlib import Path
from unittest.mock import patch
import hashlib
import os
import tempfile
import unittest

from podvigil import settings
from podvigil.controller import ownership, watchdog_spawn
from podvigil.jobspec import validate
from podvigil.pathsafe import validate_input_source
from tests.support.jobs import fixture


class RenameCompatibilityTests(unittest.TestCase):
    def test_explicit_options_and_new_variables_win_over_working_name_aliases(self):
        with tempfile.TemporaryDirectory(prefix="podvigil-rename-") as tmp:
            root = Path(tmp)
            old, new = root / "old.env", root / "new.env"
            old.write_text("RUNPOD_API_KEY=dummy-old\n")
            new.write_text("RUNPOD_API_KEY=dummy-new\n")
            env = {
                "PODWARDEN_STATE_DIR": str(root / "state-old"),
                "PODWARDEN_ENV_FILE": str(old),
                "PODWARDEN_MIN_CUDA_VERSION": "12.4",
            }
            self.addCleanup(settings.configure)
            settings.configure()
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(settings.state_dir(), (root / "state-old").resolve())
                self.assertEqual(settings.env_file(), old.resolve())
                self.assertEqual(settings.cuda_floor(), "12.4")
                os.environ.update(
                    PODVIGIL_ENV_FILE=str(new),
                    PODVIGIL_STATE_DIR=str(root / "state-new"),
                    PODVIGIL_MIN_CUDA_VERSION="12.8",
                )
                self.assertEqual(settings.env_file(), new.resolve())
                self.assertEqual(settings.state_dir(), (root / "state-new").resolve())
                self.assertEqual(settings.cuda_floor(), "12.8")
                settings.configure(env_file=old, state_dir=root / "chosen")
                self.assertEqual(settings.env_file(), old.resolve())
                self.assertEqual(settings.state_dir(), (root / "chosen").resolve())

    def test_previous_per_user_credentials_are_found_and_blocked_as_inputs(self):
        with tempfile.TemporaryDirectory(prefix="podvigil-rename-") as tmp:
            root = Path(tmp)
            current = root / "podvigil/credentials.env"
            previous = root / "podwarden/credentials.env"
            previous.parent.mkdir()
            previous.write_text("RUNPOD_API_KEY=dummy-prior\n")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(settings, "user_env_file", return_value=current),
            ):
                settings.configure()
                self.addCleanup(settings.configure)
                self.assertEqual(settings.env_file(), previous)
                with self.assertRaisesRegex(ValueError, "Never upload"):
                    validate_input_source(previous)
                self.assertEqual(previous.read_text(), "RUNPOD_API_KEY=dummy-prior\n")

    def test_existing_watchdog_and_controller_share_the_same_pacing_file(self):
        key = "dummy-account-for-rename"
        suffix = hashlib.sha256(("podwarden pacing\0" + key).encode()).hexdigest()[:16]
        self.assertEqual(settings.pacing_file(key).name, "api-rate-limit-" + suffix + ".json")

    def test_old_identity_does_not_authorize_an_unrelated_pod(self):
        job = fixture()
        pod = {"id": "owned", "name": job["pod_name"], "env": {"PODWARDEN_JOB_ID": job["job_id"]}}
        self.assertTrue(ownership.owned_pod(pod, job))
        self.assertFalse(
            ownership.owned_pod({**pod, "env": {"PODWARDEN_JOB_ID": "different"}}, job)
        )
        with patch.dict(os.environ, {"PODWARDEN_ARM_TOKEN": "a" * 32}, clear=True):
            self.assertEqual(watchdog_spawn.arming_token(), "a" * 32)
        with self.assertRaisesRegex(ValueError, "Unsafe declared"):
            validate({**job["config"], "workload_env": {"PODWARDEN_JOB_SPEC": "override"}})
