"""Golden ledger: the fake cloud end to end, through the command line, with what it leaves behind pinned.

Every restructuring step must leave this table unchanged, unless changing it is the step's purpose
(adding the version to job.json, for example); the change then shows here and nowhere else.

Each scenario runs the command line (run_cli) on a job file. The Runpod API and S3 are fakes in this
process; the watchdog is the real watchdog loop in a thread, as the detached watchdog runs it. For every
ledger the test records the files it holds and the states written in them, with the parts that change
from run to run (job IDs, times, temporary folder names) normalised. A second check applies to every
ledger: none may contradict its own summary.
"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import re
import tempfile
import threading
import time
import unittest

from podvigil.storage import Storage
from podvigil.podrt.records import read, save
from tests.support import runner as run
from tests.support.runner import patch_run
from tests.support.fakes import NO_CAPACITY_DETAIL, FakeApi, FakeS3
from tests.support.paths import ROOT
from podvigil.settings import ENV_PREFIX

KEY = "dummy-golden-account-key"
FIRST, SECOND = "NVIDIA GeForce RTX 3090", "NVIDIA RTX PRO 4500 Blackwell"
JOB_ID = re.compile(r"[0-9a-f]{32}")
TEMPORARY = re.compile(r"(volume-audit-|partial-export-)[A-Za-z0-9_]+")


def normalise(text):
    return TEMPORARY.sub(r"\1*", JOB_ID.sub("<job>", text))


class Cloud:
    """A fake API and S3, linked so that deleting a volume also deletes its bucket, and the launch patches."""

    def __init__(self, root):
        self.api = FakeApi()
        self.s3 = FakeS3(api=self.api)
        self.storage = Storage("EU-RO-1", self.s3)
        self.api.storage = self.storage
        self.jobs = root / "ledgers"
        self.threads = []
        self.after_pod_post = None
        original = self.api.request

        def request(method, path, body=None):
            value = original(method, path, body)
            if method == "POST" and path == "/pods" and self.after_pod_post:
                self.after_pod_post(body)
            return value

        self.api.request = request

    def spawn(self, folder, **_):
        # The fake reuses the same volume ID, so the next attempt waits for the previous
        # watchdog to exit.
        for thread in self.threads:
            thread.join(10)
        thread = threading.Thread(
            target=run.watchdog,
            args=(folder, self.api, self.storage),
            kwargs={"pause": lambda _: time.sleep(0.05)},
            daemon=True,
        )
        thread.start()
        self.threads.append(thread)

        class Monitor:
            def poll(self):
                return None if thread.is_alive() else 0

        return Monitor()

    def patches(self):
        return [
            patch_run("Api", return_value=self.api),
            patch_run("Storage", return_value=self.storage),
            patch_run("load_key", return_value=KEY),
            patch_run("spawn_watchdog", side_effect=self.spawn),
            patch_run("jobs_dir", return_value=self.jobs),
        ]

    def cli(self, *arguments):
        """The exit code and the lines for people (standard error); the result JSON is kept in self.stdout."""
        output, people = io.StringIO(), io.StringIO()
        patches = self.patches()
        for item in patches:
            item.start()
        try:
            with redirect_stdout(output), redirect_stderr(people):
                code = run.run_cli(list(arguments))
        finally:
            for item in reversed(patches):
                item.stop()
        for thread in self.threads:
            thread.join(20)
            if thread.is_alive():
                raise AssertionError("A watchdog thread did not finish.")
        self.stdout = output.getvalue()
        return code, people.getvalue().splitlines()

    def ledgers(self):
        if not self.jobs.exists():
            return []
        return sorted(
            self.jobs.iterdir(), key=lambda folder: read(folder / "job.json")["created_utc"]
        )

    def calls(self):
        counted = {}
        for method, path in self.api.calls:
            if method in ("POST", "DELETE"):
                key = (
                    method + " " + re.sub(r"/[^/]+$", "/<id>", path)
                    if method == "DELETE"
                    else method + " " + path
                )
                counted[key] = counted.get(key, 0) + 1
        return counted


def self_delete(cloud, start):
    """What the Pod guard leaves when it deletes its own Pod: accepted DELETE evidence, then the Pod is gone."""
    cloud.storage.put_json(
        "testvolume",
        "jobs/" + start["job_id"] + "/control/self-delete.json",
        {
            "job_id": start["job_id"],
            "pod_id": "testpod",
            "delete_accepted": True,
            "delete_http_status": 204,
        },
    )
    cloud.api.pods.clear()


def fields(name, value):
    """The states a ledger file records, without times, absolute paths or IDs that change between runs."""
    if not isinstance(value, dict):
        return type(value).__name__
    pick = lambda *names: {key: value.get(key) for key in names if key in value}
    if name == "job.json":
        return {
            "keys": sorted(value),
            "config_keys": sorted(value["config"]),
            "inputs": [item["remote"] for item in value["inputs"]],
        }
    if name in ("pod-creation-state.json", "volume-creation-state.json"):
        return value["state"]
    if name in ("pod-receipt.json", "volume-receipt.json"):
        # Omit established_by. A watchdog reading the ledger while the controller writes the
        # receipt may bind the same Pod again from the listing ("reconciled"). Both receipts
        # carry the same authority.
        return pick("id", "kind")
    if name == "pod-rejected.json":
        return pick("kind", "status", "not_sent")
    if name in ("pod-deleted.json", "volume-deleted.json"):
        evidence = value.get("evidence") or {}
        return {
            "id": value.get("id"),
            "verified": evidence.get("verified"),
            "observed_absent": evidence.get("observed_absent"),
            "delete_attempts": len(evidence.get("delete_attempts") or []),
        }
    if name == "volume-audit.json":
        return {
            "mode": value["mode"],
            "objects": sorted(
                normalise(item["key"]) + " " + item["kind"] for item in value["objects"]
            ),
        }
    if name == "recovery-checkpoint.json":
        return {
            **pick("version", "pod_id", "volume_id", "training_outcome"),
            "files": sorted(normalise(path) for path in value["files"]),
        }
    if name == "summary.json":
        return {
            "keys": sorted(value),
            **pick(
                "pod_created",
                "pod_termination_verified",
                "volume_deletion_verified",
                "training_outcome",
                "backup_not_required",
            ),
        }
    if name == "launch-failure.json":
        return {
            **pick("phase", "error_type", "http_status", "cleanup_error"),
            "message": normalise(value.get("message", ""))[:60],
        }
    if name == "watchdog.json":
        # Without a Pod, the reason comes from the stop request the watchdog last read.
        # Both the controller and recovery write one, so timing determines which it sees.
        return pick("state", "reason") if value.get("state") == "pod_deleted" else pick("state")
    if name == "recovery-handoff.json":
        return pick("state")
    if name == "stop-request.json":
        return value.get("reason")
    if name == "terminal-observed.json":
        return pick("source", "ownership_checked")
    if name == "pod-cleanup-attempt.json":
        return pick("outcome", "phase", "verified")
    if name == "downloads/backup-verified.json":
        return pick("file_count")
    if name == "downloads/artifact-manifest.json":
        return sorted(item["path"] for item in value["files"])
    if name == "downloads/artifacts/result.json":
        return pick("outcome")
    # Record only the presence of heartbeats, intents, dispatch markers and audit copies.
    return None


def observe(folder):
    files, states = [], {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(folder).as_posix()
        if name == "beacon-observation.json":
            # A watchdog can observe the bucket just as the controller deletes it.
            # Validate this optional diagnostic without pinning its timing-dependent presence.
            value = read(path)
            if (
                not isinstance(value, dict)
                or set(value) != {"state", "error_type", "utc"}
                or value["state"] != "unknown"
                or not all(
                    isinstance(value[key], str) and value[key] for key in ("error_type", "utc")
                )
            ):
                raise AssertionError("Invalid optional beacon diagnostic")
            continue
        files.append(normalise(name))
        if name.endswith(".json") and not name.startswith("downloads/volume-audit-"):
            value = fields(name, read(path))
            if value is not None:
                states[normalise(name)] = value
    return {"files": files, "states": states}


def contradictions(folder):
    """Ways a ledger can contradict its own summary. The first paid use found the watchdog writing
    storage_retained after a verified deletion; nothing of that kind may come back."""
    summary = read(folder / "summary.json")
    if not summary:
        return []
    found = []
    pod, volume = read(folder / "pod-deleted.json", {}), read(folder / "volume-deleted.json", {})
    if summary.get("volume_deletion_verified") and not run.deletion_verified(
        volume, volume.get("id")
    ):
        found.append("summary says the volume deletion is verified; volume-deleted.json does not")
    if (
        summary.get("pod_created", True)
        and summary.get("pod_termination_verified")
        and (
            (folder / "pod-receipt.json").exists() and not run.deletion_verified(pod, pod.get("id"))
        )
    ):
        found.append("summary says the Pod termination is verified; pod-deleted.json does not")
    for name in ("watchdog.json", "recovery-handoff.json"):
        if (read(folder / name, {}) or {}).get("state") == "storage_retained":
            found.append(name + " says storage_retained after a completed summary")
    lines = run.state_lines(folder)
    if summary.get("volume_deletion_verified") and not lines[-1].endswith(
        "Nothing from this job remains."
    ):
        found.append("the final state lines do not say that nothing remains")
    return found


def job_file(root, **changes):
    config = json.loads((ROOT / "jobs" / "smoke.example.json").read_text(encoding="utf-8"))
    data = root / "input-data"
    data.mkdir(exist_ok=True)
    (data / "table.csv").write_bytes(b"a,b\n1,2\n")
    config.update(
        poll_seconds=2,
        controller_stale_seconds=12,
        agent_stale_seconds=12,
        self_termination_probe=False,
        inputs=[{"local": str(data / "table.csv"), "remote": "data/table.csv"}],
        **changes,
    )
    path = root / "job.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def scenario(name):
    """Run one scenario in a fresh folder; return what it left behind."""
    with tempfile.TemporaryDirectory(prefix="golden-ledger-") as temporary:
        root = Path(temporary)
        cloud = Cloud(root)
        if name in ("success_self_deleted", "workload_failed", "fallback_to_second_candidate"):
            outcome = "training_failed" if name == "workload_failed" else "success"

            def finish(start):
                cloud.s3.finished(start["job_id"], outcome)
                self_delete(cloud, start)

            cloud.s3.on_start = finish
        if name == "success_stopped_by_watchdog":
            cloud.s3.on_start = lambda start: cloud.s3.finished(start["job_id"])
        if name == "no_capacity_at_create":
            cloud.api.pod_rejection = (400, NO_CAPACITY_DETAIL)
        if name == "no_capacity_in_catalog":
            cloud.api.gpu_catalog["NVIDIA GeForce RTX 4090"] = {
                "secure": True,
                "price": {"secure": 0.5},
                "dataCenters": [],
            }
        if name == "unreleased_pod":

            def guard_stopped(body):
                job_id = body["env"][ENV_PREFIX + "_JOB_ID"]
                prefix = "jobs/" + job_id + "/"
                cloud.storage.put_json(
                    "testvolume",
                    prefix + "agent.json",
                    {"job_id": job_id, "pod_id": "testpod", "actor": "pod_guard", "phase": "done"},
                )
                cloud.storage.put_json(
                    "testvolume",
                    prefix + "control/guard-failure.json",
                    {
                        "error_type": "RuntimeError",
                        "message": "start-up",
                        "job_id": job_id,
                        "pod_id": "testpod",
                    },
                )
                self_delete(cloud, {"job_id": job_id})

            cloud.after_pod_post = guard_stopped
        changes = {}
        if name == "fallback_to_second_candidate":
            cloud.api.pod_rejection = lambda body: (
                (400, NO_CAPACITY_DETAIL) if body["gpu"]["id"] == FIRST else None
            )
            changes = {
                "candidates": [
                    {"gpu_id": FIRST, "data_center_id": "EU-RO-1"},
                    {"gpu_id": SECOND, "data_center_id": "EU-RO-1"},
                ]
            }
        path = job_file(root, **changes)
        if changes:
            config = json.loads(path.read_text(encoding="utf-8"))
            del config["gpu_id"], config["data_center_id"]
            path.write_text(json.dumps(config), encoding="utf-8")
        code, lines = cloud.cli("launch", str(path))
        ledgers = cloud.ledgers()
        result = {
            "exit_code": code,
            "outcome": json.loads(cloud.stdout)["outcome"],
            "last_lines": [
                normalise(line.replace(str(folder), "<ledger>"))
                for line in lines[-3:]
                for folder in (ledgers[-1] if ledgers else root,)
            ],
            "requests": cloud.calls(),
            "remaining": {"pods": len(cloud.api.pods), "volumes": len(cloud.api.volumes)},
            "ledgers": [observe(folder) for folder in ledgers],
            "contradictions": [item for folder in ledgers for item in contradictions(folder)],
        }
        return result


SCENARIOS = (
    "success_self_deleted",
    "success_stopped_by_watchdog",
    "workload_failed",
    "no_capacity_at_create",
    "fallback_to_second_candidate",
    "unreleased_pod",
    "no_capacity_in_catalog",
)

# Recorded from baseline commit c63c17d and confirmed stable over repeated runs on Windows
# and Linux.
# Read the module docstring before changing anything in it.
GOLDEN_FILE = Path(__file__).with_name("golden_ledger.json")


class GoldenLedgerTests(unittest.TestCase):
    maxDiff = None

    def test_optional_beacon_diagnostic_preserves_the_lifecycle_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="podvigil-golden-observation-") as tmp:
            folder = Path(tmp)
            save(folder / "summary.json", {"training_outcome": "success"})
            expected = observe(folder)
            observation = {
                "state": "unknown",
                "error_type": "NoSuchBucket",
                "utc": "2026-01-01T00:00:00+00:00",
            }
            save(folder / "beacon-observation.json", observation)
            self.assertEqual(observe(folder), expected)
            save(folder / "beacon-observation.json", {**observation, "state": "verified"})
            with self.assertRaisesRegex(AssertionError, "Invalid optional beacon diagnostic"):
                observe(folder)

    def test_every_scenario_leaves_the_pinned_ledger(self):
        golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(golden), sorted(SCENARIOS))
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                observed = scenario(name)
                self.assertEqual(observed["contradictions"], [])
                self.assertEqual(observed["remaining"], {"pods": 0, "volumes": 0})
                self.assertEqual(observed, golden[name])


if __name__ == "__main__":
    import sys

    print(
        json.dumps(
            {name: scenario(name) for name in (sys.argv[1:] or SCENARIOS)}, indent=1, sort_keys=True
        )
    )
