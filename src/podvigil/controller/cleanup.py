"""Verified deletion of the job's own Pod and, after the audit, of its own volume."""

from .. import events
from ..podrt.api import ApiError
from ..podrt.records import best_effort_save, save, utc
from ..podrt.teardown import delete_verified
from ..provider.client import cleanup_priority, provider_now
from ..storage import Storage
from .creation import rejected_dispatch
from .ledger import status_read
from .ownership import absent_from_inventory, accepted_self_delete, locate, ownership_match


def delete_owned_volume(
    api,
    job,
    folder,
    volume_id,
    attempts=16,
    storage=None,
    backup_dir=None,
    allow_pristine_inputs=False,
    allow_unreleased=False,
    released_pod_id=None,
    copy_all=False,
    before_delete=None,
):
    storage = storage or Storage(job["config"]["data_center_id"])
    if copy_all:
        audit = storage.audit_for_delete(
            job, volume_id, backup_dir, allow_pristine_inputs, copy_all=True
        )
    elif released_pod_id is not None:
        audit = storage.audit_for_delete(
            job, volume_id, backup_dir, allow_pristine_inputs, released_pod_id=released_pod_id
        )
    elif allow_unreleased:
        audit = storage.audit_for_delete(
            job, volume_id, backup_dir, allow_pristine_inputs, allow_unreleased=True
        )
    else:
        audit = storage.audit_for_delete(job, volume_id, backup_dir, allow_pristine_inputs)

    save(folder / "volume-audit.json", audit)
    if before_delete is not None:
        # Durable recovery evidence must precede the irreversible DELETE.
        before_delete(audit)
    with cleanup_priority(api):
        evidence = delete_verified(
            api,
            "/network-volumes/" + volume_id,
            lambda v: ownership_match(folder, job, "volume", v),
            attempts=attempts,
            confirm_absent=lambda: absent_from_inventory(api, "volume", volume_id),
        )
    save(
        folder / "volume-deleted.json",
        {"id": volume_id, "verified_utc": utc(), "evidence": evidence},
    )
    events.record(
        folder,
        "volume_deletion_verified",
        volume_id=volume_id,
        audit=audit.get("mode"),
        objects=audit.get("object_count"),
    )
    return evidence


def cleanup_pod(api, job, folder, pod_id=None, *, observed_pod=None):
    with cleanup_priority(api):
        return _cleanup_pod(api, job, folder, pod_id, observed_pod=observed_pod)


def _cleanup_pod(api, job, folder, pod_id=None, *, observed_pod=None):
    pod_id = pod_id or locate(api, job, folder, "pod")
    if pod_id is None:
        if (folder / "pod-dispatch.json").exists() and not rejected_dispatch(folder, job, "pod"):
            raise RuntimeError(
                "Creation response was ambiguous. Absence is not confirmed; "
                "watchdog will reconcile."
            )
        return None

    previous = status_read(folder / "pod-deleted.json", {})
    terminal = status_read(folder / "terminal-observed.json", {})
    terminal_seen = (
        terminal.get("job_id") == job["job_id"]
        and terminal.get("pod_id") == pod_id
        and (
            (
                terminal.get("source") == "owned_tombstone"
                and terminal.get("ownership_checked") is True
            )
            or (
                terminal.get("source") == "accepted_self_delete"
                and accepted_self_delete(terminal.get("remote_evidence"), job, pod_id)
            )
        )
    )
    previous_verified = (
        previous.get("id") == pod_id
        and isinstance(previous.get("evidence"), dict)
        and previous["evidence"].get("verified") is True
    )

    preflight = None
    clock_unknown = hasattr(api, "_clock_anchor") and api._clock_anchor is None
    sampled_present = observed_pod is not None and observed_pod.get("status") != "TERMINATED"
    if observed_pod is not None and (
        observed_pod.get("id") != pod_id or not ownership_match(folder, job, "pod", observed_pod)
    ):
        raise PermissionError("Clock observation does not match the owned Pod; refusing deletion.")
    startup_preflight = (
        not previous_verified
        and not terminal_seen
        and (clock_unknown or provider_now(api) < job.get("startup_deadline_epoch", 0))
    )
    if startup_preflight or sampled_present:
        receipt = status_read(folder / "pod-receipt.json", {})
        trusted = (
            receipt.get("id") == pod_id
            and receipt.get("job_id") == job["job_id"]
            and receipt.get("kind") == "pod"
            and receipt.get("established_by") in ("create_response", "reconciled")
            and pod_id not in job["prior_pod_ids"]
        )
        if not trusted and not sampled_present:
            current = api.get_or_none("/pods/" + pod_id)
            if current is None or not ownership_match(folder, job, "pod", current):
                raise RuntimeError(
                    "Early Pod absence is inconclusive; retain guards through "
                    "startup visibility window."
                )
        # Explicit cleanup has already been requested. A definitive receipt (or
        # current ownership check) authorizes this DELETE even if GET lags 201.
        # An early 404 rejection is not proof that the Pod cannot appear later.
        try:
            api.request("DELETE", "/pods/" + pod_id)
        except ApiError as exc:
            best_effort_save(
                folder / "pod-cleanup-attempt.json",
                {"id": pod_id, "status": exc.status, "outcome": "unverified", "utc": utc()},
            )
            raise RuntimeError(
                "Startup cleanup request not accepted; continue reconciling "
                "without declaring absence."
            ) from exc
        meta = getattr(api, "last_response", None) or {}
        preflight = {
            "status": meta.get("status"),
            "outcome": "accepted",
            "actor": "local_controller_or_watchdog",
            "phase": "sampled_owned_pod" if sampled_present else "startup_visibility_guard",
        }
        best_effort_save(
            folder / "pod-cleanup-attempt.json",
            {"id": pod_id, "job_id": job["job_id"], **preflight, "verified": False, "utc": utc()},
        )

    evidence = delete_verified(
        api,
        "/pods/" + pod_id,
        lambda p: ownership_match(folder, job, "pod", p),
        confirm_absent=lambda: absent_from_inventory(api, "pod", pod_id),
        actor="local_controller_or_watchdog",
    )
    if preflight:
        evidence["delete_attempts"].insert(0, preflight)
    history = previous.get("history", []) + (
        [previous["evidence"]] if previous.get("evidence") else []
    )
    best_effort_save(
        folder / "pod-deleted.json",
        {"id": pod_id, "verified_utc": utc(), "evidence": evidence, "history": history[-16:]},
    )
    events.record(
        folder,
        "pod_deletion_verified",
        pod_id=pod_id,
        delete_attempts=len(evidence.get("delete_attempts") or []),
    )
    return pod_id
