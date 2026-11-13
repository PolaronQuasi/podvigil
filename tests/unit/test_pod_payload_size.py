"""The Pod code travels inside the Pod create request, which Runpod caps at 100,000 bytes.

payload inlines the files of src/podvigil/podrt (JSON, zlib, base64) next to the job spec. The input list
travels as a hashed manifest on the volume, so the request no longer grows with the inputs; the Pod code must
still leave room for the settings and the Pods already in the account. In the Pod the files are written into one
private folder, which is imported as the package podrt: the folder must stay flat (the guard protects only
regular files there) and its modules may import nothing but the standard library and each other.
"""

from pathlib import Path
import ast
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

from podvigil.controller import request
from podvigil.jobspec import validate
from podvigil.podrt import records
from tests.support import runner as run
from tests.support.paths import ROOT

REQUEST_LIMIT = 100000  # payload refuses a larger body.
BUNDLE_BUDGET = 56000  # Pod code share of the request; the rest is the job spec and inputs.
REAL_INPUT_COUNT = 62  # The first paid use (2026-11-12) shipped 62 input files.


def bundle():
    """The Pod code exactly as payload encodes it."""
    return base64.b64encode(zlib.compress(json.dumps(request.pod_sources()).encode())).decode()


def realistic_job(inputs):
    cfg = validate(json.loads((ROOT / "jobs/smoke.example.json").read_text()))
    cfg["inputs"] = [
        {
            "local": "C:/Users/someone/projects/experiment/src/module_%03d.py" % index,
            "remote": "src/experiment/module_%03d.py" % index,
        }
        for index in range(inputs)
    ]
    return {
        "job_id": "f" * 32,
        "pod_name": "wondomon-" + "f" * 32,
        "prior_pod_ids": ["x" * 24] * 3,
        "deadline_epoch": time.time() + 3600,
        "startup_deadline_epoch": time.time() + 900,
        "config": cfg,
        "inputs": [
            {"remote": item["remote"], "size": 123456, "sha256": "a" * 64} for item in cfg["inputs"]
        ],
    }


class PodPayloadSizeTests(unittest.TestCase):
    def test_pod_code_stays_within_its_share_of_the_request(self):
        size = len(bundle())
        self.assertLessEqual(
            size,
            BUNDLE_BUDGET,
            f"The Pod code is {size} bytes in the request, over its {BUNDLE_BUDGET}-byte share. "
            "Shrink it or bake the agent into the image.",
        )

    def test_request_with_the_real_input_count_fits(self):
        body = run.payload(realistic_job(REAL_INPUT_COUNT), "testvolume")
        size = len(json.dumps(body).encode())
        self.assertLessEqual(size, min(REQUEST_LIMIT, run.POD_REQUEST_LIMIT))
        # The request carries exactly the Pod files, as measured above.
        shipped = re.search(r"b64decode\('([A-Za-z0-9+/=]+)'\)", body["cmd"][0]).group(1)
        self.assertEqual(shipped, bundle())
        self.assertEqual(
            sorted(json.loads(zlib.decompress(base64.b64decode(shipped)))),
            sorted(request.POD_FILES),
        )

    def test_the_request_does_not_grow_with_the_inputs(self):
        sizes = [
            len(json.dumps(run.payload(realistic_job(count), "testvolume")).encode())
            for count in (0, 62, 5000)
        ]
        # Only the digits of the manifest's size and count change.
        self.assertLess(max(sizes) - min(sizes), 20, sizes)

    def test_the_pod_code_folder_is_flat_and_shipped_whole(self):
        folder = request.POD_CODE
        self.assertEqual(folder, ROOT / "src" / "podvigil" / "podrt")
        entries = sorted(path.name for path in folder.iterdir() if path.name != "__pycache__")
        self.assertEqual(
            entries,
            sorted(request.POD_FILES),
            "Every file in podrt/ is shipped, and podrt/ holds no folder: add a new module to POD_FILES.",
        )

    def test_the_pod_code_imports_only_the_standard_library_and_itself(self):
        siblings = {name.removesuffix(".py") for name in request.POD_FILES} - {"__init__"}
        for name in request.POD_FILES:
            tree = ast.parse((request.POD_CODE / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(module=name, imports=alias.name):
                            self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    with self.subTest(module=name, imports=node.module):
                        self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)
                elif isinstance(node, ast.ImportFrom):
                    with self.subTest(module=name, imports="." * node.level + (node.module or "")):
                        self.assertEqual(
                            node.level, 1, "only sibling modules of the same flat folder"
                        )
                        targets = (
                            [node.module] if node.module else [alias.name for alias in node.names]
                        )
                        self.assertLessEqual(set(targets), siblings)

    def test_the_bootstrap_imports_the_folder_as_a_package(self):
        bootstrap = request.pod_bootstrap({"__init__.py": ""})
        compile(bootstrap, "<bootstrap>", "exec")
        self.assertIn("submodule_search_locations", bootstrap)
        self.assertIn(
            "runpy.run_module('podrt.agent',run_name='__main__',alter_sys=True)", bootstrap
        )
        self.assertNotIn("sys.path.insert", bootstrap)  # nothing writable is put on the import path

    def test_the_bootstrap_runs_the_agent_as_main_with_its_siblings(self):
        # Use the shipped files with a stub agent that reports how it was started.
        source = request.pod_sources()
        source["agent.py"] = (
            "import pathlib,sys\nfrom . import isolation, records, teardown\nfrom .api import ApiError\n"
            "print(__name__, __package__, records.VOLUME_MOUNT, sys.modules['__main__'].__name__,"
            " teardown.ApiError is ApiError, pathlib.Path(isolation.__file__).name)\n"
            "print(pathlib.Path(__file__).parent)\n"
        )
        # Pass the bootstrap through a file, as the container tests do.
        # The bootstrap exceeds the Windows command-line length limit.
        with tempfile.TemporaryDirectory(prefix="pod-bootstrap-") as temporary:
            script = Path(temporary) / "bootstrap.py"
            script.write_text(request.pod_bootstrap(source), encoding="utf-8")
            done = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-u",
                    "-c",
                    "import sys;exec(compile(open(sys.argv[1],encoding='utf-8').read(),sys.argv[1],'exec'))",
                    str(script),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr)
        report, folder = done.stdout.splitlines()
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(
            report.split(),
            ["__main__", "podrt", records.VOLUME_MOUNT, "__main__", "True", "isolation.py"],
        )
        self.assertEqual(
            sorted(path.name for path in Path(folder).iterdir()), sorted(request.POD_FILES)
        )


if __name__ == "__main__":
    unittest.main()
