"""Live smoke: one short paid job on Runpod, run by hand only. Never part of the automatic lanes.

This folder has no __init__.py, so `unittest discover` never collects it. Run it on purpose, with the
job file to launch and an explicit acknowledgement that it creates billed resources, for example:

    PAID_SMOKE_JOB=jobs/smoke.example.json PAID_SMOKE_ACCEPT_CHARGES=yes \
        .venv/Scripts/python.exe -B -m unittest tests.live.test_live_smoke -v

It runs the command line as a user would (the repository's managed_run.py) with the credentials and the
state folder the runner normally resolves, not the test session's dummy ones, and checks what a release
gate needs: exit code 0, a successful outcome, the Pod and the volume deleted with their absence verified,
and a verified local backup of the results. The ledger stays in the runner's state folder.
A paid smoke needs the owner's explicit approval each time; local tests never authorise one.
"""

from pathlib import Path
import json
import os
import subprocess
import sys
import unittest

from tests import real_environment

ROOT = Path(__file__).resolve().parents[2]
JOB = os.environ.get("PAID_SMOKE_JOB")
ACCEPTED = os.environ.get("PAID_SMOKE_ACCEPT_CHARGES") == "yes"


@unittest.skipUnless(
    JOB and ACCEPTED, "Paid smoke: set PAID_SMOKE_JOB and PAID_SMOKE_ACCEPT_CHARGES=yes to run it"
)
class LiveSmokeTests(unittest.TestCase):
    def test_one_paid_job_ends_with_verified_cleanup(self):
        done = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "managed_run.py"),
                "launch",
                str(Path(JOB).resolve()),
            ],
            cwd=ROOT,
            env=real_environment(),
            capture_output=True,
            text=True,
            timeout=3600,
        )
        # Standard output holds the result JSON alone; the lines for people are on standard error.
        lines = done.stderr.strip().splitlines()
        report = done.stdout[-4000:] + done.stderr[-4000:]
        self.assertGreaterEqual(len(lines), 2, report)
        self.assertEqual(json.loads(done.stdout)["outcome"], "success", report)
        self.assertTrue(
            lines[-2].startswith("Pod ")
            and lines[-2].endswith("deleted; its absence was verified."),
            report,
        )
        self.assertTrue(
            lines[-1].endswith("deleted; its absence was verified. Nothing from this job remains."),
            report,
        )
        self.assertEqual(done.returncode, 0, report)
        ledger = Path(
            next(line.split(": ", 1)[1] for line in lines if line.startswith("Job ledger: "))
        )
        summary = json.loads((ledger / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["training_outcome"], "success")
        self.assertTrue(summary["pod_termination_verified"] and summary["volume_deletion_verified"])
        self.assertTrue((ledger / "downloads" / "backup-verified.json").is_file())


if __name__ == "__main__":
    unittest.main()
