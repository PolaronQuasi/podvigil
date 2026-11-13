"""Offline release evidence, the CI scanner's output, and the documented input-module invocation."""

from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import run_validation as validation
from tests.container import IMAGE_VARIABLES
from tests.support.paths import ROOT


class ValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-validation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, name, content="fixture\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def record(self, callback=lambda: None):
        suite = unittest.TestSuite([unittest.FunctionTestCase(callback)])
        with (
            patch.object(validation, "ROOT", self.root),
            patch.object(unittest.defaultTestLoader, "discover", return_value=suite),
            patch.object(sys, "argv", ["run_validation"]),
            redirect_stdout(io.StringIO()),
        ):
            code = validation.main()
        record = json.loads(
            (self.root / "output" / "validation" / "validation.json").read_text(encoding="utf-8")
        )
        return code, record

    def test_fingerprints_cover_examples_docs_fixtures_ci_and_package_inputs(self):
        names = (
            "src/podvigil/module.py",
            "tests/unit/test_example.py",
            "tests/fixture.bin",
            "examples/train.py",
            "examples/data/points.csv",
            "examples/config/run.json",
            "docs/OPERATIONS.md",
            "docs/fixture.json",
            ".gitea/workflows/ci.yml",
            ".github/workflows/verify.yml",
            "tools/ci.py",
            "assets/icon.svg",
            "package.json",
            "package-lock.json",
            "jobs/smoke.example.json",
            "README.md",
            "pyproject.toml",
            "requirements.txt",
            "requirements.lock.txt",
            "LICENSE",
            "NOTICE",
            ".gitattributes",
            ".gitignore",
            ".editorconfig",
        )
        for name in names:
            self.write(name)
        hashes = validation.source_hashes(self.root)
        self.assertEqual(set(hashes), set(names))
        self.assertEqual(set(hashes.values()), {hashlib.sha256(b"fixture\n").hexdigest()})
        self.write("examples/data/points.csv", "changed\n")
        self.assertNotEqual(
            validation.source_hashes(self.root)["examples/data/points.csv"],
            hashes["examples/data/points.csv"],
        )

    def test_fingerprints_exclude_credentials_generated_output_and_dependencies(self):
        self.write("src/podvigil/module.py")
        for name in (
            ".env",
            ".env.local",
            "examples/credentials.env",
            "examples/keys.env",
            "tests/dummy.key",
            "tests/dummy.pem",
            "jobs/train.local.json",
            "output/validation/validation.json",
            "output/script.py",
            "downloads/results.json",
            "build/lib/module.py",
            ".venv/Lib/site-packages/dependency.py",
            "src/venv/dependency.py",
            "tests/__pycache__/test_example.pyc",
            "examples/node_modules/dependency.js",
            "src/pkg.egg-info/PKG-INFO",
        ):
            self.write(name)
        self.assertEqual(set(validation.source_hashes(self.root)), {"src/podvigil/module.py"})

    def test_record_uses_the_effective_container_image_with_legacy_fallback(self):
        current, legacy, oldest = IMAGE_VARIABLES
        cases = (
            ({}, None),
            ({current: "sha256:new"}, "sha256:new"),
            ({legacy: "sha256:old"}, "sha256:old"),
            ({oldest: "sha256:oldest"}, "sha256:oldest"),
            ({legacy: "sha256:old", oldest: "sha256:oldest"}, "sha256:old"),
            ({current: "sha256:new", legacy: "sha256:old"}, "sha256:new"),
            ({current: "", legacy: "sha256:old"}, "sha256:old"),
        )
        for supplied, expected in cases:
            with self.subTest(supplied=supplied), patch.dict(os.environ):
                for name in IMAGE_VARIABLES:
                    os.environ.pop(name, None)
                os.environ.update(supplied)
                code, record = self.record()
                self.assertEqual(code, 0)
                self.assertEqual(record["docker_image"], expected)
                self.assertEqual(record["git"], {"commit": None, "dirty": None})

    def test_a_document_change_during_validation_invalidates_the_record(self):
        page = self.write("docs/OPERATIONS.md", "before\n")
        code, record = self.record(lambda: page.write_text("after\n", encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertFalse(record["successful"])
        self.assertEqual(record["source_changed_during_run"], ["docs/OPERATIONS.md"])
        self.assertNotIn("output/validation-tests.log", record["source_sha256"])

    def test_changing_git_identity_during_validation_invalidates_the_record(self):
        before = {"commit": "a" * 40, "dirty": True}
        after = {"commit": "b" * 40, "dirty": True}
        with patch.object(validation, "git_state", side_effect=[before, after]):
            code, record = self.record()
        self.assertEqual(code, 1)
        self.assertEqual(record["git_before_run"], before)
        self.assertEqual(record["git"], after)
        self.assertTrue(record["git_changed_during_run"])

    def test_missing_git_is_unknown_not_clean(self):
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(validation.git_state(self.root), {"commit": None, "dirty": None})

    @unittest.skipUnless(shutil.which("git"), "Git is needed to check checkout dirtiness")
    def test_git_dirty_state_includes_new_inputs_but_ignores_local_files(self):
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)
        self.write(".git/info/exclude", ".env\noutput/\n.venv/\n")
        self.write(".env", "DUMMY_ONLY=1\n")
        self.write("output/validation/validation.json")
        self.write(".venv/dependency.py")
        self.assertEqual(validation.git_state(self.root), {"commit": None, "dirty": False})
        self.write("examples/data/new.csv")
        self.assertEqual(validation.git_state(self.root), {"commit": None, "dirty": True})


class ReleaseExamplesTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "Git is needed to exercise the CI scanner")
    def test_ci_scanner_reports_filenames_without_matching_secret_values(self):
        workflow = (ROOT / ".gitea" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        command = next(
            line.strip().split(" || ", 1)[0]
            for line in workflow.splitlines()
            if line.strip().startswith("git grep ")
        )
        with tempfile.TemporaryDirectory(prefix="podvigil-ci-scan-") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
            tokens = ["rpa_" + "A" * 24, "rps_" + "B" * 24, "-----BEGIN " + "PRIVATE KEY-----"]
            (root / "fixture.txt").write_text("\n".join(tokens) + "\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "fixture.txt"], check=True, capture_output=True
            )
            done = subprocess.run(
                shlex.split(command), cwd=root, capture_output=True, text=True, timeout=10
            )
            # Matches make the workflow fail after printing only paths.
            self.assertEqual(done.returncode, 0)
            self.assertEqual(done.stdout.strip(), "fixture.txt")
            for token in tokens:
                self.assertNotIn(token, done.stdout + done.stderr)
            (root / "fixture.txt").write_text("no credentials here\n", encoding="utf-8")
            clean = subprocess.run(
                shlex.split(command), cwd=root, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(clean.returncode, 1)
            self.assertEqual(clean.stdout, "")

    def test_documented_activated_python_finds_the_script_from_the_output_folder(self):
        page = (ROOT / "docs" / "WORKLOAD-CONTRACT.md").read_text(encoding="utf-8")
        example = re.search(r'`(\["bash", "-c", "source /opt/env/bin/activate[^`]+\])`', page)
        self.assertIsNotNone(example)
        command = shlex.split(json.loads(example.group(1))[2].split("&&", 1)[1])
        self.assertEqual(command[0], "python")
        with tempfile.TemporaryDirectory(prefix="podvigil-doc-command-") as tmp:
            root = Path(tmp)
            inputs, output = root / "inputs", root / "output"
            inputs.mkdir()
            output.mkdir()
            (inputs / "train.py").write_text("print('input module ran')\n", encoding="utf-8")
            environment = dict(os.environ, PYTHONPATH=str(inputs), PYTHONDONTWRITEBYTECODE="1")
            done = subprocess.run(
                [sys.executable, "-B", *command[1:]],
                cwd=output,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), "input module ran")


if __name__ == "__main__":
    unittest.main()
