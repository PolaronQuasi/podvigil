"""The detached watchdog: its command, its arming, and the loop that enforces the deadline."""

from pathlib import Path
from contextlib import nullcontext
import math
import os
import sys
import time

from .. import events
from ..credentials import account_api, credential_proof
from ..errors import public_error
from ..jobspec import validate
from ..podrt.records import best_effort_save, exclusive_lock, read, save, utc
from ..policy import CLOUD
from ..provider.client import cleanup_priority, provider_now
from ..settings import program, real_path, watchdog_options
from ..status import state_lines
from ..storage import Storage
from .cleanup import cleanup_pod
from .creation import (
    CANCEL_GRACE_MARGIN,
    CANCEL_REASON,
    never_dispatched,
    record_stop,
    rejected_dispatch,
)
from .creation_recovery import active_handoff, reconcile_from_audit
from .ledger import ledger_folder, status_read, watch_log
from .ownership import accepted_self_delete, locate, ownership_match
from .recovery import recover, recovery_handoff


def watchdog_bootstrap():
    """The watchdog runs this command line in a fresh interpreter: the package is not run by its path.

    It puts the folder that holds the package on the path and calls the package's command line.
    """
    # Pass the controller's command for the advice in the watchdog's log.
    return (
        "import sys;sys.path.insert(0,{!r});from {}.cli import run_cli;"
        "raise SystemExit(run_cli(sys.argv[1:],program={!r}))".format(
            str(Path(__file__).resolve().parents[__name__.count(".")]),
            __name__.split(".")[0],
            program(),
        )
    )


def spawn_watchdog(folder, command=None, require_same_credentials=True, *, nominal=None):
    """Start the detached watchdog. By default it must prove it resolved this controller's credentials;
    reattach and resume-monitoring guard existing resources and accept a recorded difference instead.

    The watchdog runs outside this process's tree, so it gets every path by its real location
    (settings.real_path). `nominal`, the path the ledger was made under (default: `folder` as given), is
    named only in an error."""
    from .watchdog_spawn import spawn

    nominal = folder if nominal is None else nominal
    folder = real_path(folder)
    return spawn(
        folder,
        command
        or [
            sys.executable,
            "-B",
            "-c",
            watchdog_bootstrap(),
            "watchdog",
            *watchdog_options(),
            str(folder),
        ],
        credential_proof=credential_proof,
        require_same_credentials=require_same_credentials,
        nominal=nominal,
    )


class AgentObservation:
    """A read outage contributes zero evidence that a supervisor is frozen."""

    def __init__(self, poll_seconds):
        self.poll = poll_seconds
        self.epoch = None
        self.stale_seconds = 0.0
        self.previous_read = None
        self.seen = False

    def observe(self, status, mono):
        if status is None:
            self.previous_read = None
            return
        epoch = status.get("heartbeat_epoch")
        if not self.seen or epoch != self.epoch:
            self.epoch, self.stale_seconds, self.seen = epoch, 0.0, True
        elif self.previous_read is not None:
            elapsed = mono - self.previous_read
            # Suspend/resume and long request stalls reset the evidence window.
            if elapsed > max(30, self.poll * 3):
                self.stale_seconds = 0.0
            else:
                self.stale_seconds += max(0, min(elapsed, self.poll * 1.5))
        self.previous_read = mono

    def unknown(self):
        self.previous_read = None


def watchdog(folder, api=None, storage=None, pause=time.sleep):
    folder = ledger_folder(folder)
    with exclusive_lock(folder / "watchdog.lock", timeout=1):
        from .watchdog_spawn import arm

        arm(folder, credential_proof)
        watch_log(folder, "watchdog armed")
        events.record(folder, "watchdog_started")
        try:
            return watchdog_loop(folder, api, storage, pause)
        finally:
            try:
                state = status_read(folder / "watchdog.json", {}) or {}
                watch_log(
                    folder,
                    "watchdog stopped; last state "
                    + str(state.get("state"))
                    + ". "
                    + " ".join(state_lines(folder)),
                )
                events.record(folder, "watchdog_stopped", state=state.get("state"))
            except Exception:
                pass


def cancel_grace_seconds(cfg):
    """How long a cancelled running workload may take to stop, publish its outputs and end its Pod."""
    return cfg["finalize_grace_seconds"] + CANCEL_GRACE_MARGIN


def finishing(folder, job, cfg, api, storage, pod_id, story):
    """For a cancel of a released job: the time.monotonic() until which the watchdog waits for the Pod to
    finish by itself, or None when it does not wait (the workload was not released, or the request could
    not be written).

    The first call puts the request on the volume (jobs/<id>/cancel.json, naming this job and this Pod).
    The Pod then stops the workload as at its own time limit, publishes its outputs with outcome
    "cancelled" and deletes itself. The wait is finalize_grace_seconds plus CANCEL_GRACE_MARGIN and never
    passes the deadline; it is kept in cancel-grace.json, so a restarted watchdog never extends it.
    """
    if not (folder / "training-released.json").exists():
        return None
    kept = status_read(folder / "cancel-grace.json", {})
    if isinstance(kept, dict) and kept.get("job_id") == job["job_id"]:
        until = kept.get("until_epoch")
        return (
            time.monotonic() + max(0.0, until - time.time())
            if type(until) in (int, float)
            else None
        )
    grace = cancel_grace_seconds(cfg)
    deadline = job.get("deadline_epoch")
    if type(deadline) in (int, float):
        grace = min(grace, max(0.0, deadline - provider_now(api)))
    until = time.time() + grace
    try:
        volume_id = locate(api, job, folder, "volume")
        (storage or Storage(cfg["data_center_id"])).put_json(
            volume_id,
            "jobs/" + job["job_id"] + "/cancel.json",
            {
                "job_id": job["job_id"],
                "pod_id": pod_id,
                "requested_utc": utc(),
                "grace_seconds": round(grace),
            },
        )
    except Exception:
        best_effort_save(
            folder / "cancel-grace.json",
            {
                "job_id": job["job_id"],
                "pod_id": pod_id,
                "until_epoch": None,
                "asked": False,
                "utc": utc(),
            },
        )
        return None
    save(
        folder / "cancel-grace.json",
        {
            "job_id": job["job_id"],
            "pod_id": pod_id,
            "until_epoch": until,
            "asked": True,
            "utc": utc(),
        },
    )
    story("asked_pod_to_finish", pod_id=pod_id, grace_seconds=round(grace))
    return time.monotonic() + grace


def acknowledge_resume(folder, job):
    """Acknowledge only after this iteration observed that handoff is revoked."""
    request = status_read(folder / "watchdog-resume-request.json", {})
    if not isinstance(request, dict) or request.get("job_id") != job["job_id"]:
        return
    nonce = request.get("nonce")
    if (
        not isinstance(nonce, str)
        or len(nonce) != 32
        or any(c not in "0123456789abcdef" for c in nonce)
    ):
        return
    previous = status_read(folder / "watchdog-resume-ack.json", {})
    if (
        isinstance(previous, dict)
        and previous.get("nonce") == nonce
        and previous.get("job_id") == job["job_id"]
        and previous.get("pid") == os.getpid()
    ):
        return
    best_effort_save(
        folder / "watchdog-resume-ack.json",
        {"job_id": job["job_id"], "nonce": nonce, "pid": os.getpid(), "acknowledged_utc": utc()},
    )


def watchdog_loop(folder, api=None, storage=None, pause=time.sleep):
    job = read(folder / "job.json")
    if not job:
        raise ValueError("Missing job ownership ledger.")
    cfg = validate(job["config"])
    observation = AgentObservation(cfg["poll_seconds"])
    intent = status_read(folder / "pod-intent.json", {})
    mono_deadline = None
    prep_mono = time.monotonic() + max(
        0, job.get("preparation_deadline_epoch", time.time() + 1800) - time.time()
    )
    done_since = None
    latched_reason = None
    said = {}
    recorded = set()

    def story(event, **fields):
        # Each distinct event once: a retried cleanup must not fill events.jsonl.
        key = (event, tuple(sorted((name, str(value)) for name, value in fields.items())))
        if key not in recorded:
            recorded.add(key)
            events.record(folder, event, **fields)

    def say(*parts):
        # Log each distinct line at most once per ten minutes, regardless of polling.
        # Logging can never stop cleanup.
        try:
            text, now = "".join(str(part) for part in parts), time.monotonic()
            if now - said.get(text, -math.inf) >= 600:
                if len(said) > 200:
                    said.clear()
                said[text] = now
                watch_log(folder, text)
        except Exception:
            pass

    while True:
        delay = cfg["poll_seconds"]
        if active_handoff(folder, job):
            best_effort_save(
                folder / "watchdog.json",
                {
                    "pid": os.getpid(),
                    "state": "operator_took_over",
                    "cleanup_verified": False,
                    "message": (
                        "Automatic monitoring ended by explicit operator handoff; "
                        "billing is unverified."
                    ),
                },
            )
            say("monitoring ended: an operator took over; billing is unverified")
            story("monitoring_handed_off")
            return
        acknowledge_resume(folder, job)
        best_effort_save(
            folder / "watchdog.json",
            {"pid": os.getpid(), "heartbeat_epoch": time.time(), "state": "armed"},
        )
        try:
            api = api or account_api(job)  # retain the original account context
            job = status_read(folder / "job.json") or job
            intent = status_read(folder / "pod-intent.json") or intent
            reason = latched_reason or status_read(folder / "stop-request.json", {}).get("reason")
            if not intent and time.monotonic() >= prep_mono:
                reason = reason or "preparation time limit"
            sampled_pod = None
            clock_unknown = hasattr(api, "_clock_anchor") and api._clock_anchor is None
            if reason or (intent and clock_unknown):
                # An explicit stop needs no provider clock. Clock bootstrap and
                # ownership discovery must also be able to spend cleanup's
                # reserved units / use its local fallback if shared state fails.
                with cleanup_priority(api):
                    pod_id = locate(api, job, folder, "pod")
                    if not reason and intent and clock_unknown:
                        if pod_id:
                            sampled_pod = api.get_or_none("/pods/" + pod_id)
                        else:
                            api.listing("/pods", "pods")
            else:
                pod_id = None
            if not reason and intent:
                if mono_deadline is None:
                    mono_deadline = time.monotonic() + max(
                        0, intent["deadline_epoch"] - provider_now(api)
                    )
                if (
                    provider_now(api) >= intent["deadline_epoch"]
                    or time.monotonic() >= mono_deadline
                ):
                    reason = "absolute runtime limit"
            if pod_id is None:
                with cleanup_priority(api) if reason else nullcontext():
                    pod_id = locate(api, job, folder, "pod")

            if pod_id and reason == CANCEL_REASON and not latched_reason:
                until = finishing(folder, job, cfg, api, storage, pod_id, story)
                if until is not None and time.monotonic() < until:
                    # Watch for the Pod to finish during the grace period. The deadline
                    # and the checks for a frozen or failed Pod below still apply.
                    reason = None
                    if intent:
                        if mono_deadline is None:
                            mono_deadline = time.monotonic() + max(
                                0, intent["deadline_epoch"] - provider_now(api)
                            )
                        if (
                            provider_now(api) >= intent["deadline_epoch"]
                            or time.monotonic() >= mono_deadline
                        ):
                            reason = "absolute runtime limit"

            if pod_id and reason:
                record_stop(folder, reason)
                say("stopping Pod ", pod_id, ": ", reason)
                story("stopping_pod", pod_id=pod_id, reason=reason)
                # Reuse the ownership-checked point sample: spending another GET
                # first can exhaust the last reserved unit before DELETE.
                if sampled_pod is None:
                    cleanup_pod(api, job, folder, pod_id)
                else:
                    cleanup_pod(api, job, folder, pod_id, observed_pod=sampled_pod)
                if cfg.get("self_termination_probe"):
                    best_effort_save(
                        folder / "self-termination-probe.json",
                        {"passed": False, "reason": reason, "local_fallback": True, "utc": utc()},
                    )
                best_effort_save(
                    folder / "watchdog.json",
                    {"pid": os.getpid(), "state": "pod_deleted", "reason": reason},
                )
                say("Pod ", pod_id, " deleted and its absence verified; recovering results")
                # Recovery is serialized with the controller. Do not exit on a
                # first stale/unknown heartbeat and leave the volume unattended.
                recovery_handoff(folder, api, storage)
                return

            if pod_id:
                # Query status independently of GET visibility. The Pod may have
                # deleted itself before the controller ever observed "done".
                status = None
                status_read_ok = False
                try:
                    volume_id = locate(api, job, folder, "volume")
                    storage = storage or Storage(cfg["data_center_id"])
                    candidate = storage.get_json(volume_id, "jobs/" + job["job_id"] + "/agent.json")
                    status_read_ok = True
                    if candidate and candidate.get("job_id") == job["job_id"]:
                        status = candidate
                    observation.observe(status, time.monotonic())
                except Exception:
                    observation.unknown()

                pod = api.get_or_none("/pods/" + pod_id)
                terminal = bool(status and status.get("phase") == "done")
                tombstone = bool(
                    pod is not None
                    and pod.get("status") == "TERMINATED"
                    and ownership_match(folder, job, "pod", pod)
                )
                remote = None
                if terminal or tombstone:
                    try:
                        remote = storage.get_json(
                            volume_id, "jobs/" + job["job_id"] + "/control/self-delete.json"
                        )
                    except Exception:
                        pass
                accepted = accepted_self_delete(remote, job, pod_id)
                terminal = terminal or tombstone
                if tombstone or accepted:
                    best_effort_save(
                        folder / "terminal-observed.json",
                        {
                            "job_id": job["job_id"],
                            "pod_id": pod_id,
                            "source": "owned_tombstone" if tombstone else "accepted_self_delete",
                            "ownership_checked": tombstone,
                            "remote_evidence": remote,
                            "utc": utc(),
                        },
                    )

                if pod is not None:
                    try:
                        # Identity already has a creation receipt; metadata may
                        # legitimately be omitted from subsequent GET responses.
                        if not ownership_match(folder, job, "pod", pod):
                            raise RuntimeError("Pod ownership no longer matches")
                        if pod.get("cloud") is not None and pod.get("cloud") != CLOUD:
                            raise RuntimeError("Non-Secure Pod returned")
                        cost = pod.get("cost")
                        if cost is not None and (
                            type(cost) not in (int, float)
                            or not math.isfinite(cost)
                            or cost > cfg["max_compute_usd_per_hour"]
                        ):
                            raise RuntimeError("Pod price invalid/exceeds ceiling")
                    except RuntimeError:
                        reason = "Pod cloud/identity/price validation failed"
                    if pod.get("status") in ("ERROR", "EXITED"):
                        reason = reason or "Pod stopped or failed"

                # A monitoring 404 is UNKNOWN, never a request to delete.
                if terminal:
                    if done_since is None:
                        done_since = time.monotonic()
                    probe = cfg.get("self_termination_probe", False)
                    # Plain "done" describes the workload, not provider deletion.
                    # During the self-delete probe it cannot bypass the grace or
                    # startup visibility guard, even if GET and LIST both lag.
                    if tombstone or (pod is None and accepted):
                        cleanup_pod(api, job, folder, pod_id)
                        if probe:
                            proof = status_read(folder / "pod-deleted.json", {}).get("evidence", {})
                            local_attempt = bool(proof.get("delete_attempts"))
                            best_effort_save(
                                folder / "self-termination-probe.json",
                                {
                                    "passed": accepted and not local_attempt,
                                    "local_fallback": local_attempt,
                                    "observation": (
                                        "Pod became absent/terminated without local DELETE"
                                    ),
                                    "self_deletion_inferred": True,
                                    "remote_evidence": remote,
                                    "reason": (
                                        "Accepted self-DELETE evidence and account absence "
                                        "checked; absence alone does not prove actor."
                                    ),
                                    "utc": utc(),
                                },
                            )
                        best_effort_save(
                            folder / "watchdog.json",
                            {
                                "pid": os.getpid(),
                                "state": "pod_deleted",
                                "reason": "terminal status and absence",
                            },
                        )
                        say(
                            "Pod ", pod_id, " finished and its absence verified; recovering results"
                        )
                        story(
                            "pod_finished",
                            pod_id=pod_id,
                            source="tombstone" if tombstone else "self_delete",
                        )
                        recovery_handoff(folder, api, storage)
                        return
                    if not probe or time.monotonic() - done_since >= cfg.get(
                        "self_termination_grace_seconds", 60
                    ):
                        reason = reason or (
                            "self-termination probe timeout" if probe else "job finished"
                        )
                elif status_read_ok and status is None and not observation.seen:
                    # Release is durably recorded locally. Loss of S3 visibility
                    # after release must not cancel a running healthy workload.
                    if (
                        intent
                        and provider_now(api) >= intent["startup_deadline_epoch"]
                        and not (folder / "training-released.json").exists()
                    ):
                        reason = reason or "startup/agent timeout"

                if observation.stale_seconds >= cfg["agent_stale_seconds"]:
                    reason = reason or "Pod supervisor repeatedly observed frozen"
                if reason:
                    latched_reason = reason
                    continue  # cleanup above is independent of telemetry reads
            elif reason:
                latched_reason = reason
                if not record_stop(folder, reason):
                    raise RuntimeError("Creation fence unavailable; continuing reconciliation.")
                unresolved = [
                    kind
                    for kind in ("pod", "volume")
                    if (folder / (kind + "-dispatch.json")).exists()
                    and not status_read(folder / (kind + "-receipt.json"))
                    and not rejected_dispatch(folder, job, kind)
                ]
                if unresolved:
                    # Reconcile both creations; a lost volume POST response can
                    # otherwise leave billed storage after all guards exit.
                    for kind in unresolved:
                        locate(api, job, folder, kind)
                    now = provider_now(api) if intent else time.time()
                    overdue = now - (
                        intent.get("deadline_epoch") or job.get("preparation_deadline_epoch", now)
                    )
                    delay = 60 if overdue >= 300 else cfg["poll_seconds"]
                    if overdue >= 300:
                        for kind in unresolved:
                            # Optional positive provider evidence can recover an ID
                            # even after the resource vanished from its inventory.
                            try:
                                reconcile_from_audit(api, job, folder, kind)
                            except Exception as exc:
                                best_effort_save(
                                    folder / (kind + "-audit-observation.json"),
                                    {
                                        "job_id": job["job_id"],
                                        "state": "unknown",
                                        "utc": utc(),
                                        "error_type": type(exc).__name__,
                                    },
                                )
                    best_effort_save(
                        folder / "watchdog.json",
                        {
                            "pid": os.getpid(),
                            "state": "operator_resolution_needed"
                            if overdue >= 300
                            else "creation_unresolved",
                            "resources": unresolved,
                            "reason": "Ambiguous creation; no repeat POST or assumed absence.",
                            "poll_seconds": delay,
                        },
                    )
                    say(
                        "creation outcome unknown for ",
                        " and ".join(unresolved),
                        " (stop reason: ",
                        reason,
                        "); reconciling without a repeat request",
                        "; operator resolution needed" if overdue >= 300 else "",
                    )
                else:
                    # Full-volume preservation gate applies even before training.
                    say("no Pod to stop (", reason, "); recovering the volume")
                    story("stopping_without_pod", reason=reason)
                    try:
                        recover(folder, api, storage)
                        state = (
                            "no_pod_created"
                            if never_dispatched(folder, job)
                            or rejected_dispatch(folder, job, "pod")
                            else "complete"
                        )
                        say("recovery finished: ", state)
                    except Exception as exc:
                        state = "storage_retained"
                        error = public_error(exc, [getattr(api, "key", None)])
                        best_effort_save(
                            folder / "recovery-handoff.json",
                            {
                                "state": state,
                                "utc": utc(),
                                "error_type": type(exc).__name__,
                                "error": error,
                                "message": (
                                    "Recovery unresolved; inspect ledger and retain storage."
                                ),
                            },
                        )
                        say(
                            "recovery failed, storage retained: ",
                            error["error_type"],
                            ": ",
                            error.get("message", "(message withheld)"),
                        )
                    best_effort_save(
                        folder / "watchdog.json",
                        {"pid": os.getpid(), "state": state, "reason": reason},
                    )
                    return
        except Exception as exc:
            error = public_error(exc, [getattr(api, "key", None)])
            best_effort_save(
                folder / "watchdog.json",
                {
                    "pid": os.getpid(),
                    "heartbeat_epoch": time.time(),
                    "state": "observation_or_cleanup_unverified",
                    "error_type": type(exc).__name__,
                    "message": error.get("message"),
                },
            )
            say(
                "observation or cleanup not verified, retrying: ",
                error["error_type"],
                ": ",
                error.get("message", "(message withheld)"),
            )
        pause(delay)
