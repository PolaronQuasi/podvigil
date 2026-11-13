"""Container-only fake, shipped as the Pod code's api.py (podrt.api) in an isolated test directory.

No network imports, credentials, or provider calls. Fault injection lives here,
never in the production agent. Docker additionally runs with --network none.
The test's host folder mounted at records.VOLUME_MOUNT plays the network
volume, and RunpodVolumeModel gives it the measured Runpod volume behavior.
"""

from pathlib import Path
import base64
import errno
import json
import os
import stat
import sys
import time


class ApiError(RuntimeError):
    def __init__(self, status, *args):
        self.status = status
        super().__init__("dummy API failure")


class RunpodVolumeModel:
    """Make folders behave like the Runpod network volume measured on 2026-11-12.

    The volume is MooseFS through FUSE: every entry reports uid 0, gid 0 and mode 777,
    chmod has no effect, and chown to any other owner fails with EPERM, even for root.
    Only this process's os/pathlib views change, and only for paths at or below the roots.
    """

    names = ("stat", "lstat", "fstat", "chmod", "fchmod", "chown", "fchown", "lchown")
    extra = (
        "st_atime",
        "st_mtime",
        "st_ctime",
        "st_atime_ns",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_blksize",
        "st_blocks",
        "st_rdev",
    )

    def __init__(self, *roots):
        self.roots = [os.path.abspath(os.fspath(root)) for root in roots]
        self.original = None
        self.path_original = None

    def modeled(self, target, dir_fd=None):
        try:
            if isinstance(target, int):
                path = os.readlink("/proc/self/fd/" + str(target))
            else:
                path = os.fsdecode(os.fspath(target))
                if dir_fd is not None and not os.path.isabs(path):
                    path = os.path.join(os.readlink("/proc/self/fd/" + str(dir_fd)), path)
                path = os.path.abspath(path)
        except (OSError, TypeError, ValueError):
            return False
        return any(path == root or path.startswith(root.rstrip("/") + "/") for root in self.roots)

    def view(self, result):
        fields = list(result)
        fields[0] = stat.S_IFMT(result.st_mode) | 0o777
        fields[4] = fields[5] = 0
        return os.stat_result(
            fields, {name: getattr(result, name) for name in self.extra if hasattr(result, name)}
        )

    def refuse(self, target, uid, gid):
        if uid not in (0, -1) or gid not in (0, -1):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(target))

    def __enter__(self):
        if self.original is not None:
            raise RuntimeError("Volume model already installed.")
        original = self.original = {name: getattr(os, name) for name in self.names}
        self.path_original = {name: getattr(Path, name) for name in ("stat", "chmod")}

        def stat_(path, *, dir_fd=None, follow_symlinks=True):
            result = original["stat"](path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            return self.view(result) if self.modeled(path, dir_fd) else result

        def lstat(path, *, dir_fd=None):
            result = original["lstat"](path, dir_fd=dir_fd)
            return self.view(result) if self.modeled(path, dir_fd) else result

        def fstat(fd):
            result = original["fstat"](fd)
            return self.view(result) if self.modeled(fd) else result

        def chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
            if not self.modeled(path, dir_fd):
                return original["chmod"](path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        def fchmod(fd, mode):
            if not self.modeled(fd):
                return original["fchmod"](fd, mode)

        def chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
            if self.modeled(path, dir_fd):
                return self.refuse(path, uid, gid)
            return original["chown"](path, uid, gid, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        def fchown(fd, uid, gid):
            if self.modeled(fd):
                return self.refuse(fd, uid, gid)
            return original["fchown"](fd, uid, gid)

        def lchown(path, uid, gid):
            if self.modeled(path):
                return self.refuse(path, uid, gid)
            return original["lchown"](path, uid, gid)

        for name, function in (
            ("stat", stat_),
            ("lstat", lstat),
            ("fstat", fstat),
            ("chmod", chmod),
            ("fchmod", fchmod),
            ("chown", chown),
            ("fchown", fchown),
            ("lchown", lchown),
        ):
            setattr(os, name, function)
        # Python 3.10's pathlib accessor caches the original os functions. Route the public
        # methods through the same model on every version; lstat/lchmod delegate to these.
        Path.stat, Path.chmod = stat_, chmod
        return self

    def __exit__(self, *exc):
        for name, function in self.original.items():
            setattr(os, name, function)
        for name, function in self.path_original.items():
            setattr(Path, name, function)
        self.original = None
        self.path_original = None


class SocketProxy:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def sendall(self, data):
        self.inner.sendall(b"!" if data == b"G" else data)


class Api:
    def __init__(self, key):
        if key != "DUMMY":
            raise RuntimeError("Only dummy credentials are permitted in this fake.")
        from . import records as job_state  # Loaded by now; a top-level import would be circular.

        self.case = os.environ.get("REVIEW_CASE", "success")
        self.root = Path(job_state.VOLUME_MOUNT)
        agent = sys.modules["__main__"]
        if not getattr(agent, "volume_model", None):
            # Guard and supervisor both see the volume as the real Pod did.
            roots = [self.root] + (
                [Path("/workspace")] if self.case == "workspace_ignores_modes" else []
            )
            agent.volume_model = RunpodVolumeModel(*roots).__enter__()
        if self.case == "heartbeat_failure" and os.getpid() != 1:
            original = agent.save

            def save(path, value):
                if Path(path).name == "agent.json":
                    raise OSError(errno.ENOSPC, "injected heartbeat failure")
                return original(path, value)

            agent.save = save
        if self.case == "deny_release" and os.getpid() == 1:
            original = agent.socket.socketpair

            def pair():
                parent, child = original()
                return SocketProxy(parent), child

            agent.socket.socketpair = pair
        if self.case == "retry_signals" and os.getpid() == 1:
            original_sleep = time.sleep

            def sleep(seconds):
                if seconds == 15:
                    (self.root / "retry-sleep").touch()
                    original_sleep(2)  # Shorten only the production retry backoff.
                else:
                    original_sleep(seconds)

            time.sleep = sleep

    def request(self, method, path, body=None):
        job = json.loads(base64.b64decode(os.environ["WONDOMON_JOB_SPEC"]))
        if method == "DELETE":
            events = Path("/sys/fs/cgroup/memory.events")
            with (self.root / "delete-events.jsonl").open("a") as stream:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "path": path,
                        "memory_events": events.read_text() if events.exists() else "",
                        "live_pids": sorted(
                            int(p.name) for p in Path("/proc").iterdir() if p.name.isdecimal()
                        ),
                    },
                    stream,
                )
                stream.write("\n")
            if self.case == "retry_signals" and not (self.root / "allow-delete").exists():
                raise ApiError(403)
            (self.root / "deleted").write_text("verified by fake")
            return None
        if method != "GET":
            raise AssertionError("The agent must not create any resource.")
        return {
            "id": "testpod",
            "name": "renamed" if self.case == "mutated" else job["pod_name"],
            "env": {} if self.case == "mutated" else {"WONDOMON_JOB_ID": job["job_id"]},
            "cloud": "COMMUNITY" if self.case == "bad_cloud" else "SECURE",
            "actions": ["terminate"],
            "mounts": {"network": [{"volumeId": "testvolume", "path": str(self.root)}]},
        }

    def get_or_none(self, path):
        return None if (self.root / "deleted").exists() else self.request("GET", path)
