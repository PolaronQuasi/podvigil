"""Publication checks reject private material without echoing its contents."""

from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import tempfile
import unittest
import zipfile

from tools import ci


class PublicationChecks(unittest.TestCase):
    def test_secret_scan_reports_only_the_path(self):
        secret = "rpa_" + "Z" * 30
        with tempfile.TemporaryDirectory(prefix="podvigil-ci-check-") as tmp:
            root = Path(tmp)
            (root / "example.txt").write_text(secret)
            output = io.StringIO()
            with (
                patch.object(ci, "ROOT", root),
                patch.object(ci, "tracked", return_value=["example.txt"]),
                redirect_stdout(output),
                self.assertRaises(RuntimeError) as caught,
            ):
                ci.audit()
            self.assertIn("example.txt", str(caught.exception))
            self.assertNotIn(secret, str(caught.exception) + output.getvalue())

    def test_distribution_rejects_embedded_credentials_and_working_name_package(self):
        with tempfile.TemporaryDirectory(prefix="podvigil-ci-check-") as tmp:
            for member in (".env", "podwarden/__init__.py", "../outside.py"):
                path = Path(tmp) / "bad.whl"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(member, "placeholder")
                with self.subTest(member=member), self.assertRaises(RuntimeError):
                    ci.inspect_distribution(path)
