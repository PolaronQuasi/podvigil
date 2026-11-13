"""The job file: its fields and defaults, their validation, launch candidates, folder and pattern inputs."""

from pathlib import Path
import copy
import json
import math
import os
import re
import stat

from .pathsafe import (
    credential_name,
    input_paths,
    is_link_or_reparse,
    portable_paths,
    safe_relative,
    validate_input_source,
)
from .policy import CLOUD, S3_DATACENTERS, STANDARD_ARTIFACTS, VOLUME_TYPE, cuda_version
from .settings import CUDA_FLOOR_VARIABLE, ENV_PREFIXES, cuda_floor

# A job file may list launch candidates in order; each sets these fields and nothing else.
CANDIDATE_FIELDS = frozenset(
    {"gpu_id", "data_center_id", "max_compute_usd_per_hour", "max_compute_usd"}
)
MAX_CANDIDATES = 8

# A job cannot set names reserved by the runner (both prefixes), Runpod or AWS.
RESERVED_PREFIXES = ("RUNPOD_", "AWS_", *(prefix + "_" for prefix in ENV_PREFIXES))

# The only limit on the input count catches a mistyped folder such as a whole disk.
MAX_EXPANDED_INPUTS = 10000

DEFAULTS = {
    "cloud": CLOUD,
    "gpu_count": 1,
    "disk_gb": 30,
    "volume_gb": 20,
    "preparation_timeout_seconds": 1800,
    "startup_timeout_seconds": 900,
    "finalize_grace_seconds": 120,
    "poll_seconds": 10,
    "controller_stale_seconds": 180,
    "agent_stale_seconds": 180,
    "inputs": [],
    "required_artifacts": [],
    "self_termination_probe": False,
    "self_termination_grace_seconds": 60,
    "volume_type": VOLUME_TYPE,
    "workload_env": {},
}

REQUIRED = {
    "cloud",
    "image",
    "gpu_id",
    "data_center_id",
    "min_cuda_version",
    "command",
    "max_runtime_seconds",
    "max_compute_usd_per_hour",
    "max_compute_usd",
    "gpu_count",
    "disk_gb",
    "volume_gb",
    "preparation_timeout_seconds",
    "startup_timeout_seconds",
    "finalize_grace_seconds",
    "poll_seconds",
    "controller_stale_seconds",
    "agent_stale_seconds",
    "inputs",
    "required_artifacts",
    "self_termination_probe",
    "self_termination_grace_seconds",
    "volume_type",
    "workload_env",
}


def finite_positive(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def validate(config):
    cfg = {**DEFAULTS, **config}
    if set(cfg) != REQUIRED:
        raise ValueError(f"Missing/unknown job fields: {sorted(set(cfg) ^ REQUIRED)}")
    if cfg["cloud"] != CLOUD:
        raise ValueError("Only SECURE cloud is permitted. There is no Community fallback.")
    if cfg["volume_type"] != VOLUME_TYPE:
        raise ValueError("Only STANDARD network volumes are permitted.")
    if type(cfg["self_termination_probe"]) is not bool:
        raise ValueError("self_termination_probe must be a boolean.")

    variables = cfg["workload_env"]
    if not isinstance(variables, dict):
        raise ValueError("workload_env must be an object of string values.")
    for name, value in variables.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)
            or name.startswith(RESERVED_PREFIXES)
            or any(
                word in name for word in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "CREDENTIAL")
            )
            or name in ("PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD")
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise ValueError("Unsafe declared workload environment variable.")
    cfg["workload_env"] = dict(variables)

    for name in ("image", "gpu_id", "data_center_id", "min_cuda_version"):
        if not isinstance(cfg[name], str) or not cfg[name].strip():
            raise ValueError(f"{name} must be nonempty.")
    image = cfg["image"]
    if "@sha256:" not in image and (
        ":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest")
    ):
        raise ValueError(
            "Pin the container image to a specific tag or digest; latest is forbidden."
        )
    if not re.fullmatch(r"\d+\.\d+", cfg["min_cuda_version"]):
        raise ValueError("min_cuda_version must be major.minor.")
    if cfg["data_center_id"] not in S3_DATACENTERS:
        raise ValueError(
            "Select a documented Runpod S3 data center; update the reviewed list if needed."
        )

    for name in (
        "gpu_count",
        "disk_gb",
        "volume_gb",
        "max_runtime_seconds",
        "preparation_timeout_seconds",
        "startup_timeout_seconds",
        "finalize_grace_seconds",
        "poll_seconds",
        "controller_stale_seconds",
        "agent_stale_seconds",
        "self_termination_grace_seconds",
    ):
        if type(cfg[name]) is not int or cfg[name] < 1:
            raise ValueError(f"{name} must be a positive integer.")
    if cfg["gpu_count"] > 8 or not 10 <= cfg["volume_gb"] <= 4096:
        raise ValueError("GPU count or volume size is outside this runner's bounds.")
    if not 300 <= cfg["max_runtime_seconds"] <= 7 * 86400:
        raise ValueError("Runtime must be between five minutes and seven days.")
    if not 60 <= cfg["preparation_timeout_seconds"] <= 86400:
        raise ValueError("Preparation timeout must be between one minute and one day.")
    if not 1 <= cfg["self_termination_grace_seconds"] <= 3600:
        raise ValueError("Self-termination grace must be between one second and one hour.")
    if not 30 <= cfg["finalize_grace_seconds"] < cfg["max_runtime_seconds"] / 2:
        raise ValueError(
            "Reserve at least 30 seconds and less than half the runtime for finalization."
        )
    if cfg["startup_timeout_seconds"] >= cfg["max_runtime_seconds"]:
        raise ValueError("Startup timeout must be shorter than the total runtime.")
    if not 2 <= cfg["poll_seconds"] <= 30:
        raise ValueError("Polling must be between 2 and 30 seconds.")
    if min(cfg["controller_stale_seconds"], cfg["agent_stale_seconds"]) < 6 * cfg["poll_seconds"]:
        raise ValueError("Heartbeat grace must be at least six polling intervals.")

    for name in ("max_compute_usd", "max_compute_usd_per_hour"):
        if not finite_positive(cfg[name]):
            raise ValueError(f"{name} must be a finite positive number.")

    cmd = cfg["command"]
    if (
        not isinstance(cmd, list)
        or not cmd
        or not all(isinstance(x, str) and "\0" not in x for x in cmd)
        or not cmd[0]
    ):
        raise ValueError("command must be a nonempty argv array, not a shell string.")

    cfg["required_artifacts"] = required_artifacts(cfg)
    if not isinstance(cfg["inputs"], list):
        raise ValueError("inputs must be a list of explicit files.")
    names = set()
    for item in cfg["inputs"]:
        if not isinstance(item, dict) or set(item) != {"local", "remote"}:
            raise ValueError("Each input must contain only local and remote paths.")
        name = safe_relative(item["remote"])
        if name in names or credential_name(name.split("/")[-1]):
            raise ValueError("Duplicate input or credential file is forbidden.")
        names.add(name)
        if not isinstance(item["local"], str) or not item["local"]:
            raise ValueError("Input source must be a nonempty local path.")
        if credential_name(Path(item["local"]).name):
            raise ValueError("Never upload a .env file.")

    input_paths(item["remote"] for item in cfg["inputs"])
    return cfg


def check_cuda_floor(cfg):
    """A new job may not ask for less than the CUDA floor (settings.cuda_floor). This is not part of
    validate(): the watchdog and recovery validate stored jobs, which a floor set later must never refuse."""
    floor = cuda_floor()
    if cuda_version(cfg["min_cuda_version"]) < cuda_version(floor):
        raise ValueError(
            f"min_cuda_version {cfg['min_cuda_version']} is below this runner's CUDA floor "
            f"{floor} (the {CUDA_FLOOR_VARIABLE} setting)."
        )
    return cfg


def required_artifacts(config):
    names = config.get("required_artifacts", [])
    if not isinstance(names, list):
        raise ValueError(
            "required_artifacts must be a list of paths relative to the output directory."
        )
    portable_paths([*STANDARD_ARTIFACTS, *names])
    return list(names)


def _plain_file(path, origin):
    """True for a regular file, False for a folder. Links, reparse points, special and credential files
    are rejected, never skipped."""
    info = path.lstat()
    if is_link_or_reparse(info):
        raise ValueError(
            f"Input {origin} reaches a link or reparse point, which is never followed: {path}"
        )
    if stat.S_ISDIR(info.st_mode):
        return False
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Input {origin} reaches a special file: {path}")
    if credential_name(path.name):
        raise ValueError(
            f"Input {origin} contains a credential file, which is never uploaded: {path}"
        )
    return True


def _remote_name(prefix, relative, origin, path):
    try:
        return safe_relative(relative if prefix is None else prefix + "/" + relative)
    except ValueError:
        raise ValueError(
            f"Input {origin} has a file whose name cannot be an input name: {path}"
        ) from None


def _folder(value, base, origin):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"Input {origin} needs a nonempty folder path.")
    folder = Path(value)
    folder = Path(os.path.abspath(folder if folder.is_absolute() else base / folder))
    try:
        info = folder.lstat()
    except FileNotFoundError:
        raise ValueError(f"Input {origin} names a folder that does not exist: {folder}") from None
    if is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(
            f"Input {origin} is not a plain folder (links are never followed): {folder}"
        )
    return folder


def _directory_inputs(item, base):
    if set(item) != {"directory", "remote"}:
        raise ValueError('A directory input has exactly "directory" and "remote".')
    origin = "directory " + json.dumps(item["directory"])
    prefix = safe_relative(item["remote"])
    top = _folder(item["directory"], base, origin)

    def unreadable(error):
        raise ValueError(f"Input {origin} has a folder that cannot be read: {error.filename}")

    found = []
    for current, folders, files in os.walk(top, onerror=unreadable, followlinks=False):
        current = Path(current)
        folders.sort()
        for name in folders:
            if is_link_or_reparse((current / name).lstat()):
                raise ValueError(
                    f"Input {origin} reaches a link or reparse point, which is never followed: "
                    f"{current / name}"
                )
        for name in sorted(files):
            path = current / name
            if _plain_file(path, origin):
                found.append(
                    {
                        "local": str(path),
                        "remote": _remote_name(
                            prefix, path.relative_to(top).as_posix(), origin, path
                        ),
                    }
                )
                if len(found) > MAX_EXPANDED_INPUTS:
                    raise ValueError(
                        f"Input {origin} holds more than {MAX_EXPANDED_INPUTS:,} files."
                    )
    if not found:
        raise ValueError(f"Input {origin} holds no files.")
    return found


def _pattern_inputs(item, base):
    if not {"pattern"} <= set(item) <= {"pattern", "remote", "base"}:
        raise ValueError('A pattern input has "pattern" and optionally "remote" and "base".')
    pattern = item["pattern"]
    origin = "pattern " + json.dumps(pattern)
    if (
        not isinstance(pattern, str)
        or not pattern
        or "\\" in pattern
        or ":" in pattern
        or "\0" in pattern
        or pattern.startswith("/")
        or "**" in pattern
        or any(part in ("", ".", "..") for part in pattern.split("/"))
    ):
        raise ValueError(
            f"Input {origin} must be relative, separated by '/', without '.', '..' or '**' "
            "(use a directory input for a whole tree)."
        )
    prefix = safe_relative(item["remote"]) if "remote" in item else None
    root = _folder(item.get("base", "."), base, origin)
    found = []
    for path in sorted(root.glob(pattern)):
        if _plain_file(path, origin):
            found.append(
                {
                    "local": str(path),
                    "remote": _remote_name(prefix, path.relative_to(root).as_posix(), origin, path),
                }
            )
            if len(found) > MAX_EXPANDED_INPUTS:
                raise ValueError(f"Input {origin} matches more than {MAX_EXPANDED_INPUTS:,} files.")
    if not found:
        raise ValueError(f"Input {origin} matched no files below {root}.")
    return found


def expand_inputs(items, base):
    """Turn directory and pattern entries into the explicit file list that the rest of the runner uses.

    {"directory": "data", "remote": "data"}: every file below the folder, recursively, as data/<path>.
    {"pattern": "configs/*.json", "remote": "project", "base": "../.."}: the files the pattern matches below
    base (default: the job file's folder), named project/<path below base>; "remote" is optional.
    Relative paths start at the job file's folder. Explicit {"local", "remote"} entries pass unchanged.
    Every resulting file still passes validate() and validate_input_source().
    """
    if not isinstance(items, list):
        raise ValueError("inputs must be a list.")
    expanded = []
    for item in items:
        if isinstance(item, dict) and "directory" in item:
            expanded.extend(_directory_inputs(item, base))
        elif isinstance(item, dict) and "pattern" in item:
            expanded.extend(_pattern_inputs(item, base))
        else:
            expanded.append(item)
        if len(expanded) > MAX_EXPANDED_INPUTS:
            raise ValueError(f"The inputs expand to more than {MAX_EXPANDED_INPUTS:,} files.")
    return expanded


def _finish(config, base):
    config = check_cuda_floor(validate(config))
    for item in config["inputs"]:
        local = Path(item["local"])
        if not local.is_absolute():
            local = base / local
        item["local"] = str(validate_input_source(local))
    return config


def read_job(path):
    """The job file's validated configurations in launch order: one, or one per candidate.

    A candidate sets gpu_id and data_center_id, and may set its own max_compute_usd_per_hour and
    max_compute_usd; every other field is shared. Directory and pattern inputs are expanded here.
    """
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("A job file holds one JSON object.")
    base = path.resolve().parent
    if "inputs" in raw:
        raw["inputs"] = expand_inputs(raw["inputs"], base)
    if "candidates" not in raw:
        return [_finish(raw, base)]
    candidates = raw.pop("candidates")
    if "gpu_id" in raw or "data_center_id" in raw:
        raise ValueError(
            "With candidates, give gpu_id and data_center_id in each candidate, "
            "not at the top level."
        )
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError(f"candidates must be a list of 1 to {MAX_CANDIDATES} entries.")
    configs, pairs = [], set()
    for index, candidate in enumerate(candidates, 1):
        if (
            not isinstance(candidate, dict)
            or not {"gpu_id", "data_center_id"} <= set(candidate) <= CANDIDATE_FIELDS
        ):
            raise ValueError(
                f"Candidate {index} needs gpu_id and data_center_id and may also set only "
                "max_compute_usd_per_hour and max_compute_usd."
            )
        # Any JSON value can be compared here. validate() checks the types.
        pair = json.dumps([candidate["gpu_id"], candidate["data_center_id"]])
        if pair in pairs:
            raise ValueError(f"Candidate {index} repeats an earlier GPU and data center.")
        pairs.add(pair)
        configs.append(_finish({**copy.deepcopy(raw), **candidate}, base))
    return configs


def read_config(path):
    """The single validated configuration of a job file without candidates."""
    configs = read_job(path)
    if len(configs) != 1:
        raise ValueError(
            "This job file lists several candidates; plan and launch take them in order."
        )
    return configs[0]
