"""Pod-agent lifecycle regressions; dummy APIs only.

The real PID-1 cases are in tests/container/test_pod_lifecycle_linux.py.
No image pulls, account access, or checkout mounts.
"""

from pathlib import Path
from unittest.mock import patch
import errno
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from urllib.error import HTTPError

from podvigil.podrt import agent
from podvigil.podrt.records import read
from podvigil.podrt.api import Api, ApiError
from tests.support.pod import job


class FakeApi:
    """Independent read/write permission controls, with real response metadata."""

    def __init__(
        self,
        *,
        get_denied=None,
        delete_denied=None,
        stop_denied=None,
        deny_after_delete=False,
        absent=False,
    ):
        self.key = "DUMMY"
        self.get_denied, self.delete_denied = get_denied, delete_denied
        self.stop_denied, self.deny_after_delete = stop_denied, deny_after_delete
        self.absent, self.calls, self.last_response = absent, [], None

    def response(self, method, path, status):
        self.last_response = dict(
            method=method, path=path, status=status, response_date="Tue, 10 Nov 2026 20:00:00 GMT"
        )

    def now(self):
        assert any(method == "GET" for method, _ in self.calls)
        return time.time()

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "GET":
            if self.get_denied and (not self.absent or self.deny_after_delete):
                self.response(method, path, self.get_denied)
                raise ApiError(self.get_denied, method, path)
            self.response(method, path, 404 if self.absent else 200)
            if self.absent:
                raise ApiError(404, method, path)
            return {"id": "testpod", "cloud": "SECURE", "name": "mutated", "env": {}}
        if method == "DELETE":
            self.response(method, path, self.delete_denied or 204)
            if self.delete_denied:
                raise ApiError(self.delete_denied, method, path)
            self.absent = True
            return None
        if method == "POST":
            assert path == "/pods/testpod/action" and body == {"action": "stop"}
            self.response(method, path, self.stop_denied or 200)
            if self.stop_denied:
                raise ApiError(self.stop_denied, method, path)
            return {"id": "testpod", "status": "EXITED"}
        raise AssertionError("Unexpected mutation: " + method)

    def get_or_none(self, path):
        try:
            return self.request("GET", path)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise


class PodLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pod-")
        self.addCleanup(temp.cleanup)
        # The job's folder on the Pod's disk, and its folder on the network volume.
        self.folder = Path(temp.name) / "local"
        self.volume = Path(temp.name) / "volume"
        self.output = self.folder / "artifacts"
        self.output.mkdir(parents=True)
        self.volume.mkdir()
        self.job = job()
        self.result = {
            "job_id": self.job["job_id"],
            "pod_id": "testpod",
            "outcome": "success",
            "exit_code": 0,
        }
        for mock in (
            patch("podvigil.podrt.agent.job_folder", return_value=self.folder),
            patch("podvigil.podrt.agent.volume_folder", return_value=self.volume),
            patch("podvigil.podrt.agent.volume_mounted", return_value=True),
            patch.dict(os.environ, {"RUNPOD_POD_ID": "testpod"}),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        # Only portable units omit waiting. Real container cases use production
        # two separated reads (including the full one-second separation).
        actual_delete = agent.delete_verified
        mock = patch(
            "podvigil.podrt.agent.delete_verified",
            side_effect=lambda *args, **kwargs: actual_delete(
                *args, sleep=lambda _: None, **kwargs
            ),
        )
        mock.start()
        self.addCleanup(mock.stop)

    def evidence(self):
        return read(self.volume / "control/self-delete.json")

    def finalize(self, quiesce):
        return agent.finalize_outputs(
            self.job, "testpod", self.folder, self.result, quiesce, volume=self.volume
        )

    def test_delete_denied_stops_once_and_exits_unverified(self):
        for status in (401, 403):
            for stop_denied in (None, 403):
                with self.subTest(status=status, stop_denied=stop_denied):
                    api = FakeApi(delete_denied=status, stop_denied=stop_denied)
                    with self.assertRaises(agent.SelfCleanupUnverified):
                        agent.terminate(api, "testpod", self.job)
                    evidence = self.evidence()
                    self.assertEqual(evidence["actor"], "pod_guard")
                    self.assertEqual(evidence["pod_id"], "testpod")
                    self.assertEqual(evidence["delete_attempts"], 1)
                    self.assertFalse(evidence["delete_accepted"])
                    self.assertFalse(evidence["absence_verified"])
                    self.assertTrue(evidence["stop_attempted"])
                    self.assertEqual(evidence["stop_accepted"], stop_denied is None)
                    self.assertEqual([m for m, _ in api.calls].count("POST"), 1)
                    self.assertNotIn("PATCH", [m for m, _ in api.calls])
                    status_file = read(self.volume / "agent.json")
                    self.assertEqual(
                        (status_file["phase"], status_file["cleanup_state"]),
                        ("done", "auth_denied"),
                    )
                    self.assertEqual(status_file["pod_id"], "testpod")

    def test_get_denied_does_not_skip_permitted_self_delete(self):
        api = FakeApi(get_denied=403)
        agent.terminate(api, "testpod", self.job)
        evidence = self.evidence()
        self.assertEqual([m for m, _ in api.calls], ["GET", "DELETE", "GET", "GET"])
        self.assertTrue(evidence["delete_accepted"])
        self.assertEqual(evidence["delete_http_status"], 204)
        self.assertTrue(evidence["absence_verified"])
        self.assertFalse(evidence["stop_attempted"])

    def test_get_denied_after_accepted_delete_is_not_success(self):
        api = FakeApi(get_denied=403, deny_after_delete=True)
        with self.assertRaises(agent.SelfCleanupUnverified):
            agent.terminate(api, "testpod", self.job)
        evidence = self.evidence()
        self.assertEqual(evidence["state"], "verification_denied")
        self.assertEqual(evidence["delete_http_status"], 204)
        self.assertFalse(evidence["absence_verified"])
        self.assertFalse(evidence["stop_attempted"])
        self.assertEqual([m for m, _ in api.calls], ["GET", "DELETE", "GET"])

    def test_denied_get_and_delete_still_attempts_delete_before_stop(self):
        api = FakeApi(get_denied=401, delete_denied=403)
        with self.assertRaises(agent.SelfCleanupUnverified):
            agent.terminate(api, "testpod", self.job)
        self.assertEqual([m for m, _ in api.calls], ["GET", "DELETE", "POST"])
        self.assertEqual(self.evidence()["delete_attempts"], 1)

    def test_stop_dispatch_cutoff_keeps_terminal_diagnostic(self):
        class ContainerStopped(BaseException):
            pass

        api = FakeApi(delete_denied=403)
        original = api.request

        def request(method, path, body=None):
            if method == "POST":
                status = read(self.volume / "agent.json")
                self.assertEqual(
                    (status["phase"], status["pod_id"], status["cleanup_state"]),
                    ("done", "testpod", "auth_denied"),
                )
                self.assertTrue(self.evidence()["stop_attempted"])
                self.assertFalse(self.evidence()["stop_accepted"])
                raise ContainerStopped
            return original(method, path, body)

        api.request = request
        with self.assertRaises(ContainerStopped):
            agent.terminate(api, "testpod", self.job)
        self.assertFalse(self.evidence()["absence_verified"])

    def test_initial_absence_requires_explicit_accepted_self_delete(self):
        api = FakeApi(absent=True)
        agent.terminate(api, "testpod", self.job)
        evidence = self.evidence()
        self.assertTrue(evidence["absence_verified"])
        self.assertTrue(evidence["delete_accepted"])
        self.assertEqual(evidence["delete_attempts"], 1)
        self.assertEqual(evidence["delete_http_status"], 204)
        self.assertEqual(evidence["absence_basis"], "accepted_self_delete")

    def test_delete_attempt_is_durable_before_api_dispatch(self):
        api = FakeApi()
        original = api.request

        def request(method, path, body=None):
            if method == "DELETE":
                evidence = self.evidence()
                self.assertEqual(evidence["state"], "delete_attempted")
                self.assertEqual(evidence["request_outcome"], "unknown")
                self.assertFalse(evidence["delete_accepted"])
            return original(method, path, body)

        api.request = request
        agent.terminate(api, "testpod", self.job)

    def test_previous_get_metadata_cannot_prove_delete_status(self):
        class EndRetry(BaseException):
            pass

        api = FakeApi()
        original = api.request

        def request(method, path, body=None):
            before = api.last_response
            value = original(method, path, body)
            if method == "DELETE":
                api.last_response = before
            return value

        api.request = request
        with (
            patch("podvigil.podrt.agent.time.sleep", side_effect=EndRetry),
            self.assertRaises(EndRetry),
        ):
            agent.terminate(api, "testpod", self.job)
        self.assertFalse(self.evidence()["delete_accepted"])
        self.assertFalse(self.evidence()["absence_verified"])
        self.assertIsNone(self.evidence().get("delete_http_status"))

    def test_broken_evidence_storage_cannot_cancel_cleanup(self):
        api = FakeApi()
        with patch("podvigil.podrt.agent.reserved_save", side_effect=OSError(errno.ENOSPC, "full")):
            agent.terminate(api, "testpod", self.job)
        self.assertTrue(api.absent)

    def test_guard_gets_provider_clock_before_supervisor_spawn(self):
        api = FakeApi()
        with (
            patch("podvigil.podrt.agent.os.getpid", return_value=1),
            patch("podvigil.podrt.agent.sys.platform", "linux"),
            patch("podvigil.podrt.agent.drain_processes"),
            patch("podvigil.podrt.agent.isolation.lock_volume"),
            patch("podvigil.podrt.agent.isolation.protect_code"),
            patch("podvigil.podrt.agent.isolation.prepare_control"),
            patch(
                "podvigil.podrt.agent.subprocess.Popen", side_effect=OSError("spawn failed")
            ) as spawn,
        ):
            agent.guard(self.job, api, "testpod")
        spawn.assert_called_once()
        self.assertTrue(api.absent)
        beacon = read(self.volume / "control/identity.json")
        self.assertEqual((beacon["pod_id"], beacon["actor"]), ("testpod", "pod_guard"))

    def test_deadlines_use_provider_epoch_and_monotonic_not_local_wallclock(self):
        api = FakeApi()
        api.now = lambda: 1000
        api.response("GET", "/pods/testpod", 200)
        self.job.update(deadline_epoch=1060, startup_deadline_epoch=1030)
        with (
            patch("podvigil.podrt.agent.time.time", side_effect=AssertionError("local clock used")),
            patch("podvigil.podrt.agent.time.monotonic", return_value=100),
        ):
            self.assertEqual(agent.deadline_anchors(api, self.job), (160, 130, 158))

    def test_missing_provider_date_fails_closed(self):
        api = FakeApi()
        api.last_response = {"method": "GET", "status": 200, "response_date": None}
        with self.assertRaisesRegex(RuntimeError, "Date header"):
            agent.deadline_anchors(api, self.job)

    def test_nonfinite_provider_clock_fails_closed(self):
        api = FakeApi()
        api.now = lambda: float("nan")
        with self.assertRaisesRegex(RuntimeError, "clock unavailable"):
            agent.deadline_anchors(api, self.job)

    def test_workload_environment_allows_runtime_and_declared_controls_only(self):
        env = agent.child_environment(
            {
                "PATH": "/bin",
                "CUDA_VISIBLE_DEVICES": "0",
                "HOME": "/root",
                "HF_HOME": "/cache",
                "RUNPOD_API_KEY": "DUMMY",
                "WONDOMON_JOB_SPEC": "private",
                "AWS_ACCESS_KEY_ID": "DUMMY",
                "HF_TOKEN": "DUMMY",
                "COMPANY_AUTH": "DUMMY",
                "PYTHONPATH": "/bootstrap",
            },
            self.output,
            Path("/workspace/inputs"),
            {"MY_SEED": "123", "NCCL_DEBUG": "WARN"},
        )
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0")
        self.assertEqual(env["MY_SEED"], "123")
        self.assertEqual(env["PYTHONPATH"], str(Path("/workspace/inputs")))
        self.assertEqual(env["WONDOMON_INPUT_DIR"], str(Path("/workspace/inputs")))
        self.assertEqual(
            set(env),
            {
                "PATH",
                "CUDA_VISIBLE_DEVICES",
                "HOME",
                "HF_HOME",
                "MY_SEED",
                "NCCL_DEBUG",
                "PYTHONPATH",
                "WONDOMON_INPUT_DIR",
                "WONDOMON_OUTPUT_DIR",
            },
        )

    def test_declared_credentials_or_interpreter_injection_rejected(self):
        for name in (
            "RUNPOD_API_KEY",
            "WONDOMON_JOB_SPEC",
            "AWS_REGION",
            "HF_TOKEN",
            "MY_SECRET",
            "MY_PASSWORD",
            "PYTHONPATH",
            "LD_PRELOAD",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                agent.child_environment({}, self.output, declared={name: "dummy"})

    def test_outcome_is_committed_before_quiesce_and_inventory_failure(self):
        def quiesce():
            self.assertEqual(read(self.output / "result.json"), self.result)
            # The volume copy is already durable.
            self.assertEqual(read(self.volume / "artifacts/result.json"), self.result)

        with patch(
            "podvigil.podrt.agent.make_manifest", side_effect=PermissionError("scan denied")
        ):
            self.assertFalse(self.finalize(quiesce))
        self.assertEqual(read(self.output / "result.json"), self.result)
        marker = read(self.volume / "control/finalization-incomplete.json")
        self.assertFalse(marker["complete"])
        self.assertEqual(marker["stage"], "inventory_and_hashing")
        self.assertEqual(marker["error_type"], "PermissionError")
        # The raw outcome and log still reach the volume for partial recovery; no manifest does.
        self.assertEqual(marker["salvaged_files"], 2)
        self.assertEqual(read(self.volume / "artifacts/result.json"), self.result)
        self.assertTrue((self.volume / "artifacts/train.log").is_file())
        self.assertFalse((self.volume / "artifact-manifest.json").exists())

    def test_simulated_finalization_cutoff_preserves_outcome_and_pending_marker(self):
        class Cutoff(BaseException):
            pass

        with (
            patch("podvigil.podrt.agent.make_manifest", side_effect=Cutoff),
            self.assertRaises(Cutoff),
        ):
            self.finalize(lambda: None)
        self.assertEqual(read(self.output / "result.json"), self.result)
        self.assertEqual(read(self.volume / "artifacts/result.json"), self.result)
        marker = read(self.volume / "control/finalization-incomplete.json")
        self.assertFalse(marker["complete"])
        self.assertEqual(marker["stage"], "inventory_and_hashing")
        self.assertFalse((self.volume / "artifact-manifest.json").exists())

    def test_success_removes_incomplete_marker(self):
        self.assertTrue(self.finalize(lambda: None))
        self.assertFalse((self.volume / "control/finalization-incomplete.json").exists())
        self.assertTrue((self.folder / "artifact-manifest.json").exists())
        self.assertEqual(
            (self.volume / "artifact-manifest.json").read_bytes(),
            (self.folder / "artifact-manifest.json").read_bytes(),
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Requires Linux filesystem")
    def test_reserved_result_symlink_never_overwrites_target(self):
        target = self.folder / "precious"
        target.write_text("only copy")
        (self.output / "result.json").symlink_to(target)
        self.assertFalse(self.finalize(lambda: None))
        self.assertEqual(target.read_text(), "only copy")
        self.assertEqual(
            read(self.volume / "control/finalization-incomplete.json")["result"], self.result
        )
        self.assertFalse((self.folder / "artifact-manifest.json").exists())
        self.assertFalse((self.volume / "artifact-manifest.json").exists())
        # The published result contains the supervisor's own record.
        self.assertEqual(read(self.volume / "artifacts/result.json"), self.result)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Requires Linux filesystem")
    def test_colon_filename_preserves_success_outcome_in_bundle(self):
        (self.output / "epoch:1.bin").write_bytes(b"checkpoint")
        self.assertTrue(self.finalize(lambda: None))
        self.assertEqual(read(self.output / "result.json")["outcome"], "success")
        self.assertEqual(read(self.volume / "artifact-manifest.json")["version"], 2)
        self.assertEqual((self.volume / "artifacts/epoch:1.bin").read_bytes(), b"checkpoint")


class SelfDeleteHttpProofTests(unittest.TestCase):
    """Real Api/delete_verified with only HTTP transport and clock replaced."""

    def setUp(self):
        class UnexpectedRequest(BaseException):
            pass

        self.calls, self.records, self.sleeps = [], [], []
        self.tick = 0.0
        self.plan = []
        self.api = Api("DUMMY-self-delete-proof", "http://127.0.0.1:1").configure_pacing(
            max_wait=2, read_retries=2
        )
        self.job = job()
        # Expiry cannot excuse false absence.
        self.job.update(deadline_epoch=0, startup_deadline_epoch=0)
        actual_delete = agent.delete_verified

        def opened(req, timeout):
            self.calls.append((req.get_method(), req.full_url.rsplit("/v2", 1)[-1]))
            if not self.plan:
                raise UnexpectedRequest("Unexpected extra HTTP request")
            method, status, body = self.plan.pop(0)
            self.assertEqual(req.get_method(), method)
            headers = {"Date": "Tue, 10 Nov 2026 20:00:00 GMT"}
            if status >= 400:
                raise HTTPError(req.full_url, status, "dummy response", headers, io.BytesIO(b"{}"))
            response = io.BytesIO(json.dumps(body).encode() if body is not None else b"")
            response.status, response.headers = status, headers
            return response

        def record(job, pod, name, value, actor="pod_guard"):
            self.records.append(json.loads(json.dumps(value)))
            return True

        for mocked in (
            patch(
                "podvigil.podrt.api.urllib.request.build_opener",
                return_value=types.SimpleNamespace(open=opened),
            ),
            patch("socket.create_connection", side_effect=AssertionError("Network forbidden")),
            patch("podvigil.podrt.agent.signal_handlers", return_value={}),
            patch("podvigil.podrt.agent.restore_signals"),
            patch("podvigil.podrt.agent.agent_status"),
            patch("podvigil.podrt.agent.control_record", side_effect=record),
            patch.dict(os.environ, {"RUNPOD_POD_ID": "testpod"}),
            patch("podvigil.podrt.agent.time.monotonic", side_effect=lambda: self.tick),
            patch("podvigil.podrt.agent.time.sleep", side_effect=self.sleep),
            patch(
                "podvigil.podrt.agent.delete_verified",
                side_effect=lambda *a, **kw: actual_delete(*a, sleep=self.sleep, **kw),
            ),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.tick += seconds

    def test_first_http404_spends_next_cleanup_request_on_delete_then_fresh_reads(self):
        self.plan = [
            ("GET", 404, None),
            ("DELETE", 204, None),
            ("GET", 404, None),
            ("GET", 404, None),
        ]
        agent.terminate(self.api, "testpod", self.job)
        self.assertFalse(self.plan)
        self.assertEqual([m for m, _ in self.calls], ["GET", "DELETE", "GET", "GET"])
        self.assertEqual(self.sleeps, [1])
        evidence = self.records[-1]
        self.assertEqual(evidence["delete_http_status"], 204)
        self.assertEqual(evidence["absence_basis"], "accepted_self_delete")
        self.assertTrue(evidence["absence_verified"])
        pending = next(value for value in self.records if value["state"] == "absence_unproven")
        self.assertFalse(pending["absence_verified"])
        self.assertFalse(pending["delete_accepted"])

    def test_delete404_is_not_acceptance_then_running_pod_is_deleted(self):
        self.plan = [
            ("GET", 404, None),
            ("DELETE", 404, None),
            ("GET", 200, {"id": "testpod", "status": "RUNNING", "name": "renamed", "env": {}}),
            ("DELETE", 204, None),
            ("GET", 404, None),
            ("GET", 404, None),
        ]
        agent.terminate(self.api, "testpod", self.job)
        self.assertFalse(self.plan)
        self.assertEqual(self.records[-1]["delete_attempts"], 2)
        rejected = next(record for record in self.records if record["state"] == "delete_failed")
        self.assertEqual(rejected["last_http_status"], 404)
        self.assertFalse(rejected["absence_verified"] or rejected["delete_accepted"])
        self.assertEqual(self.sleeps, [1, 1])

    def test_bootstrap_http404_cannot_return_guard_with_zero_deletes(self):
        self.plan = [
            ("GET", 404, None),
            ("GET", 404, None),
            ("DELETE", 204, None),
            ("GET", 404, None),
            ("GET", 404, None),
        ]
        with (
            patch("podvigil.podrt.agent.os.getpid", return_value=1),
            patch("podvigil.podrt.agent.sys.platform", "linux"),
            patch("podvigil.podrt.agent.drain_processes"),
            patch("podvigil.podrt.agent.isolation.protect_code"),
            patch("podvigil.podrt.agent.isolation.lock_volume"),
            patch("podvigil.podrt.agent.isolation.prepare_control"),
            patch("podvigil.podrt.agent.subprocess.Popen") as spawn,
        ):
            agent.guard(self.job, self.api, "testpod")
        spawn.assert_not_called()
        self.assertFalse(self.plan)
        self.assertEqual(self.records[-1]["delete_http_status"], 204)
        self.assertTrue(self.records[-1]["absence_verified"])

    def test_persistent404_never_claims_absence_even_after_job_deadline(self):
        class StopReview(BaseException):
            pass

        self.plan = [("GET", 404, None), ("DELETE", 404, None)] * 3

        def bounded_sleep(seconds):
            if seconds == 15:
                raise StopReview
            self.sleep(seconds)

        with (
            patch("podvigil.podrt.agent.time.sleep", side_effect=bounded_sleep),
            self.assertRaises(StopReview),
        ):
            agent.terminate(self.api, "testpod", self.job)
        self.assertFalse(self.plan)
        self.assertFalse(any(record["absence_verified"] for record in self.records))
        self.assertFalse(any(record["delete_accepted"] for record in self.records))
        self.assertEqual([m for m, _ in self.calls].count("DELETE"), 3)

    def test_owned_terminated_with_separated_reads_and_inventory_needs_no_delete(self):
        tombstone = {"id": "testpod", "status": "TERMINATED"}
        self.plan = [
            ("GET", 200, tombstone),
            ("GET", 200, tombstone),
            ("GET", 200, {"pods": [], "pagination": {"hasNextPage": False, "nextCursor": None}}),
        ]
        agent.terminate(self.api, "testpod", self.job)
        self.assertFalse(self.plan)
        evidence = self.records[-1]
        self.assertEqual(evidence["delete_attempts"], 0)
        self.assertFalse(evidence["delete_accepted"])
        self.assertEqual(evidence["absence_basis"], "owned_terminated")
        self.assertTrue(evidence["verification"]["proof"]["inventory"]["complete"])
        self.assertEqual(self.sleeps, [1])

    def test_http403_get_does_not_prevent_permitted_self_delete(self):
        self.plan = [
            ("GET", 403, None),
            ("DELETE", 204, None),
            ("GET", 404, None),
            ("GET", 404, None),
        ]
        agent.terminate(self.api, "testpod", self.job)
        self.assertFalse(self.plan)
        self.assertTrue(self.records[-1]["delete_accepted"])
        self.assertFalse(self.records[-1]["stop_attempted"])


if __name__ == "__main__":
    unittest.main()
