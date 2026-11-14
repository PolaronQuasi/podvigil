"""What a ledger proves about the job's Pod and volume: the final state lines, the status and list commands."""

from pathlib import Path
import time

from .controller.creation import rejected_dispatch
from .controller.ledger import epoch_utc, job_folders, ledger_folder, status_read
from .controller.ownership import deletion_verified
from .settings import program
from .cost import compute_estimate
from .errors import public_message


def resource_state(folder, job, kind):
    """What this ledger proves about the job's Pod or volume. Reads only; 'exists' includes 'deletion unverified'."""
    receipt = status_read(folder / (kind + "-receipt.json"), {})
    rid = receipt.get("id") if isinstance(receipt, dict) else None
    if isinstance(rid, str) and rid:
        if deletion_verified(status_read(folder / (kind + "-deleted.json")), rid):
            return {"state": "deleted", "id": rid}
        return {"state": "exists", "id": rid}
    if (folder / (kind + "-dispatch.json")).exists():
        if (
            isinstance(job, dict)
            and isinstance(job.get("job_id"), str)
            and rejected_dispatch(folder, job, kind)
        ):
            rejected = status_read(folder / (kind + "-rejected.json"), {})
            return {"state": "rejected", "http_status": rejected.get("status") or None}
        return {"state": "unknown"}
    # The dispatch record is written before every create request, so without it none was sent.
    return {"state": "not_created"}


NOTHING_CREATED = [
    "Pod: none was created.",
    "Volume: none was created. Nothing from this run remains.",
]

# A watchdog heard from within this time still guards the job.
WATCHDOG_HEARD_SECONDS = 300
TERMINAL_PHASES = frozenset({"finished", "failed", "recovery failed"})


def watchdog_heard(folder, now=None):
    """True while the job's watchdog was heard from recently: its last loop ended armed a short while ago."""
    state = status_read(Path(folder) / "watchdog.json", {})
    beat = state.get("heartbeat_epoch") if isinstance(state, dict) else None
    return (
        isinstance(state, dict)
        and state.get("state") == "armed"
        and type(beat) in (int, float)
        and 0 <= (time.time() if now is None else now) - beat <= WATCHDOG_HEARD_SECONDS
    )


def finishing_until(folder, job):
    """After a cancel, while the Pod was asked to finish by itself (watchdog.finishing) and the watchdog still
    runs: the UTC time after which the watchdog deletes the Pod if it has not ended. None otherwise."""
    grace = status_read(Path(folder) / "cancel-grace.json", {})
    until = grace.get("until_epoch") if isinstance(grace, dict) else None
    if (
        isinstance(job, dict)
        and isinstance(grace, dict)
        and grace.get("job_id") == job.get("job_id")
        and grace.get("asked") is True
        and type(until) in (int, float)
        and time.time() < until
        and not (Path(folder) / "summary.json").exists()
        and watchdog_heard(folder)
    ):
        return epoch_utc(until)
    return None


def cancel_pending(folder, job):
    """True in the moment after a cancel of a released job, before its live watchdog has acted on the request
    (no cancel-grace.json yet). recover now would delete the Pod before it is asked to publish."""
    folder = Path(folder)
    request = status_read(folder / "cancel-request.json", {})
    return (
        isinstance(job, dict)
        and isinstance(request, dict)
        and request.get("job_id") == job.get("job_id")
        and (folder / "training-released.json").exists()
        and not (folder / "cancel-grace.json").exists()
        and not (folder / "summary.json").exists()
        and watchdog_heard(folder)
    )


def state_lines(folder):
    """Two lines that say what exists for this job: first the Pod, then the volume."""
    folder = Path(folder)
    command = program()  # the command the user runs, so the advice can be pasted as it is
    job = status_read(folder / "job.json", {})

    # For a job running under its watchdog, the lines point to cancel and status
    # because recover would stop it.
    guarded = (
        not (folder / "summary.json").exists()
        and not (folder / "stop-request.json").exists()
        and watchdog_heard(folder)
    )

    # While the cancelled Pod finishes on its own, recover would delete it
    # before it publishes.
    finishing = finishing_until(folder, job)

    # Earlier, the cancel may be recorded before the watchdog asks the Pod to finish.
    pending = cancel_pending(folder, job)

    lines = []
    for kind, name in (("pod", "Pod"), ("volume", "Volume")):
        state = resource_state(folder, job, kind)
        rid = state.get("id")
        if state["state"] == "deleted":
            lines.append(f"{name} {rid}: deleted; its absence was verified.")
        elif state["state"] == "exists" and pending:
            lines.append(
                f"{name} {rid}: "
                + (
                    "a cancel was requested; the watchdog is about to ask it to finish "
                    "and publish its outputs. It bills until it ends; its deletion is "
                    "NOT verified yet. Follow: "
                    f"{command} status "
                    if kind == "pod"
                    else "holds the job's inputs and results and bills as storage; the watchdog "
                    f"recovers the results and then deletes it. Follow: {command} status "
                )
                + str(folder)
            )
        elif state["state"] == "exists" and finishing:
            lines.append(
                f"{name} {rid}: "
                + (
                    "was asked to finish and publish its outputs, and bills until it ends; "
                    "its deletion is NOT verified yet. The watchdog deletes it if it has not "
                    "ended by " + finishing + ". Follow: "
                    f"{command} status "
                    if kind == "pod"
                    else "holds the job's inputs and results and bills as storage; the watchdog "
                    f"recovers the results and then deletes it. Follow: {command} status "
                )
                + str(folder)
            )
        elif state["state"] == "exists" and guarded:
            deadline = epoch_utc(job.get("deadline_epoch")) if isinstance(job, dict) else None
            lines.append(
                f"{name} {rid}: "
                + (
                    "runs and bills; the watchdog guards it and deletes it by the deadline "
                    + (deadline or "(set when the Pod is requested)")
                    + " at the latest. Stop it early: "
                    f"{command} cancel "
                    if kind == "pod"
                    else "holds the job's inputs and results and bills as storage; the watchdog "
                    f"recovers the results and then deletes it. Follow: {command} status "
                )
                + str(folder)
            )
        elif state["state"] == "exists":
            lines.append(
                f"{name} {rid}: "
                + (
                    f"may still exist and bill; its deletion is NOT verified. Run: {command} "
                    "recover "
                    if kind == "pod"
                    else "still exists and is billed as storage until deleted. "
                    f"Results and cleanup: {command} recover "
                )
                + str(folder)
            )
        elif state["state"] == "rejected":
            status = state.get("http_status")
            lines.append(
                f"{name}: none was created ("
                + (
                    f"Runpod refused the request with HTTP {status})."
                    if status
                    else "the request was not sent)."
                )
            )
        elif state["state"] == "unknown":
            lines.append(
                f"{name}: the create request's outcome is unknown, "
                f"so a {name.lower()} may exist and bill. Check: {command} status " + str(folder)
            )
        else:
            lines.append(f"{name}: none was created.")
    if all(
        resource_state(folder, job, kind)["state"] in ("deleted", "rejected", "not_created")
        for kind in ("pod", "volume")
    ):
        lines[-1] += " Nothing from this job remains."
    return lines


def status(folder):
    """Read-only summary of one job ledger: phase, Pod and volume, deadlines, price quote and outcome.

    It takes no lock, keeps no file open and creates nothing, so it is safe while the job runs.
    """
    folder = ledger_folder(folder)
    job = status_read(folder / "job.json", {})
    job = job if isinstance(job, dict) else {}
    cfg = job.get("config") if isinstance(job.get("config"), dict) else {}
    summary, failure = (
        status_read(folder / "summary.json"),
        status_read(folder / "launch-failure.json"),
    )
    handoff = status_read(folder / "recovery-handoff.json")
    watch, isolation = (
        status_read(folder / "watchdog.json"),
        status_read(folder / "watchdog-isolation.json"),
    )
    pod, volume = resource_state(folder, job, "pod"), resource_state(folder, job, "volume")
    if isinstance(summary, dict):
        phase = "finished"
    elif isinstance(failure, dict):
        phase = "failed"
    elif isinstance(handoff, dict) and handoff.get("state") in ("complete", "storage_retained"):
        phase = "finished" if handoff["state"] == "complete" else "recovery failed"
    elif pod["state"] == "deleted":
        phase = "recovering results"
    elif (folder / "stop-request.json").exists() and pod["state"] in ("exists", "unknown"):
        phase = "stopping"
    elif pod["state"] == "exists":
        phase = "running" if (folder / "training-released.json").exists() else "starting"
    elif pod["state"] == "unknown":
        phase = "pod creation unresolved"
    else:
        phase = "preparing"

    now = time.time()
    deadline = job.get("deadline_epoch")
    quote = job.get("quote") if isinstance(job.get("quote"), dict) else None
    estimate = compute_estimate(folder, job, pod, now)
    if estimate is not None:
        estimate = {"usd": estimate["usd"], "basis": estimate["basis"]}

    watchdog_state = None
    if isinstance(watch, dict):
        beat = watch.get("heartbeat_epoch")
        watchdog_state = {
            "state": watch.get("state"),
            "pid": watch.get("pid"),
            "message": watch.get("message"),
            "seconds_since_heartbeat": round(now - beat, 1) if type(beat) in (int, float) else None,
        }

    return {
        "job_id": job.get("job_id"),
        "ledger": str(folder),
        "phase": phase,
        "ended": phase in TERMINAL_PHASES,
        "detached": (folder / "detached.json").exists(),
        "gpu": cfg.get("gpu_id"),
        "gpu_count": cfg.get("gpu_count"),
        "data_center": cfg.get("data_center_id"),
        "pod": pod,
        "volume": volume,
        "deadline_utc": epoch_utc(deadline),
        "seconds_to_deadline": (
            round(deadline - now)
            if epoch_utc(deadline) and phase not in TERMINAL_PHASES | {"recovering results"}
            else None
        ),
        "startup_deadline_utc": epoch_utc(job.get("startup_deadline_epoch")),
        "preparation_deadline_utc": epoch_utc(job.get("preparation_deadline_epoch")),
        "price_quote": quote,
        "compute_ceiling_usd_per_hour": cfg.get("max_compute_usd_per_hour"),
        "compute_budget_usd": cfg.get("max_compute_usd"),
        "estimated_compute_cost": estimate,
        "training_outcome": summary.get("training_outcome") if isinstance(summary, dict) else None,
        "failure": (
            {
                name: failure.get(name)
                for name in ("phase", "error_type", "message", "http_status", "utc")
                if name in failure
            }
            if isinstance(failure, dict)
            else None
        ),
        "watchdog": watchdog_state,
        "recovery_handoff": (
            {
                key: handoff[key]
                for key in ("state", "error_type", "message", "utc")
                if key in handoff
            }
            if isinstance(handoff, dict)
            else None
        ),
        "watchdog_credentials": (
            isolation.get("credentials") if isinstance(isolation, dict) else None
        ),
    }


def job_list(jobs=None):
    """The `list` command: one row per job ledger in the state folder, newest first. Reads only."""
    rows = []
    for folder in job_folders(jobs):
        job = status_read(folder / "job.json", {})
        created = job.get("created_utc") if isinstance(job, dict) else None
        try:
            item = status(folder)
        except Exception as exc:
            rows.append(
                {
                    "job_id": folder.name,
                    "created_utc": created,
                    "phase": "unreadable",
                    "training_outcome": None,
                    "pod": None,
                    "volume": None,
                    "gpu": None,
                    "data_center": None,
                    "ledger": str(folder),
                    "error": public_message(exc),
                }
            )
            continue
        rows.append(
            {
                "job_id": item["job_id"] or folder.name,
                "created_utc": created,
                "phase": item["phase"],
                "training_outcome": item["training_outcome"],
                "pod": item["pod"]["state"],
                "volume": item["volume"]["state"],
                "gpu": item["gpu"],
                "data_center": item["data_center"],
                "ledger": item["ledger"],
            }
        )
    return rows


def list_lines(rows, jobs):
    """The job list as a table for people."""
    if not rows:
        return ["No jobs in " + str(jobs) + "."]
    lines = [
        "Jobs in " + str(jobs) + ", newest first:",
        (
            f"  {'ID':12}  {'CREATED (UTC)':16}  {'PHASE':23}  {'OUTCOME':15}  "
            f"{'POD':11}  {'VOLUME':11}  GPU"
        ),
    ]
    for row in rows:
        created = (row["created_utc"] or "?")[:16].replace("T", " ")
        lines.append(
            f"  {row['job_id'][:12]:12}  {created:16}  {row['phase']:23}  "
            f"{str(row['training_outcome'] or '-'):15}  {str(row['pod'] or '?'):11}  "
            f"{str(row['volume'] or '?'):11}  {row['gpu'] or '?'}"
        )
    lines.append(
        "Name a job in any job command by its ledger folder, `latest`, or the start of its ID "
        "(4 or more characters)."
    )
    return lines
