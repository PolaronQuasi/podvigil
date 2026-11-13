"""A controller-side job record and the ledger files a launched job has. Moved here from test_safety."""

import json
import time

from podvigil.podrt.records import save
from podvigil.jobspec import validate
from tests.support.paths import ROOT


def fixture():
    cfg = validate(json.loads((ROOT / "jobs/smoke.example.json").read_text()))
    cfg.update(
        poll_seconds=2,
        controller_stale_seconds=12,
        agent_stale_seconds=12,
        self_termination_probe=False,
    )
    return {
        "job_id": "a" * 32,
        "pod_name": "wondomon-" + "a" * 32,
        "volume_name": "wondomon-results-" + "a" * 32,
        "config": cfg,
        "prior_pod_ids": [],
        "prior_volume_ids": [],
        "inputs": [],
        "volume_id": "testvolume",
        "deadline_epoch": time.time() + 600,
        "startup_deadline_epoch": time.time() + 240,
    }


def ledger(folder, job, stale=False):
    save(folder / "job.json", job)
    save(
        folder / "pod-intent.json",
        {k: job[k] for k in ("deadline_epoch", "startup_deadline_epoch")},
    )
    save(folder / "pod-dispatch.json", {"state": "dispatching"})
    save(folder / "volume-intent.json", {"name": job["volume_name"]})
    save(folder / "pod-receipt.json", {"id": "testpod"})
    save(folder / "volume-receipt.json", {"id": "testvolume"})
    save(folder / "controller-heartbeat.json", {"epoch": time.time() - (100 if stale else 0)})
