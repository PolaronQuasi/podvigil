"""Conservative recovery of single-attempt, ambiguous creation requests.

Audit records supply positive identity evidence, never proof from an empty list.
Manual handoff ends local monitoring only; it cannot authorize resource deletion.
"""

from pathlib import Path
import json
import re
import urllib.parse

from ..podrt.records import best_effort_save, read, save, utc
from .ownership import creation_receipt, owned_pod, owned_volume


def resource_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid resource ID.")
    return value


def reconcile_from_audit(api, job, folder, kind):
    """Use exact create details when available; omit all secret-bearing values.

    The provider's audit value has no guaranteed shape. Unsupported/partial
    entries remain inconclusive. A name or a search match alone is insufficient.
    """
    if kind not in ("pod", "volume"):
        raise ValueError("Unknown resource kind.")
    folder = Path(folder)
    if not (folder / (kind + "-dispatch.json")).is_file():
        return None
    receipt_path = folder / (kind + "-receipt.json")
    existing = read(receipt_path)
    if existing:
        return existing["id"]

    name = job["pod_name" if kind == "pod" else "volume_name"]
    query = urllib.parse.urlencode({"actions": "create", "search": name})
    entries = api.listing("/audit-logs?" + query, "auditLogs")
    if not isinstance(entries, list):
        raise RuntimeError("Incomplete audit response; creation remains unresolved.")

    matches = owned_pod if kind == "pod" else owned_volume
    types = {"pod"} if kind == "pod" else {"networkVolume", "network-volume"}
    candidates = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or entry.get("action") != "create"
            or entry.get("resourceType") not in types
        ):
            continue
        try:
            rid = resource_id(entry.get("resourceId"))
            raw = entry.get("value")
            if not isinstance(raw, str) or len(raw) > 262144:
                continue
            value = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or value.get("id", rid) != rid:
            continue
        candidate = {**value, "id": rid}
        try:
            belongs = matches(candidate, job)
        except (TypeError, AttributeError):
            belongs = False
        if belongs:
            candidates[rid] = candidate

    if len(candidates) > 1:
        raise RuntimeError(
            "Multiple matching creation audit records; operator inspection required."
        )
    if not candidates:
        best_effort_save(
            folder / (kind + "-audit-observation.json"),
            {
                "job_id": job["job_id"],
                "state": "inconclusive",
                "utc": utc(),
                "message": "No complete matching create identity; absence was not inferred.",
            },
        )
        return None

    rid, candidate = next(iter(candidates.items()))
    # A currently visible resource must independently agree with the identity.
    path = ("/pods/" if kind == "pod" else "/network-volumes/") + rid
    current = api.get_or_none(path)
    if current is not None and not matches(current, job):
        raise PermissionError("Creation audit conflicts with the current resource identity.")
    receipt = creation_receipt(current or candidate, job, kind, "reconciled")
    receipt["evidence_source"] = "authenticated_provider_create_audit"
    save(receipt_path, receipt)
    save(
        folder / (kind + "-audit-observation.json"),
        {
            "job_id": job["job_id"],
            "id": rid,
            "state": "identity_resolved",
            "utc": utc(),
            "current_resource_visible": current is not None,
            "deletion_verified": False,
            "raw_audit_values_saved": False,
        },
    )
    return rid


def active_handoff(folder, job):
    try:
        record = read(Path(folder) / "operator-handoff.json", {})
    except (OSError, ValueError):
        return False
    return bool(
        record.get("job_id") == job["job_id"]
        and record.get("active") is True
        and record.get("billing_unverified_acknowledged") is True
        and record.get("cleanup_verified") is False
    )


def validate_handoff_note(note, acknowledged):
    if acknowledged is not True:
        raise ValueError(
            "Explicit acknowledgement of unverified compute/storage billing is required."
        )
    if not isinstance(note, str) or not note.strip() or len(note) > 1000:
        raise ValueError("Provide a short, nonempty handoff note without credentials.")
    # Never save a pasted credential or authorization string in a public ledger.
    if re.search(r"rpa_|bearer\s|secret\s*[:=]|api[_ -]?key\s*[:=]", note, re.I):
        raise ValueError("The handoff note must not contain credentials.")
    return note.strip()


def record_handoff(folder, job, note, acknowledged):
    """An explicit operator takeover is not a successful cleanup receipt."""
    note = validate_handoff_note(note, acknowledged)
    record = {
        "job_id": job["job_id"],
        "active": True,
        "billing_unverified_acknowledged": True,
        "cleanup_verified": False,
        "compute_termination_verified": False,
        "storage_deletion_verified": False,
        "note": note,
        "utc": utc(),
        "next": "Operator owns reconciliation; use resume-monitoring or recover with this ledger.",
    }
    save(Path(folder) / "operator-handoff.json", record)
    return record
