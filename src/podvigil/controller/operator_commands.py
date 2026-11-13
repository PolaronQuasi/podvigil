"""Operator commands for an existing ledger: reattach, resolve a creation, hand off, resume, cancel."""

import math
import threading
import time
import uuid

from ..credentials import account_api
from ..podrt.records import exclusive_lock, read, save, utc
from ..status import resource_state
from ..storage import Storage
from .creation import record_stop, request_cancel, stop_requested
from .creation_recovery import (
    active_handoff,
    reconcile_from_audit,
    record_handoff,
    resource_id,
    validate_handoff_note,
)
from .launch import heartbeat_worker, supervise
from .ledger import ledger_folder, status_read
from .ownership import creation_receipt, owned_pod, owned_volume
from .ownership import locate
from .recovery import recover
from .watchdog import cancel_grace_seconds, spawn_watchdog


def reattach(folder, api=None, storage=None):
    """Restore supervision without changing the original compute deadline."""
    folder = ledger_folder(folder)
    job = read(folder / "job.json")
    api = api or account_api(job)
    storage = storage or Storage(job["config"]["data_center_id"])
    pod_id, volume_id = locate(api, job, folder, "pod"), locate(api, job, folder, "volume")
    if not pod_id or not volume_id or "deadline_epoch" not in job:
        raise RuntimeError(
            "Reattach requires reconciled Pod/volume receipts and the original deadline."
        )
    api.get_or_none("/pods/" + pod_id)  # synchronise provider time, even on an absent response
    stop_requested(folder)
    stop = threading.Event()
    save(folder / "controller-heartbeat.json", {"epoch": time.time()})
    worker = threading.Thread(target=heartbeat_worker, args=(folder, stop), daemon=True)
    worker.start()
    try:
        try:
            with exclusive_lock(folder / "watchdog.lock", timeout=0.2):
                pass
        except TimeoutError:
            monitor = None  # the existing single-instance watchdog still holds its OS lock
        else:
            # Existing resources need a guard even if its credentials differ.
            # The difference is recorded and shown.
            monitor = spawn_watchdog(folder, require_same_credentials=False)
        supervise(folder, job, api, storage, monitor, pod_id, volume_id)
        return recover(folder, api, storage)
    finally:
        stop.set()
        worker.join(timeout=2)


def resolve_creation(folder, pod_id=None, api=None, *, volume_id=None, audit=False):
    """Bind a visible owned resource, or positive authenticated create evidence."""
    folder = ledger_folder(folder)
    job = read(folder / "job.json")
    if not job:
        raise ValueError("Job ownership ledger not found.")
    if sum((pod_id is not None, volume_id is not None, bool(audit))) != 1:
        raise ValueError("Select exactly one Pod ID, volume ID, or audit reconciliation.")
    api = api or account_api(job)
    with exclusive_lock(folder / "control.lock", timeout=120):
        if audit:
            return {
                "job_id": job["job_id"],
                "identities": {
                    kind: reconcile_from_audit(api, job, folder, kind) for kind in ("pod", "volume")
                },
                "cleanup_verified": False,
                "next": "reattach or recover using this ledger",
            }
        kind = "pod" if pod_id is not None else "volume"
        rid = resource_id(pod_id if kind == "pod" else volume_id)
        existing = status_read(folder / (kind + "-receipt.json"))
        if existing and existing.get("id") != rid:
            raise RuntimeError(
                "Refusing to replace a different resource identity already bound to this job."
            )
        if not any(
            (folder / (kind + suffix)).exists() for suffix in ("-intent.json", "-dispatch.json")
        ):
            raise RuntimeError("No matching creation intent exists in this ledger.")
        resource = api.get_or_none(("/pods/" if kind == "pod" else "/network-volumes/") + rid)
        matches = owned_pod if kind == "pod" else owned_volume
        if resource is None or not matches(resource, job):
            raise RuntimeError(
                "The supplied resource ID cannot be verified as belonging to this job."
            )
        save(folder / (kind + "-receipt.json"), creation_receipt(resource, job, kind, "reconciled"))
        return {
            kind + "_id": rid,
            "resolved": True,
            "cleanup_verified": False,
            "next": "reattach or recover using this ledger",
        }


def handoff(folder, note, acknowledged):
    folder = ledger_folder(folder)
    job = read(folder / "job.json")
    if not job:
        raise ValueError("Job ownership ledger not found.")
    validate_handoff_note(note, acknowledged)
    with exclusive_lock(folder / "monitor-transition.lock", timeout=5):
        if not record_stop(folder, "operator taking responsibility for unresolved billing"):
            raise RuntimeError("Creation fence unavailable; cannot hand off monitoring safely.")
        return record_handoff(folder, job, note, acknowledged)


# Resource states in which no Pod can bill.
COMPUTE_STOPPED = ("deleted", "not_created", "rejected")


def watchdog_running(folder):
    """True while a watchdog holds this job's single-instance lock."""
    try:
        with exclusive_lock(folder / "watchdog.lock", timeout=0.2):
            return False
    except TimeoutError:
        return True


def cancel(folder, wait=None, pause=time.sleep):
    """Stop a job: write the stop request and let the job's own verified cleanup act on it.

    It closes the creation fence and records the request (record_stop), as every other stop does, and keeps
    cancel-request.json. A controller still preparing the job stops at its next check. For a workload that
    runs, the watchdog first asks the Pod to finish (watchdog.finishing): the Pod stops it as at its own time
    limit, publishes its outputs with outcome "cancelled" and deletes itself, within finalize_grace_seconds
    plus a margin and never past the deadline. Then, or at once before the workload runs, the watchdog
    deletes the Pod if it still exists, checks its absence and recovers the results. A Pod that does not
    finish in time is deleted; outputs it had not published are lost (the outcome then says outputs_lost).
    This command calls no API and deletes nothing itself. When no watchdog runs, it starts one, as
    resume-monitoring does. It waits up to `wait` seconds (default: that grace plus two minutes) and says
    what exists.
    """
    folder = ledger_folder(folder)
    job = read(folder / "job.json")
    if not job:
        raise ValueError("Job ownership ledger not found.")
    if wait is None:
        wait = min(3600, cancel_grace_seconds(job["config"]) + 120)
    if type(wait) not in (float, int) or not math.isfinite(wait) or not 0 <= wait <= 3600:
        raise ValueError("The cancel wait must be between 0 and 3600 seconds.")

    def states():
        return {
            "pod": resource_state(folder, job, "pod"),
            "volume": resource_state(folder, job, "volume"),
        }

    summary = status_read(folder / "summary.json", {})
    if isinstance(summary, dict) and summary.get("job_id") == job["job_id"]:
        return {
            "job_id": job["job_id"],
            "state": "already_finished",
            "stop_request_written": False,
            "training_outcome": summary.get("training_outcome"),
            **states(),
        }
    if active_handoff(folder, job):
        raise RuntimeError(
            "An operator took over this job's monitoring (hand-off), so no watchdog acts on a stop "
            "request. Run resume-monitoring first, then cancel again. Nothing was changed."
        )

    fenced = request_cancel(folder, job, "cancel")
    started = False
    end = time.monotonic() + wait
    while True:
        running = watchdog_running(folder)
        current = states()
        # A preparation interrupted after volume creation still needs its watchdog.
        # Do not retry a completed recovery that deliberately retained that volume.
        recovery = status_read(folder / "recovery-handoff.json", {})
        preparation_pending = (
            current["pod"]["state"] in ("not_created", "rejected")
            and current["volume"]["state"] not in COMPUTE_STOPPED
            and not (isinstance(recovery, dict) and recovery.get("state") == "storage_retained")
        )
        if (
            current["pod"]["state"] in COMPUTE_STOPPED
            and (not running or (folder / "summary.json").exists())
            and (not preparation_pending or started)
        ):
            break
        if not running and not started:
            # The job's own guard acts on the stop request. Existing resources need a
            # guard even if its credentials differ. The difference is recorded and shown.
            spawn_watchdog(folder, require_same_credentials=False)
            started = True
            continue
        if time.monotonic() >= end:
            break
        pause(1)

    current = states()
    stopped = current["pod"]["state"] in COMPUTE_STOPPED
    summary = status_read(folder / "summary.json", {})
    handoff = status_read(folder / "recovery-handoff.json", {})
    # When the wait ends during recovery, the volume may still be deleted or kept.
    # Defer storage_retained until recovery_handoff records the result.
    recovering = (
        stopped
        and current["volume"]["state"] not in COMPUTE_STOPPED
        and watchdog_running(folder)
        and not (
            isinstance(handoff, dict) and handoff.get("state") in ("complete", "storage_retained")
        )
    )
    return {
        "job_id": job["job_id"],
        "stop_request_written": True,
        "fence_closed": fenced,
        "watchdog_started": started,
        "training_outcome": summary.get("training_outcome") if isinstance(summary, dict) else None,
        "state": (
            "pending"
            if not stopped
            else "compute_stopped"
            if current["volume"]["state"] in COMPUTE_STOPPED
            else "recovering"
            if recovering
            else "storage_retained"
        ),
        **current,
    }


def resume_monitoring(folder, timeout=40):
    """Require a fresh monitor acknowledgement; never reopen the creation fence."""
    folder = ledger_folder(folder)
    job = read(folder / "job.json")
    if not job:
        raise ValueError("Job ownership ledger not found.")
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("Resume acknowledgement timeout must be between zero and 120 seconds.")
    with exclusive_lock(folder / "monitor-transition.lock", timeout=5):
        previous = status_read(folder / "operator-handoff.json", {})
        if active_handoff(folder, job):
            save(
                folder / "operator-handoff.json",
                {**previous, "active": False, "resumed_utc": utc()},
            )
        nonce = uuid.uuid4().hex
        save(
            folder / "watchdog-resume-request.json",
            {"job_id": job["job_id"], "nonce": nonce, "utc": utc()},
        )
        deadline = time.monotonic() + timeout
        spawned = False
        while time.monotonic() < deadline:
            occupied = False
            try:
                with exclusive_lock(folder / "watchdog.lock", timeout=0.05):
                    pass
            except TimeoutError:
                occupied = True
            ack = status_read(folder / "watchdog-resume-ack.json", {})
            if (
                isinstance(ack, dict)
                and ack.get("job_id") == job["job_id"]
                and ack.get("nonce") == nonce
                and type(ack.get("pid")) is int
                and ack["pid"] > 0
            ):
                state = (
                    ("monitor_armed" if spawned else "existing_monitor_active")
                    if occupied
                    else "monitor_acknowledged_and_exited"
                )
                return {
                    "job_id": job["job_id"],
                    "state": state,
                    "cleanup_verified": False,
                    "resume_nonce": nonce,
                    "acknowledged_pid": ack["pid"],
                    "original_deadline_unchanged": True,
                }
            if not occupied:
                if spawned:
                    raise RuntimeError(
                        "Watchdog exited before acknowledging resume; monitoring is unverified."
                    )
                spawn_watchdog(folder, require_same_credentials=False)
                spawned = True
            time.sleep(0.05)
        raise RuntimeError(
            "Watchdog did not acknowledge resume; monitoring is unverified. "
            "Retry or inspect the job ledger."
        )
