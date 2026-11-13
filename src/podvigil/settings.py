"""Where the runner keeps its own state, and which file holds its credentials. Neither is the source tree.

State (the job ledgers and the API pacing file): the --state-dir option, else the <NAME>_STATE_DIR
environment variable, else a per-user folder (default_state_dir). Ledgers that versions before 0.1.0 wrote
under the repository's output/jobs stay readable: every job command takes the ledger folder's path.

Credentials: the --env-file option, else the file the <NAME>_ENV_FILE environment variable names, else the
per-user file (user_env_file), else, last, the .env in the repository root, where versions before 0.1.0
kept them. The watchdog gets both choices on its command line.
"""

from pathlib import Path
import hashlib
import os
import sys

from . import NAME
from .policy import MIN_CUDA_VERSION, cuda_version

ROOT = Path(__file__).resolve().parents[2]  # the repository root of a checkout
# This version never writes to the state folder used before 0.1.0.
LEGACY_STATE_DIR = ROOT / "output"

STATE_DIR_VARIABLE = NAME.upper() + "_STATE_DIR"
ENV_FILE_VARIABLE = NAME.upper() + "_ENV_FILE"
ENV_PREFIX = NAME.upper()  # the tool's own prefix for environment variable names
LEGACY_ENV_PREFIX = "WONDOMON"  # the prefix before 0.1.0: still read, and still given to workloads
PREVIOUS_ENV_PREFIX = "PODWARDEN"  # the unreleased working name
ENV_PREFIXES = (ENV_PREFIX, PREVIOUS_ENV_PREFIX, LEGACY_ENV_PREFIX)
CUDA_FLOOR_VARIABLE = ENV_PREFIX + "_MIN_CUDA_VERSION"
LEGACY_ENV_FILE = ROOT / ".env"  # read last, so that existing checkouts keep working
_chosen = {"state_dir": None, "env_file": None, "program": None}


def configure(*, state_dir=None, env_file=None, program=None):
    """Record this process's command-line choices; None means not given."""
    _chosen["state_dir"] = None if state_dir is None else Path(state_dir).expanduser().resolve()
    _chosen["env_file"] = None if env_file is None else Path(env_file).expanduser().resolve()
    _chosen["program"] = program


def program():
    """The command the user runs, for the advice in messages: managed_run.py in a checkout, python -m <NAME>,
    or, by default, the installed <NAME> command."""
    return _chosen["program"] or NAME


def default_state_dir():
    """%LOCALAPPDATA%\\<NAME> on Windows, ~/Library/Application Support/<NAME> on macOS, and
    $XDG_STATE_HOME/<NAME> (default ~/.local/state/<NAME>) elsewhere."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / NAME
    xdg = os.environ.get("XDG_STATE_HOME")
    return (
        Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "state"
    ) / NAME


def state_dir():
    if _chosen["state_dir"] is not None:
        return _chosen["state_dir"]
    named = os.environ.get(STATE_DIR_VARIABLE) or os.environ.get(PREVIOUS_ENV_PREFIX + "_STATE_DIR")
    return Path(named).expanduser().resolve() if named else default_state_dir()


def real_path(path):
    """`path` as it really is on disk: the path every process of this user sees.

    A shell inside a packaged Windows app writes a new folder under %LOCALAPPDATA% to the app's private copy
    (...\\AppData\\Local\\Packages\\<app>\\LocalCache\\Local\\...), and only processes inside that app find it at
    the usual path. Resolving names the private copy, which any process can open. Resolve once the folder or
    file exists: before that there is nothing to resolve.
    """
    return Path(os.path.realpath(path))


def real_state_dir():
    """The state folder, created if missing, by its real path (real_path). The watchdog gets this one."""
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return real_path(folder)


def cuda_floor():
    """The lowest host CUDA version a new job may ask for: the <NAME>_MIN_CUDA_VERSION setting, else the
    default (policy.MIN_CUDA_VERSION, 12.8). The watchdog and recovery never apply it to a stored job."""
    value = (
        os.environ.get(CUDA_FLOOR_VARIABLE)
        or os.environ.get(PREVIOUS_ENV_PREFIX + "_MIN_CUDA_VERSION")
        or MIN_CUDA_VERSION
    )
    try:
        cuda_version(value)
    except ValueError:
        raise ValueError(
            f"{CUDA_FLOOR_VARIABLE} must be major.minor, for example {MIN_CUDA_VERSION}; "
            f"it is {value!r}."
        ) from None
    return value


def jobs_dir():
    """New job ledgers go here, one folder per job."""
    return state_dir() / "jobs"


def pacing_file(key=None):
    """The account's API pacing state, shared by every controller and watchdog that uses this state folder.

    One file per API key, named by a short one-way fingerprint of it, so that a new key starts with fresh
    pacing instead of being refused by the old key's file. The key itself is never written.
    """
    if key is None:
        return state_dir() / "api-rate-limit.json"
    # Keep the namespace stable so an old watchdog and a renamed controller
    # share the same quota file.
    tag = hashlib.sha256((PREVIOUS_ENV_PREFIX.lower() + " pacing\0" + key).encode()).hexdigest()[
        :16
    ]
    return state_dir() / ("api-rate-limit-" + tag + ".json")


def user_env_file():
    """%LOCALAPPDATA%\\<NAME>\\credentials.env on Windows, ~/Library/Application Support/<NAME>/credentials.env
    on macOS, and $XDG_CONFIG_HOME/<NAME>/credentials.env (default ~/.config/<NAME>/credentials.env) elsewhere."""
    if os.name == "nt":
        folder = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / NAME
    elif sys.platform == "darwin":
        folder = Path.home() / "Library" / "Application Support" / NAME
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        folder = (Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config") / NAME
    return folder / "credentials.env"


def named_env_file():
    """The file named by --env-file, else by <NAME>_ENV_FILE; None when neither names one."""
    named = (
        _chosen["env_file"]
        or os.environ.get(ENV_FILE_VARIABLE)
        or os.environ.get(PREVIOUS_ENV_PREFIX + "_ENV_FILE")
    )
    return Path(named).expanduser().resolve() if named else None


def env_file():
    """The credentials file this process reads (see the module docstring). A file named by the option or
    the variable must exist and is never skipped; the repository's .env, the last resort, may be missing."""
    named = named_env_file()
    if named is not None:
        if not named.is_file():
            raise ValueError(
                "The credentials file named by --env-file or "
                + ENV_FILE_VARIABLE
                + " does not exist: "
                + str(named)
            )
        return named
    user = user_env_file()
    previous = previous_user_env_file()
    return user if user.is_file() else previous if previous.is_file() else LEGACY_ENV_FILE


def previous_user_env_file():
    """The working name's credentials remain readable; never move or rewrite them during an upgrade."""
    current = user_env_file()
    return current.parent.with_name(PREVIOUS_ENV_PREFIX.lower()) / current.name


def credential_files():
    """Every file that may hold this runner's credentials. No input may be one of them or a link to one."""
    return [
        path
        for path in (named_env_file(), user_env_file(), previous_user_env_file(), LEGACY_ENV_FILE)
        if path is not None
    ]


def watchdog_options():
    """The options that give the watchdog this process's choices, by their real paths (real_path): the watchdog
    runs outside this process's tree, and on Windows it does not inherit the environment."""
    options = ["--state-dir", str(real_state_dir())]
    found = env_file()
    if found.is_file():
        options += ["--env-file", str(real_path(found))]
    return options
