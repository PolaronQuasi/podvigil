"""Linux guard/reaper and its job supervisor. Standard library only.

The bootstrap runs this module through runpy as podrt.agent. The guard (PID 1, or the reaper
of its descendants under an injected init) owns deletion; its child supervisor
never owns the guard's lifetime. Whole-container/host failures still require the
external watchdog. The injected Pod key's REST scope needs live QA.

The workload's layout lives under /workspace on the Pod's own disk. Only root
touches the network volume: inputs are copied from it and checked, the records
the controller polls go to it, and at the end the outputs and manifest do.
"""

from pathlib import Path
from contextlib import nullcontext
import base64
import hashlib
import json
import math
import os
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import unicodedata

from . import bundle as output_bundle
from . import isolation
from .api import Api, ApiError
from .records import (
    VOLUME_MOUNT,
    digest,
    exclusive_lock,
    read,
    save,
    utc,
)
from .teardown import delete_verified


def spec():
    """The job spec: the one <prefix>_JOB_SPEC variable whose <prefix>_JOB_ID, if set, names the same job.

    The controller names both with its own prefix, which this code does not spell; before 0.1.0 it was
    WONDOMON_. None or several is refused.
    """
    found = []
    for name, value in os.environ.items():
        if not name.endswith("_JOB_SPEC"):
            continue
        try:
            job = json.loads(base64.b64decode(value))
        except ValueError:
            continue
        tag = os.environ.get(name[:-4] + "ID")
        if isinstance(job, dict) and (tag is None or tag == job.get("job_id")):
            found.append(job)
    if len(found) != 1:
        raise RuntimeError(
            "Expected one job spec in the Pod's environment; found " + str(len(found)) + "."
        )
    return found[0]


def scoped_api(key):
    api = Api(key)
    configure = getattr(api, "configure_pacing", None)
    if configure is not None:
        configure(max_wait=2, read_retries=2)
    return api


def api_pacing(api, *, cleanup=False, deadline=None):
    pacing = getattr(api, "pacing", None)
    return pacing(cleanup=cleanup, deadline=deadline, max_wait=2) if pacing else nullcontext()


def notice(message):
    try:
        print(message, flush=True)
    except Exception:
        pass  # A broken log sink must not prevent deletion.


def signal_handlers(handler):
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, handler)
    return previous


def restore_signals(previous):
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def safe_job_id(job):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job["job_id"]):
        raise ValueError("Unsafe job identifier.")
    return job["job_id"]


def workspace_root():
    """The workload's workspace: a plain folder on the Pod's own disk."""
    return Path("/workspace")


def job_folder(job):
    return workspace_root() / "jobs" / safe_job_id(job)


def volume_root():
    """The network volume's mount; only root reaches it (isolation.lock_volume)."""
    return Path(VOLUME_MOUNT)


def volume_mounted():
    # Use os.path.ismount so tests that fake Path.is_mount cannot write here.
    return os.path.ismount(volume_root())


def volume_folder(job):
    """The job's folder on the volume, at the keys the controller reads over S3."""
    return volume_root() / "jobs" / safe_job_id(job)


def reserved_save(path, value):
    """Never follow a user-created link at a reserved outcome/control path."""
    directory(path.parent, create=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise RuntimeError("Reserved record is a link or special file.")
    save(path, value)


def control_record(job, pod_id, name, value, actor="pod_guard"):
    # Discovery and diagnostics only. The controller needs its own ledger to delete.
    try:
        if not volume_mounted():
            return False
        reserved_save(
            volume_folder(job) / "control" / name,
            {
                **value,
                "job_id": job["job_id"],
                "pod_id": pod_id,
                "actor": actor,
                "updated_utc": utc(),
            },
        )
        return True
    except Exception:
        return False  # A broken volume cannot prevent compute cleanup.


def guard_failure(job, pod_id, exc, where=None, actor="pod_guard"):
    """Leave the reason of a failure on the volume: the Pod's log is gone once the Pod is deleted.

    Also used by the supervisor. Never raises: cleanup follows it.
    """
    try:
        try:
            import traceback

            frames = traceback.extract_tb(exc.__traceback__)
            where = where or (frames[-1].name + ":" + str(frames[-1].lineno) if frames else None)
        except Exception:
            pass
        record = {"error_type": type(exc).__name__, "message": str(exc)[:500], "where": where}
        if "ownership/mode" in str(exc):
            record["volume_probe"] = volume_probe()
            record["workspace_probe"] = volume_probe(workspace_root())
        control_record(job, pod_id, "guard-failure.json", record, actor)
    except Exception:
        pass


def volume_probe(root=None):
    """What a filesystem (default: the volume) does with ownership and mode; leaves nothing behind."""
    root = volume_root() if root is None else Path(root)
    out = {}
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as mounts:
            out["mount"] = next(
                (line.split()[:4] for line in mounts if line.split()[1:2] == [str(root)]), None
            )
        seen = lambda p: [
            p.stat().st_uid,
            p.stat().st_gid,
            format(stat.S_IMODE(p.stat().st_mode), "o"),
        ]
        out["root"] = seen(root)
        probe = root / (".guard-probe-" + str(os.getpid()))
        probe.mkdir()
        try:
            out["new_folder"] = seen(probe)
            os.chown(probe, 0, 0)
            os.chmod(probe, 0o700)
            out["folder_root_700"] = seen(probe)
            os.chown(probe, 61000, 61000)
            out["folder_user_700"] = seen(probe)
            item = probe / "f"
            item.write_bytes(b"x")
            os.chown(item, 0, 0)
            os.chmod(item, 0o444)
            out["file_root_444"] = seen(item)
            item.unlink()
        finally:
            for leftover in list(probe.iterdir()):
                leftover.unlink()
            probe.rmdir()
    except Exception as failure:
        out["error"] = type(failure).__name__ + ": " + str(failure)[:200]
    return out


def agent_status(job, pod_id, phase, actor="pod_supervisor", **fields):
    try:
        if not volume_mounted():
            return False
        reserved_save(
            volume_folder(job) / "agent.json",
            {
                "job_id": job["job_id"],
                "pod_id": pod_id,
                "actor": actor,
                "phase": phase,
                "heartbeat_epoch": time.time(),
                **fields,
            },
        )
        return True
    except Exception:
        return False


class SelfDeleteDenied(PermissionError):
    def __init__(self, status, operation):
        self.status, self.operation = status, operation
        super().__init__("Pod-scoped authentication denied a cleanup request.")


class SelfCleanupUnverified(RuntimeError):
    pass


def terminate(api, pod_id, job):
    # The injected key and Pod ID authorize self-deletion despite mutable name,
    # environment and cloud fields. Caller or ledger IDs have no such authority.
    if not pod_id or pod_id != os.environ.get("RUNPOD_POD_ID"):
        raise PermissionError("Self-cleanup refuses an ID other than RUNPOD_POD_ID.")
    evidence = {
        "delete_attempts": 0,
        "delete_accepted": False,
        "absence_verified": False,
        "stop_attempted": False,
        "stop_accepted": False,
        "events": [],
    }

    def record(state, **fields):
        evidence.update(state=state, **fields)
        evidence["events"] = (evidence["events"] + [{"state": state, "utc": utc(), **fields}])[-32:]
        return control_record(job, pod_id, "self-delete.json", evidence)

    class ObservedApi:
        def __init__(self):
            self.owned_terminal_seen = False

        @property
        def last_response(self):
            return getattr(api, "last_response", None)

        def listing(self, path, field):
            try:
                return api.listing(path, field)
            except ApiError as exc:
                record("inventory_failed", last_operation="LIST", last_http_status=exc.status)
                if exc.status in (401, 403):
                    raise SelfDeleteDenied(exc.status, "LIST") from None
                raise

        def get_or_none(self, path):
            try:
                value = api.get_or_none(path)
            except ApiError as exc:
                record("read_failed", last_operation="GET", last_http_status=exc.status)
                if exc.status in (401, 403):
                    raise SelfDeleteDenied(exc.status, "GET") from None
                raise
            record(
                "read_back", last_operation="GET", last_http_status=404 if value is None else 200
            )
            if value is not None:
                self.owned_terminal_seen = (
                    value.get("id") == pod_id and value.get("status") == "TERMINATED"
                )
                return value
            if not evidence["delete_accepted"] and not self.owned_terminal_seen:
                # The injected ID authorizes DELETE before a second 404.
                record("absence_unproven", absence_verified=False)
                self.request("DELETE", path)
                return self.get_or_none(path)  # This point read is after acceptance.
            return None

        def request(self, method, path):
            record(
                "delete_attempted",
                delete_attempts=evidence["delete_attempts"] + 1,
                last_operation=method,
                last_http_status=None,
                request_outcome="unknown",
            )
            try:
                response = api.request(method, path)
            except ApiError as exc:
                record("delete_failed", last_http_status=exc.status, request_outcome="rejected")
                if exc.status in (401, 403):
                    raise SelfDeleteDenied(exc.status, method) from None
                raise
            metadata = self.last_response or {}
            if metadata.get("method") != method or metadata.get("path") != path:
                metadata = {}  # Never attribute a preceding GET's 200 to DELETE.
            status = metadata.get("status")
            if type(status) is not int or not 200 <= status < 300:
                record("delete_response_unverified", request_outcome="unknown")
                raise RuntimeError("Own-Pod DELETE has no explicit accepted HTTP response.")
            record(
                "delete_accepted",
                delete_accepted=True,
                request_outcome="accepted",
                delete_http_status=status,
            )
            return response

    previous = signal_handlers(signal.SIG_IGN)
    denied_read_delete_attempted = False
    try:
        record("starting")
        while True:
            try:
                # Reads, STOP and DELETE share the cleanup reserve beyond the workload deadline.
                with api_pacing(api, cleanup=True):
                    proof = delete_verified(
                        ObservedApi(),
                        "/pods/" + pod_id,
                        lambda pod: pod.get("id") == pod_id,
                        attempts=3,
                        actor="pod_guard",
                    )
                confirmation = proof.get("proof") or {}
                inventory_proof = confirmation.get("inventory") or {}
                owned_terminal = (
                    proof.get("verified") is True
                    and confirmation.get("observed_tombstone") is True
                    and inventory_proof.get("complete") is True
                    and inventory_proof.get("absent") is True
                    and any(
                        item.get("type") == "point_read"
                        and item.get("state") == "terminated"
                        and item.get("ownership_checked") is True
                        for item in proof.get("observations", [])
                    )
                )
                if not evidence["delete_accepted"] and not owned_terminal:
                    # The injected ID authorizes DELETE while startup GETs lag.
                    # Initial GET 404s cannot release the guard. DELETE 404s
                    # cannot prove accepted dispatch.
                    record("absence_unproven", absence_verified=False, verification=proof)
                    with api_pacing(api, cleanup=True):
                        ObservedApi().request("DELETE", "/pods/" + pod_id)
                    continue  # Fresh separated reads must follow acceptance.
                record(
                    "absence_verified",
                    absence_verified=True,
                    verification=proof,
                    absence_basis="accepted_self_delete"
                    if evidence["delete_accepted"]
                    else "owned_terminated",
                )
                agent_status(
                    job,
                    pod_id,
                    "done",
                    actor="pod_guard",
                    cleanup_state="absence_verified",
                    delete_attempts=evidence["delete_attempts"],
                    delete_accepted=evidence["delete_accepted"],
                )
                return
            except SelfDeleteDenied as exc:
                # The injected ID authorizes self-DELETE after GET denial.
                # Keep GET results genuine and scope this to the injected ID.
                if (
                    exc.operation == "GET"
                    and not evidence["delete_accepted"]
                    and not denied_read_delete_attempted
                ):
                    denied_read_delete_attempted = True
                    try:
                        with api_pacing(api, cleanup=True):
                            ObservedApi().request("DELETE", "/pods/" + pod_id)
                    except SelfDeleteDenied as denied:
                        exc = denied
                    except Exception:
                        record("delete_unverified", request_outcome="unknown")
                    else:
                        continue  # Try real read-back; never infer absence.
                if evidence["delete_accepted"] and exc.operation != "DELETE":
                    record(
                        "verification_denied", denial_http_status=exc.status, absence_verified=False
                    )
                    agent_status(
                        job,
                        pod_id,
                        "done",
                        actor="pod_guard",
                        cleanup_state="verification_denied",
                        delete_attempts=evidence["delete_attempts"],
                        delete_accepted=True,
                    )
                    raise SelfCleanupUnverified(
                        "SELF CLEANUP UNVERIFIED: DELETE accepted, but absence verification denied. "
                        "External verification is required."
                    ) from None
                # STOP preserves the volume. Deletion and REST write scope remain unverified.
                # https://docs.runpod.io/pods/manage-pods#stop-a-pod
                record("auth_denied", denial_http_status=exc.status)
                record("stop_attempted", stop_attempted=True)
                # Persist the terminal diagnostic before STOP can end the container.
                agent_status(
                    job,
                    pod_id,
                    "done",
                    actor="pod_guard",
                    cleanup_state="auth_denied",
                    delete_attempts=evidence["delete_attempts"],
                    stop_attempted=True,
                    stop_accepted=False,
                )
                try:
                    with api_pacing(api, cleanup=True):
                        api.request("POST", "/pods/" + pod_id + "/action", {"action": "stop"})
                    metadata = getattr(api, "last_response", None) or {}
                    record(
                        "stop_accepted", stop_accepted=True, stop_http_status=metadata.get("status")
                    )
                except Exception as fallback:
                    record(
                        "stop_failed",
                        stop_http_status=getattr(fallback, "status", None),
                        stop_error_type=type(fallback).__name__,
                    )
                record("auth_denied", absence_verified=False)
                agent_status(
                    job,
                    pod_id,
                    "done",
                    actor="pod_guard",
                    cleanup_state="auth_denied",
                    delete_attempts=evidence["delete_attempts"],
                    stop_attempted=True,
                    stop_accepted=evidence["stop_accepted"],
                )
                raise SelfCleanupUnverified(
                    "SELF CLEANUP UNVERIFIED: authentication denied; stop fallback attempted. "
                    "External cleanup is required; storage may remain billable."
                ) from None
            except PermissionError:
                raise
            except (Exception, KeyboardInterrupt):
                notice(
                    "Pod deletion not yet verified; retrying. External watchdog remains necessary."
                )
                time.sleep(15)
    finally:
        restore_signals(previous)


def deadline_anchors(api, job):
    """Call only after GET own Pod has sampled the provider Date header."""
    response = getattr(api, "last_response", None)
    if isinstance(response, dict) and not response.get("response_date"):
        raise RuntimeError("Provider Date header unavailable; refusing local-clock fallback.")
    epoch = api.now()
    if type(epoch) not in (int, float) or not math.isfinite(epoch):
        raise RuntimeError("Provider clock unavailable; refusing local-wall-clock deadlines.")
    monotonic = time.monotonic()
    end = monotonic + max(0, job["deadline_epoch"] - epoch)
    startup = min(end, monotonic + max(0, job["startup_deadline_epoch"] - epoch))
    training = monotonic + max(
        0, job["deadline_epoch"] - job["config"]["finalize_grace_seconds"] - epoch
    )
    return end, startup, training


# Set by the guard after it becomes its descendants' reaper under an injected init.
SUBREAPER = [False]


def descendants(root):
    """PIDs whose chain of parents leads to `root`, read from /proc."""
    parents = {}
    with os.scandir("/proc") as entries:
        for entry in entries:
            if entry.name.isdecimal():
                try:
                    fields = Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
                    parents[int(entry.name)] = int(fields[1])
                except (OSError, ValueError, IndexError):
                    continue  # The process ended while it was read.
    found, frontier = set(), {root}
    while frontier:
        frontier = {
            pid for pid, parent in parents.items() if parent in frontier and pid not in found
        }
        found |= frontier
    return found


def reap(supervisor):
    """The guard collects orphaned workload children as well as the supervisor."""
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            return
        if supervisor is not None and pid == supervisor.pid:
            supervisor.returncode = os.waitstatus_to_exitcode(status)


def drain_processes(supervisor, *, include_supervisor=False, timeout=3):
    """Quiesce the private container before attesting artifacts or deleting it."""
    # Never enumerate or kill host processes in local tests or on import.
    # PID 1 owns the container. Under an injected init, reap only descendants.
    anchor = os.getpid() == 1
    if not anchor and not SUBREAPER[0]:
        raise RuntimeError(
            "Process reaping requires the private container's PID 1 or the armed guard."
        )
    excluded = {1}
    if supervisor is not None and not include_supervisor:
        excluded.add(supervisor.pid)
    until = time.monotonic() + max(0, timeout)
    while True:
        reap(supervisor)
        if anchor:
            with os.scandir("/proc") as entries:
                pids = [
                    int(entry.name)
                    for entry in entries
                    if entry.name.isdecimal() and int(entry.name) not in excluded
                ]
        else:
            pids = [pid for pid in descendants(os.getpid()) if pid not in excluded]
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if time.monotonic() >= until:
            raise TimeoutError("Processes did not quiesce; no complete manifest is safe.")
        time.sleep(0.05)


def guard(job, api, pod_id):
    """Long-lived entrypoint: arm first, launch supervisor, then always delete."""
    supervisor = channel = child_channel = None
    previous = {}
    cancelled = [False]

    def interrupted(signum, frame):
        cancelled[0] = True  # No exceptions, including during cleanup.

    try:
        previous = signal_handlers(interrupted)
        if not sys.platform.startswith("linux"):
            raise RuntimeError("The Pod guard must run on Linux.")
        if os.getpid() != 1:
            # Runpod's injected init blocked the first Pod on 2026-11-12.
            # A subreaper collects orphaned descendants as PID 1 would.
            # The guard issues DELETE in either case.
            isolation.become_subreaper()
            SUBREAPER[0] = True
        if job["config"]["cloud"] != "SECURE":
            raise RuntimeError("Only Secure Cloud is permitted.")
        isolation.protect_code(Path(__file__).parent)
        isolation.lock_volume(volume_root())  # First: the volume keeps no ownership or modes.
        isolation.prepare_control(workspace_root(), job_folder(job))
        control_record(job, pod_id, "identity.json", {"source": "RUNPOD_POD_ID"})
        with api_pacing(api):
            pod = api.request("GET", "/pods/" + pod_id)
        if pod.get("id") != pod_id or pod.get("cloud") != "SECURE":
            raise RuntimeError("Secure Cloud identity validation failed.")
        deadline, startup, _ = deadline_anchors(api, job)
        now = time.monotonic()
        if cancelled[0] or now >= startup:
            raise TimeoutError("No startup time remains.")
        # Anonymous IPC removes the guard's dependency on heartbeat-file writes.
        channel, child_channel = socket.socketpair()
        channel.settimeout(1)
        # Load podrt and run podrt.agent just as the Pod command did.
        supervisor_bootstrap = (
            "import importlib.util as u,pathlib,runpy,sys;f=pathlib.Path(sys.argv[1]);p=f.parent;"
            "k=u.spec_from_file_location('podrt',p/'__init__.py',submodule_search_locations=[str(p)]);"
            "m=u.module_from_spec(k);sys.modules['podrt']=m;k.loader.exec_module(m);"
            "sys.argv=[str(f),'supervisor',sys.argv[2]];"
            "runpy.run_module('podrt.agent',run_name='__main__',alter_sys=True)"
        )
        supervisor = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-u",
                "-c",
                supervisor_bootstrap,
                __file__,
                str(child_channel.fileno()),
            ],
            pass_fds=(child_channel.fileno(),),
            close_fds=True,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
        )
        # main removed the key from os.environ. Pass it only over private inherited IPC.
        key = api.key.encode("utf-8")
        if not 0 < len(key) <= 65536:
            raise RuntimeError("Invalid scoped-key length.")
        channel.sendall(len(key).to_bytes(4, "big") + key)
        child_channel.close()
        child_channel = None
        last_seen, released, stopping = time.monotonic(), False, None
        while True:
            reap(supervisor)
            if supervisor.poll() is not None:
                break
            now = time.monotonic()
            expired = now >= deadline
            stale = now - last_seen > job["config"]["agent_stale_seconds"]
            startup_failed = not released and now >= startup
            if stopping is None and (cancelled[0] or expired or stale or startup_failed):
                stopping = min(deadline, now + job["config"]["finalize_grace_seconds"])
                supervisor.send_signal(signal.SIGTERM)
            if expired or (stopping is not None and now >= stopping):
                break
            if not select.select([channel], [], [], 0.2)[0]:
                continue
            messages = channel.recv(4096)
            if not messages:
                break
            last_seen = time.monotonic()
            for message in messages:
                if message in (ord("H"), ord("R")):
                    # Fresh handshake at boot AND immediately before user code.
                    if stopping is not None or cancelled[0] or time.monotonic() >= startup:
                        channel.sendall(b"!")
                    else:
                        channel.sendall(b"A" if message == ord("H") else b"G")
                        released = released or message == ord("R")
                elif message == ord("Q"):
                    drain_processes(supervisor, timeout=min(3, max(0, deadline - time.monotonic())))
                    channel.sendall(b"D")
                elif message == ord("F"):
                    cancelled[0] = True
                elif message != ord("."):
                    raise RuntimeError("Invalid supervisor handshake.")
    except (Exception, KeyboardInterrupt) as exc:
        notice("Guard failure: " + type(exc).__name__)
        guard_failure(job, pod_id, exc)
    finally:
        # Always clean up after startup/IPC failures or supervisor exit.
        # Keep the signal latch until DELETE and read-back finish.
        try:
            if os.getpid() == 1 or SUBREAPER[0]:
                drain_processes(supervisor, include_supervisor=True)
        except Exception:
            notice("Process shutdown incomplete; terminating Pod with unfinalized outputs.")
        finally:
            for stream in (channel, child_channel):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            try:
                terminate(api, pod_id, job)
            finally:
                restore_signals(previous)


def exchange(channel, request, expected, timeout=10):
    channel.sendall(request)
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if select.select([channel], [], [], min(0.2, max(0, until - time.monotonic())))[0]:
            if channel.recv(1) != expected:
                raise RuntimeError("PID-1 guard refused or lost the handshake.")
            return
    raise TimeoutError("PID-1 guard did not acknowledge readiness.")


WORKLOAD_ENV = frozenset(
    """PATH HOME USER LOGNAME LANG LC_ALL LC_CTYPE TZ
LD_LIBRARY_PATH LIBRARY_PATH CPATH CUDA_HOME CUDA_PATH CUDA_VISIBLE_DEVICES
CUDA_MODULE_LOADING CUDA_CACHE_PATH NVIDIA_VISIBLE_DEVICES NVIDIA_DRIVER_CAPABILITIES
NVIDIA_REQUIRE_CUDA OMP_NUM_THREADS MKL_NUM_THREADS OPENBLAS_NUM_THREADS
NCCL_DEBUG NCCL_SOCKET_IFNAME NCCL_IB_DISABLE NCCL_P2P_DISABLE TORCH_HOME
XDG_CACHE_HOME HF_HOME HUGGINGFACE_HUB_CACHE TRANSFORMERS_CACHE PIP_CACHE_DIR
PYTHONUNBUFFERED PYTHONHASHSEED PYTHONDONTWRITEBYTECODE""".split()
)


# Specs before 0.1.0 omit their prefix list and use WONDOMON.
ENV_PREFIXES = ("WONDOMON",)


def child_environment(environ, output, inputs=None, declared=None, prefixes=ENV_PREFIXES):
    """The workload's variables. The contract's folders carry every prefix in `prefixes` (the spec's
    env_prefixes: the tool's own, then WONDOMON_ as an alias); a job may declare none of them."""
    prefixes = [
        p for p in prefixes if isinstance(p, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", p)
    ]
    prefixes = prefixes or list(ENV_PREFIXES)
    reserved = ("RUNPOD_", "WONDOMON_", "AWS_", *(prefix + "_" for prefix in prefixes))
    result = {k: v for k, v in environ.items() if k in WORKLOAD_ENV}
    for name, value in (declared or {}).items():
        if (
            not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)
            or name.startswith(reserved)
            or any(
                word in name for word in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "CREDENTIAL")
            )
            or name in ("PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD")
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise ValueError("Unsafe declared workload environment variable.")
        result[name] = value
    for prefix in prefixes:
        result[prefix + "_OUTPUT_DIR"] = str(output)
        if inputs is not None:
            result[prefix + "_INPUT_DIR"] = str(inputs)
    if inputs is not None:
        result["PYTHONPATH"] = str(inputs)
    return result


def stop_child(child, grace=10):
    if child is None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait(timeout=10)
        return
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=10)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # PID 1 additionally drains escaped sessions/orphans before manifest hashing.


def directory(path, *, create=False):
    path = Path(path)
    for part in (*reversed(path.parents), path):
        if create:
            part.mkdir(exist_ok=True)
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Output directory traverses a link or special file.")


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def portable_component(name):
    # stdlib-only: the bootstrap intentionally does not bundle policy.py.
    if (
        name in ("", ".", "..")
        or name.endswith((" ", "."))
        or re.search(r'[\x00-\x1f<>:"/\\|?*\ud800-\udfff]', name)
        or len(name.encode("utf-16-le")) > 510
    ):
        raise RuntimeError("Unsafe output filename.")
    folded = unicodedata.normalize("NFKC", name).upper().casefold()
    stem = folded.split(".", 1)[0].rstrip(" ")
    if re.fullmatch(
        r"con|prn|aux|nul|clock\$|conin\$|conout\$|(?:com|lpt)[0-9]", stem
    ) or re.fullmatch(r"[^.]{1,6}~[0-9]+(?:\.[^.]{1,3})?", folded):
        raise RuntimeError("Reserved output filename.")
    return folded


def identity(info):
    # CPython 3.12 on Windows reports different ctime semantics through lstat
    # and fstat. Creation time is not a content-change indicator there anyway.
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns if os.name != "nt" else 0,
    )


def inventory(output):
    """Exhaustive scan: scandir/lstat failures are never silently discarded."""
    directory(output)
    device = output.lstat().st_dev
    result = {}

    def visit(path):
        info = path.lstat()
        if (
            info.st_dev != device
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise RuntimeError("Linked or separately mounted output is unsafe.")
        result[path.relative_to(output).as_posix()] = identity(info)
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                children = sorted(entry.name for entry in entries)
            names = set()
            for name in children:
                key = portable_component(name)
                if key in names:
                    raise RuntimeError("Output filenames collide on the backup filesystem.")
                names.add(key)
                visit(path / name)
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Output must contain only directories and unlinked regular files.")

    visit(output)
    return result


def make_manifest(job, folder, cache=None, deadline=None):
    """Caller has quiesced processes; preserve unusual paths in an opaque bundle."""
    return output_bundle.finalize(job, folder, cache=cache, deadline=deadline, quiesced=True)


def claim_attempt(folder):
    """Persist the once-only marker before any workload can execute."""
    with exclusive_lock(folder / "start.lock"):
        try:
            (folder / "started-once.json").lstat()
        except FileNotFoundError:
            output = folder / "artifacts"
            try:
                with os.scandir(output) as entries:
                    if next(entries, None) is not None:
                        raise RuntimeError("Unclaimed outputs exist; preserve them for recovery.")
            except FileNotFoundError:
                pass
            save(folder / "started-once.json", {"started_utc": utc()})
            return
        raise RuntimeError("Container restart detected; preserve the first attempt and terminate.")


def finalize_outputs(job, pod_id, folder, result, quiesce, cache=None, deadline=None, *, volume):
    """Commit outcome before slow shutdown/scanning; retain a cutoff diagnostic.

    Snapshot on the Pod's disk (`folder`), then copy it to the volume (`volume`), manifest
    last, within the same deadline. After a failure the raw outputs that fit are copied.
    """
    output = folder / "artifacts"
    diagnostic = {"complete": False, "stage": "outcome", "result": result}
    early = ("artifacts/result.json",)  # The final copy replaces the early one.

    def mark(**fields):
        diagnostic.update(fields)
        control_record(job, pod_id, "finalization-incomplete.json", diagnostic, "pod_supervisor")

    mark()
    outcome_error = None
    try:
        reserved_save(output / "result.json", result)
    except Exception as exc:
        outcome_error = exc  # Preserve the outcome in the control diagnostic.
    try:
        reserved_save(volume / early[0], result)  # On the volume before the slow steps.
    except Exception:
        pass  # The control diagnostic holds it too.
    quiesced = False
    try:
        mark(stage="quiesce")
        quiesce()
        quiesced = True
        if cache is not None:
            cache.stop(
                timeout=5 if deadline is None else max(0, min(5, deadline - time.monotonic()))
            )
        if outcome_error is not None:
            raise outcome_error
        reserved_save(output / "result.json", result)
        log = output / "train.log"
        try:
            info = log.lstat()
        except FileNotFoundError:
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        else:
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise RuntimeError("Reserved log is a link or special file.")
        mark(stage="inventory_and_hashing")
        manifest = make_manifest(job, folder, cache=cache, deadline=deadline)
        mark(stage="volume_copy")
        output_bundle.publish(job, folder, volume, manifest, deadline=deadline, replace=early)
    except (Exception, KeyboardInterrupt) as exc:
        mark(error_type=type(exc).__name__, state="failed")
        if quiesced:  # No workload process is left.
            try:
                mark(
                    salvaged_files=output_bundle.salvage(
                        folder, volume, deadline=deadline, replace=early
                    )
                )
            except (Exception, KeyboardInterrupt):
                pass
        notice("Artifact finalization incomplete; outcome retained for partial recovery.")
        return False
    try:
        path = volume / "control" / "finalization-incomplete.json"
        path.unlink(missing_ok=True)
        sync_directory(path.parent)
    except Exception:
        mark(complete=True, state="complete")
    return True


# The controller writes this to the volume (request.INPUT_MANIFEST).
INPUT_MANIFEST = "control/inputs.json"
# The watchdog writes this to request early completion (watchdog.finishing).
CANCEL_REQUEST = "cancel.json"


def cancel_requested(job, pod_id, volume):
    """True when the controller asked this Pod to finish now: stop the workload as at the time limit."""
    try:
        value = read(volume / CANCEL_REQUEST, {})
    except (OSError, ValueError):
        return False  # absent, or seen half written
    return (
        isinstance(value, dict)
        and value.get("job_id") == job["job_id"]
        and value.get("pod_id") == pod_id
    )


def input_list(job, volume):
    """The input list. The controller writes it to the volume and puts only its size, SHA-256 and count
    in the Pod request, which Runpod caps; it is read without following a link and checked before it is
    trusted. A spec that carries the list itself (the Pod's own tests) is used as it is."""
    if "input_manifest" not in job:
        return job["inputs"]
    record = job["input_manifest"]
    with output_bundle._open_file(volume, INPUT_MANIFEST) as stream:
        raw = stream.read(record["size"] + 1)
    if len(raw) != record["size"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise RuntimeError("The input manifest on the volume is not the one the Pod request names.")
    manifest = json.loads(raw.decode("utf-8"))
    inputs = manifest.get("inputs") if isinstance(manifest, dict) else None
    if (
        not isinstance(inputs, list)
        or manifest.get("job_id") != job["job_id"]
        or len(inputs) != record["count"]
    ):
        raise RuntimeError("The input manifest on the volume does not belong to this job.")
    for item in inputs:
        if (
            not isinstance(item, dict)
            or set(item) != {"remote", "size", "sha256"}
            or not isinstance(item["remote"], str)
            or type(item["size"]) is not int
            or item["size"] < 0
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise RuntimeError("The input manifest has a malformed entry.")
    return inputs


def copy_inputs(job, source, destination, check=None):
    """Copy the listed inputs from the volume to the Pod's disk, each checked against its SHA-256.

    No link is followed. `destination` must be new or empty: nothing unchecked may be there.
    """
    destination = Path(destination)
    try:
        destination.mkdir()
    except FileExistsError:
        info = destination.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("The Pod's input folder is a link or special file.")
        with os.scandir(destination) as entries:
            if next(entries, None) is not None:
                raise RuntimeError(
                    "The Pod's input folder already holds files; only checked inputs may."
                )
    for item in job["inputs"]:
        try:
            output_bundle.copy_file(
                source,
                item["remote"],
                destination,
                (item["size"], item["sha256"]),
                check=check,
                executable=True,
            )
        except RuntimeError as exc:
            raise RuntimeError(
                f"Input {item['remote']!r} was not copied and checked: {exc}"
            ) from None


def execute(job, api, pod_id, channel):
    """Supervisor child. The guard, not this process, owns deletion."""
    root = workspace_root()
    folder = job_folder(job)
    output = folder / "artifacts"
    volume = volume_folder(job)
    child = None
    cache, finalization_deadline = None, None
    threads = []
    stop, failed = threading.Event(), threading.Event()
    cancelled, phase = [False], {"value": "preflight"}
    previous = {}
    reason, code, began, claimed = "agent_preflight_failed", None, utc(), False

    def interrupted(signum, frame):
        cancelled[0] = True

    def health():
        if cancelled[0]:
            raise InterruptedError("Cancellation requested.")
        if failed.is_set():
            raise RuntimeError("Supervisor heartbeat failed.")

    def heartbeat_loop():
        try:
            while not stop.is_set():
                channel.sendall(b".")
                stop.wait(1)
        except Exception:
            failed.set()
            try:
                channel.sendall(b"F")
            except Exception:
                pass

    def telemetry_loop():
        # Storage failures (including a blocked write) cannot starve IPC beats.
        while not stop.is_set():
            control_record(
                job, pod_id, "identity.json", {"source": "RUNPOD_POD_ID"}, "pod_supervisor"
            )
            agent_status(job, pod_id, phase["value"])
            stop.wait(5)

    try:
        previous = signal_handlers(interrupted)
        channel.settimeout(1)
        exchange(channel, b"H", b"A")
        health()
        heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat.start()
        threads.append(heartbeat)
        if not volume_mounted():
            raise RuntimeError("The network volume is not mounted at " + VOLUME_MOUNT + ".")
        directory(volume, create=True)
        # The volume survives a container restart that wipes the Pod's disk.
        claim_attempt(volume)
        claimed = True
        # Start telemetry before the slow copy.
        telemetry = threading.Thread(target=telemetry_loop, daemon=True)
        telemetry.start()
        threads.append(telemetry)
        directory(folder, create=True)
        directory(output, create=True)
        job["inputs"] = input_list(job, volume)
        copy_inputs(job, volume_root() / "inputs", root / "inputs", health)
        layout = isolation.prepare(root, folder, Path(__file__).parent, volume=volume_root())
        with api_pacing(api):
            pod = api.request("GET", "/pods/" + pod_id)
        if pod.get("id") != pod_id or pod.get("cloud") != "SECURE":
            raise RuntimeError("Secure Cloud identity validation failed.")
        finalization_deadline, startup_deadline, mono_deadline = deadline_anchors(api, job)
        mounts = pod.get("mounts", {}).get("network", [])
        if not any(
            m.get("volumeId") == job["volume_id"] and m.get("path") == VOLUME_MOUNT for m in mounts
        ):
            raise RuntimeError("Durable output volume is missing.")
        # Lifecycle check only. Scoped-key DELETE authorization remains unverified.
        if "terminate" not in pod.get("actions", []):
            raise RuntimeError("Pod does not advertise a terminate action.")
        for item in job["inputs"]:
            health()
            path = root / "inputs" / item["remote"]
            if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
                raise RuntimeError("Input verification failed.")
        health()
        cache = output_bundle.HashCache(output).start()
        phase["value"] = "ready"
        while time.monotonic() < min(startup_deadline, mono_deadline):
            health()
            try:
                start = read(volume / "start.json", {})  # Written by the controller over S3.
            except ValueError:
                start = {}  # Seen half written.
            if (
                isinstance(start, dict)
                and start.get("job_id") == job["job_id"]
                and start.get("pod_id") == pod_id
            ):
                break
            stop.wait(0.2)
        else:
            raise TimeoutError("Controller did not release training within the startup limit.")
        phase["value"] = "training"
        inputs = root / "inputs"

        def authorize_workload():
            health()
            exchange(channel, b"R", b"G")
            health()

        with (output / "train.log").open("ab", buffering=0) as log:
            child = isolation.launch(
                job["config"]["command"],
                layout,
                child_environment(
                    os.environ,
                    output,
                    inputs,
                    job["config"].get("workload_env"),
                    job.get("env_prefixes") or ENV_PREFIXES,
                ),
                log,
                timeout=min(30, max(0, startup_deadline - time.monotonic())),
                authorize=authorize_workload,
            )
            next_check = 0.0
            while child.poll() is None:
                health()
                now = time.monotonic()
                if now >= mono_deadline:
                    reason = "time_limit"
                    break
                if now >= next_check:
                    next_check = now + 2
                    if cancel_requested(job, pod_id, volume):
                        reason = "cancelled"  # stopped and published as at the time limit
                        break
                stop.wait(0.2)
            code = child.returncode
        if reason not in ("time_limit", "cancelled"):
            reason = "success" if code == 0 else "training_failed"
    except (Exception, KeyboardInterrupt) as exc:
        reason = (
            "interrupted"
            if isinstance(exc, (InterruptedError, KeyboardInterrupt))
            else "agent_failed"
        )
        notice("Supervisor failure: " + type(exc).__name__)
        guard_failure(job, pod_id, exc, actor="pod_supervisor")
    finally:
        phase["value"] = "finalizing"

        def quiesce():
            stop_child(child)
            if child is not None:
                result["exit_code"] = child.returncode
            exchange(channel, b"Q", b"D")

        result = {
            "job_id": job["job_id"],
            "pod_id": pod_id,
            "outcome": reason,
            "exit_code": code,
            "started_utc": began,
            "finished_utc": utc(),
        }
        try:
            if claimed:
                finalize_outputs(
                    job,
                    pod_id,
                    folder,
                    result,
                    quiesce,
                    cache,
                    finalization_deadline,
                    volume=volume,
                )
            else:
                quiesce()  # Restarts must not overwrite the first attempt's files.
        except (Exception, KeyboardInterrupt):
            notice("Artifact finalization incomplete; retain network volume for recovery.")
        finally:
            stop.set()
            if cache is not None:
                try:
                    cache.stop(timeout=0)
                except Exception:
                    pass  # PID 1 still enforces the lifetime of this process.
            for thread in threads:
                thread.join(timeout=1)
            agent_status(job, pod_id, "done")
            channel.close()
            restore_signals(previous)


def main():
    protection_error = None
    try:
        isolation.protect_process()  # Before reading spec/key or receiving IPC.
    except Exception as exc:
        protection_error = exc
    job = spec()
    pod_id = os.environ["RUNPOD_POD_ID"]
    key = os.environ.pop("RUNPOD_API_KEY", None)
    # Popping prevents exec inheritance. dumpable=0 and the separate UID keep
    # ordinary workload code out of process memory. This relies on the image
    # and kernel and provides no separate VM or kernel boundary.
    if len(sys.argv) > 1 and sys.argv[1] == "supervisor":
        if protection_error is not None:
            raise RuntimeError("Supervisor memory protection unavailable.") from None
        channel = socket.socket(fileno=int(sys.argv[2]))
        channel.settimeout(10)

        def receive(size):
            data = bytearray()
            while len(data) < size:
                chunk = channel.recv(size - len(data))
                if not chunk:
                    raise RuntimeError("Guard closed scoped-key IPC.")
                data.extend(chunk)
            return bytes(data)

        size = int.from_bytes(receive(4), "big")
        if not 0 < size <= 65536:
            raise RuntimeError("Invalid scoped-key IPC.")
        execute(job, scoped_api(receive(size).decode("utf-8")), pod_id, channel)
    else:
        if not key:
            raise RuntimeError("Missing platform-scoped credential.")
        if protection_error is not None:
            notice("Guard memory protection unavailable; refusing workload.")
            try:
                guard_failure(job, pod_id, protection_error, "protect_process")
            finally:
                terminate(scoped_api(key), pod_id, job)
            return
        guard(job, scoped_api(key), pod_id)


if __name__ == "__main__":
    try:
        main()
    except SelfCleanupUnverified as exc:
        notice(str(exc))
        raise SystemExit(3)
