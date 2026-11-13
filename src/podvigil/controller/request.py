"""The Pod creation request, its size budget, and the checks on the Pod that comes back."""

from pathlib import Path
import base64
import hashlib
import json
import math
import zlib

from .. import LEDGER_SCHEMA, __version__
from ..jobspec import validate
from ..podrt.records import VOLUME_MOUNT
from ..policy import CLOUD
from ..settings import ENV_PREFIX, ENV_PREFIXES
from .ownership import owned_pod

# Runpod refuses a Pod request above 102,400 bytes (HTTP 413). Keep a margin under it.
POD_REQUEST_LIMIT = 100000

# The input list is stored on the volume beside the Pod's control records
# (podrt.agent.INPUT_MANIFEST). Only its size, SHA-256 and count go in the Pod
# request, so the number of inputs no longer counts against the request's limit.
INPUT_MANIFEST = "control/inputs.json"


def input_manifest(job_id, inputs):
    """The input manifest's exact bytes and the record that binds them: (bytes, {size, sha256, count})."""
    raw = json.dumps(
        {"job_id": job_id, "inputs": inputs}, sort_keys=True, separators=(",", ":")
    ).encode()
    return raw, {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "count": len(inputs)}


def payload(job, volume_id):
    # Revalidate immediately at the only paid Pod-creation boundary.
    body = pod_request(job, volume_id)
    size = len(json.dumps(body).encode())
    if size > POD_REQUEST_LIMIT:
        raise ValueError(request_too_large(size, len(job["prior_pod_ids"]), created=True))
    return body


def request_budget(cfg, inputs, prior_pod_ids=()):
    """Bytes of the Pod request this job would send, measured before anything is created.

    The input list is not in the request: only its manifest's size, SHA-256 and count are. Placeholders
    take at least the length of what launch fills in later (job ID, deadlines, manifest, volume ID), so the
    real request is not larger than this. The Pods already in the account still count, about 25 bytes each.
    """
    probe = {
        "job_id": "0" * 32,
        "pod_name": "wondomon-" + "0" * 32,
        "prior_pod_ids": list(prior_pod_ids),
        "deadline_epoch": 1763000000.1234567,
        "startup_deadline_epoch": 1763000000.1234567,
        "input_manifest": {"size": 10**15, "sha256": "0" * 64, "count": 10**9},
        "config": cfg,
    }
    size = len(json.dumps(pod_request(probe, "v" * 64)).encode())
    return {
        "bytes": size,
        "limit_bytes": POD_REQUEST_LIMIT,
        "headroom_bytes": POD_REQUEST_LIMIT - size,
        "input_files": len(inputs),
        "existing_pods_counted": len(prior_pod_ids),
    }


def request_too_large(size, prior_count, *, created):
    return (
        "The Pod request would be {:,} bytes; this runner sends at most {:,} "
        "(Runpod refuses more than 102,400). The request holds the Pod's code and the "
        "job's settings{}; the input list is not in it.".format(
            size,
            POD_REQUEST_LIMIT,
            ", and the {:,} Pods already in the account add about 25 bytes each".format(prior_count)
            if prior_count
            else "",
        )
        + (" The Pod was not requested." if created else " Nothing was created.")
    )


def check_request_budget(cfg, inputs, prior_pod_ids=()):
    budget = request_budget(cfg, inputs, prior_pod_ids)
    if budget["bytes"] > POD_REQUEST_LIMIT:
        raise ValueError(request_too_large(budget["bytes"], len(prior_pod_ids), created=False))
    return budget


# The Pod imports the shipped code as the package podrt. Keep it flat and limited
# to the standard library. Every byte counts against the request limit
# (tests/unit/test_pod_payload_size.py). The original shipping order compresses best.
POD_CODE = Path(__file__).resolve().parents[1] / "podrt"
POD_FILES = (
    "api.py",
    "records.py",
    "teardown.py",
    "agent.py",
    "isolation.py",
    "bundle.py",
    "__init__.py",
)


def pod_sources():
    """The Pod code exactly as it is shipped: file name -> text."""
    return {name: (POD_CODE / name).read_text(encoding="utf-8") for name in POD_FILES}


def pod_bootstrap(source):
    """The Pod's command: write the code into a new private folder, import that folder as the package
    podrt and run podrt.agent as __main__. The folder stays flat; the guard checks that (protect_code)."""
    bundle = base64.b64encode(zlib.compress(json.dumps(source).encode())).decode()
    return (
        "import base64,importlib.util as u,json,pathlib,runpy,sys,tempfile,zlib;"
        "p=pathlib.Path(tempfile.mkdtemp(prefix='wondomon-'));"
        f"s=json.loads(zlib.decompress(base64.b64decode('{bundle}')));"
        "[(p/n).write_text(v,encoding='utf-8') for n,v in s.items()];"
        "k=u.spec_from_file_location('podrt',p/'__init__.py',submodule_search_locations=[str(p)]);"
        "m=u.module_from_spec(k);sys.modules['podrt']=m;k.loader.exec_module(m);"
        "runpy.run_module('podrt.agent',run_name='__main__',alter_sys=True)"
    )


def pod_request(job, volume_id):
    """The Pod creation body. payload() adds the size check at the creation boundary."""
    cfg = validate(job["config"])
    bootstrap = pod_bootstrap(pod_sources())
    remote = {
        k: job[k]
        for k in ("job_id", "pod_name", "prior_pod_ids", "deadline_epoch", "startup_deadline_epoch")
    }

    # Launch writes the manifest to the volume first. A job built by plan or tests
    # gets the record of the same manifest.
    manifest = job.get("input_manifest") or input_manifest(job["job_id"], job["inputs"])[1]
    remote["input_manifest"] = {name: manifest[name] for name in ("size", "sha256", "count")}
    remote["config"] = {k: v for k, v in cfg.items() if k != "inputs"}
    remote["volume_id"] = volume_id
    remote["runner_version"], remote["ledger_schema"] = __version__, LEDGER_SCHEMA
    # The workload contract's variable prefixes: the tool's own, then compatibility aliases.
    remote["env_prefixes"] = list(ENV_PREFIXES)

    body = {
        "name": job["pod_name"],
        "cloud": CLOUD,
        "image": cfg["image"],
        "gpu": {
            "id": cfg["gpu_id"],
            "count": cfg["gpu_count"],
            "minCudaVersion": cfg["min_cuda_version"],
        },
        "dataCenterIds": [cfg["data_center_id"]],
        "disk": cfg["disk_gb"],
        "mounts": {"network": [{"volumeId": volume_id, "path": VOLUME_MOUNT}]},
        "entrypoint": ["python3", "-B", "-u", "-c"],
        "cmd": [bootstrap],
        "ports": [],
        "startSsh": False,
        "startJupyter": False,
        "env": {
            ENV_PREFIX + "_JOB_ID": job["job_id"],
            ENV_PREFIX + "_JOB_SPEC": base64.b64encode(json.dumps(remote).encode()).decode(),
        },
    }
    return body


def assert_pod(pod, job, ready=False):
    cfg = job["config"]
    if not owned_pod(pod, job):
        raise RuntimeError("Created Pod identity does not match this job.")
    if pod.get("cloud") != CLOUD:
        raise RuntimeError("Returned Pod is not Secure Cloud; immediate termination required.")
    if pod.get("dataCenterId") is not None and pod["dataCenterId"] != cfg["data_center_id"]:
        raise RuntimeError("Pod is in a different data center.")
    cost = pod.get("cost")
    if cost is not None and (
        type(cost) not in (float, int)
        or not math.isfinite(cost)
        or cost < 0
        or cost > cfg["max_compute_usd_per_hour"]
    ):
        raise RuntimeError("Actual Pod price is missing or exceeds the configured ceiling.")
    gpu = pod.get("gpu") or {}
    if (gpu.get("id") is not None and gpu["id"] != cfg["gpu_id"]) or (
        gpu.get("count") is not None and gpu["count"] != cfg["gpu_count"]
    ):
        raise RuntimeError("Allocated GPU does not match the request.")
    if ready and (
        pod.get("dataCenterId") is None or gpu.get("id") is None or cost is None or cost <= 0
    ):
        raise RuntimeError("Pod allocation/price has not been confirmed; workload cannot start.")
