"""The input list as a hashed manifest on the volume, not in the Pod request (item 7): no more input cap.

Found in the first paid use (2026-11-12): the input list travelled in the Pod request, which Runpod caps, so a
job could list only about 235 inputs. Now launch records the manifest's exact size and SHA-256 in job.json, writes
jobs/<id>/control/inputs.json to the volume and reads it back; the request carries only the size, SHA-256 and
count, and the Pod checks the file against them before it trusts the list. The delete audit checks the manifest
byte for byte, so it never keeps a volume for it and never deletes one whose manifest changed.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import base64
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest

from podvigil.controller import request
from podvigil.podrt import agent
from podvigil.podrt.records import read
from podvigil.settings import ENV_PREFIX
from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.fakes import FakeApi, FakeS3
from tests.support.jobs import fixture
from tests.support.paths import ROOT
from tests.support.runner import patch_run


class LaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="podvigil-manifest-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.api = FakeApi()
        self.s3 = FakeS3(api=self.api)
        self.storage = Storage("EU-RO-1", self.s3)
        self.api.storage = self.storage
        self.bodies = []
        original = self.api.request

        def request(method, path, body=None):
            if (method, path) == ("POST", "/pods"):
                self.bodies.append(body)
            return original(method, path, body)

        self.api.request = request

    def job_file(self, count):
        data = self.root / "data"
        data.mkdir()
        for index in range(count):
            (data / f"part-{index:04d}.txt").write_bytes(b"%d\n" % index)
        config = json.loads((ROOT / "jobs" / "smoke.example.json").read_text(encoding="utf-8"))
        config.update(
            self_termination_probe=False,
            poll_seconds=2,
            controller_stale_seconds=12,
            agent_stale_seconds=12,
            inputs=[{"directory": "data", "remote": "data"}],
        )
        path = self.root / "job.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def spawn(self, folder, **_):
        """The real watchdog loop in a thread, as the detached watchdog runs it: it ends the finished job."""
        thread = threading.Thread(
            target=run.watchdog,
            args=(folder, self.api, self.storage),
            kwargs={"pause": lambda _: time.sleep(0.05)},
            daemon=True,
        )
        thread.start()
        self.threads.append(thread)

        class Monitor:
            pid = 4242

            def poll(self):
                return None if thread.is_alive() else 0

        return Monitor()

    def launch(self, path):
        self.s3.on_start = lambda start: self.s3.finished(start["job_id"])
        self.threads = []
        out = io.StringIO()
        with (
            patch_run("Api", return_value=self.api),
            patch_run("Storage", return_value=self.storage),
            patch_run("load_key", return_value="dummy-manifest-key"),
            patch_run("spawn_watchdog", side_effect=self.spawn),
            patch_run("jobs_dir", return_value=self.root / "jobs"),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            code = run.run_cli(["launch", str(path)])
        for thread in self.threads:
            thread.join(30)
            self.assertFalse(thread.is_alive(), "the watchdog did not finish")
        return code, json.loads(out.getvalue())

    def test_six_hundred_inputs_run_and_the_request_carries_only_the_manifest_hash(self):
        code, result = self.launch(self.job_file(600))
        self.assertEqual((code, result["outcome"]), (0, "success"), result)
        folder = next((self.root / "jobs").iterdir())
        job = read(folder / "job.json")
        record = job["input_manifest"]
        self.assertEqual(record["key"], "jobs/" + job["job_id"] + "/control/inputs.json")
        self.assertEqual(record["count"], 600)
        # The Pod request: the manifest's size, SHA-256 and count, and no input list.
        spec = json.loads(base64.b64decode(self.bodies[0]["env"][ENV_PREFIX + "_JOB_SPEC"]))
        self.assertNotIn("inputs", spec)
        self.assertEqual(
            spec["input_manifest"], {name: record[name] for name in ("size", "sha256", "count")}
        )
        # The volume held exactly those bytes; the audit checked them and the volume is gone.
        audit = read(folder / "volume-audit.json")
        entry = next(item for item in audit["objects"] if item["key"] == record["key"])
        self.assertEqual(
            (entry["kind"], entry["size"], entry["sha256"]),
            ("input_manifest", record["size"], record["sha256"]),
        )
        self.assertFalse(self.api.volumes)
        self.assertLess(len(json.dumps(self.bodies[0]).encode()), request.POD_REQUEST_LIMIT)

    def test_the_request_size_no_longer_depends_on_the_inputs(self):
        cfg = fixture()["config"]
        few = request.request_budget(cfg, [])["bytes"]
        many = request.request_budget(
            cfg, [{"remote": "d/%06d" % index, "size": 1} for index in range(5000)]
        )["bytes"]
        self.assertEqual(few, many)
        job = fixture()
        small = len(json.dumps(run.payload(job, "testvolume")).encode())
        job["inputs"] = [
            {"remote": "data/" + "x" * 100 + str(index), "size": 10**9, "sha256": "f" * 64}
            for index in range(5000)
        ]
        large = len(json.dumps(run.payload(job, "testvolume")).encode())
        self.assertLess(abs(large - small), 40)  # only the digits of the size and count differ


class AuditTests(unittest.TestCase):
    """The manifest is checked byte for byte in every audit mode."""

    def setUp(self):
        self.job = fixture()
        raw, record = request.input_manifest(self.job["job_id"], [])
        self.raw = raw
        self.job["input_manifest"] = {
            **record,
            "key": "jobs/" + self.job["job_id"] + "/control/inputs.json",
        }
        self.s3 = FakeS3()
        self.s3.objects[("testvolume", "managed-owner.json")] = json.dumps(
            {"job_id": self.job["job_id"]}
        ).encode()
        self.storage = Storage("EU-RO-1", self.s3)

    def test_the_exact_manifest_passes_the_pristine_audit(self):
        self.s3.objects[("testvolume", self.job["input_manifest"]["key"])] = self.raw
        audit = self.storage.audit_for_delete(self.job, "testvolume", allow_pristine_inputs=True)
        self.assertIn("input_manifest", [item["kind"] for item in audit["objects"]])

    def test_a_changed_manifest_keeps_the_volume(self):
        self.s3.objects[("testvolume", self.job["input_manifest"]["key"])] = self.raw.replace(
            b"[]", b"[ ]"
        )
        with self.assertRaisesRegex(RuntimeError, "Keep the volume"):
            self.storage.audit_for_delete(self.job, "testvolume", allow_pristine_inputs=True)

    def test_a_ledger_from_before_the_manifest_audits_as_before(self):
        del self.job["input_manifest"]
        self.assertEqual(
            self.storage.audit_for_delete(self.job, "testvolume", allow_pristine_inputs=True)[
                "object_count"
            ],
            1,
        )
        self.s3.objects[("testvolume", "jobs/" + self.job["job_id"] + "/control/inputs.json")] = (
            self.raw
        )
        with self.assertRaisesRegex(RuntimeError, "Unexpected volume objects"):
            self.storage.audit_for_delete(self.job, "testvolume", allow_pristine_inputs=True)

    def test_a_malformed_record_is_refused(self):
        self.job["input_manifest"]["key"] = "jobs/other/control/inputs.json"
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.storage.audit_for_delete(self.job, "testvolume", allow_pristine_inputs=True)


class PodSideTests(unittest.TestCase):
    """podrt.agent.input_list: the Pod checks the file against the request before it trusts the list."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pod-manifest-")
        self.addCleanup(temporary.cleanup)
        self.volume = Path(temporary.name) / "jobs" / ("b" * 32)
        (self.volume / "control").mkdir(parents=True)
        self.inputs = [
            {"remote": "lib.py", "size": 10, "sha256": hashlib.sha256(b"VALUE = 1\n").hexdigest()}
        ]
        self.job = {"job_id": "b" * 32}

    def write(self, inputs=None, job_id=None):
        raw, record = request.input_manifest(
            job_id or self.job["job_id"], self.inputs if inputs is None else inputs
        )
        (self.volume / "control" / "inputs.json").write_bytes(raw)
        self.job["input_manifest"] = record
        return raw

    def test_the_names_agree(self):
        self.assertEqual(agent.INPUT_MANIFEST, request.INPUT_MANIFEST)

    def test_a_checked_manifest_gives_the_list(self):
        self.write()
        self.assertEqual(agent.input_list(self.job, self.volume), self.inputs)

    def test_anything_else_is_refused(self):
        cases = {
            "changed bytes": lambda: (self.volume / "control" / "inputs.json").write_bytes(
                self.write().replace(b"lib.py", b"lib.px")
            ),
            "another job": lambda: self.write(job_id="c" * 32),  # its hash is right, its job is not
            "missing": lambda: (self.write(), (self.volume / "control" / "inputs.json").unlink()),
        }
        for name, prepare in cases.items():
            with self.subTest(case=name):
                prepare()
                with self.assertRaises((RuntimeError, OSError)):
                    agent.input_list(self.job, self.volume)
        self.write()
        self.job["input_manifest"]["count"] = 2
        with self.assertRaisesRegex(RuntimeError, "does not belong"):
            agent.input_list(self.job, self.volume)
        self.write(inputs=[{"remote": "lib.py", "size": -1, "sha256": "0" * 64}])
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            agent.input_list(self.job, self.volume)

    def test_a_spec_that_carries_the_list_itself_is_used_as_it_is(self):
        self.assertEqual(
            agent.input_list({"job_id": "b" * 32, "inputs": self.inputs}, self.volume), self.inputs
        )


if __name__ == "__main__":
    unittest.main()
