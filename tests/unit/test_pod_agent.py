"""Pod review regressions; no account key, .env, or provider access.

Run portable tests: python -B -m unittest tests.unit.test_pod_agent -v
The real Linux container runs are in tests/container/test_pod_guard_linux.py.
"""

from pathlib import Path
from unittest.mock import patch
import errno
import hashlib
import os
import socket
import tempfile
import unittest

from podvigil.podrt import agent
from podvigil.podrt.records import save
from tests.support.pod import job


class LocalApi:
    def __init__(self, pod=None):
        self.pod = pod or {"id": "testpod", "name": "mutated", "env": {}, "cloud": "COMMUNITY"}
        self.calls = []
        self.last_response = None

    def get_or_none(self, path):
        self.calls.append(("GET", path))
        self.last_response = {
            "method": "GET",
            "path": path,
            "status": 404 if self.pod is None else 200,
        }
        return self.pod

    def request(self, method, path):
        self.calls.append((method, path))
        if method != "DELETE":
            raise AssertionError(method)
        self.pod = None
        self.last_response = {"method": "DELETE", "path": path, "status": 204}


class PodReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pod-review-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.output = self.folder / "artifacts"
        self.output.mkdir()
        self.job = job()
        (self.output / "train.log").write_bytes(b"log")
        save(self.output / "result.json", {"job_id": self.job["job_id"], "outcome": "success"})
        (self.output / "model.bin").write_bytes(b"checkpoint")

    def assert_no_manifest(self):
        self.assertFalse((self.folder / "artifact-manifest.json").exists())

    def test_manifest_hashes_all_files(self):
        nested = self.output / "checkpoints"
        nested.mkdir()
        (nested / "step.bin").write_bytes(b"last step")
        result = agent.make_manifest(self.job, self.folder)
        self.assertEqual(len(result["files"]), 4)
        for entry in result["files"]:
            content = (self.folder / entry["path"]).read_bytes()
            self.assertEqual(entry["size"], len(content))
            self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())

    def test_manifest_hashes_raw_binary_bytes(self):
        (self.output / "model.bin").write_bytes(b"one\r\ntwo\x1a\x00\xff")
        manifest = agent.make_manifest(self.job, self.folder)
        entry = next(item for item in manifest["files"] if item["path"] == "artifacts/model.bin")
        self.assertEqual(entry["sha256"], hashlib.sha256(b"one\r\ntwo\x1a\x00\xff").hexdigest())

    def test_scan_error_invalidates_prior_complete_manifest(self):
        agent.make_manifest(self.job, self.folder)
        blocked = self.output / "checkpoints"
        blocked.mkdir()
        (blocked / "only-copy.bin").write_bytes(b"precious")
        original = os.scandir

        def scan(path):
            actual = (
                Path(os.readlink(f"/proc/self/fd/{path}")) if isinstance(path, int) else Path(path)
            )
            if actual == blocked:
                raise PermissionError("injected unreadable directory")
            return original(path)

        with (
            patch("podvigil.podrt.agent.os.scandir", side_effect=scan),
            self.assertRaises(PermissionError),
        ):
            agent.make_manifest(self.job, self.folder)
        self.assert_no_manifest()

    def test_stat_error_does_not_publish_complete_manifest(self):
        original = os.stat

        def lstat(path, *args, **kwargs):
            if Path(path).name == "model.bin":
                raise OSError(errno.EIO, "injected stat error")
            return original(path, *args, **kwargs)

        with patch("podvigil.podrt.bundle.os.stat", side_effect=lstat), self.assertRaises(OSError):
            agent.make_manifest(self.job, self.folder)
        self.assert_no_manifest()

    def test_file_added_during_hashing_rejects_manifest(self):
        original = agent.output_bundle.inventory
        calls = [0]

        def scan(path, deadline=None):
            calls[0] += 1
            if calls[0] == 2:
                (path / "late.bin").write_bytes(b"late checkpoint")
            return original(path, deadline)

        with (
            patch("podvigil.podrt.bundle.inventory", side_effect=scan),
            self.assertRaisesRegex(RuntimeError, "tree changed"),
        ):
            agent.make_manifest(self.job, self.folder)
        self.assert_no_manifest()

    def test_flush_failure_rejects_manifest(self):
        with (
            patch("podvigil.podrt.agent.os.fsync", side_effect=OSError(errno.EIO, "flush failed")),
            self.assertRaises(OSError),
        ):
            agent.make_manifest(self.job, self.folder)
        self.assert_no_manifest()

    def test_missing_success_artifact_rejects_manifest(self):
        self.job["config"]["required_artifacts"] = ["missing.bin"]
        with self.assertRaisesRegex(RuntimeError, "required"):
            agent.make_manifest(self.job, self.folder)
        self.assert_no_manifest()

    def test_failed_job_can_preserve_available_checkpoints(self):
        self.job["config"]["required_artifacts"] = ["missing.bin"]
        save(
            self.output / "result.json",
            {"job_id": self.job["job_id"], "outcome": "training_failed"},
        )
        self.assertTrue(agent.make_manifest(self.job, self.folder)["complete"])

    def test_reserved_and_unsafe_output_names_rejected(self):
        for name in (
            "..",
            "a/b",
            "a\\b",
            "NUL",
            "COM1.txt",
            "file.",
            "file ",
            "x:y",
            "LONGNA~1.TXT",
            "x\x00y",
        ):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                agent.portable_component(name)

    def test_internal_hardlinks_preserved_in_bundle(self):
        os.link(self.output / "model.bin", self.output / "alias.bin")
        manifest = agent.make_manifest(self.job, self.folder)
        self.assertEqual(manifest["version"], 2)

    @unittest.skipUnless(os.name == "posix", "Linux filesystem semantics")
    def test_case_collisions_preserved_in_bundle(self):
        (self.output / "Model.bin").write_bytes(b"different")
        self.assertEqual(agent.make_manifest(self.job, self.folder)["version"], 2)

    @unittest.skipUnless(os.name == "posix", "Linux special files")
    def test_fifo_rejected_and_symlink_preserved_without_following(self):
        unsafe = self.output / "unsafe"
        os.mkfifo(unsafe)
        with self.assertRaises(ValueError):
            agent.make_manifest(self.job, self.folder)
        unsafe.unlink()
        unsafe.symlink_to(self.output / "model.bin")
        self.assertEqual(agent.make_manifest(self.job, self.folder)["version"], 2)

    @unittest.skipUnless(os.name == "posix", "Linux symlink semantics")
    def test_symlink_output_root_rejected(self):
        actual = self.folder / "actual"
        self.output.rename(actual)
        self.output.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(ValueError):
            agent.make_manifest(self.job, self.folder)
        self.assert_no_manifest()

    @unittest.skipUnless(hasattr(os, "seteuid"), "Linux privilege regression")
    def test_real_permission_error_propagates(self):
        if os.geteuid() != 0:
            self.skipTest("Requires root to switch effective UID in the isolated container")
        self.folder.chmod(0o777)
        hidden = self.output / "hidden"
        hidden.mkdir()
        (hidden / "only-copy.bin").write_bytes(b"checkpoint")
        hidden.chmod(0)
        try:
            os.seteuid(65534)
            with self.assertRaises(PermissionError):
                agent.make_manifest(self.job, self.folder)
        finally:
            os.seteuid(0)
            hidden.chmod(0o755)
        self.assert_no_manifest()

    def test_once_marker_is_durable_and_never_overwritten(self):
        attempt = self.folder / "attempt"
        attempt.mkdir()
        agent.claim_attempt(attempt)
        before = (attempt / "started-once.json").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "restart"):
            agent.claim_attempt(attempt)
        self.assertEqual((attempt / "started-once.json").read_bytes(), before)

    def test_unclaimed_existing_outputs_are_preserved(self):
        with self.assertRaisesRegex(RuntimeError, "Unclaimed"):
            agent.claim_attempt(self.folder)
        self.assertFalse((self.folder / "started-once.json").exists())
        self.assertEqual((self.output / "model.bin").read_bytes(), b"checkpoint")

    def test_marker_write_failure_is_fatal_before_release(self):
        attempt = self.folder / "attempt"
        attempt.mkdir()
        with (
            patch("podvigil.podrt.agent.save", side_effect=OSError(errno.ENOSPC, "full")),
            self.assertRaises(OSError),
        ):
            agent.claim_attempt(attempt)
        self.assertFalse((attempt / "started-once.json").exists())

    def test_self_cleanup_ignores_mutable_identity_and_wrong_cloud(self):
        api = LocalApi()
        with patch.dict(os.environ, {"RUNPOD_POD_ID": "testpod"}):
            agent.terminate(api, "testpod", self.job)
        self.assertIsNone(api.pod)
        self.assertIn(("DELETE", "/pods/testpod"), api.calls)

    def test_self_cleanup_refuses_arbitrary_id(self):
        api = LocalApi()
        with (
            patch.dict(os.environ, {"RUNPOD_POD_ID": "testpod"}),
            self.assertRaises(PermissionError),
        ):
            agent.terminate(api, "unrelated", self.job)
        self.assertFalse(api.calls)

    def test_self_cleanup_refuses_wrong_get_identity(self):
        api = LocalApi({"id": "unrelated"})
        with (
            patch.dict(os.environ, {"RUNPOD_POD_ID": "testpod"}),
            self.assertRaises(PermissionError),
        ):
            agent.terminate(api, "testpod", self.job)
        self.assertNotIn(("DELETE", "/pods/testpod"), api.calls)

    def test_supervisor_spawn_failure_still_deletes(self):
        api = LocalApi()
        with (
            patch.dict(os.environ, {"RUNPOD_POD_ID": "testpod"}),
            patch("podvigil.podrt.agent.os.getpid", return_value=1),
            patch("podvigil.podrt.agent.sys.platform", "linux"),
            patch("podvigil.podrt.agent.drain_processes"),
            patch(
                "podvigil.podrt.agent.subprocess.Popen",
                side_effect=OSError(errno.EAGAIN, "process limit"),
            ),
        ):
            agent.guard(self.job, api, "testpod")
        self.assertIsNone(api.pod)

    def test_readiness_rejection_is_fatal(self):
        left, right = socket.socketpair()
        try:
            right.sendall(b"!")
            with self.assertRaises(RuntimeError):
                agent.exchange(left, b"R", b"G")
        finally:
            left.close()
            right.close()

    def test_drain_refuses_host_processes(self):
        with (
            patch("podvigil.podrt.agent.os.getpid", return_value=123),
            self.assertRaises(RuntimeError),
        ):
            agent.drain_processes(None)


if __name__ == "__main__":
    unittest.main()
