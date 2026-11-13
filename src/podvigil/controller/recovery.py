"""Recovery after the Pod is gone: results, the whole-volume audit, volume deletion, partial exports."""

from pathlib import Path
import time

from .. import events
from ..credentials import account_api
from ..errors import public_error
from ..podrt.records import best_effort_save, digest, exclusive_lock, read, save, utc
from ..provider.client import provider_now
from ..storage import OUTCOMES as RESULT_OUTCOMES, Storage, verify_local_audit
from .cleanup import cleanup_pod, delete_owned_volume
from .creation import never_dispatched, record_stop, rejected_dispatch
from .ledger import ledger_folder, status_read, watch_log
from .ownership import (
    absent_from_inventory,
    deletion_verified,
    locate,
    ownership_match,
    volume_already_deleted,
)

# Storage reports this when the outputs were not all published.
INCOMPLETE = "No valid complete artifact manifest"
PARTIAL_ADVICE = (
    "The workload's outputs are incomplete. export-partial copies what is there and deletes "
    "nothing; recover --accept-partial copies every object with its bytes checked, then "
    "deletes the volume."
)

SPECIAL_MODES = {
    "not_started": "unreleased_pod",
    "outputs_lost": "released_unpublished",
    "partial_outputs": "partial_outputs_copied",
}
CHECKPOINT_FILE = "recovery-checkpoint.json"
CHECKPOINT_ERROR = (
    "Special recovery evidence is missing, changed or invalid; completion is not verified."
)


def special_binding(folder, job, pod_id, volume_id, outcome, audit):
    """Bind the intended outcome to this job, receipts, release and durable audit copies."""
    try:
        if (
            outcome not in SPECIAL_MODES
            or audit["mode"] != SPECIAL_MODES[outcome]
            or read(folder / "job.json") != job
            or (folder / "training-released.json").exists() != (outcome != "not_started")
            or read(folder / "volume-audit.json") != audit
        ):
            raise ValueError()
        names = [
            "job.json",
            "pod-receipt.json",
            "volume-receipt.json",
            "volume-audit.json",
        ]
        for kind, rid in (("pod", pod_id), ("volume", volume_id)):
            receipt = read(folder / (kind + "-receipt.json"))
            if (
                not isinstance(rid, str)
                or not rid
                or not isinstance(receipt, dict)
                or receipt.get("id") != rid
                or receipt.get("job_id", job["job_id"]) != job["job_id"]
                or receipt.get("kind", kind) != kind
                or rid in job["prior_" + kind + "_ids"]
            ):
                raise ValueError()

        if outcome != "not_started":
            names.append("training-released.json")
        index = verify_local_audit(job, volume_id, folder, audit, pod_id)
        names.append(index.relative_to(folder).as_posix())
        return {
            "version": 1,
            "job_id": job["job_id"],
            "pod_id": pod_id,
            "volume_id": volume_id,
            "training_outcome": outcome,
            "files": {
                name: {
                    "size": (folder / name).stat().st_size,
                    "sha256": digest(folder / name),
                }
                for name in names
            },
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        raise RuntimeError(CHECKPOINT_ERROR) from None


def prepare_special_recovery(folder, job, pod_id, volume_id, outcome, audit):
    # The callback runs after the audit is saved, before DELETE is sent. It is not
    # a successful summary and cannot by itself prove either resource's absence.
    if not deletion_verified(status_read(folder / "pod-deleted.json"), pod_id):
        raise RuntimeError(CHECKPOINT_ERROR)
    save(folder / CHECKPOINT_FILE, special_binding(folder, job, pod_id, volume_id, outcome, audit))


def special_evidence(folder, job, pod_id, volume_id, previous=None):
    """An interrupted special recovery needs its checkpoint; a legacy completed summary needs its audit."""
    try:
        checkpoint = read(folder / CHECKPOINT_FILE)
        audit = read(folder / "volume-audit.json")
        outcome = (checkpoint if checkpoint is not None else previous or {}).get("training_outcome")
        expected = special_binding(folder, job, pod_id, volume_id, outcome, audit)
        if checkpoint is not None:
            if type(checkpoint.get("version")) is not int or checkpoint != expected:
                raise ValueError()
        elif not (
            isinstance(previous, dict)
            and previous.get("job_id") == job["job_id"]
            and previous.get("pod_created") is True
            and previous.get("pod_termination_verified") is True
            and previous.get("volume_deletion_verified") is True
            and deletion_verified(previous.get("pod_evidence"), pod_id)
            and deletion_verified(previous.get("volume_evidence"), volume_id)
        ):
            raise ValueError()
        if not deletion_verified(status_read(folder / "pod-deleted.json"), pod_id):
            raise ValueError()
        return outcome, audit
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        raise RuntimeError(CHECKPOINT_ERROR) from None


def confirm_volume_absence(api, folder, volume_id):
    """The caller already observed absence once; an old verified record or fresh independent checks are needed."""
    if not deletion_verified(status_read(folder / "volume-deleted.json"), volume_id):
        time.sleep(1)
        if api.get_or_none(
            "/network-volumes/" + volume_id
        ) is not None or not absent_from_inventory(api, "volume", volume_id):
            raise RuntimeError("Volume absence was not independently confirmed; retry recovery.")
        save(
            folder / "volume-deleted.json",
            {
                "id": volume_id,
                "verified_utc": utc(),
                "evidence": {
                    "delete_attempted": False,
                    "observed_absent": True,
                    "point_reads": 2,
                    "inventory_absent": True,
                    "deleted_by": "unknown",
                },
            },
        )


def finish_special_recovery(folder, job, pod_id, volume_id, *, evidence=None, previous=None):
    outcome, audit = evidence or special_evidence(folder, job, pod_id, volume_id, previous)
    if not deletion_verified(status_read(folder / "volume-deleted.json"), volume_id):
        raise RuntimeError(CHECKPOINT_ERROR)
    summary = {
        "job_id": job["job_id"],
        "pod_created": True,
        "pod_termination_verified": True,
        "volume_deletion_verified": True,
        "training_outcome": outcome,
        "pod_evidence": status_read(folder / "pod-deleted.json"),
        "volume_evidence": status_read(folder / "volume-deleted.json"),
        "completed_utc": utc(),
    }
    if outcome == "not_started":
        summary.update(
            backup_not_required=True,
            self_termination_probe=status_read(folder / "self-termination-probe.json"),
            guard_records=audit["control_backup_directory"],
        )
    elif outcome == "outputs_lost":
        summary.update(
            outputs_lost=True,
            guard_records=audit["control_backup_directory"],
            explanation=(
                "The workload was released and ran, but it never published its outputs: they "
                "were on the Pod's own disk, which is gone with the Pod. The volume held only "
                "the inputs and the Pod's own records; the records were copied to the ledger "
                "and the volume was deleted."
            ),
        )
    else:
        copies = {item["key"]: item.get("local_path") for item in audit["objects"]}
        result_copy = copies.get("jobs/" + job["job_id"] + "/artifacts/result.json")
        result = status_read(Path(result_copy), {}) if result_copy else {}
        workload = (
            result.get("outcome")
            if isinstance(result, dict) and result.get("job_id") == job["job_id"]
            else None
        )
        summary.update(
            partial_outputs=True,
            workload_outcome=workload if workload in RESULT_OUTCOMES else None,
            explanation=(
                "The workload's outputs were incomplete. On the operator's request every "
                "object on the volume was copied to copy_directory and each copy checked "
                "against the bytes read; objects.json there maps each key to its file. "
                "Then the volume was deleted."
            ),
            copy_directory=audit["control_backup_directory"],
            object_count=audit["object_count"],
        )
    if previous is not None:
        # Keep the original completion time/evidence only after rechecking the saved copies.
        stable = set(summary) - {
            "pod_evidence",
            "volume_evidence",
            "completed_utc",
            "self_termination_probe",
        }
        if (
            not isinstance(previous, dict)
            or any(previous.get(key) != summary[key] for key in stable)
            or not deletion_verified(previous.get("pod_evidence"), pod_id)
            or not deletion_verified(previous.get("volume_evidence"), volume_id)
        ):
            raise RuntimeError(CHECKPOINT_ERROR)
        return previous
    save(folder / "summary.json", summary)
    events.record(folder, "recovered", training_outcome=outcome, pod_created=True)
    return summary


def recover(folder, api=None, storage=None):
    folder = ledger_folder(folder)
    with exclusive_lock(folder / "recovery.lock", timeout=1800):
        return recover_locked(folder, api, storage)


def recover_locked(folder, api=None, storage=None):
    from ..storage import verify_local_backup

    job = read(folder / "job.json")
    if not job:
        raise ValueError("Job ownership ledger not found.")
    api = api or account_api(job)
    if not record_stop(folder, "recover/finish cleanup"):
        cleanup_pod(api, job, folder)
        raise RuntimeError("Creation fence not confirmed; storage deletion is deferred.")

    # Never download while leaving compute billing silently.
    pod_id = cleanup_pod(api, job, folder)
    volume_id = locate(api, job, folder, "volume")
    if pod_id is None:
        if not never_dispatched(folder, job) and not rejected_dispatch(folder, job, "pod"):
            raise RuntimeError(
                "No positive proof that a Pod was never dispatched. "
                "Keep volume; reconcile the job ledger."
            )
        if volume_id:
            # The controller and watchdog both recover. If the first one verified deletion,
            # the second skips the audit because S3 cannot list a deleted volume.
            if not volume_already_deleted(api, folder, volume_id):
                delete_owned_volume(
                    api,
                    job,
                    folder,
                    volume_id,
                    storage=storage,
                    backup_dir=folder / "downloads",
                    allow_pristine_inputs=True,
                )
        elif (folder / "volume-dispatch.json").exists() and not rejected_dispatch(
            folder, job, "volume"
        ):
            raise RuntimeError("Volume creation is unresolved; keep reconciling its identity.")
        elif volume_id is None:
            state = status_read(folder / "volume-creation-state.json", {})
            volume_never = (
                state.get("job_id") == job["job_id"]
                and state.get("state") == "never_dispatched"
                and not (folder / "volume-dispatch.json").exists()
            )
            if not volume_never and not rejected_dispatch(folder, job, "volume"):
                raise RuntimeError(
                    "Missing positive volume creation/rejection evidence; "
                    "cleanup cannot be certified."
                )

        previous = status_read(folder / "summary.json") or {}
        if (
            previous.get("job_id") == job["job_id"]
            and previous.get("pod_created") is False
            and previous.get("volume_deletion_verified") is True
            and (
                not volume_id
                or deletion_verified(status_read(folder / "volume-deleted.json"), volume_id)
            )
        ):
            # An earlier recovery already certified this job. Keep its record.
            return previous
        summary = {
            "job_id": job["job_id"],
            "pod_created": False,
            "pod_termination_verified": True,
            "volume_deletion_verified": True,
            "training_outcome": "not_started",
            "backup_not_required": True,
            "pod_evidence": "positive never-dispatched/rejected record and closed fence",
            "volume_evidence": status_read(folder / "volume-deleted.json"),
            "completed_utc": utc(),
        }
        save(folder / "summary.json", summary)
        events.record(folder, "recovered", training_outcome="not_started", pod_created=False)
        return summary

    if not volume_id:
        raise RuntimeError(
            "No volume receipt; inspect creation reconciliation before declaring completion."
        )
    if api.get_or_none("/network-volumes/" + volume_id) is None:
        done = status_read(folder / "summary.json") or {}
        if (not done and (folder / CHECKPOINT_FILE).exists()) or done.get(
            "training_outcome"
        ) in SPECIAL_MODES:
            evidence = special_evidence(folder, job, pod_id, volume_id, done or None)
            confirm_volume_absence(api, folder, volume_id)
            return finish_special_recovery(
                folder, job, pod_id, volume_id, evidence=evidence, previous=done or None
            )
        verify_local_backup(job, volume_id, folder / "downloads")
        if (
            deletion_verified(status_read(folder / "volume-deleted.json"), volume_id)
            and done.get("job_id") == job["job_id"]
            and done.get("pod_termination_verified") is True
            and done.get("volume_deletion_verified") is True
        ):
            # Keep our verified deletion and summary. Later evidence can be weaker.
            return done
        confirm_volume_absence(api, folder, volume_id)
    else:
        storage = storage or Storage(job["config"]["data_center_id"])
        try:
            storage.collect(job, volume_id, folder / "downloads")
        except RuntimeError as missing:
            summary = unreleased_cleanup(
                api, job, folder, volume_id, storage, missing
            ) or unpublished_cleanup(api, job, folder, volume_id, storage, missing, pod_id)
            if summary is None:
                if INCOMPLETE in str(missing) and (folder / "training-released.json").exists():
                    raise RuntimeError(str(missing) + " " + PARTIAL_ADVICE) from None
                raise
            return summary
        verify_local_backup(job, volume_id, folder / "downloads")
        delete_owned_volume(
            api, job, folder, volume_id, storage=storage, backup_dir=folder / "downloads"
        )

    result = read(folder / "downloads" / "artifacts" / "result.json", {})
    summary = {
        "job_id": job["job_id"],
        "pod_termination_verified": True,
        "volume_deletion_verified": True,
        "training_outcome": result.get("outcome", "unknown"),
        "pod_evidence": status_read(folder / "pod-deleted.json"),
        "volume_evidence": status_read(folder / "volume-deleted.json"),
        "self_termination_probe": status_read(folder / "self-termination-probe.json"),
        "download_directory": str(folder / "downloads"),
        "completed_utc": utc(),
    }
    save(folder / "summary.json", summary)
    events.record(
        folder,
        "recovered",
        training_outcome=summary["training_outcome"],
        pod_created=True,
    )
    return summary


def unreleased_cleanup(api, job, folder, volume_id, storage, missing):
    """Delete the volume of a Pod whose workload was never released; None if that is not proven.

    A Pod guard that stops at start-up leaves no artifact manifest, and waiting for one would keep the
    volume billed for ever. This applies only when all of the following hold: the failure is the missing
    manifest; this controller never released the workload; the guard's own status record for this job is
    on the volume; and the audit finds nothing but the pristine inputs and the guard's pre-release
    records (a start marker, a manifest, an artifact or any other object fails it). The guard's records
    are copied to the backup directory first. In every other case the caller keeps the volume.
    """
    if (
        "No valid complete artifact manifest" not in str(missing)
        or (folder / "training-released.json").exists()
    ):
        return None
    try:
        status = storage.get_json(volume_id, "jobs/" + job["job_id"] + "/agent.json")
        if (
            not isinstance(status, dict)
            or status.get("job_id") != job["job_id"]
            or status.get("actor") != "pod_guard"
        ):
            return None
        pod_id = (status_read(folder / "pod-receipt.json") or {}).get("id")
        delete_owned_volume(
            api,
            job,
            folder,
            volume_id,
            storage=storage,
            backup_dir=folder / "downloads",
            allow_pristine_inputs=True,
            allow_unreleased=True,
            before_delete=lambda audit: prepare_special_recovery(
                folder, job, pod_id, volume_id, "not_started", audit
            ),
        )
    except Exception:
        return None
    return finish_special_recovery(folder, job, pod_id, volume_id)


def unpublished_cleanup(api, job, folder, volume_id, storage, missing, pod_id):
    """Delete the volume of a released job whose workload never published anything; None if that is not
    proven. The outcome is outputs_lost: the workload ran, and its outputs are gone.

    The workload's outputs stay on the Pod's own disk until the Pod publishes them, manifest last. After a
    cancel, a Ctrl+C or a hard Pod death before that, they are gone with the Pod, and waiting for a manifest
    would keep the volume billed for ever. This applies only when all of this holds: the failure is the
    missing manifest; the workload was released; the Pod's deletion is verified; the Pod's own status record
    for this job is on the volume; and the audit finds nothing but the pristine inputs, the input manifest
    and the exact records the release and this job's Pod leave, each checked (storage.RELEASED_RECORDS).
    Any artifact, bundle part, manifest, salvaged output or other object keeps the volume. The records are
    copied to the ledger first. Why a volume was kept is saved in volume-kept.json.
    """
    if (
        "No valid complete artifact manifest" not in str(missing)
        or not (folder / "training-released.json").exists()
        or not deletion_verified(status_read(folder / "pod-deleted.json"), pod_id)
    ):
        return None
    try:
        status = storage.get_json(volume_id, "jobs/" + job["job_id"] + "/agent.json")
        if (
            not isinstance(status, dict)
            or status.get("job_id") != job["job_id"]
            or status.get("pod_id") != pod_id
            or status.get("actor") not in ("pod_guard", "pod_supervisor")
        ):
            raise RuntimeError(
                "The Pod's own status record for this job is not on the volume. Keep the volume."
            )
        delete_owned_volume(
            api,
            job,
            folder,
            volume_id,
            storage=storage,
            backup_dir=folder / "downloads",
            allow_pristine_inputs=True,
            released_pod_id=pod_id,
            before_delete=lambda audit: prepare_special_recovery(
                folder, job, pod_id, volume_id, "outputs_lost", audit
            ),
        )
    except Exception as exc:
        best_effort_save(
            folder / "volume-kept.json",
            {
                "job_id": job["job_id"],
                "utc": utc(),
                "reason": public_error(exc, [getattr(api, "key", None)]),
            },
        )
        return None
    return finish_special_recovery(folder, job, pod_id, volume_id)


def recovery_handoff(folder, api, storage):
    """Finish backup after controller death; persistent storage errors stay visible."""
    try:
        recover(folder, api, storage)
        best_effort_save(folder / "recovery-handoff.json", {"state": "complete", "utc": utc()})
        watch_log(folder, "recovery complete")
    except Exception as exc:
        error = public_error(exc, [getattr(api, "key", None)])
        partial = None
        try:
            partial = export_partial(folder, api, storage)
        except Exception:
            pass
        best_effort_save(
            folder / "recovery-handoff.json",
            {
                "state": "storage_retained",
                "error_type": type(exc).__name__,
                "error": error,
                "utc": utc(),
                "partial_export": partial,
                "message": (
                    "Compute is terminated. Storage may remain billed; "
                    "run recover or export partial outputs."
                ),
            },
        )
        events.record(folder, "recovery_failed", **error, partial_export=partial is not None)
        watch_log(
            folder,
            "recovery failed, storage retained: "
            + error["error_type"]
            + ": "
            + error.get("message", "(message withheld)")
            + ("; partial export saved" if partial else "; no partial export"),
        )


def export_partial(folder, api=None, storage=None):
    folder = ledger_folder(folder)
    with exclusive_lock(folder / "recovery.lock", timeout=5):
        job = read(folder / "job.json")
        if not job:
            raise ValueError("Job ownership ledger not found.")
        api = api or account_api(job)
        record_stop(folder, "partial output recovery")
        cleanup_pod(api, job, folder)
        volume_id = locate(api, job, folder, "volume")
        if not volume_id:
            raise RuntimeError("No known result volume is available.")
        volume = api.get_or_none("/network-volumes/" + volume_id)
        if volume is None or not ownership_match(folder, job, "volume", volume):
            raise RuntimeError("Result volume ownership/availability is not verified.")
        storage = storage or Storage(job["config"]["data_center_id"])
        result = storage.export_partial(job, volume_id, folder / "partial-downloads")
        result.update(pod_termination_verified=True, volume_deleted=False, storage_retained=True)
        save(folder / "partial-export.json", result)
        events.record(
            folder, "partial_export", files=result["file_count"], failed=result["failed_count"]
        )
        return result


def accept_partial(folder, api=None, storage=None):
    """recover --accept-partial: recover; and when the workload's outputs are incomplete, keep a checked copy
    of every object on the volume in the ledger, then delete the volume. The outcome is partial_outputs.

    Only on the operator's request, and only when all of this holds: recovery stopped at the missing or
    incomplete manifest; the workload was released; the Pod's deletion is verified; the volume is the
    job's own. The audit (storage.audit_for_delete with copy_all) reads every object once, copies it to
    partial-downloads/volume-audit-*/ and checks the copy; objects.json there maps each key to its file.
    The inputs and the input manifest must still match their originals, the listing must not change, and
    an object the link index names keeps the volume. Any failure keeps the volume.
    """
    folder = ledger_folder(folder)
    with exclusive_lock(folder / "recovery.lock", timeout=5):
        job = read(folder / "job.json")
        if not job:
            raise ValueError("Job ownership ledger not found.")
        api = api or account_api(job)
        try:
            return recover_locked(folder, api, storage)
        except RuntimeError as exc:
            if INCOMPLETE not in str(exc):
                raise
        if not (folder / "training-released.json").exists():
            raise RuntimeError(
                "The workload was never released, so the volume holds no outputs to accept. "
                "Nothing was deleted."
            )
        receipt = status_read(folder / "pod-receipt.json", {})
        pod_id = receipt.get("id") if isinstance(receipt, dict) else None
        if not isinstance(pod_id, str) or not deletion_verified(
            status_read(folder / "pod-deleted.json"), pod_id
        ):
            raise RuntimeError("The Pod's deletion is not verified. Nothing was deleted.")
        volume_id = locate(api, job, folder, "volume")
        volume = api.get_or_none("/network-volumes/" + volume_id) if volume_id else None
        if volume is None or not ownership_match(folder, job, "volume", volume):
            raise RuntimeError(
                "Result volume ownership/availability is not verified. Nothing was deleted."
            )
        storage = storage or Storage(job["config"]["data_center_id"])
        delete_owned_volume(
            api,
            job,
            folder,
            volume_id,
            storage=storage,
            backup_dir=folder / "partial-downloads",
            allow_pristine_inputs=True,
            copy_all=True,
            before_delete=lambda audit: prepare_special_recovery(
                folder, job, pod_id, volume_id, "partial_outputs", audit
            ),
        )
        return finish_special_recovery(folder, job, pod_id, volume_id)


def export_unresolved(folder, api=None, storage=None):
    """Rescue accessible bytes after expiry without asserting Pod absence."""
    folder = ledger_folder(folder)
    with exclusive_lock(folder / "recovery.lock", timeout=5):
        job = read(folder / "job.json")
        if not job:
            raise ValueError("Job ownership ledger not found.")
        api = api or account_api(job)
        # Establish provider Date on a fresh client.
        api.listing("/pods", "pods")
        deadline = job.get("deadline_epoch", job.get("preparation_deadline_epoch"))
        if deadline is None or provider_now(api) < deadline + 300:
            raise RuntimeError(
                "Unresolved export requires the original deadline plus five minutes to elapse."
            )
        if not record_stop(folder, "unresolved creation export; compute status unknown"):
            raise RuntimeError("Creation fence unavailable; unresolved export deferred.")
        volume_id = locate(api, job, folder, "volume")
        if not volume_id:
            raise RuntimeError("Resolve the owned volume identity before exporting it.")
        volume = api.get_or_none("/network-volumes/" + volume_id)
        if volume is None or not ownership_match(folder, job, "volume", volume):
            raise RuntimeError("Volume ownership is unverified.")
        storage = storage or Storage(job["config"]["data_center_id"])
        result = storage.export_partial(job, volume_id, folder / "unresolved-downloads")
        result.update(
            complete=False,
            pod_termination_verified=False,
            volume_deleted=False,
            storage_retained=True,
            billing_unverified=True,
            message=(
                "Snapshot may race an unobserved writer; it never authorizes deletion. "
                "Watchdog remains active."
            ),
        )
        save(folder / "unresolved-export.json", result)
        return result
