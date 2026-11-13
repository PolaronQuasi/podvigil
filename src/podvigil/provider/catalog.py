"""Read-only catalog checks: price, capacity and volume types before anything is created; the survey."""

import math
import re
import urllib.parse

from ..podrt.api import ApiError
from ..podrt.records import utc
from ..policy import CLOUD, S3_DATACENTERS, cuda_version
from ..settings import CUDA_FLOOR_VARIABLE, cuda_floor

# HTTP 400 from POST /pods when the GPU and data center have no capacity.
CAPACITY_DETAIL = re.compile(
    r"no longer any instances available|no instances? (?:are |is )?(?:currently )?available"
    r"|not enough capacity|insufficient capacity|out of capacity",
    re.I,
)


class NoCapacity(RuntimeError):
    """A definite answer that this GPU and data center have no Secure Cloud capacity now."""


def check_catalog(api, cfg):
    query = urllib.parse.urlencode(
        {
            "include": "AVAILABILITY",
            "product": "POD",
            "cloud": CLOUD,
            "count": cfg["gpu_count"],
            "minCudaVersion": cfg["min_cuda_version"],
        }
    )
    gpu = api.request(
        "GET", "/catalog/gpus/" + urllib.parse.quote(cfg["gpu_id"], safe="") + "?" + query
    )
    price = gpu.get("price", {}).get("secure")
    if (
        gpu.get("secure") is not True
        or not isinstance(price, (float, int))
        or not math.isfinite(price)
        or price <= 0
    ):
        raise RuntimeError("A valid Secure Cloud GPU price is unavailable.")
    if price * cfg["gpu_count"] > cfg["max_compute_usd_per_hour"]:
        raise RuntimeError("Live Secure Cloud price exceeds the configured ceiling.")
    dc = next((d for d in gpu.get("dataCenters", []) if d.get("id") == cfg["data_center_id"]), None)
    if not dc or dc.get("availability") not in ("LOW", "MEDIUM", "HIGH"):
        raise NoCapacity(
            "No confirmed Secure Cloud capacity in the selected data center ("
            + cfg["gpu_id"]
            + " in "
            + cfg["data_center_id"]
            + "). No Community fallback."
        )
    center = api.request("GET", "/catalog/datacenters/" + cfg["data_center_id"])
    if "STANDARD" not in center.get("networkVolumeTypes", []):
        raise RuntimeError(
            "Selected data center lacks STANDARD network volumes; no premium fallback."
        )
    return {"checked_utc": utc(), "cloud": CLOUD, "compute_usd_per_hour": price * cfg["gpu_count"]}


def survey(api, gpu_count=1, min_cuda_version=None):
    """Read-only: which Secure Cloud GPU types can be launched now, and in which data centers. Creates nothing.

    A data center is usable when it is on this runner's S3 list and offers STANDARD network volumes. A GPU
    type is listed when Secure Cloud offers it at a valid price and reports capacity (LOW, MEDIUM or HIGH)
    for this GPU count on machines at or above the CUDA floor in at least one usable data center.
    """
    if type(gpu_count) is not int or not 1 <= gpu_count <= 8:
        raise ValueError("The GPU count must be an integer from 1 to 8.")
    floor = cuda_floor()
    min_cuda_version = floor if min_cuda_version is None else min_cuda_version
    if cuda_version(min_cuda_version) < cuda_version(floor):
        raise ValueError(
            "This runner's CUDA floor is "
            + floor
            + " (the "
            + CUDA_FLOOR_VARIABLE
            + " setting); a survey cannot go below it."
        )
    standard, not_in_catalog = {}, []
    for center in sorted(S3_DATACENTERS):
        try:
            info = api.request("GET", "/catalog/datacenters/" + urllib.parse.quote(center, safe=""))
        except ApiError as exc:
            if exc.status != 404:
                raise
            not_in_catalog.append(center)
            continue
        types = info.get("networkVolumeTypes") if isinstance(info, dict) else None
        standard[center] = isinstance(types, list) and "STANDARD" in types
    usable = sorted(center for center, offered in standard.items() if offered)
    query = urllib.parse.urlencode(
        {
            "include": "AVAILABILITY",
            "product": "POD",
            "cloud": CLOUD,
            "count": gpu_count,
            "minCudaVersion": min_cuda_version,
        }
    )
    found = api.request("GET", "/catalog/gpus?" + query)
    gpus = found.get("gpus") if isinstance(found, dict) else found
    if not isinstance(gpus, list):
        raise RuntimeError(
            "Malformed GPU catalog response; the survey has no result. Nothing was created."
        )
    rows = []
    for gpu in gpus:
        if (
            not isinstance(gpu, dict)
            or gpu.get("secure") is not True
            or not isinstance(gpu.get("id"), str)
        ):
            continue
        price = (gpu.get("price") if isinstance(gpu.get("price"), dict) else {}).get("secure")
        if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
            continue
        available = {
            entry["id"]: entry["availability"]
            for entry in gpu.get("dataCenters") or []
            if isinstance(entry, dict)
            and entry.get("id") in usable
            and entry.get("availability") in ("LOW", "MEDIUM", "HIGH")
        }
        if available:
            rows.append(
                {
                    "gpu_id": gpu["id"],
                    "vram_gb": gpu.get("memory"),
                    "usd_per_hour": round(price * gpu_count, 4),
                    "usd_per_gpu_hour": price,
                    "vcpu": gpu.get("minVcpuCount"),
                    "ram_gb": gpu.get("minRam"),
                    "data_centers": dict(sorted(available.items())),
                }
            )
    rows.sort(key=lambda row: (row["usd_per_hour"], row["gpu_id"]))
    return {
        "checked_utc": utc(),
        "cloud": CLOUD,
        "gpu_count": gpu_count,
        "min_cuda_version": min_cuda_version,
        "usable_data_centers": usable,
        "without_standard_volumes": sorted(
            center for center, offered in standard.items() if not offered
        ),
        "not_in_catalog": not_in_catalog,
        "gpus": rows,
        "note": (
            "vCPU and RAM are the lowest the catalog reports for this GPU count. Capacity is a hint, "
            "not a reservation: a launch can still find none."
        ),
        "paid_resources_created": False,
    }


def survey_lines(result):
    """The survey as a table for people."""

    def text(value):
        return "?" if value is None else str(value)

    lines = [
        (
            f"Secure Cloud GPU types with capacity now, {result['gpu_count']} GPU per Pod, CUDA "
            f"{result['min_cuda_version']} or newer (checked {result['checked_utc']}):"
        ),
    ]
    if result["gpus"]:
        lines.append(
            f"  {'GPU':46} {'VRAM GB':>7} {'USD/h':>7} {'vCPU':>5} {'RAM GB':>6}  data centers"
        )
        for row in result["gpus"]:
            lines.append(
                f"  {row['gpu_id'][:46]:46} {text(row['vram_gb']):>7} {row['usd_per_hour']:7.2f} "
                f"{text(row['vcpu']):>5} {text(row['ram_gb']):>6}  "
                + ", ".join(f"{center} {level}" for center, level in row["data_centers"].items())
            )
    else:
        lines.append("  none")
    lines.append(
        "Usable data centers (on the S3 list, with STANDARD volumes): "
        + (", ".join(result["usable_data_centers"]) or "none")
    )
    if result["without_standard_volumes"]:
        lines.append(
            "Not usable, no STANDARD volumes: " + ", ".join(result["without_standard_volumes"])
        )
    if result["not_in_catalog"]:
        lines.append("Not in the catalog: " + ", ".join(result["not_in_catalog"]))
    lines.append(result["note"] + " Nothing was created.")
    return lines
