"""The network volume as a test folder: what the controller puts there, and the controller's view of it.

Moved here from test_pod_volume; the unit and the container tests of the Pod volume both use it.
"""

from pathlib import Path
import hashlib
import json
import shutil
import tempfile

from podvigil.storage import Storage
from podvigil.podrt.records import read
from tests.support.fakes import FakeS3

# Written by the container fake.
FAKE_MARKERS = {
    "deleted",
    "delete-events.jsonl",
    "api-events.jsonl",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ledger_inputs(inputs):
    return [
        {"remote": name, "size": len(data), "sha256": sha(data)} for name, data in inputs.items()
    ]


def with_input_manifest(job_value, inputs):
    """job_value as today's controller gives it to the Pod: the input manifest's record instead of the list.
    Returns the manifest's bytes, which fill_volume puts on the volume."""
    from podvigil.controller.request import input_manifest

    raw, record = input_manifest(job_value["job_id"], ledger_inputs(inputs))
    job_value.pop("inputs", None)
    job_value["input_manifest"] = record
    return raw


def fill_volume(volume, job_value, inputs=None, release=True, manifest=None):
    """Put on the volume what the controller puts there over S3 before and at the release: the owner
    record, the inputs, the input manifest (its bytes, from with_input_manifest) and the release marker."""

    def put(key, data):
        (volume / key).parent.mkdir(parents=True, exist_ok=True)
        (volume / key).write_bytes(data)

    put("managed-owner.json", json.dumps({"job_id": job_value["job_id"]}).encode())
    for name, data in (inputs or {}).items():
        put("inputs/" + name, data)
    if manifest is not None:
        put("jobs/" + job_value["job_id"] + "/control/inputs.json", manifest)
    if release:
        put(
            "jobs/" + job_value["job_id"] + "/start.json",
            json.dumps({"job_id": job_value["job_id"], "pod_id": "testpod"}).encode(),
        )


def s3_view(volume):
    """The volume as the controller lists it over S3: regular files under their relative keys."""
    s3 = FakeS3()
    for path in sorted(Path(volume).rglob("*")):
        key = path.relative_to(volume).as_posix()
        if path.is_file() and not path.is_symlink() and key not in FAKE_MARKERS:
            s3.objects[("testvolume", key)] = path.read_bytes()
    return s3


def controller_accepts(test, volume, job_value, inputs=None, audit=True):
    """The controller's own collect and (by default) full-volume delete audit pass on this volume."""
    ledger = {
        "job_id": job_value["job_id"],
        "volume_id": "testvolume",
        "inputs": ledger_inputs(inputs or {}),
        "config": {
            "required_artifacts": job_value["config"].get("required_artifacts", []),
            "inputs": [],
        },
    }
    if "input_manifest" in job_value:  # as launch records it in job.json
        ledger["input_manifest"] = {
            **job_value["input_manifest"],
            "key": "jobs/" + job_value["job_id"] + "/control/inputs.json",
        }
    storage = Storage("EU-RO-1", s3_view(volume))
    backup = Path(tempfile.mkdtemp(prefix="pod-volume-backup-"))
    test.addCleanup(shutil.rmtree, backup, True)
    manifest = storage.collect(ledger, "testvolume", backup / "downloads")
    if audit:
        test.assertEqual(
            storage.audit_for_delete(ledger, "testvolume", backup / "downloads")["mode"],
            "verified_backup",
        )
    return manifest, read(backup / "downloads" / "artifacts" / "result.json")


def keys(folder):
    return {
        path.relative_to(folder).as_posix() for path in Path(folder).rglob("*") if path.is_file()
    }
