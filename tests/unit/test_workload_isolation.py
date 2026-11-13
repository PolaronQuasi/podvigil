"""Credential and permission boundary tests; real Linux cases need Linux and root.

No production credentials or checkout mounts. The Docker cases are in
tests/container/test_workload_isolation_linux.py.
"""

from pathlib import Path
from unittest.mock import patch
import os
import shutil
import sys
import tempfile
import unittest

from podvigil.podrt import isolation
from podvigil.podrt import agent
from podvigil.podrt.records import VOLUME_MOUNT, read


class IsolationPortableTests(unittest.TestCase):
    def test_scoped_api_configures_bounded_pacing(self):
        with patch("podvigil.podrt.agent.Api") as factory:
            api = agent.scoped_api("DUMMY")
            factory.assert_called_once_with("DUMMY")
            api.configure_pacing.assert_called_once_with(max_wait=2, read_retries=2)

    def test_cleanup_pacing_has_no_expired_workload_deadline(self):
        with patch("podvigil.podrt.agent.Api") as factory:
            api = factory.return_value
            with agent.api_pacing(api, cleanup=True):
                pass
            api.pacing.assert_called_once_with(cleanup=True, deadline=None, max_wait=2)

    def test_private_home_caches_override_inherited_and_declared_locations(self):
        value = isolation.workload_environment(
            {
                "HOME": "/root",
                "HF_HOME": "/root/hf",
                "CUDA_CACHE_PATH": "/root/cuda",
                "TMPDIR": "/root/tmp",
                "PATH": "/bin",
            },
            {"home": "/tmp/private-workload"},
        )
        for key in (
            "HOME",
            "HF_HOME",
            "CUDA_CACHE_PATH",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "TORCH_HOME",
            "PIP_CACHE_DIR",
            "TRANSFORMERS_CACHE",
            "HUGGINGFACE_HUB_CACHE",
        ):
            self.assertTrue(value[key].startswith("/tmp/private-workload"), key)
        self.assertEqual(value["PATH"], "/bin")

    def test_no_unsupported_platform_root_execution_fallback(self):
        with (
            patch("podvigil.podrt.isolation.sys.platform", "win32"),
            self.assertRaises(RuntimeError),
        ):
            isolation.prepare(
                Path("/workspace"),
                Path("/workspace/jobs/j"),
                Path("/code"),
                volume=Path(VOLUME_MOUNT),
            )


@unittest.skipUnless(
    sys.platform.startswith("linux"), "Actual Linux ownership and processes required"
)
class IsolationLinuxUnitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="workload-isolation-unit-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        # An ancestor with root-only mode would prevent the final UID reaching
        # otherwise valid outputs, unlike the production /workspace folder.
        self.base.chmod(0o711)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.folder = self.root / "jobs" / "test"
        self.code = self.base / "code"
        self.code.mkdir()
        shutil.copyfile(isolation.__file__, self.code / Path(isolation.__file__).name)
        # A folder in place of the network volume mount, below its lock folder.
        self.volume = self.base / "guard" / "data"
        self.volume.mkdir(parents=True)
        mounts = patch(
            "podvigil.podrt.isolation.mounts",
            return_value=[("/", "0:1", "/"), (str(self.volume), "0:99", "/")],
        )
        mounts.start()
        self.addCleanup(mounts.stop)
        isolation.protect_process()

    def prepare(self):
        return isolation.prepare(self.root, self.folder, self.code, volume=self.volume)

    def test_real_helper_identity_empty_groups_caps_no_new_privs_and_loader(self):
        layout = self.prepare()
        # This unit runner itself is PID 1; the full-agent case below has the
        # separate PID-1 guard and root supervisor whose permissions we check.
        layout["guard_pid"] = os.getpid()
        code = (
            "import json,os;from pathlib import Path;"
            "s=dict(x.split(':',1) for x in Path('/proc/self/status').read_text().splitlines());"
            "Path('identity.json').write_text(json.dumps({'uid':os.getresuid(),'gid':os.getresgid(),"
            "'groups':os.getgroups(),'caps':{k:s[k].strip() for k in s if k.startswith('Cap')},"
            "'nnp':s['NoNewPrivs'].strip(),'home':os.environ['HOME']}))"
        )
        with (self.folder / "artifacts/train.log").open("wb") as log:
            proc = isolation.launch(
                [sys.executable, "-c", code],
                layout,
                {"PATH": "/usr/local/bin:/usr/bin:/bin", "LD_PRELOAD": "/not/a/library"},
                log,
                timeout=10,
                authorize=lambda: None,
            )
            self.assertEqual(proc.wait(timeout=10), 0)
        evidence = read(self.folder / "artifacts/identity.json")
        self.assertEqual(evidence["uid"], [61000] * 3)
        self.assertEqual(evidence["gid"], [61000] * 3)
        self.assertEqual(evidence["groups"], [])
        self.assertEqual(evidence["nnp"], "1")
        self.assertTrue(all(int(value, 16) == 0 for value in evidence["caps"].values()))
        self.assertNotEqual(evidence["home"], "/root")

    def test_ignored_chmod_fails_before_workload(self):
        with (
            patch("podvigil.podrt.isolation.os.fchmod", return_value=None),
            self.assertRaises(RuntimeError),
        ):
            self.prepare()

    def test_chown_denied_fails_before_workload(self):
        with (
            patch("podvigil.podrt.isolation.os.fchown", side_effect=PermissionError),
            self.assertRaises(PermissionError),
        ):
            self.prepare()

    def test_input_symlink_rejected_without_modifying_target(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        target = self.base / "target"
        target.write_bytes(b"must survive")
        before = target.stat()
        (inputs / "alias").symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(target.read_bytes(), b"must survive")
        self.assertEqual(target.stat().st_mode, before.st_mode)

    def test_input_hardlink_rejected_without_repermissioning_target(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        target = self.base / "target"
        target.write_bytes(b"must survive")
        before = target.stat()
        os.link(target, inputs / "alias")
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(target.stat().st_mode, before.st_mode)

    def test_control_symlink_rejected(self):
        self.folder.mkdir(parents=True)
        target = self.base / "target"
        target.mkdir()
        (self.folder / "control").symlink_to(target, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
