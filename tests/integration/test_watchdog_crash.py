"""The production REST client and the detached watchdog against a loopback fake.

The watchdog is started by the production launcher and must finish the cleanup after its controller is
killed. No Runpod key is read: both processes resolve dummy credentials.
"""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.controller.ownership import owned_pod
from podvigil.podrt.api import Api, ApiError
from podvigil.podrt.teardown import delete_verified
from tests.support.fakes import DUMMY_CREDENTIALS_CODE
from tests.support.jobs import ledger
from tests.support.loopback import LoopbackApi
from tests.support.paths import ROOT


class HttpAndCrashTests(unittest.TestCase):
    def setUp(self):
        loopback = LoopbackApi().start()
        self.addCleanup(loopback.stop)
        self.job, self.api, self.url = loopback.job, loopback.api, loopback.url

    def test_real_http_cleanup_retries_and_sanitizes_errors(self):
        client = Api("test-key", self.url)
        self.api.delete_failures = 1
        delete_verified(
            client, "/pods/testpod", lambda p: owned_pod(p, self.job), sleep=lambda _: None
        )
        with self.assertRaises(ApiError) as caught:
            client.request("GET", "/pods/missing")
        self.assertNotIn("SECRET_MUST_NOT_APPEAR", str(caught.exception))
        self.assertNotIn("test-key", str(caught.exception))

    def test_detached_watchdog_survives_force_killed_controller(self):
        with tempfile.TemporaryDirectory(prefix="wondomon-crash-test-") as tmp:
            folder = Path(tmp)
            self.job["deadline_epoch"] = time.time() + 18
            ledger(folder, self.job)
            # This uses the production detached-process launcher. Only the cloud
            # API is replaced with the loopback simulator; no Runpod key is read.
            watch_code = (
                DUMMY_CREDENTIALS_CODE
                + "from pathlib import Path;from tests.support.runner import watchdog;"
                "from podvigil.podrt.api import Api;from tests.support.fakes import NoAgentStorage;"
                f"watchdog(Path({str(folder)!r}),Api('test-key',{self.url!r}),NoAgentStorage())"
            )
            controller_code = (
                DUMMY_CREDENTIALS_CODE + "from pathlib import Path;import sys,time,threading;"
                "from tests.support.runner import spawn_watchdog,heartbeat_worker;"
                f"p=Path({str(folder)!r});"
                "threading.Thread(target=heartbeat_worker,args=(p,threading.Event()),daemon=True).start();"
                f"spawn_watchdog(p,[sys.executable,'-B','-c',{watch_code!r}]);time.sleep(90)"
            )
            options = {"cwd": str(ROOT), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            controller = subprocess.Popen([sys.executable, "-B", "-c", controller_code], **options)
            try:
                deadline = time.monotonic() + 8
                while (
                    not (folder / "watchdog-isolation.json").exists()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                self.assertTrue((folder / "watchdog-isolation.json").exists())
                self.assertIn("testpod", self.api.pods)
                controller.kill()
                controller.wait(timeout=5)
                deadline = time.monotonic() + 25
                while not (folder / "pod-deleted.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(
                    (folder / "pod-deleted.json").exists(), "Detached watchdog failed to clean up"
                )
                self.assertFalse(self.api.pods)
                # Controller loss also hands off backup/retained-storage reporting.
                # Keep the fake HTTP server and ledger alive until that finishes.
                deadline = time.monotonic() + 12
                while (
                    not (folder / "recovery-handoff.json").exists() and time.monotonic() < deadline
                ):
                    time.sleep(0.1)
                self.assertTrue((folder / "recovery-handoff.json").exists())
                time.sleep(0.3)
            finally:
                if controller.poll() is None:
                    controller.kill()
                    controller.wait(timeout=5)
