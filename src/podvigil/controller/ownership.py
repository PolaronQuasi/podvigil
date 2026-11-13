"""Which Pod and volume belong to the job: receipts, listings, the volume beacon, deletion records."""

import datetime
import re
import time

from ..podrt.records import best_effort_save, save, utc
from ..provider.client import provider_now
from ..settings import ENV_PREFIXES
from ..storage import Storage
from .ledger import status_read


def ownership_match(folder, job, kind, resource):
    return receipt_owns(resource, job, status_read(folder / (kind + "-receipt.json")), kind)


def absent_from_inventory(api, kind, resource_id):
    path, field = ("/pods", "pods") if kind == "pod" else ("/network-volumes", "networkVolumes")
    return all(resource.get("id") != resource_id for resource in api.listing(path, field))


def deletion_verified(record, resource_id):
    """True for this ledger's own deletion record of resource_id: a verified DELETE or a confirmed absence."""
    if not isinstance(record, dict) or not resource_id or record.get("id") != resource_id:
        return False
    evidence = record.get("evidence")
    return isinstance(evidence, dict) and (
        evidence.get("verified") is True
        or (evidence.get("observed_absent") is True and evidence.get("inventory_absent") is True)
    )


def volume_already_deleted(api, folder, volume_id):
    """Our verified deletion record stands only while the provider still reports the volume absent."""
    if not deletion_verified(status_read(folder / "volume-deleted.json"), volume_id):
        return False
    return api.get_or_none("/network-volumes/" + volume_id) is None


def locate(api, job, folder, kind):
    receipt = status_read(folder / (kind + "-receipt.json"))
    if receipt:
        return receipt["id"]
    if not any(
        (folder / (kind + suffix)).exists() for suffix in ("-intent.json", "-dispatch.json")
    ):
        return None

    path, field, matches = (
        ("/pods", "pods", owned_pod)
        if kind == "pod"
        else ("/network-volumes", "networkVolumes", owned_volume)
    )
    found = [r for r in api.listing(path, field) if matches(r, job)]
    if len(found) > 1:
        raise RuntimeError(
            "Multiple owned resources found. Do not create again; inspect the ledger."
        )
    if found:
        best_effort_save(
            folder / (kind + "-receipt.json"), creation_receipt(found[0], job, kind, "reconciled")
        )
        return found[0]["id"]

    if kind == "pod" and (folder / "pod-dispatch.json").exists():
        try:
            return reconcile_beacon(api, job, folder)
        except PermissionError:
            raise
        except Exception as exc:
            best_effort_save(
                folder / "beacon-observation.json",
                {"state": "unknown", "error_type": type(exc).__name__, "utc": utc()},
            )
    return None


def reconcile_beacon(api, job, folder, storage=None):
    """An owned-volume beacon is a discovery hint, never authority to DELETE."""
    volume_id = locate(api, job, folder, "volume")
    if not volume_id:
        return None
    volume = api.get_or_none("/network-volumes/" + volume_id)
    if volume is None or not ownership_match(folder, job, "volume", volume):
        return None
    storage = storage or Storage(job["config"]["data_center_id"])
    prefix = "jobs/" + job["job_id"] + "/control/"
    identity = storage.get_json(volume_id, prefix + "identity.json")
    if not identity or identity.get("job_id") != job["job_id"]:
        return None
    pod_id = identity.get("pod_id")
    if (
        not isinstance(pod_id, str)
        or not pod_id
        or not pod_id.replace("-", "").replace("_", "").isalnum()
    ):
        raise RuntimeError("Invalid Pod identity beacon; retain storage for inspection.")
    pod = api.get_or_none("/pods/" + pod_id)
    if pod is not None:
        if not owned_pod(pod, job):
            raise PermissionError(
                "Volume beacon points to an unrelated Pod; no deletion authority."
            )
        save(folder / "pod-receipt.json", creation_receipt(pod, job, "pod", "reconciled"))
        return pod_id

    evidence = storage.get_json(volume_id, prefix + "self-delete.json")
    if (
        not accepted_self_delete(evidence, job, pod_id)
        or pod_id in job["prior_pod_ids"]
        or provider_now(api) < job.get("deadline_epoch", float("inf"))
    ):
        return None
    # After the original lifetime, a complete account inventory plus separated
    # absence reads and persisted self-delete evidence allow backup to proceed.
    # This special receipt will NEVER match a subsequently visible resource.
    time.sleep(1)
    if api.get_or_none("/pods/" + pod_id) is not None or not absent_from_inventory(
        api, "pod", pod_id
    ):
        return None
    save(
        folder / "pod-receipt.json",
        {
            "id": pod_id,
            "job_id": job["job_id"],
            "kind": "pod",
            "established_by": "self_deletion_observed",
            "recorded_utc": utc(),
            "deletion_authority": False,
        },
    )
    save(
        folder / "beacon-reconciliation.json",
        {
            "pod_id": pod_id,
            "remote_evidence": evidence,
            "point_absence_reads": 2,
            "inventory_absent": True,
            "deadline_elapsed": True,
            "deletion_authority": False,
            "utc": utc(),
        },
    )
    return pod_id


def accepted_self_delete(value, job, pod_id):
    return bool(
        isinstance(value, dict)
        and value.get("job_id") == job["job_id"]
        and value.get("pod_id") == pod_id
        and value.get("delete_accepted") is True
        and type(value.get("delete_http_status")) is int
        and 200 <= value["delete_http_status"] < 300
    )


# New Pods carry the tool's own name as a job tag. Pods of ledgers before 0.1.0
# carry the old one.
JOB_ID_NAMES = tuple(prefix + "_JOB_ID" for prefix in ENV_PREFIXES)


def owned_pod(pod, job):
    env = pod.get("env") or {}
    return (
        pod.get("name") == job["pod_name"]
        and isinstance(env, dict)
        and any(env.get(name) == job["job_id"] for name in JOB_ID_NAMES)
        and pod.get("id") not in job["prior_pod_ids"]
    )


def owned_volume(volume, job):
    return (
        volume.get("name") == job["volume_name"]
        and volume.get("dataCenter") == job["config"]["data_center_id"]
        and volume.get("id") not in job["prior_volume_ids"]
    )


def _created_at(value):
    """Canonical RFC3339 instant, retaining sub-microsecond precision if present."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})(?:\.(\d+))?"
        r"([Zz]|[+-]\d{2}:\d{2})",
        value,
    )
    if not match:
        return None
    day, clock, fraction, zone = match.groups()
    try:
        stamp = datetime.datetime.fromisoformat(
            day + "T" + clock + ("+00:00" if zone.upper() == "Z" else zone)
        ).astimezone(datetime.timezone.utc)
    except (ValueError, OverflowError):
        return None
    fraction = (fraction or "").rstrip("0")
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + ("." + fraction if fraction else "") + "Z"


def creation_receipt(resource, job, kind, established_by):
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        raise ValueError("A creation receipt requires a definitive resource ID.")

    fingerprint = {"id": resource_id}
    created_at = _created_at(resource.get("createdAt"))
    if created_at is not None:
        fingerprint["createdAt"] = created_at
    # Scheduling fields can be nullable or change representation during provisioning.
    # Preserve them for diagnosis; they never revoke authority over a receipted ID.
    diagnostics = {
        field: resource[field]
        for field in (("cloud", "dataCenterId") if kind == "pod" else ("dataCenter", "type"))
        if field in resource
    }
    if kind == "pod" and isinstance(resource.get("gpu"), dict):
        diagnostics["gpu"] = {
            k: resource["gpu"][k] for k in ("id", "count") if k in resource["gpu"]
        }
    return {
        "id": resource["id"],
        "job_id": job["job_id"],
        "kind": kind,
        "established_by": established_by,
        "fingerprint": fingerprint,
        "diagnostics": diagnostics,
        "recorded_utc": utc(),
    }


def receipt_owns(resource, job, receipt, kind):
    """Mutable names/tags discover ownership; a verified receipt preserves it."""
    prior = job["prior_pod_ids"] if kind == "pod" else job["prior_volume_ids"]
    if resource.get("id") in prior:
        return False
    if not receipt or not receipt.get("job_id"):
        return owned_pod(resource, job) if kind == "pod" else owned_volume(resource, job)
    if (
        receipt.get("job_id") != job["job_id"]
        or receipt.get("kind") != kind
        or receipt.get("id") != resource.get("id")
        or receipt.get("established_by") not in ("create_response", "reconciled")
    ):
        return False

    fingerprint = receipt.get("fingerprint", {})
    if not isinstance(fingerprint, dict) or fingerprint.get("id") != resource.get("id"):
        return False
    if "createdAt" in fingerprint and resource.get("createdAt") is not None:
        expected = _created_at(fingerprint["createdAt"])
        if expected is None or _created_at(resource.get("createdAt")) != expected:
            return False
    # Also accept old receipts that put diagnostic scheduling fields in fingerprint.
    return True
