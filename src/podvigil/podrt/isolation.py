"""Linux DAC boundary for ordinary workload code, not a VM/kernel sandbox.

The trusted root supervisor launches this file with -I -S and a minimal loader
environment. Only this fresh, single-threaded helper changes credentials. User
code, its interpreter options and its environment take effect after the drop.
The layout lives on the Pod's own disk; lock_volume keeps the network volume,
which keeps no ownership or modes, out of the workload's reach.
"""

from pathlib import Path
import ctypes
import errno
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile


WORKLOAD_UID = 61000
WORKLOAD_GID = 61000
PR_SET_DUMPABLE, PR_GET_DUMPABLE = 4, 3
PR_SET_CHILD_SUBREAPER, PR_GET_CHILD_SUBREAPER = 36, 37
PR_SET_NO_NEW_PRIVS, PR_GET_NO_NEW_PRIVS = 38, 39
PR_CAPBSET_DROP, PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL = 24, 47, 4
PR_SET_KEEPCAPS, PR_SET_SECUREBITS = 8, 28


def linux_root():
    if not sys.platform.startswith("linux") or os.getuid() != 0 or os.geteuid() != 0:
        raise RuntimeError("Workload isolation requires a trusted Linux root supervisor.")


def prctl(option, arg=0):
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(
        ctypes.c_int(option),
        ctypes.c_ulong(arg),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if result < 0:
        raise OSError(ctypes.get_errno(), "Required Linux process protection failed")
    return result


def become_subreaper():
    """Orphaned descendants are re-parented to this process instead of the container's init.

    A platform may start the container command under an injected init, so the Pod guard is not
    PID 1 there. With this flag it still collects every process its workload leaves behind.
    """
    linux_root()
    prctl(PR_SET_CHILD_SUBREAPER, 1)
    flag = ctypes.c_int(0)
    libc = ctypes.CDLL(None, use_errno=True)
    if (
        libc.prctl(
            ctypes.c_int(PR_GET_CHILD_SUBREAPER),
            ctypes.byref(flag),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )
        < 0
        or flag.value != 1
    ):
        raise RuntimeError("The Pod guard could not become the reaper of its descendants.")


def protect_process():
    """Call before reading credentials, both at PID 1 and after supervisor exec."""
    linux_root()
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    prctl(PR_SET_DUMPABLE, 0)
    if prctl(PR_GET_DUMPABLE) != 0:
        raise RuntimeError("Supervisor memory protection was not applied.")
    os.umask(0o077)


def checked_path(path, directory=False):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)) or (
        not directory and info.st_nlink != 1
    ):
        raise RuntimeError("Isolation path is a link, alias, or special file.")
    return info


def permissions(path, uid, gid, mode, *, directory=False):
    """No symlink following; unsupported/ignored ownership or mode fails closed."""
    checked_path(path, directory)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        before = Path(path).lstat()
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("Isolation path changed while opening it.")
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        info = os.fstat(fd)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode):
            raise RuntimeError(
                "Filesystem does not enforce required ownership/mode. "
                f"{path}: uid {info.st_uid}, gid {info.st_gid}, mode {stat.S_IMODE(info.st_mode):o}; "
                f"required uid {uid}, gid {gid}, mode {mode:o}."
            )
    finally:
        os.close(fd)


def root_directory(path, mode=0o711):
    path = Path(path)
    path.mkdir(exist_ok=True)
    permissions(path, 0, 0, mode, directory=True)


def protect_code(code_dir):
    linux_root()
    code_dir = Path(code_dir)
    if not code_dir.is_absolute():
        raise RuntimeError("Supervisor code must have an absolute private location.")
    info = checked_path(code_dir, True)
    if info.st_uid != 0:
        raise RuntimeError("Supervisor code is not root owned.")
    permissions(code_dir, 0, 0, 0o700, directory=True)
    with os.scandir(code_dir) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        path = code_dir / name
        info = checked_path(path)
        if info.st_uid != 0:
            raise RuntimeError("Supervisor module is not root owned.")
        permissions(path, 0, 0, 0o600)


def mounts():
    """(mount point, device, folder of that device shown there) for every mount this process sees."""

    def text(field):
        # /proc/self/mountinfo writes space, tab, newline and backslash as octal escapes.
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)

    with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as lines:
        return [
            (text(fields[4]), fields[2], text(fields[3]))
            for fields in map(str.split, lines)
            if len(fields) > 4
        ]


def _overlaps(first, second):
    return any(
        a == b or b == "/" or a.startswith(b.rstrip("/") + "/")
        for a, b in ((first, second), (second, first))
    )


def lock_volume(mount):
    """Keep the network volume out of the workload's reach. Call before user code runs.

    The Runpod volume keeps no ownership or modes (MooseFS through FUSE, 2026-11-12: all
    uid 0, gid 0, mode 777; chmod ignored; chown refused even for root). Its mount sits below
    a folder on the Pod's disk that becomes root-only (700). Another mount of the same tree
    outside that folder would bypass the lock and is refused. verify_access checks it again.
    """
    linux_root()
    mount = Path(mount)
    parent = mount.parent
    if not mount.is_absolute() or parent == Path(mount.anchor) or ".." in mount.parts:
        raise RuntimeError("The network volume needs a private parent folder.")
    for path in reversed(mount.parents):
        checked_path(path, True)  # Real folders only: never lock or follow a link.
    entries = mounts()
    own = [entry for entry in entries if entry[0] == str(mount)]
    if not own:
        raise RuntimeError(f"The network volume is not mounted at {mount}.")
    checked_path(mount, True)
    device, tree = own[-1][1], own[-1][2]
    for point, other, shown in entries:
        inside = point.startswith(str(parent).rstrip("/") + "/")
        if other == device and not inside and _overlaps(shown, tree):
            raise RuntimeError(
                f"The network volume is also reachable at {point}; the lock would not hold."
            )
    permissions(parent, 0, 0, 0o700, directory=True)
    return {"volume": str(mount), "volume_lock": str(parent)}


def prepare_control(root, folder):
    linux_root()
    root, folder = Path(root), Path(folder)
    if folder.parent.parent != root or folder.parent.name != "jobs":
        raise RuntimeError("Control directory is outside the job workspace.")
    # Leave trusted / and /workspace ancestors alone. Do not follow uploaded links.
    for path in (root, root / "jobs", folder):
        root_directory(path)
    root_directory(folder / "control", 0o700)
    with os.scandir(folder) as entries:
        paths = [Path(entry.path) for entry in entries]
    for path in paths:
        if path.name in ("artifacts", "control", "output-bundle"):
            continue
        permissions(path, 0, 0, 0o600)


def readonly_inputs(path):
    path = Path(path)
    checked_path(path, True)
    with os.scandir(path) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        child = path / name
        info = child.lstat()
        if stat.S_ISDIR(info.st_mode):
            readonly_inputs(child)
        else:
            checked_path(child)  # Reject symlinks/hardlinks, FIFOs and devices.
            permissions(child, 0, 0, 0o555 if info.st_mode & 0o111 else 0o444)
    permissions(path, 0, 0, 0o555, directory=True)


def prepare(root, folder, code_dir, *, volume):
    """Prepare only the isolated Pod workspace, before user code can execute.

    `root` is on the Pod's own disk; `volume` (the network volume's mount) is locked away.
    """
    linux_root()
    root, folder = Path(root), Path(folder)
    protect_code(code_dir)
    locked = lock_volume(volume)
    lock = Path(locked["volume_lock"])
    if lock == root or lock in root.parents or root in lock.parents:
        raise RuntimeError("The workspace and the network volume must be separate folders.")
    prepare_control(root, folder)
    inputs, output = root / "inputs", folder / "artifacts"
    inputs.mkdir(exist_ok=True)
    readonly_inputs(inputs)
    output.mkdir(exist_ok=True)
    permissions(output, WORKLOAD_UID, WORKLOAD_GID, 0o700, directory=True)
    home = Path(tempfile.mkdtemp(prefix="wondomon-workload-"))
    permissions(home, WORKLOAD_UID, WORKLOAD_GID, 0o700, directory=True)
    for name in ("cache", "tmp"):
        (home / name).mkdir()
        permissions(home / name, WORKLOAD_UID, WORKLOAD_GID, 0o700, directory=True)
    return {
        "output": str(output),
        "inputs": str(inputs),
        "home": str(home),
        "folder": str(folder),
        "code": str(Path(code_dir) / Path(__file__).name),
        "guard_pid": os.getppid(),
        "supervisor_pid": os.getpid(),
        **locked,
    }


def workload_environment(environment, layout):
    result = dict(environment)
    home = layout["home"]
    result.update(
        HOME=home,
        USER="wondomon-workload",
        LOGNAME="wondomon-workload",
        TMPDIR=home + "/tmp",
        XDG_CACHE_HOME=home + "/cache",
        CUDA_CACHE_PATH=home + "/cache/cuda",
        TORCH_HOME=home + "/cache/torch",
        HF_HOME=home + "/cache/huggingface",
        HUGGINGFACE_HUB_CACHE=home + "/cache/hub",
        TRANSFORMERS_CACHE=home + "/cache/transformers",
        PIP_CACHE_DIR=home + "/cache/pip",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return result


def drop_privileges():
    linux_root()
    prctl(PR_SET_NO_NEW_PRIVS, 1)
    prctl(PR_SET_KEEPCAPS, 0)
    # Lock NOROOT, disabled KEEP_CAPS and NO_CAP_AMBIENT_RAISE. The normal
    # UID transition still clears effective and permitted capabilities.
    prctl(PR_SET_SECUREBITS, 1 | 2 | 32 | 64 | 128)
    prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL)
    last = int(Path("/proc/sys/kernel/cap_last_cap").read_text())
    if not 0 <= last <= 63:
        raise RuntimeError("Unsupported Linux capability range.")
    for capability in range(last + 1):
        prctl(PR_CAPBSET_DROP, capability)
    os.setgroups([])
    os.setresgid(WORKLOAD_GID, WORKLOAD_GID, WORKLOAD_GID)
    os.setresuid(WORKLOAD_UID, WORKLOAD_UID, WORKLOAD_UID)

    # Explicitly clear inheritable as well as permitted/effective capabilities.
    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.capset(ctypes.byref(Header(0x20080522, 0)), ctypes.byref((Data * 2)())) < 0:
        raise OSError(ctypes.get_errno(), "Capability clearing failed")
    if (
        os.getresuid() != (WORKLOAD_UID,) * 3
        or os.getresgid() != (WORKLOAD_GID,) * 3
        or os.getgroups()
        or prctl(PR_GET_NO_NEW_PRIVS) != 1
    ):
        raise RuntimeError("Workload identity/drop verification failed.")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    if any(
        int(status[name].strip(), 16) for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    ):
        raise RuntimeError("Workload retains Linux capabilities.")


def denied(operation):
    try:
        result = operation()
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            return
        raise
    else:
        if isinstance(result, int):
            os.close(result)
        raise RuntimeError("Workload could access a protected supervisor resource.")


def verify_access(layout):
    """Run as the actual final UID: stat alone is insufficient on remote mounts."""
    for pid in {layout["guard_pid"], layout["supervisor_pid"]}:
        if pid <= 0:
            raise RuntimeError("Invalid supervisor identity.")
        for name in ("environ", "mem"):
            denied(lambda: os.open(f"/proc/{pid}/{name}", os.O_RDONLY))
        denied(lambda: os.listdir(f"/proc/{pid}/fd"))
        denied(lambda: os.kill(pid, 0))
    denied(lambda: os.listdir(Path(layout["folder"]) / "control"))
    denied(lambda: os.open(layout["code"], os.O_RDONLY))
    # The volume keeps no modes. Its root-only parent protects it.
    denied(lambda: os.listdir(layout["volume_lock"]))
    denied(lambda: os.stat(layout["volume"]))
    denied(lambda: os.open(layout["volume"], os.O_RDONLY | os.O_DIRECTORY))
    for directory in (layout["inputs"], layout["folder"], str(Path(layout["folder"]) / "control")):
        probe = Path(directory) / (".isolation-probe-" + str(os.getpid()))
        denied(lambda: os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    for directory in (layout["output"], layout["home"]):
        probe = Path(directory) / (".isolation-probe-" + str(os.getpid()))
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b"permission check")
            os.fsync(fd)
        finally:
            os.close(fd)
            probe.unlink()


def launch(command, layout, environment, stdout, *, timeout, authorize):
    """No preexec_fn: root loader gets no workload-controlled environment."""
    linux_root()
    parent, child = socket.socketpair()
    process = None
    try:
        parent.settimeout(max(0.01, timeout))
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", layout["code"], str(child.fileno())],
            cwd="/",
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            pass_fds=(child.fileno(),),
            close_fds=True,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
        )
        child.close()
        data = json.dumps(
            {
                "layout": layout,
                "command": command,
                "environment": workload_environment(environment, layout),
            }
        ).encode()
        if len(data) > 1024 * 1024:
            raise RuntimeError("Workload launch data exceeds limit.")
        parent.sendall(len(data).to_bytes(4, "big") + data)
        if parent.recv(1) != b"R":
            raise RuntimeError("Workload isolation checks did not pass.")
        authorize()  # Last guard/readiness check occurs after privilege checks.
        parent.sendall(b"G")
        return process
    except BaseException:
        if process is not None:
            process.kill()
            process.wait(timeout=5)
        raise
    finally:
        parent.close()
        child.close()


def main():
    protect_process()
    channel = socket.socket(fileno=int(sys.argv[1]))
    channel.settimeout(30)

    def receive(count):
        data = bytearray()
        while len(data) < count:
            chunk = channel.recv(count - len(data))
            if not chunk:
                raise RuntimeError("Supervisor closed workload launch IPC.")
            data.extend(chunk)
        return bytes(data)

    size = int.from_bytes(receive(4), "big")
    if not 0 < size <= 1024 * 1024:
        raise RuntimeError("Invalid workload launch frame.")
    value = json.loads(receive(size))
    layout, command, environment = value["layout"], value["command"], value["environment"]
    drop_privileges()
    verify_access(layout)
    os.chdir(layout["output"])
    channel.sendall(b"R")
    if receive(1) != b"G":
        raise RuntimeError("Supervisor did not release isolated workload.")
    channel.close()
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print("Workload isolation failed: " + type(exc).__name__, file=sys.stderr, flush=True)
        raise SystemExit(126)
