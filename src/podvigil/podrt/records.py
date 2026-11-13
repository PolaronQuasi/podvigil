"""Atomic, separate-writer job records: OS locks, durable saves, reads and digests."""

from pathlib import Path
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
import time
import uuid

# Shared mount path for the Pod request and runtime. Change it here only.
# The volume keeps no ownership or modes, so isolation.lock_volume locks its parent.
VOLUME_MOUNT = "/guard-volume/data"


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@contextmanager
def exclusive_lock(path, timeout=5):
    """OS-owned lock, released automatically on process death; never unlink it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
        until = time.monotonic() + timeout
        acquired = False
        try:
            while not acquired:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= until:
                        raise TimeoutError("Another process holds this job lock.") from None
                    time.sleep(0.05)
            stream.seek(0)
            yield stream
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def fsync_file(path):
    # A write-capable handle is required by Windows _commit/FlushFileBuffers.
    with Path(path).open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _replace_once(source, destination):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # REPLACE_EXISTING | WRITE_THROUGH; both paths are on the same volume.
        if not move(str(source), str(destination), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, destination)
        descriptor = os.open(str(Path(destination).parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def durable_replace(source, destination):
    source, destination = Path(source), Path(destination)
    fsync_file(source)
    for attempt in range(20):
        try:
            _replace_once(source, destination)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(min(0.005 * 2**attempt, 0.05))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        # Windows readers briefly deny rename/delete sharing. Retry this local
        # contention without exposing a partially written ownership record.
        durable_replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def best_effort_save(path, value):
    """Observability cannot become a prerequisite for terminating compute."""
    try:
        save(path, value)
        return True
    except Exception:
        return False


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
