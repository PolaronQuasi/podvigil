"""The output-bundle POSIX suite inside a network-less Linux container.

Set PODVIGIL_REVIEW_DOCKER_IMAGE to an already-installed Python image ID; images are never pulled.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest
import uuid

from tests.container import review_image
from tests.support.pod import POD_SOURCES, stage_sources


@unittest.skipUnless(review_image(), "Set installed image ID for offline Linux bundle tests")
class BundleDockerTests(unittest.TestCase):
    def test_network_disabled_linux_bundle_suite(self):
        with TemporaryDirectory(prefix="wondomon-bundle-") as temporary:
            root = Path(temporary)
            stage_sources(
                root,
                *POD_SOURCES,
                "src/podvigil/storage.py",
                "src/podvigil/policy.py",
                "src/podvigil/jobspec.py",
                "src/podvigil/pathsafe.py",
                "src/podvigil/credentials.py",
                "src/podvigil/settings.py",
                "src/podvigil/provider/__init__.py",
                "src/podvigil/provider/client.py",
                "tests/support/fakes.py",
                "tests/unit/test_output_bundle.py",
            )
            container = "wondomon-bundle-" + uuid.uuid4().hex
            command = [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--name",
                container,
                "--cpus",
                "2",
                "--memory",
                "1g",
                "--pids-limit",
                "64",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--mount",
                f"type=bind,source={root},target=/review,readonly",
                "--workdir",
                "/review",
                "--entrypoint",
                "python3",
                review_image(),
                "-B",
                "-m",
                "unittest",
                "tests.unit.test_output_bundle.BundlePosixTests",
                "-v",
            ]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stdout + result.stderr, flush=True)
            finally:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=15)
