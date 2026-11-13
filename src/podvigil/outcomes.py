"""Named outcomes and their exit codes: the one table, stable from release to release.

Every command ends with one of these. The process exits with the code; the result JSON on standard output
carries both, as "outcome" and "exit_code". A name means the same for every command. Codes 0 to 4 and 130 kept
the meaning they had before the table existed.
"""

from .controller.ledger import status_read

OUTCOMES = (
    # name                code  meaning
    (
        "success",
        0,
        "the command did what was asked; for launch: the workload succeeded, its results "
        "are verified and nothing of the job remains",
    ),
    (
        "error",
        1,
        "the command failed and no other outcome applies; the closing lines say what exists",
    ),
    (
        "workload_failed",
        2,
        "the job ended and its cleanup is verified, but the workload's outcome is not "
        "success (training_failed, time_limit, not_started, ...); also run-local",
    ),
    (
        "probe_failed",
        3,
        "launch: the workload succeeded, but the self-termination probe did not pass",
    ),
    ("no_capacity", 4, "no candidate had Secure Cloud capacity now; nothing of the job remains"),
    (
        "rejected",
        5,
        "Runpod refused a create request for another reason; nothing of the job remains",
    ),
    (
        "ambiguous",
        6,
        "a create request's outcome is unknown: a Pod or volume may exist and bill; the "
        "watchdog keeps reconciling",
    ),
    ("cleanup_unverified", 7, "a Pod may still exist and bill: its deletion is not verified"),
    (
        "storage_retained",
        8,
        "compute has ended, but the volume is kept and billed as storage: its results are "
        "not all recovered (recover, export-partial)",
    ),
    ("detached", 9, "launch --detach returned while the workload runs; the watchdog guards it"),
    (
        "cancelled",
        10,
        "launch: the job was stopped by cancel before its workload finished; what the "
        "workload published is recovered, and nothing remains",
    ),
    (
        "pending",
        11,
        "cancel: the stop request is written; the Pod's deletion is not verified yet, "
        "or the watchdog still recovers the results",
    ),
    (
        "outputs_lost",
        12,
        "the workload ran, but it never published its outputs and they are lost with "
        "the Pod; the Pod and the volume are deleted, the Pod's records kept",
    ),
    (
        "partial_outputs",
        13,
        "recover --accept-partial: the workload's outputs were incomplete; every "
        "object on the volume was copied to the ledger with its bytes checked, then "
        "the volume was deleted; the Pod's deletion is verified",
    ),
    (
        "usage_error",
        64,
        "the command line is wrong; the parser printed the usage on standard error",
    ),
    (
        "interrupted",
        130,
        "interrupted with Ctrl+C; check the job before assuming billing has ended",
    ),
)
EXIT_CODES = {name: code for name, code, _ in OUTCOMES}
MEANINGS = {name: meaning for name, _, meaning in OUTCOMES}

# When these commands fail, the outcome depends on which resources remain.
ACTING = frozenset(
    {
        "launch",
        "recover",
        "reattach",
        "cancel",
        "export-partial",
        "export-unresolved",
        "resume-monitoring",
        "watchdog",
    }
)
STOPPED = ("deleted", "not_created", "rejected")


def remains(folder):
    """The outcome a failed command's ledger implies, or None when nothing of the job remains."""
    from .status import resource_state

    job = status_read(folder / "job.json", {})
    job = job if isinstance(job, dict) else {}
    pod, volume = resource_state(folder, job, "pod"), resource_state(folder, job, "volume")
    if "unknown" in (pod["state"], volume["state"]):
        return "ambiguous"
    if pod["state"] == "exists":
        return "cleanup_unverified"
    if volume["state"] == "exists":
        return "storage_retained"
    return None


def of_result(action, result, configs=None):
    """The outcome of a command that returned a result."""
    if action in ("launch", "recover", "reattach") and result.get("training_outcome") in (
        "outputs_lost",
        "partial_outputs",
    ):
        return result["training_outcome"]
    if action == "launch":
        if result.get("outcome") == "detached":
            return "detached"
        if result.get("training_outcome") == "cancelled":
            # The Pod stopped and published on a cancel.
            return "cancelled"
        if result.get("training_outcome") != "success":
            return "workload_failed"
        if (
            configs
            and configs[0].get("self_termination_probe")
            and not (result.get("self_termination_probe") or {}).get("passed")
        ):
            return "probe_failed"
        return "success"
    if action == "cancel":
        if (
            result.get("state") == "compute_stopped"
            and result.get("training_outcome") == "outputs_lost"
        ):
            return "outputs_lost"
        return {
            "compute_stopped": "success",
            "already_finished": "success",
            "storage_retained": "storage_retained",
        }.get(result.get("state"), "pending")
    if action == "run-local":
        if result.get("workload_outcome") == "interrupted":
            return "interrupted"
        return (
            "success"
            if result.get("workload_outcome") == "success"
            and not result.get("required_artifacts_missing")
            else "workload_failed"
        )
    return "success"


def of_failure(action, exc, folder, no_capacity):
    """The outcome of a command that failed with `exc`; folder is its job ledger or None."""
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    if action in ACTING and folder is not None:
        left = remains(folder)
        if left is not None:
            return left
        if action == "launch" and (folder / "cancel-request.json").exists():
            return "cancelled"
    if no_capacity:
        return "no_capacity"
    if action == "launch" and folder is not None:
        from .status import resource_state

        job = status_read(folder / "job.json", {})
        job = job if isinstance(job, dict) else {}
        if "rejected" in (
            resource_state(folder, job, "pod")["state"],
            resource_state(folder, job, "volume")["state"],
        ):
            return "rejected"
    return "error"
