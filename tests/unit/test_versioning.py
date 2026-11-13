"""One version source: the package's __version__, shown by --version and kept in job.json and the Pod spec.

A later recover reads older ledgers; the schema number tells their layouts apart. Ledgers written before
0.1.0 have neither field and stay readable.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
import base64
import copy
import io
import json
import os
import re
import tempfile
import unittest

from podvigil.podrt.records import read
import podvigil
from podvigil.settings import ENV_PREFIX
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture, ledger
from tests.support.paths import ROOT
from tests.support.runner import patch_run


class VersionTests(unittest.TestCase):
    def test_version_option_prints_the_package_version(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            run.run_cli(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), podvigil.NAME + " " + podvigil.__version__)
        self.assertRegex(podvigil.__version__, r"^\d+\.\d+\.\d+$")

    def test_version_stays_one_line_even_with_a_zero_or_narrow_terminal_width(self):
        for width in ("0", "1", "10", "20", "80"):
            with self.subTest(columns=width), patch.dict(os.environ, {"COLUMNS": width}):
                output, errors = io.StringIO(), io.StringIO()
                with (
                    redirect_stdout(output),
                    redirect_stderr(errors),
                    self.assertRaises(SystemExit) as caught,
                ):
                    run.run_cli(["--version"])
                self.assertEqual(caught.exception.code, 0)
                self.assertEqual(
                    output.getvalue(), podvigil.NAME + " " + podvigil.__version__ + "\n"
                )
                self.assertEqual(errors.getvalue(), "")

    def test_pyproject_reads_the_one_version_source(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('version = { attr = "' + podvigil.NAME + '.__version__" }', text)
        self.assertIsNone(re.search(r"(?m)^version\s*=\s*[\"']", text), "a second, static version")

    def test_pod_spec_carries_version_and_schema(self):
        body = run.payload(fixture(), "testvolume")
        spec = json.loads(base64.b64decode(body["env"][ENV_PREFIX + "_JOB_SPEC"]))
        self.assertEqual(
            (spec["runner_version"], spec["ledger_schema"]),
            (podvigil.__version__, podvigil.LEDGER_SCHEMA),
        )

    def test_job_record_carries_version_and_schema(self):
        with tempfile.TemporaryDirectory(prefix="wondomon-version-") as tmp:
            jobs = Path(tmp) / "jobs"
            refusal = RuntimeError("stopped once the ledger exists")
            with (
                patch_run("Api", return_value=FakeApi()),
                patch_run("Storage", return_value=Storage("EU-RO-1", FakeS3())),
                patch_run("load_key", return_value="dummy-test-key"),
                patch_run("spawn_watchdog", side_effect=refusal),
                patch_run("jobs_dir", return_value=jobs),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(RuntimeError, "stopped once the ledger exists"):
                    run.launch(copy.deepcopy(fixture()["config"]))
            job = read(next(jobs.iterdir()) / "job.json")
        self.assertEqual(
            (job["runner_version"], job["ledger_schema"]),
            (podvigil.__version__, podvigil.LEDGER_SCHEMA),
        )

    def test_a_ledger_without_version_fields_stays_readable(self):
        with tempfile.TemporaryDirectory(prefix="wondomon-old-ledger-") as tmp:
            folder = Path(tmp)
            job = fixture()
            self.assertNotIn("runner_version", job)
            ledger(folder, job)  # the files and fields of a ledger from before 0.1.0
            self.assertEqual(run.status(folder)["job_id"], job["job_id"])
            self.assertEqual(len(run.state_lines(folder)), 2)


if __name__ == "__main__":
    unittest.main()
