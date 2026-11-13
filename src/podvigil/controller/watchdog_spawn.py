"""Launch a venv-preserving watchdog outside the controller's live process tree.

On Windows service-mediated process creation avoids inheriting enclosing Job
Objects. The resulting process is checked before paid creation is permitted.
The handshake uses a nonce, never Popen.pid (the Windows venv redirector differs).
"""

from pathlib import Path
import ctypes
import hmac
import json
import os
import re
import subprocess
import sys
import time
import uuid
import runpy
import site
import base64

from ..podrt.records import read, save
from ..settings import ENV_PREFIX, ENV_PREFIXES

# The broker gives the watchdog the folder holding the arming records. If the
# watchdog cannot see the ledger, it reports where it looked in that folder
# (report_unseen_ledger).
ARM_FOLDER = ENV_PREFIX + "_ARM_FOLDER"

# The watchdog still reads the old environment variable name for the arming nonce.
ARM_TOKEN = ENV_PREFIX + "_ARM_TOKEN"
ARM_TOKEN_NAMES = tuple(prefix + "_ARM_TOKEN" for prefix in ENV_PREFIXES)

# The broker uses this exit code when it cannot read the arming record because
# the controller's folder is invisible outside its process tree.
LEDGER_UNSEEN_EXIT = 3


def written_at(nominal, folder):
    """Where the controller wrote the ledger: its path, and the real path when the two differ."""
    if nominal is None or os.path.normcase(os.path.abspath(nominal)) == os.path.normcase(
        str(folder)
    ):
        return str(folder)
    return f"{nominal} (on disk: {folder})"


def ledger_unseen(nominal, folder, looked_in=None):
    """Why arming failed when the watchdog's side cannot see the job ledger, with both paths named."""
    seen = (
        f"; the watchdog looked for it at {looked_in} and found no job.json there"
        if looked_in is not None
        else "; the watchdog's starter, outside this process's tree, cannot read it there"
    )
    return (
        "The watchdog cannot see the job ledger. This controller wrote it at "
        + written_at(nominal, folder)
        + seen
        + ". On Windows a shell inside a packaged app keeps new folders under "
        "%LOCALAPPDATA% in the app's private copy, which other processes do not see "
        "at the usual path; give --state-dir a folder outside %LOCALAPPDATA%."
    )


def arming_token():
    """The arming nonce the broker gave this process, under either name; None outside arming."""
    return next((os.environ[name] for name in ARM_TOKEN_NAMES if os.environ.get(name)), None)


def report_unseen_ledger(path):
    """Called by a watchdog that cannot see the ledger it was given. During arming it leaves, beside the
    controller's arming record, where it looked: paths only. Creates no folder; does nothing outside arming;
    never raises."""
    try:
        token, folder = arming_token(), os.environ.get(ARM_FOLDER)
        if not token or not folder or not re.fullmatch(r"[0-9a-f]{32}", token):
            return
        stem = Path(folder) / ("watchdog-arm-" + token)
        if stem.with_suffix(".launch.json").is_file():
            save(
                stem.with_suffix(".unseen.json"),
                {
                    "token": token,
                    "pid": os.getpid(),
                    "looked_in": str(path),
                    "real_path": os.path.realpath(path),
                },
            )
    except Exception:
        pass


def relaunch_code():
    """This module in a fresh interpreter, which cannot run a package member by its path: put the folder
    that holds the package first on the path, in place of the current directory, and run the module as
    __main__. The arguments after -c reach it unchanged. The module's own name is __spec__.name: in the
    broker it runs as __main__."""
    name = __spec__.name
    return "import runpy,sys;sys.path[0]={!r};runpy.run_module({!r},run_name='__main__',alter_sys=True)".format(
        str(Path(__file__).resolve().parents[name.count(".")]), name
    )


def _kernel():
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    return k


def in_job(pid):
    if os.name != "nt":
        return False
    from ctypes import wintypes

    k = _kernel()
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        answer = wintypes.BOOL()
        if not k.IsProcessInJob(handle, None, ctypes.byref(answer)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(answer.value)
    finally:
        k.CloseHandle(handle)


def live_ancestors(pid):
    if os.name != "nt":
        return []  # start_new_session supplies POSIX session isolation.
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k = _kernel()
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    result = []
    while pid in parents and parents[pid] in parents:
        pid = parents[pid]
        if pid in result:
            raise RuntimeError("Unverifiable watchdog process ancestry")
        result.append(pid)
    return result


class Monitor:
    def __init__(self, pid):
        self.pid = pid
        self.handle = None
        if os.name == "nt":
            self.handle = _kernel().OpenProcess(0x1000, False, pid)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())

    def poll(self):
        if os.name == "nt":
            from ctypes import wintypes

            code = wintypes.DWORD()
            if not _kernel().GetExitCodeProcess(self.handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return None if code.value == 259 else code.value
        try:
            os.kill(self.pid, 0)
            return None
        except ProcessLookupError:
            return 0

    def close(self):
        if self.handle:
            _kernel().CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


def compare_credentials(observed, expected):
    """Names of the credentials whose keyed hashes differ. Never the hashes or the values."""
    if not isinstance(observed, dict) or not isinstance(expected, dict):
        return {"same": False, "different": ["no credential proof from the watchdog"]}

    def same(a, b):
        if a is None or b is None:
            return a is None and b is None
        return isinstance(a, str) and isinstance(b, str) and hmac.compare_digest(a, b)

    different = sorted(
        name
        for name in set(observed) | set(expected)
        if not same(observed.get(name), expected.get(name))
    )
    return {"same": not different, "different": different}


def credential_mismatch(different):
    return (
        "The watchdog resolved different credentials than this controller: "
        + ", ".join(different)
        + ". "
        "On Windows the watchdog starts through WMI and does not inherit this shell's "
        "environment variables, so keys set only in the environment are missing there. "
        "Put the keys in the .env file."
    )


def arm(folder, credential_proof=None):
    """Called by the actual watchdog after acquiring its single-instance lock.

    credential_proof(salt) returns keyed hashes of the credentials this process resolved; the
    controller compares them with its own before it creates anything.
    """
    token = arming_token()
    if not token:
        return
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise RuntimeError("Invalid watchdog arming nonce")
    stem = Path(folder) / ("watchdog-arm-" + token)
    credentials = None
    if credential_proof is not None:
        try:
            salt = (read(stem.with_suffix(".launch.json"), {}) or {}).get("credential_salt")
            if isinstance(salt, str) and re.fullmatch(r"[0-9a-f]{32}", salt):
                credentials = credential_proof(salt)
        except Exception:
            credentials = None  # the controller sees a missing proof and refuses
    save(
        stem.with_suffix(".json"),
        {
            "token": token,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "in_job": in_job(os.getpid()),
            "executable": sys.executable,
            "credentials": credentials,
        },
    )
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if stem.with_suffix(".cancel").exists():
            raise RuntimeError("Watchdog arming cancelled")
        if stem.with_suffix(".accepted").exists():
            return
        time.sleep(0.05)
    raise RuntimeError("Controller did not accept watchdog isolation")


def spawn(folder, command, credential_proof=None, require_same_credentials=False, *, nominal=None):
    """Start and verify the watchdog. With credential_proof, the watchdog's keyed credential hashes are
    compared with this process's; require_same_credentials refuses to arm on any difference.

    `folder` is used by its real path, the one the watchdog gets; `nominal`, the path the ledger was made
    under, is only named in the error when the watchdog's side cannot see the ledger (ledger_unseen)."""
    folder = Path(folder).resolve()
    token = uuid.uuid4().hex
    salt = uuid.uuid4().hex
    stem = folder / ("watchdog-arm-" + token)
    spec = stem.with_suffix(".launch.json")
    if require_same_credentials and credential_proof is None:
        raise ValueError("A credential check needs a credential_proof function.")
    save(
        spec,
        {
            "command": command,
            "folder": str(folder),
            "token": token,
            "venv_prefix": sys.prefix,
            "venv_executable": sys.executable,
            "base_executable": getattr(sys, "_base_executable", sys.executable),
            "credential_salt": salt,
        },
    )

    options = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(Path(__file__).parent),
    }
    if os.name != "nt":
        options["start_new_session"] = True
    broker = None
    try:
        broker_command = [
            getattr(sys, "_base_executable", sys.executable),
            "-B",
            "-c",
            relaunch_code(),
            str(spec),
        ]
        if os.name == "nt":
            # Win32_Process.Create is service-mediated: unlike CreateProcess,
            # it does not inherit the caller's Job Object (including nested jobs
            # that disallow breakaway). No scheduled task or elevated service is
            # installed. Still verify the actual watchdog's job/ancestry below.
            literal = lambda s: "'" + s.replace("'", "''") + "'"
            ps = (
                "$ErrorActionPreference='Stop';"
                "$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[UInt16]0};"
                "$result=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{"
                "CommandLine=" + literal(subprocess.list2cmdline(broker_command)) + ";"
                "CurrentDirectory="
                + literal(str(Path(__file__).parent))
                + ";ProcessStartupInformation=$startup};"
                "$result | Select-Object ReturnValue,ProcessId | ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    base64.b64encode(ps.encode("utf-16-le")).decode(),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
                **options,
            )
            if result.returncode:
                save(
                    folder / "watchdog-launch-error.json",
                    {"returncode": result.returncode, "stderr": result.stderr[:4000]},
                )
                raise RuntimeError(
                    "Windows service-mediated watchdog launch failed; "
                    "see watchdog-launch-error.json"
                )
            launch = json.loads(result.stdout)
            if launch.get("ReturnValue") != 0:
                raise RuntimeError(
                    "Windows refused watchdog process creation: " + str(launch.get("ReturnValue"))
                )
            try:
                broker_monitor = Monitor(launch["ProcessId"])
            except OSError as exc:
                if getattr(exc, "winerror", None) != 87:
                    raise
            else:
                end = time.monotonic() + 10
                while broker_monitor.poll() is None and time.monotonic() < end:
                    time.sleep(0.05)
                code = broker_monitor.poll()
                broker_monitor.close()
                if code == LEDGER_UNSEEN_EXIT:
                    raise RuntimeError(ledger_unseen(nominal, folder))
                if code != 0:
                    raise RuntimeError("Watchdog intermediary failed or did not exit")
        else:
            with (folder / "watchdog.log").open("ab", buffering=0) as log:
                broker = subprocess.Popen(
                    broker_command, stdout=log, stderr=subprocess.STDOUT, **options
                )
            broker.wait(timeout=10)
            if broker.returncode == LEDGER_UNSEEN_EXIT:
                raise RuntimeError(ledger_unseen(nominal, folder))
            if broker.returncode:
                raise RuntimeError("Watchdog intermediary failed; inspect watchdog.log")

        end = time.monotonic() + 15
        while time.monotonic() < end:
            try:
                proof = read(stem.with_suffix(".json"), {})
            except (OSError, ValueError):
                proof = {}
            if proof.get("token") == token:
                monitor = Monitor(proof["pid"])
                code, joined_job, ancestors = (
                    monitor.poll(),
                    in_job(monitor.pid),
                    live_ancestors(monitor.pid),
                )
                if code is not None or joined_job or os.getpid() in ancestors:
                    monitor.close()
                    raise RuntimeError(
                        f"Watchdog isolation failed: exit={code}, in_job={joined_job}, "
                        f"controller={os.getpid()}, ancestors={ancestors}"
                    )
                credentials = (
                    {"checked": False}
                    if credential_proof is None
                    else {
                        "checked": True,
                        **compare_credentials(proof.get("credentials"), credential_proof(salt)),
                    }
                )
                if require_same_credentials and not credentials.get("same"):
                    monitor.close()
                    raise RuntimeError(credential_mismatch(credentials["different"]))
                if credentials.get("checked") and not credentials.get("same"):
                    print(
                        "WARNING: "
                        + credential_mismatch(credentials["different"])
                        + " The watchdog still guards "
                        "the existing resources, but its result recovery may fail.",
                        file=sys.stderr,
                        flush=True,
                    )
                save(stem.with_suffix(".accepted"), {"pid": monitor.pid, "token": token})
                save(
                    folder / "watchdog-isolation.json",
                    {
                        **proof,
                        "controller_pid": os.getpid(),
                        "broker_exited": True,
                        "live_controller_ancestor": False,
                        "verified": True,
                        "credentials": credentials,
                    },
                )
                monitor.credentials = credentials
                return monitor
            try:
                unseen = read(stem.with_suffix(".unseen.json"), {})
            except (OSError, ValueError):
                unseen = {}
            if isinstance(unseen, dict) and unseen.get("token") == token:
                raise RuntimeError(ledger_unseen(nominal, folder, unseen.get("looked_in")))
            time.sleep(0.05)
        raise RuntimeError(
            "Watchdog did not acknowledge its arming nonce. If it cannot see the job ledger: this "
            "controller wrote it at " + written_at(nominal, folder) + ", and the watchdog runs "
            "outside this process's tree; see watchdog.log there."
        )
    except Exception as exc:
        save(stem.with_suffix(".cancel"), {"cancelled": True})
        time.sleep(0.2)  # let an unaccepted child release its ledger/log handles
        reason = (
            str(exc)
            if isinstance(exc, (RuntimeError, ValueError, TimeoutError))
            else type(exc).__name__
        )
        raise RuntimeError(
            "Independent watchdog failed to arm; no new paid resource may be created. "
            "Cause: " + reason
        ) from exc


def broker(spec_path):
    try:
        spec = read(spec_path)
    except (OSError, ValueError):
        spec = None
    if not isinstance(spec, dict):
        # The controller's folder is invisible from this process, which runs outside
        # the controller's tree as the watchdog will. On Windows, stay long enough
        # for the controller to read the exit code.
        time.sleep(3 if os.name == "nt" else 0)
        raise SystemExit(LEDGER_UNSEEN_EXIT)

    env = dict(os.environ)
    env[ARM_TOKEN] = spec["token"]
    env[ARM_FOLDER] = spec["folder"]
    options = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
        "cwd": str(Path(__file__).resolve().parent),
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with (Path(spec["folder"]) / "watchdog.log").open("ab", buffering=0) as log:
        subprocess.Popen(
            [
                spec["base_executable"],
                "-B",
                "-c",
                relaunch_code(),
                "--venv-command",
                str(spec_path),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            **options,
        )


def venv_command(spec_path):
    spec = read(spec_path)
    prefix = Path(spec["venv_prefix"])
    venv_sites = (
        [prefix / "Lib" / "site-packages"]
        if os.name == "nt"
        else [
            prefix
            / "lib"
            / ("python" + str(sys.version_info.major) + "." + str(sys.version_info.minor))
            / "site-packages"
        ]
    )
    if prefix != Path(sys.base_prefix):
        sys.path[:] = [p for p in sys.path if "site-packages" not in Path(p).parts]
        sys.prefix = sys.exec_prefix = str(prefix)
        for directory in venv_sites:
            site.addsitedir(str(directory))
    sys.executable = spec["venv_executable"]
    command = list(spec["command"])
    if Path(command.pop(0)).resolve() not in {
        Path(spec["venv_executable"]).resolve(),
        Path(spec["base_executable"]).resolve(),
    }:
        raise RuntimeError("Watchdog command must use the current Python environment")
    while command and command[0] in ("-B", "-u"):
        command.pop(0)
    sys.dont_write_bytecode = True
    if command[0] == "-c":
        sys.argv = ["-c", *command[2:]]
        exec(compile(command[1], "<watchdog-command>", "exec"), {"__name__": "__main__"})
    else:
        sys.argv = command
        runpy.run_path(command[0], run_name="__main__")


if __name__ == "__main__":
    if sys.argv[1] == "--venv-command":
        venv_command(sys.argv[2])
    else:
        broker(sys.argv[1])
