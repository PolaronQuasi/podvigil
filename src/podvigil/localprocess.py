"""Local workload lifetime: Windows job objects and a dedicated Linux child reaper.

The helper reads its command only after the parent has armed cleanup. It uses only
the standard library and runs in isolated Python mode, not the workload's imports.
This controls process lifetime; it is not a security sandbox.
"""

from pathlib import Path
import ctypes
import json
import os
import signal
import subprocess
import sys
import time


class WindowsJob:
    """A non-inheritable job handle owns descendants even after their parent exits."""

    def __init__(self):
        from ctypes import wintypes as w

        class Limits(ctypes.Structure):
            _fields_ = [
                ("per_process", ctypes.c_int64),
                ("per_job", ctypes.c_int64),
                ("flags", w.DWORD),
                ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t),
                ("processes", w.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("limits", Limits),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_int64 * 4),
                ("faults", w.DWORD),
                ("total", w.DWORD),
                ("active", w.DWORD),
                ("terminated", w.DWORD),
            ]

        self.accounting = Accounting
        self.kernel = k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = (
            [ctypes.c_void_p, w.LPCWSTR],
            w.HANDLE,
        )
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.QueryInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.c_void_p,
        ]
        k.CloseHandle.argtypes = [w.HANDLE]
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway flags.
        if not k.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            k.CloseHandle(self.handle)
            self.handle = None
            raise error

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle is None:
            return
        try:
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            end = time.monotonic() + 10
            while True:
                record = self.accounting()
                if not self.kernel.QueryInformationJobObject(
                    self.handle, 1, ctypes.byref(record), ctypes.sizeof(record), None
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not record.active:
                    return
                if time.monotonic() >= end:
                    raise TimeoutError("Local workload processes did not stop.")
                time.sleep(0.01)
        finally:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ProcessTree:
    """Start a gated workload and stop all remaining children before returning results."""

    def __init__(self, command, cwd, environment, log):
        self.process = None
        self.job = WindowsJob() if os.name == "nt" else None
        self.closed = False
        helper = [
            getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable,
            "-I",
            "-S",
            "-B",
            str(Path(__file__).resolve()),
        ]
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            self.process = subprocess.Popen(
                helper,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                **options,
            )
            if self.job is not None:
                self.job.assign(self.process)
            self.process.stdin.write(json.dumps(command).encode("utf-8") + b"\n")
            self.process.stdin.close()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        child = self.process
        try:
            if self.job is not None:
                self.job.close()
            elif child is not None and child.poll() is None:
                # Linux's dedicated helper reaps even children that started new sessions.
                child.terminate()
                try:
                    # Ten seconds for SIGTERM, ten for reaping, plus startup margin.
                    child.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    raise TimeoutError("Local workload processes did not stop.") from None
        finally:
            if child is not None:
                if child.stdin is not None:
                    child.stdin.close()
                child.wait(timeout=10)


def _reap_children(child):
    """Only this helper's children; unreaped children reserve their PIDs until collected."""
    end = time.monotonic() + 10
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    while True:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if not pid:
                break
            if pid == child.pid:
                child.returncode = os.waitstatus_to_exitcode(status)
        for pid in children.read_text().split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if time.monotonic() >= end:
            raise TimeoutError("Local workload processes did not stop.")
        time.sleep(0.01)


def main():
    stopped = [False]
    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__(0, True))
        signal.signal(signal.SIGINT, lambda *_: stopped.__setitem__(0, True))
    linux = sys.platform.startswith("linux")
    if linux:
        libc = ctypes.CDLL(None, use_errno=True)
        # PR_SET_CHILD_SUBREAPER affects only the helper process.
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot supervise local workload descendants.")
    command = json.loads(sys.stdin.buffer.readline())
    if stopped[0]:
        return 1
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    try:
        while child.poll() is None and not stopped[0]:
            time.sleep(0.02)
    finally:
        if linux:
            if stopped[0] and child.poll() is None:
                # The helper owns this group. Give ordinary workloads the same checkpoint
                # grace as before; the subreaper still kills detached/ignored descendants.
                os.killpg(os.getpgrp(), signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            _reap_children(child)
        elif os.name != "nt":
            # Other POSIX platforms lack the Linux subreaper. Keep the helper as
            # group leader while stopping ordinary descendants. A workload can
            # deliberately escape that group.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.killpg(os.getpgrp(), signal.SIGTERM)
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgrp(), signal.SIGKILL)
    code = child.returncode
    if code is not None and code < 0 and os.name != "nt":
        signal.signal(-code, signal.SIG_DFL) if -code not in (
            signal.SIGKILL,
            signal.SIGSTOP,
        ) else None
        os.kill(os.getpid(), -code)
    return code if code is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
