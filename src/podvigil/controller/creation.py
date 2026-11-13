"""Single-attempt creation behind the creation fence; dispatch, rejection and never-dispatched records."""

import os
import time

from .. import events
from ..podrt.api import ApiError
from ..podrt.records import best_effort_save, exclusive_lock, read, save, utc
from ..policy import CLOUD
from .ledger import status_read
from .ownership import creation_receipt

CANCEL_REASON = "cancel requested by the operator"

# A cancelled running workload may take finalize_grace_seconds to stop and publish.
# The Pod then deletes itself. The watchdog waits that long plus this margin,
# up to the deadline (watchdog.finishing).
CANCEL_GRACE_MARGIN = 30


def request_cancel(folder, job, source):
    """Record a cancel: cancel-request.json and the usual stop request (the closed creation fence). The
    watchdog acts on it; nothing is deleted here. Returns whether the fence is closed."""
    save(
        folder / "cancel-request.json",
        {"job_id": job["job_id"], "pid": os.getpid(), "source": source, "utc": utc()},
    )
    fenced = record_stop(folder, CANCEL_REASON)
    events.record(folder, "cancel_requested", fence_closed=fenced, source=source)
    return fenced


def stop_requested(folder):
    if (folder / "stop-request.json").exists():
        raise RuntimeError("Cleanup was requested; no new resources may be created.")


def record_stop(folder, reason):
    # The preallocated lock byte is the creation fence. Metadata is only a
    # human-readable explanation. A locked or full status file must not block deletion.
    fenced = False
    try:
        with exclusive_lock(folder / "control.lock", timeout=1) as lock:
            lock.seek(0)
            lock.write(b"!")
            lock.flush()
            os.fsync(lock.fileno())
            fenced = True
    except Exception:
        pass
    best_effort_save(folder / "stop-request.json", {"reason": reason, "at_utc": utc()})
    return fenced


def managed_create(api, job, folder, kind, body, intent):
    # Serialize intent + the bounded POST with the watchdog's stop/exit decision.
    # The OS releases this lock if the controller dies during the request.
    with exclusive_lock(folder / "control.lock", timeout=120) as lock:
        if lock.read(1) == b"!":
            raise RuntimeError("Job creation fence is closed.")
        stop_requested(folder)
        if time.time() >= job.get("preparation_deadline_epoch", float("inf")):
            raise RuntimeError("Preparation deadline expired; no new resource may be created.")
        beat = read(folder / "controller-heartbeat.json", {"epoch": 0})
        if time.time() - beat["epoch"] > job["config"]["controller_stale_seconds"]:
            raise RuntimeError("Controller heartbeat is stale; no new resource may be created.")
        if kind == "pod" and body.get("cloud") != CLOUD:
            raise ValueError("Only SECURE cloud may cross the Pod creation boundary.")
        save(folder / (kind + "-intent.json"), intent)
        return create_once(api, folder, kind, body)


def rejected_dispatch(folder, job, kind):
    rejected = status_read(folder / (kind + "-rejected.json"), {})
    return (
        rejected.get("job_id") == job["job_id"]
        and rejected.get("kind") == kind
        and (
            rejected.get("status") in (400, 401, 402, 403, 404, 413, 422, 429)
            or (rejected.get("status") == 0 and rejected.get("not_sent") is True)
        )
    )


def never_dispatched(folder, job):
    state = status_read(folder / "pod-creation-state.json", {})
    return (
        state.get("job_id") == job["job_id"]
        and state.get("state") == "never_dispatched"
        and not (folder / "pod-dispatch.json").exists()
        and not (folder / "pod-receipt.json").exists()
    )


def create_once(api, folder, kind, body):
    """No implicit retries: a timeout can hide a successfully billed creation."""
    path = "/pods" if kind == "pod" else "/network-volumes"
    job = read(folder / "job.json")
    if not job:
        raise RuntimeError("A persistent ownership ledger is required before creation.")
    save(
        folder / (kind + "-creation-state.json"),
        {"job_id": job["job_id"], "state": "dispatching", "utc": utc()},
    )
    save(folder / (kind + "-dispatch.json"), {"state": "dispatching", "utc": utc()})
    events.record(folder, "create_requested", kind=kind)
    try:
        response = api.request("POST", path, body)
    except ApiError as exc:
        if exc.status in (400, 401, 402, 403, 404, 413, 422, 429) or getattr(
            exc, "not_sent", False
        ):
            save(
                folder / (kind + "-rejected.json"),
                {
                    "job_id": job["job_id"],
                    "kind": kind,
                    "status": exc.status,
                    "not_sent": getattr(exc, "not_sent", False),
                    "utc": utc(),
                },
            )
            events.record(folder, "create_refused", kind=kind, http_status=exc.status)
        else:
            events.record(folder, "create_outcome_unknown", kind=kind, http_status=exc.status)
        raise
    except BaseException as exc:
        events.record(folder, "create_outcome_unknown", kind=kind, error_type=type(exc).__name__)
        raise

    # Save the ID before any validation or subsequent request. Never persist env.
    save(
        folder / (kind + "-receipt.json"), creation_receipt(response, job, kind, "create_response")
    )
    events.record(folder, "created", kind=kind, **{kind + "_id": response.get("id")})
    return response
