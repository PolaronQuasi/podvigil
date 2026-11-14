"""Plan and launch: one job on one GPU and data center, the candidate fallback, startup supervision."""

from pathlib import Path
import hashlib
import hmac
import math
import os
import sys
import threading
import time
import uuid

from .. import LEDGER_SCHEMA, __version__, events
from ..credentials import load_key
from ..errors import public_error, public_message
from ..jobspec import check_cuda_floor, validate
from ..outcomes import of_failure
from ..podrt.api import ApiError, ApiPacingDeferred
from ..podrt.records import best_effort_save, digest, save, utc
from ..policy import CLOUD
from ..provider.catalog import CAPACITY_DETAIL, check_catalog, NoCapacity
from ..provider.client import configured_api, provider_now
from ..settings import jobs_dir, real_path
from ..status import resource_state
from ..storage import Storage
from .cleanup import cleanup_pod
from .creation import managed_create, record_stop, request_cancel, stop_requested
from .ledger import epoch_utc, status_read
from .ownership import owned_volume
from .recovery import recover
from .request import INPUT_MANIFEST, assert_pod, check_request_budget, input_manifest, payload
from .watchdog import spawn_watchdog


def runtime_limit(cfg):
    limit = min(
        cfg["max_runtime_seconds"],
        math.floor(3600 * cfg["max_compute_usd"] / cfg["max_compute_usd_per_hour"]),
    )
    if limit <= cfg["startup_timeout_seconds"] + cfg["finalize_grace_seconds"]:
        raise ValueError(
            "Compute budget leaves no training time after startup/finalization allowances."
        )
    return limit


def input_sizes(cfg):
    return [
        {"remote": item["remote"], "size": Path(item["local"]).stat().st_size}
        for item in cfg["inputs"]
    ]


def plan(cfg):
    # Check before hashing so an oversized request fails fast.
    budget = check_request_budget(cfg, input_sizes(cfg))
    files = [
        {
            "remote": item["remote"],
            "local": item["local"],
            "size": Path(item["local"]).stat().st_size,
            "sha256": digest(item["local"]),
        }
        for item in cfg["inputs"]
    ]
    return {
        "cloud": CLOUD,
        "community_fallback": False,
        "gpu": cfg["gpu_id"],
        "gpu_count": cfg["gpu_count"],
        "image": cfg["image"],
        "data_center": cfg["data_center_id"],
        "maximum_pod_lifetime_seconds": runtime_limit(cfg),
        "maximum_compute_rate_usd_per_hour": cfg["max_compute_usd_per_hour"],
        "compute_budget_usd": cfg["max_compute_usd"],
        "new_network_volume_gb": cfg["volume_gb"],
        "cleanup": "Terminate Pod, download and hash-check results, then delete owned volume.",
        "storage_on_backup_failure": "Retain volume and report remaining storage billing.",
        "limits": (
            "API outages can delay termination. Storage and taxes are outside the compute budget."
        ),
        "pod_request": {
            **budget,
            "note": "Measured without the Pods already in the account; "
            "launch adds about 25 bytes for each and checks again before creating anything.",
        },
        "inputs": {
            "files": len(files),
            "bytes": sum(item["size"] for item in files),
            "list": files,
            "note": "Explicit list after expanding directory and pattern entries; launch hashes "
            "the files again and the Pod checks them before the workload starts.",
        },
        "paid_resources_created": False,
    }


def plan_job(configs):
    """The plan of the first candidate, plus the order in which launch tries the others."""
    result = plan(configs[0])
    if len(configs) > 1:
        result["candidates"] = [
            {
                "order": index,
                "gpu": cfg["gpu_id"],
                "data_center": cfg["data_center_id"],
                "maximum_compute_rate_usd_per_hour": cfg["max_compute_usd_per_hour"],
                "compute_budget_usd": cfg["max_compute_usd"],
                "maximum_pod_lifetime_seconds": runtime_limit(cfg),
                "pod_request_bytes": check_request_budget(cfg, input_sizes(cfg))["bytes"],
            }
            for index, cfg in enumerate(configs, 1)
        ]
        result["fallback"] = (
            "The next candidate is tried only after a definite no-capacity answer and a "
            "verified volume deletion. A timeout, a 5xx or any other error stops the launch."
        )
    return result


def heartbeat_worker(folder, stop):
    while not stop.is_set():
        best_effort_save(folder / "controller-heartbeat.json", {"epoch": time.time()})
        stop.wait(5)


def launch(cfg, ledgers=None, *, detach=False):
    """Run one job on one GPU and data center. ledgers, if given, receives the job folder once it exists.

    With detach, return once the workload is released and the watchdog can guard it alone (supervise): it
    enforces the same deadline and recovers the results.
    """
    cfg = check_cuda_floor(validate(cfg))
    if detach and cfg["self_termination_probe"]:
        raise ValueError(
            "launch --detach cannot run the self-termination probe (self_termination_probe): its "
            "result is known only at the end of the job. Nothing was created."
        )
    limit = runtime_limit(cfg)

    # Fail before paid creation if backup credentials or the dependency are missing.
    storage = Storage(cfg["data_center_id"])
    account_key = load_key()
    api = configured_api(account_key)
    quote = check_catalog(api, cfg)
    provider_now(api)  # fail before paid creation if the provider clock is unavailable
    prior_pods = [p["id"] for p in api.listing("/pods", "pods")]
    prior_volumes = [v["id"] for v in api.listing("/network-volumes", "networkVolumes")]

    # The Pod request carries the Pod code and settings. The input list goes to the
    # volume as a manifest. Refuse an oversized request before creating resources.
    check_request_budget(cfg, input_sizes(cfg), prior_pods)
    inputs = [
        {
            "remote": item["remote"],
            "size": Path(item["local"]).stat().st_size,
            "sha256": digest(item["local"]),
        }
        for item in cfg["inputs"]
    ]

    job_id = uuid.uuid4().hex
    nominal = jobs_dir() / job_id
    nominal.mkdir(parents=True, exist_ok=False)
    # The watchdog runs outside this process's tree and needs the ledger's real path
    # (settings.real_path), for example when this shell runs inside a packaged Windows app.
    folder = real_path(nominal)
    if ledgers is not None:
        ledgers.append(folder)

    job = {
        "job_id": job_id,
        "runner_version": __version__,
        "ledger_schema": LEDGER_SCHEMA,
        "created_utc": utc(),
        "config": cfg,
        "pod_name": "wondomon-" + job_id,
        "volume_name": "wondomon-results-" + job_id,
        "prior_pod_ids": prior_pods,
        "prior_volume_ids": prior_volumes,
        "inputs": inputs,
        "quote": quote,
        "preparation_deadline_epoch": time.time() + cfg["preparation_timeout_seconds"],
        "auth_fingerprint_salt": uuid.uuid4().hex,
    }
    job["auth_key_fingerprint"] = hmac.new(
        job["auth_fingerprint_salt"].encode(), account_key.encode(), hashlib.sha256
    ).hexdigest()
    save(folder / "job.json", job)
    events.record(
        folder,
        "job_created",
        job_id=job_id,
        gpu=cfg["gpu_id"],
        data_center=cfg["data_center_id"],
        runner_version=__version__,
    )
    for kind in ("pod", "volume"):
        save(
            folder / (kind + "-creation-state.json"),
            {"job_id": job_id, "state": "never_dispatched", "utc": utc()},
        )

    stop = threading.Event()
    save(folder / "controller-heartbeat.json", {"epoch": time.time()})
    heartbeat = threading.Thread(target=heartbeat_worker, args=(folder, stop), daemon=True)
    heartbeat.start()
    monitor = None
    phase = "watchdog_arming"
    try:
        print("Job ledger: " + str(folder), file=sys.stderr, flush=True)
        # Arming requires proof that the watchdog resolved the same credentials as this controller.
        # spawn_watchdog resolves the original ledger path and names both paths if arming fails.
        monitor = spawn_watchdog(nominal)
        events.record(folder, "watchdog_armed", watchdog_pid=getattr(monitor, "pid", None))
        stop_requested(folder)

        phase = "volume_create"
        volume = managed_create(
            api,
            job,
            folder,
            "volume",
            {
                "name": job["volume_name"],
                "dataCenter": cfg["data_center_id"],
                "size": cfg["volume_gb"],
                "type": "STANDARD",
            },
            {"name": job["volume_name"], "utc": utc()},
        )
        if not owned_volume(volume, job):
            raise RuntimeError("Returned volume does not match the creation intent.")

        # Test actual volume access and transfer source/data before paying for a GPU.
        # A new volume can take some seconds to become reachable through S3; the first
        # write is retried for a bounded time on the "not there yet" answers only.
        phase = "volume_first_write"
        first_access = time.monotonic() + 90
        while True:
            try:
                storage.put_json(volume["id"], "managed-owner.json", {"job_id": job_id})
                break
            except Exception as exc:
                code = str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code"))
                if code not in ("AccessDenied", "NoSuchBucket") or time.monotonic() >= first_access:
                    raise
                stop_requested(folder)
                time.sleep(3)
        if storage.get_json(volume["id"], "managed-owner.json") != {"job_id": job_id}:
            raise RuntimeError("Storage round-trip check failed.")

        phase = "input_upload"
        for item in cfg["inputs"]:
            stop_requested(folder)
            from ..pathsafe import validate_input_source

            validate_input_source(item["local"])
            storage.upload(volume["id"], "inputs/" + item["remote"], item["local"])
        events.record(
            folder, "inputs_uploaded", files=len(inputs), bytes=sum(item["size"] for item in inputs)
        )

        phase = "input_manifest"
        stop_requested(folder)
        raw, record = input_manifest(job_id, inputs)
        job["input_manifest"] = {**record, "key": "jobs/" + job_id + "/" + INPUT_MANIFEST}
        # Save first so every later audit knows the manifest's exact bytes.
        save(folder / "job.json", job)
        storage.put_bytes(volume["id"], job["input_manifest"]["key"], raw)
        if storage.object_digest(volume["id"], job["input_manifest"]["key"], len(raw)) != {
            "size": record["size"],
            "sha256": record["sha256"],
        }:
            raise RuntimeError("The input manifest on the volume does not read back as written.")
        stop_requested(folder)
        if monitor.poll() is not None:
            raise RuntimeError("Watchdog exited before Pod creation.")

        phase = "capacity_recheck"
        check_catalog(api, cfg)  # refresh capacity, price and provider clock after uploads
        now = provider_now(api)
        job.update(
            deadline_epoch=now + limit,
            startup_deadline_epoch=now + cfg["startup_timeout_seconds"],
        )
        save(folder / "job.json", job)

        phase = "pod_create"
        body = payload(job, volume["id"])
        stop_requested(folder)
        pod = managed_create(
            api,
            job,
            folder,
            "pod",
            body,
            {
                "name": job["pod_name"],
                "deadline_epoch": job["deadline_epoch"],
                "startup_deadline_epoch": job["startup_deadline_epoch"],
                "utc": utc(),
            },
        )
        assert_pod(pod, job)

        phase = "supervise"
        if (
            supervise(folder, job, api, storage, monitor, pod["id"], volume["id"], detach=detach)
            == "detached"
        ):
            return detached(folder, job, monitor, pod["id"], volume["id"])
        phase = "recover"
        return recover(folder, api, storage)
    except BaseException as exc:
        if (
            isinstance(exc, KeyboardInterrupt)
            and (folder / "training-released.json").exists()
            and not (folder / "pod-deleted.json").exists()
            and monitor is not None
            and monitor.poll() is None
        ):
            # Ctrl+C on a running workload acts like cancel. The live watchdog asks the Pod to
            # finish, then deletes it and recovers the results. The controller deletes nothing here.
            request_cancel(folder, job, "interrupted launch")
            best_effort_save(
                folder / "launch-failure.json",
                {
                    "job_id": job_id,
                    "phase": phase,
                    "utc": utc(),
                    "error_type": "KeyboardInterrupt",
                    "outcome": "interrupted",
                    "handed_to_watchdog": True,
                    "cleanup_error": None,
                },
            )
            raise

        record_stop(folder, "controller exit/failure")
        cleanup_error = None
        try:
            cleanup_pod(api, job, folder)
            # A watchdog can die at any point in cleanup. This controller still
            # attempts recovery; its lock serializes it with a surviving watchdog.
            recover(folder, api, storage)
        except Exception as error:
            cleanup_error = public_error(error, [account_key])

        # The reason stays in the ledger. The final lines say what exists (state_lines).
        best_effort_save(
            folder / "launch-failure.json",
            {
                "job_id": job_id,
                "phase": phase,
                "utc": utc(),
                **public_error(exc, [account_key]),
                "outcome": of_failure("launch", exc, folder, definite_no_capacity(exc)),
                "cleanup_error": cleanup_error,
            },
        )
        events.record(
            folder,
            "launch_failed",
            phase=phase,
            **public_error(exc, [account_key]),
            cleanup_error=cleanup_error,
        )
        raise
    finally:
        stop.set()
        heartbeat.join(timeout=2)


def definite_no_capacity(exc):
    """True only for answers that say this candidate cannot be placed now and that created nothing.

    The catalog check found no capacity (NoCapacity), or POST /pods was refused with the documented
    answers for that: HTTP 400 with the "no instances available" detail, or HTTP 403 (no access to the
    GPU pool). A timeout, a 5xx, a 429, a 400 for another reason or any other error is not.
    """
    if isinstance(exc, NoCapacity):
        return True
    if (
        not isinstance(exc, ApiError)
        or isinstance(exc, ApiPacingDeferred)
        or exc.not_sent
        or exc.method != "POST"
        or exc.path != "/pods"
    ):
        return False
    if exc.status == 403:
        return True
    return exc.status == 400 and bool(
        CAPACITY_DETAIL.search(" ".join(t for t in (exc.title, exc.detail) if t))
    )


def attempt_left_nothing(folder, wait_seconds=60, pause=time.sleep):
    """Read the ledger (bounded wait for the watchdog) until it shows no Pod and a verified volume deletion."""
    end = time.monotonic() + wait_seconds
    while True:
        job = status_read(folder / "job.json", {})
        pod, volume = resource_state(folder, job, "pod"), resource_state(folder, job, "volume")
        if pod["state"] in ("not_created", "rejected") and volume["state"] in (
            "not_created",
            "rejected",
            "deleted",
        ):
            return True
        if time.monotonic() >= end:
            return False
        pause(2)


def launch_candidates(configs, ledgers=None, cleanup_wait=60, *, detach=False):
    """Launch on the first candidate, in the job file's order, that has capacity.

    The next candidate is tried only after a definite no-capacity answer (definite_no_capacity) and only
    when the failed attempt's ledger shows no Pod and a verified volume deletion. Every other outcome ends
    the loop as a single launch would; an ambiguous creation is never repeated, here or anywhere.
    """
    ledgers = [] if ledgers is None else ledgers
    for index, cfg in enumerate(configs, 1):
        label = f"candidate {index} of {len(configs)} ({cfg['gpu_id']} in {cfg['data_center_id']})"
        if len(configs) > 1:
            print("Trying " + label + ".", file=sys.stderr, flush=True)
        before = len(ledgers)
        try:
            return launch(cfg, ledgers, detach=detach)
        except Exception as exc:
            if not definite_no_capacity(exc):
                raise
            folder = ledgers[-1] if len(ledgers) > before else None
            if index == len(configs):
                if len(configs) == 1:
                    raise
                raise NoCapacity(
                    f"None of the {len(configs)} candidates had capacity. Last answer: "
                    + public_message(exc)
                ) from exc
            if folder is not None and not attempt_left_nothing(folder, cleanup_wait):
                raise RuntimeError(
                    f"No capacity for {label}, but its volume deletion is not verified, so the "
                    "next candidate is not tried. Ledger: " + str(folder)
                ) from exc
            print(
                f"No capacity for {label}: {public_message(exc)} "
                + (
                    "Nothing was created."
                    if folder is None
                    else "No Pod was created and the volume deletion was verified."
                )
                + " Trying the next candidate.",
                file=sys.stderr,
                flush=True,
            )
    raise ValueError("A launch needs at least one candidate configuration.")


def watchdog_guards(folder, monitor, cfg, now=None):
    """None when the watchdog can be left alone with a released job, else why not: its process must live, its
    last loop must have ended armed a short while ago, and no stop may be requested. The watchdog never reads
    the controller's heartbeat; it enforces the deadline from pod-intent.json and recovers the results itself."""
    now = time.time() if now is None else now
    if monitor is None or monitor.poll() is not None:
        return "the watchdog process is not running"
    if (folder / "stop-request.json").exists():
        return "a stop was requested"
    state = status_read(folder / "watchdog.json", {})
    state = state if isinstance(state, dict) else {}
    if state.get("state") != "armed":
        return "the watchdog's last loop did not end armed"
    beat = state.get("heartbeat_epoch")
    if type(beat) not in (int, float) or not 0 <= now - beat <= max(30, 3 * cfg["poll_seconds"]):
        return "the watchdog has not written its heartbeat recently"
    return None


def detached(folder, job, monitor, pod_id, volume_id):
    """Record the hand-over to the watchdog and say what runs, until when, and how to follow it."""
    record = {
        "job_id": job["job_id"],
        "utc": utc(),
        "controller_pid": os.getpid(),
        "watchdog_pid": getattr(monitor, "pid", None),
        "deadline_epoch": job["deadline_epoch"],
    }
    save(folder / "detached.json", record)
    events.record(
        folder,
        "detached",
        watchdog_pid=record["watchdog_pid"],
        deadline_utc=epoch_utc(job["deadline_epoch"]),
    )
    return {
        "job_id": job["job_id"],
        "outcome": "detached",
        "ledger": str(folder),
        "pod_id": pod_id,
        "volume_id": volume_id,
        "watchdog_pid": record["watchdog_pid"],
        "deadline_utc": epoch_utc(job["deadline_epoch"]),
        "note": (
            "The workload runs. The watchdog alone enforces the same deadline: it deletes "
            "the Pod and checks its absence, recovers and checks the results, then deletes "
            "the volume. The Pod's own guard stops it at the deadline too. This computer "
            "must stay on and awake until then. Follow the job with status, logs and cost; "
            "stop it early with cancel."
        ),
    }


def supervise(folder, job, api, storage, monitor, pod_id, volume_id, *, detach=False):
    """Watch the job until its Pod's deletion is recorded, releasing the workload once the Pod is ready.

    Returns "pod_deleted". With detach it returns "detached" instead, as soon as the workload is released and
    the watchdog can be left alone with the job (watchdog_guards); until then it supervises as without detach.
    """
    cfg, job_id = job["config"], job["job_id"]
    released = (folder / "training-released.json").exists()
    end = time.monotonic() + max(0, job["deadline_epoch"] - provider_now(api))
    said = None
    while not (folder / "pod-deleted.json").exists():
        if monitor is not None and monitor.poll() is not None:
            if (folder / "pod-deleted.json").exists():
                break
            raise RuntimeError("Local watchdog exited unexpectedly.")
        if provider_now(api) >= job["deadline_epoch"] or time.monotonic() >= end:
            record_stop(folder, "controller runtime limit")
            cleanup_pod(api, job, folder)
            break

        if not released:
            stop_requested(folder)
            current = status = None
            try:
                current = api.get_or_none("/pods/" + pod_id)
                status = storage.get_json(volume_id, "jobs/" + job_id + "/agent.json")
            except Exception as exc:
                best_effort_save(
                    folder / "startup-observation.json",
                    {"state": "unknown", "error_type": type(exc).__name__, "utc": utc()},
                )
            if current is not None:
                assert_pod(current, job)
            if (
                current is not None
                and status
                and status.get("job_id") == job_id
                and status.get("phase") == "ready"
            ):
                # Nullable provisioning fields may fill in later; keep polling.
                try:
                    assert_pod(current, job, ready=True)
                except RuntimeError:
                    pass
                else:
                    try:
                        storage.put_json(
                            volume_id,
                            "jobs/" + job_id + "/start.json",
                            {"job_id": job_id, "pod_id": pod_id},
                        )
                        save(folder / "training-released.json", {"utc": utc()})
                        events.record(folder, "workload_released", pod_id=pod_id)
                        released = True
                    except Exception:
                        pass  # idempotent release of the same once-only Pod/job
            if not released and provider_now(api) >= job["startup_deadline_epoch"]:
                raise TimeoutError("Pod supervisor did not become ready within startup allowance.")
        elif detach:
            refusal = watchdog_guards(folder, monitor, cfg)
            if refusal is None:
                return "detached"
            if refusal != said:
                said = refusal
                print(
                    "Not detaching yet: "
                    + refusal
                    + ". Supervising until the watchdog can take over.",
                    file=sys.stderr,
                    flush=True,
                )
        time.sleep(cfg["poll_seconds"])
    return "pod_deleted"
