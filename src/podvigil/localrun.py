"""run-local: run a job's workload on this computer under the workload contract, to check it before paying.

It lays out the folders the Pod gives the workload: the inputs copied into an input folder (each checked by size
and SHA-256, then made read-only), an empty output folder that is the working directory, and a private home with
caches. The workload gets the variables the Pod gives it, built by the Pod's own code (podrt.agent and
podrt.isolation): only the Pod's list of passed-through variables from this environment, the job's
workload_env, the input and output folders, PYTHONPATH set to the input folder, HOME, TMPDIR and the caches.
Its output goes to train.log in the output folder, as in the Pod. It runs until it ends or the workload's share
of the job's time is used up, and then the outcome is written to result.json and the required artifacts are
checked, as recovery would.

It makes no cloud call and reads no credentials. It claims no isolation: the workload runs as you, without the
Pod's separate user or its network and process limits, with this computer's programs instead of the job's
image, and maybe without a GPU.
"""

from pathlib import Path
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import time

from . import NAME
from .controller.launch import runtime_limit
from .jobspec import required_artifacts
from .localprocess import ProcessTree
from .pathsafe import input_paths, portable_paths, safe_relative, validate_input_source
from .podrt.agent import child_environment
from .podrt.isolation import workload_environment
from .podrt.records import save, utc
from .settings import ENV_PREFIXES

LOCAL_JOB = "local"  # the job folder's name here; in the Pod it is the job ID
# Variables Windows programs need to start at all; the Pod's list is for Linux.
WINDOWS_SYSTEM = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "SYSTEMDRIVE",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)
PYTHON_COMMANDS = ("python3", "python")
NOTE = (
    "Not the Pod: no cloud call and no isolation. The workload ran as you, without the "
    "Pod's separate user or its network and process limits, with this computer's programs "
    "instead of the job's image, and maybe without a GPU. Inputs were read-only as far "
    "as this computer's file modes allow."
)


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _workdir(path):
    """The run's folder and whether this run made it: a new temporary folder, or a new or empty one given."""
    if path is None:
        return Path(tempfile.mkdtemp(prefix=NAME + "-run-local-")).resolve(), True
    path = Path(path).resolve()
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ValueError(
                "--workdir must be a new or empty folder: " + str(path) + ". Nothing was run."
            )
        return path, False
    path.mkdir(parents=True)
    return path, True


def _read_only(path, on):
    """Read-only as far as this computer's file modes go; best effort, like the Pod's own folder modes here."""
    try:
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            os.chmod(path, 0o555 if on else 0o755)
        else:
            os.chmod(
                path,
                (0o555 if mode & 0o111 else 0o444) if on else (0o755 if mode & 0o111 else 0o644),
            )
    except OSError:
        pass


def copy_inputs(cfg, folder):
    """Copy each input to folder/<remote>, checked by size and SHA-256 before and after, as the Pod checks them."""
    names = [item["remote"] for item in cfg["inputs"]]
    input_paths(names)
    if os.name == "nt":
        portable_paths(names)
    total, directories = 0, {}
    for item in cfg["inputs"]:
        source = validate_input_source(item["local"])
        parts = tuple(safe_relative(item["remote"]).split("/"))
        target = folder.joinpath(*parts)
        expected = (source.stat().st_size, _digest(source))
        target.parent.mkdir(parents=True, exist_ok=True)
        for index in range(1, len(parts)):
            info = folder.joinpath(*parts[:index]).stat()
            key = info.st_dev, info.st_ino
            if directories.setdefault(key, parts[:index]) != parts[:index]:
                raise ValueError("Input paths collide as files or directories.")
        # Exclusive creation also catches aliases on case-insensitive non-Windows filesystems.
        with open(source, "rb") as original, open(target, "xb") as copied:
            shutil.copyfileobj(original, copied, 4 * 1024 * 1024)
        if (target.stat().st_size, _digest(target)) != expected:
            raise RuntimeError(
                "Input " + item["remote"] + " changed while it was copied. Nothing was run."
            )
        total += expected[0]
    return {"files": len(cfg["inputs"]), "bytes": total}


def environment(cfg, output, inputs, home):
    """The workload's variables, built by the Pod's own code; plus what Windows needs to start a program."""
    variables = child_environment(os.environ, output, inputs, cfg["workload_env"], ENV_PREFIXES)
    if os.name == "nt":
        variables.update({name: os.environ[name] for name in WINDOWS_SYSTEM if name in os.environ})
    variables = workload_environment(variables, {"home": str(home)})
    if os.name == "nt":
        variables.update(USERPROFILE=str(home), TEMP=str(home / "tmp"), TMP=str(home / "tmp"))
    return variables


def resolve_command(command, variables, cwd, python=None):
    """The command to run, with python3 or python replaced by --python when given; refuses a missing program.

    As in the Pod, a bare name is looked up on the workload's PATH and a relative path starts at its working
    folder. On Windows the Microsoft Store's app aliases are refused: they may open the Store instead of running.
    """
    argv = list(command)
    substituted = None
    if python is not None and argv[0] in PYTHON_COMMANDS:
        python = str(python)
        if os.sep in python or "/" in python:
            # Resolve from the directory where the command was given.
            python = str(Path(python).resolve())
        substituted = {"from": argv[0], "to": python}
        argv[0] = python
    hint = (
        " For a python3 or python command, name the Python to use with --python. Nothing was run."
    )
    if os.sep not in argv[0] and "/" not in argv[0]:
        found = shutil.which(argv[0], path=variables.get("PATH"))
        if found is None:
            raise ValueError(
                f"The job's command {argv[0]!r} is not on this computer's PATH." + hint
            )
        if os.name == "nt" and "\\windowsapps\\" in found.lower():
            raise ValueError(
                f"The job's command {argv[0]!r} is only a Microsoft Store app alias here ({found})."
                + hint
            )
        argv[0] = found
    else:
        path = Path(argv[0]) if Path(argv[0]).is_absolute() else Path(cwd) / argv[0]
        if not path.is_file():
            raise ValueError(
                f"The job's command {argv[0]!r} does not exist on this computer ({path})." + hint
            )
        argv[0] = str(path)
    return argv, substituted


def run_local(cfg, workdir=None, timeout=None, python=None):
    """Run the workload here under the workload contract; return what happened. See the module docstring."""
    limit = runtime_limit(cfg) - cfg["finalize_grace_seconds"] if timeout is None else timeout
    if type(limit) not in (int, float) or not 0 < limit <= 7 * 86400:
        raise ValueError("The time limit must be more than 0 seconds and at most seven days.")
    root, made = _workdir(workdir)
    inputs, home = root / "inputs", root / "home"
    output = root / "jobs" / LOCAL_JOB / "artifacts"
    try:
        for path in (inputs, output, home / "cache", home / "tmp"):
            path.mkdir(parents=True, exist_ok=True)
        variables = environment(cfg, output, inputs, home)
        argv, substituted = resolve_command(cfg["command"], variables, output, python)
        copied = copy_inputs(cfg, inputs)
    except BaseException:
        # Nothing ran: remove what this run made, never a folder that was there before.
        for name in ("inputs", "jobs", "home"):
            shutil.rmtree(root / name, ignore_errors=True)
        if made:
            shutil.rmtree(root, ignore_errors=True)
        raise

    copies = sorted(inputs.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in [*copies, inputs]:
        _read_only(path, True)
    warnings = []
    result_path, log_path = output / "result.json", output / "train.log"
    began, start = utc(), time.monotonic()
    code, outcome = None, "not_started"
    try:
        with open(log_path, "ab", buffering=0) as log:
            tree = ProcessTree(argv, output, variables, log)
            child = tree.process
            try:
                code = child.wait(timeout=limit)
                outcome = "success" if code == 0 else "training_failed"
            except subprocess.TimeoutExpired:
                outcome = "time_limit"
            except KeyboardInterrupt:
                outcome = "interrupted"
            finally:
                tree.close()
                if code is None:
                    code = child.returncode
    finally:
        for path in [inputs, *reversed(copies)]:
            _read_only(path, False)  # so that the folder is easy to inspect and remove

    seconds = round(time.monotonic() - start, 3)
    if result_path.exists():
        warnings.append(
            "The workload wrote result.json; the Pod replaces it with its own record, as here."
        )
    save(
        result_path,
        {
            "job_id": LOCAL_JOB,
            "pod_id": None,
            "outcome": outcome,
            "exit_code": code,
            "started_utc": began,
            "finished_utc": utc(),
        },
    )
    missing = [name for name in required_artifacts(cfg) if not (output / name).is_file()]
    if outcome == "success" and missing:
        warnings.append(
            "The job succeeded but required artifacts are missing; recovery would keep the volume "
            "and report the job incomplete."
        )
    files = sorted(path for path in output.rglob("*") if path.is_file() and not path.is_symlink())
    return {
        "workload_outcome": outcome,
        "workload_exit_code": code,
        "seconds": seconds,
        "time_limit_seconds": limit,
        "command": argv,
        "command_substituted": substituted,
        "workdir": str(root),
        "input_dir": str(inputs),
        "output_dir": str(output),
        "log": str(log_path),
        "inputs": copied,
        "outputs": [
            {"path": path.relative_to(output).as_posix(), "size": path.stat().st_size}
            for path in files[:500]
        ],
        "outputs_listed": min(len(files), 500),
        "outputs_total": len(files),
        "required_artifacts_missing": missing,
        "warnings": warnings,
        "contract": {
            name: variables[name]
            for name in sorted(variables)
            if name.endswith(("_OUTPUT_DIR", "_INPUT_DIR"))
            or name in ("PYTHONPATH", "HOME", "TMPDIR")
        },
        "cloud_calls": 0,
        "credentials_read": False,
        "note": NOTE,
    }
