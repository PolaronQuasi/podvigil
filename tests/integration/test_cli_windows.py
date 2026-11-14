"""Subprocess CLI integration with the production Windows watchdog launcher.

Only private copies of the REST origin and S3 constructor are adapted. The
controller, watchdog, launcher, ledger, backup and cleanup code execute normally.
The fake service produces Pod outputs; this does not execute a Linux/GPU workload.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ast
import base64
import ctypes
import json
import os
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.parse

from podvigil.storage import Storage, verify_local_backup
from podvigil.podrt.api import ApiError
from tests.support.fakes import FakeApi, FakeS3, MissingObject, NoSuchBucket
from tests.support.paths import ROOT
from podvigil.settings import ENV_PREFIX


VENV = ROOT / ".venv"
PYTHON = VENV / "Scripts" / "python.exe"
DUMMY_KEY = "cli-test-dummy-account"
# These two files in the private copy are adapted to the loopback services.
# Every other copied file must stay unchanged.
API_MODULE = "src/podvigil/podrt/api.py"
STORAGE_MODULE = "src/podvigil/storage.py"


ADAPTER_SOURCE = r"""
from pathlib import Path
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request
import boto3
import botocore
import s3transfer
from botocore.exceptions import ClientError


class HTTPFakeS3:
    def __init__(self, url):
        self.url = url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.call("adapter", {"pid": os.getpid(), "action": sys.argv[1],
            "executable": sys.executable, "prefix": sys.prefix,
            "dependencies": {module.__name__: {"version": module.__version__, "file": module.__file__}
                             for module in (boto3, botocore, s3transfer)}})

    def call(self, operation, values):
        request = urllib.request.Request(self.url + "/s3/" + operation,
            data=json.dumps(values).encode(), headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=2) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                error = json.load(exc)
            raise ClientError(error, operation) from None

    def put_object(self, Bucket, Key, Body):
        return self.call("put_object", {"Bucket": Bucket, "Key": Key,
                                      "Body": base64.b64encode(Body).decode()})

    def get_object(self, Bucket, Key):
        result = self.call("get_object", {"Bucket": Bucket, "Key": Key})
        return {"Body": io.BytesIO(base64.b64decode(result["Body"]))}

    def list_objects_v2(self, **kwargs):
        return self.call("list_objects_v2", kwargs)

    def upload_file(self, source, bucket, key):
        return self.call("upload_file", {"Bucket": bucket, "Key": key,
            "Body": base64.b64encode(Path(source).read_bytes()).decode()})

    def download_file(self, bucket, key, destination):
        result = self.call("download_file", {"Bucket": bucket, "Key": key})
        Path(destination).write_bytes(base64.b64decode(result["Body"]))
"""


class ProcessHandle:
    """Hold the exact process object so cleanup cannot kill a reused PID."""

    def __init__(self, pid):
        from ctypes import wintypes

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            *([ctypes.POINTER(wintypes.FILETIME)] * 4),
        ]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.handle = self.kernel.OpenProcess(0x1001, False, pid)
        self.pid = pid

    def running(self):
        if not self.handle:
            return False
        from ctypes import wintypes

        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259

    def created_ns(self):
        from ctypes import wintypes

        timestamps = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(
            self.handle, *(ctypes.byref(stamp) for stamp in timestamps)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        creation = timestamps[0]
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return (ticks - 116444736000000000) * 100  # FILETIME to Unix nanoseconds.

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def terminate(self):
        if self.running() and not self.kernel.TerminateProcess(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())


class FakeServices:
    def __init__(self, workspace, transient):
        self.workspace = workspace
        self.lock = threading.RLock()
        self.api = FakeApi()
        self.s3 = FakeS3(page_size=2)
        self.api.storage = Storage("EU-RO-1", self.s3)
        self.s3.on_start = self.finish
        self.get_failures = [503] if transient else []
        self.s3_failures = 1 if transient else 0
        self.events, self.probes, self.handles = [], [], {}
        self.job_id = None
        self.released = False
        self.delete_checked = False
        self.isolation_before_creation = []
        self.errors = []

    def finish(self, start):
        self.released = True
        self.s3.finished(start["job_id"])
        self.s3.objects["testvolume", "jobs/" + start["job_id"] + "/started-once.json"] = (
            b'{"simulated":true}'
        )

    def folder(self):
        jobs = list((self.workspace / "output" / "jobs").iterdir())
        if len(jobs) != 1:
            raise AssertionError("CLI must create exactly one private job ledger.")
        return jobs[0]

    def rest(self, method, path, body):
        route = urllib.parse.urlsplit(path).path
        if method == "POST" and route in ("/pods", "/network-volumes"):
            isolation = json.loads((self.folder() / "watchdog-isolation.json").read_text())
            if not isolation.get("verified") or isolation.get("in_job") is not False:
                raise AssertionError(
                    "A resource was created before watchdog isolation was verified."
                )
            self.isolation_before_creation.append(route)
            if route == "/pods":
                self.job_id = body["env"][ENV_PREFIX + "_JOB_ID"]
                if body["cloud"] != "SECURE":
                    raise AssertionError("CLI omitted Secure Cloud.")
            elif body.get("type") != "STANDARD":
                raise AssertionError("CLI omitted STANDARD storage.")
        if method == "GET" and route == "/pods/testpod" and self.get_failures and not self.released:
            status = self.get_failures.pop(0)
            raise ApiError(status, method, path)
        if method == "DELETE" and route == "/pods/testpod" and not self.released:
            raise AssertionError("A startup observation error deleted an unreleased Pod.")
        if method == "DELETE" and route == "/network-volumes/testvolume":
            if self.api.pods:
                raise AssertionError("Volume DELETE preceded Pod termination.")
            folder = self.folder()
            job = json.loads((folder / "job.json").read_text())
            verify_local_backup(job, "testvolume", folder / "downloads")
            audit = json.loads((folder / "volume-audit.json").read_text())
            if (
                audit["job_id"] != job["job_id"]
                or audit["volume_id"] != "testvolume"
                or audit["object_count"] != len(self.s3.objects)
            ):
                raise AssertionError("Volume DELETE preceded a complete, bound volume audit.")
            self.delete_checked = True
        value = self.api.request(method, path, body)
        if method == "DELETE" and route == "/network-volumes/testvolume":
            self.s3.objects.clear()  # The downloaded copy must stand on its own after deletion.
        return value

    def storage(self, operation, body):
        if operation == "adapter":
            self.probes.append(body)
            if body["pid"] not in self.handles:
                self.handles[body["pid"]] = ProcessHandle(body["pid"])
            return {}
        key = body.get("Key", "")
        if (
            operation == "get_object"
            and key.endswith("/agent.json")
            and self.s3_failures
            and not self.released
        ):
            self.s3_failures -= 1
            raise ApiError(503, "GET", "/s3/get_object")
        if operation in ("put_object", "upload_file"):
            self.s3.put_object(body["Bucket"], key, base64.b64decode(body["Body"]))
            return {}
        if operation == "list_objects_v2":
            return self.s3.list_objects_v2(**body)
        if operation in ("get_object", "download_file"):
            if operation == "download_file" and self.api.pods:
                raise AssertionError("Artifact download preceded Pod termination.")
            with self.s3.get_object(body["Bucket"], key)["Body"] as stream:
                return {"Body": base64.b64encode(stream.read()).decode()}
        raise AssertionError("Unexpected fake S3 operation.")


def handler_for(services):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def do_DELETE(self):
            self.dispatch()

        def dispatch(self):
            body = None
            size = int(self.headers.get("Content-Length", "0"))
            if size:
                body = json.loads(self.rfile.read(size))
            with services.lock:
                event = {
                    "method": self.command,
                    "path": self.path,
                    "key": (body or {}).get("Key"),
                    "time": time.monotonic(),
                }
                status = 200
                try:
                    if self.path.startswith("/s3/"):
                        value = services.storage(self.path.rsplit("/", 1)[-1], body)
                    else:
                        if self.headers.get("Authorization") != "Bearer " + DUMMY_KEY:
                            raise AssertionError("REST request did not use the private dummy key.")
                        if not self.path.startswith("/v2/"):
                            raise AssertionError("Unexpected REST route.")
                        value = services.rest(self.command, self.path[3:], body)
                        status = (
                            201
                            if self.command == "POST"
                            else 204
                            if self.command == "DELETE"
                            else 200
                        )
                except MissingObject:
                    status, value = (
                        404,
                        {"Error": {"Code": "NoSuchKey", "Message": "dummy object missing"}},
                    )
                except NoSuchBucket:
                    status, value = (
                        404,
                        {"Error": {"Code": "NoSuchBucket", "Message": "dummy volume deleted"}},
                    )
                except ApiError as exc:
                    status = exc.status
                    value = {
                        "Error": {"Code": str(status), "Message": "injected transient"},
                        "title": "Injected test response",
                        "detail": "Local fixture only",
                    }
                except Exception as exc:
                    status = 500
                    services.errors.append(f"{type(exc).__name__}: {exc}")
                    value = {
                        "Error": {"Code": "FixtureError", "Message": "local test assertion failed"}
                    }
                event["status"] = status
                services.events.append(event)
            raw = b"" if status == 204 else json.dumps(value).encode()
            # Adds the real HTTP Date used by the production clock logic.
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def private_copy(workspace, url):
    snapshots = {}
    for source in [*ROOT.glob("*.py"), *ROOT.glob("src/**/*.py")]:
        raw = source.read_bytes()
        name = source.relative_to(ROOT).as_posix()
        snapshots[name] = raw
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_bytes(raw)
    api = workspace / API_MODULE
    source = api.read_text()
    origin = "https://api.runpod.io/v2"
    if source.count(origin) < 2:
        raise AssertionError("REST adapter changed; review the private-copy injection.")
    api.write_text(source.replace(origin, url + "/v2"), encoding="utf-8")
    storage = workspace / STORAGE_MODULE
    lines = storage.read_text().splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    definition = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Storage"
    )
    constructor = next(
        node
        for node in definition.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    replacement = (
        "    def __init__(self, data_center, client=None):\n"
        "        from cli_http_s3 import HTTPFakeS3\n"
        f"        self.client = client if client is not None else HTTPFakeS3({url!r})\n"
    )
    lines[constructor.lineno - 1 : constructor.end_lineno] = [replacement]
    storage.write_text("".join(lines), encoding="utf-8")
    # In src/: both the controller and the watchdog have it on their path.
    (workspace / "src" / "cli_http_s3.py").write_text(
        textwrap.dedent(ADAPTER_SOURCE), encoding="utf-8"
    )
    (workspace / ".env").write_text(
        f"RUNPOD_API_KEY={DUMMY_KEY}\nRUNPOD_S3_ACCESS_KEY=dummy-access\nRUNPOD_S3_SECRET_KEY=dummy-secret\n",
        encoding="utf-8",
    )
    config = json.loads((ROOT / "jobs" / "smoke.example.json").read_text())
    config.update(
        poll_seconds=2,
        startup_timeout_seconds=20,
        finalize_grace_seconds=30,
        max_runtime_seconds=300,
        controller_stale_seconds=12,
        agent_stale_seconds=12,
        self_termination_probe=False,
        preparation_timeout_seconds=60,
        inputs=[{"local": "dummy-input.py", "remote": "dummy-input.py"}],
    )
    (workspace / "dummy-input.py").write_text("# private CLI fixture input\n", encoding="utf-8")
    (workspace / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return snapshots


def stop_private_processes(controller, workspace, services, deadline):
    # An unaccepted service-created watchdog must see cancellation if it starts late.
    jobs = workspace / "output" / "jobs"
    for spec in jobs.glob("*/watchdog-arm-*.launch.json"):
        token = spec.name.removeprefix("watchdog-arm-").removesuffix(".launch.json")
        (spec.parent / ("watchdog-arm-" + token + ".cancel")).write_text("{}")
    for proof_path in jobs.glob("*/watchdog-arm-*.json"):
        try:
            proof = json.loads(proof_path.read_text())
        except (OSError, ValueError):
            continue
        pid = proof.get("pid")
        if isinstance(pid, int) and pid not in services.handles:
            handle = ProcessHandle(pid)
            spec = proof_path.with_suffix(".launch.json")
            # A live PID from an old proof could have been reused. The process
            # must have been created between this private launch and its proof.
            if (
                handle.running()
                and spec.is_file()
                and spec.stat().st_mtime_ns <= handle.created_ns() <= proof_path.stat().st_mtime_ns
            ):
                services.handles[pid] = handle
            else:
                handle.close()
    for handle in services.handles.values():
        if handle.running():
            handle.terminate()
    if controller is not None and controller.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(controller.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            controller.kill()
        try:
            controller.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            controller.kill()
    for handle in services.handles.values():
        while handle.running() and time.monotonic() < deadline:
            time.sleep(0.02)
        handle.close()


@unittest.skipUnless(os.name == "nt", "Real Windows CLI/watchdog integration")
@unittest.skipUnless(
    PYTHON.is_file(),
    "Needs the project venv: python -m venv .venv, then "
    ".venv\\Scripts\\python.exe -m pip install -r requirements.lock.txt",
)
class CLILifecycleTests(unittest.TestCase):
    def run_case(self, transient):
        started = time.monotonic()
        deadline = started + 30
        controller = None
        # Do not mask the actual test failure if Windows briefly retains a file
        # handle after process termination. Any leftover data is private/dummy.
        with tempfile.TemporaryDirectory(
            prefix="wondomon-cli-", ignore_cleanup_errors=True
        ) as temporary:
            workspace = Path(temporary)
            services = FakeServices(workspace, transient)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(services))
            server.daemon_threads = True
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                snapshots = private_copy(workspace, url)
                allowed = {
                    "SYSTEMROOT",
                    "WINDIR",
                    "PATH",
                    "TEMP",
                    "TMP",
                    "USERPROFILE",
                    "COMSPEC",
                    "PATHEXT",
                    "APPDATA",
                    "LOCALAPPDATA",
                    "PROGRAMDATA",
                    "PROGRAMFILES",
                    "PSMODULEPATH",  # Keep CIM module lookup; no account credentials.
                }
                environment = {
                    key: value for key, value in os.environ.items() if key.upper() in allowed
                }
                environment.update(
                    PYTHONNOUSERSITE="1",
                    PYTHONDONTWRITEBYTECODE="1",
                    AWS_EC2_METADATA_DISABLED="true",
                    NO_PROXY="127.0.0.1",
                )
                controller = subprocess.Popen(
                    [
                        str(PYTHON),
                        "-B",
                        str(workspace / "managed_run.py"),
                        "launch",
                        "--state-dir",
                        str(workspace / "output"),
                        "--env-file",
                        str(workspace / ".env"),
                        str(workspace / "config.json"),
                    ],
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    stdout, stderr = controller.communicate(
                        timeout=max(0.1, deadline - time.monotonic() - 3)
                    )
                except subprocess.TimeoutExpired:
                    self.fail(
                        "Private CLI exceeded its bounded runtime. "
                        f"Fixture errors={services.errors}; recent events={services.events[-8:]}"
                    )
                self.assertEqual(controller.returncode, 0, stdout + stderr + repr(services.errors))
                self.assertFalse(services.errors)
                self.assertNotIn(DUMMY_KEY, stdout + stderr)
                self.assertFalse(services.api.pods)
                self.assertFalse(services.api.volumes)
                self.assertFalse(services.s3.objects)
                self.assertTrue(services.delete_checked)
                self.assertEqual(services.isolation_before_creation, ["/network-volumes", "/pods"])
                folder = services.folder()
                summary = json.loads((folder / "summary.json").read_text())
                job = json.loads((folder / "job.json").read_text())
                verify_local_backup(job, "testvolume", folder / "downloads")
                self.assertEqual(
                    (folder / "downloads" / "artifacts" / "model.bin").read_bytes(),
                    b"example weights",
                )
                self.assertEqual(summary["training_outcome"], "success")
                self.assertTrue(summary["pod_termination_verified"])
                self.assertTrue(summary["volume_deletion_verified"])
                proof = json.loads((folder / "watchdog-isolation.json").read_text())
                self.assertTrue(proof["verified"])
                self.assertTrue(proof["broker_exited"])
                self.assertFalse(proof["in_job"])
                self.assertFalse(proof["live_controller_ancestor"])
                self.assertNotEqual(proof["pid"], controller.pid)
                by_action = {probe["action"]: probe for probe in services.probes}
                self.assertEqual(set(by_action), {"launch", "watchdog"})
                self.assertEqual(by_action["watchdog"]["pid"], proof["pid"])
                self.assertNotEqual(by_action["launch"]["pid"], by_action["watchdog"]["pid"])
                for probe in by_action.values():
                    self.assertEqual(Path(probe["executable"]).resolve(), PYTHON.resolve())
                    self.assertEqual(Path(probe["prefix"]).resolve(), VENV.resolve())
                    for dependency in probe["dependencies"].values():
                        self.assertTrue(
                            Path(dependency["file"]).resolve().is_relative_to(VENV.resolve())
                        )
                        self.assertTrue(dependency["version"])
                for name, original in snapshots.items():
                    if name not in (STORAGE_MODULE, API_MODULE):
                        self.assertEqual((workspace / name).read_bytes(), original)
                events = services.events
                pod_delete = next(
                    i
                    for i, event in enumerate(events)
                    if event["method"] == "DELETE" and event["path"] == "/v2/pods/testpod"
                )
                first_download = next(
                    i for i, event in enumerate(events) if event["path"] == "/s3/download_file"
                )
                volume_delete = next(
                    i
                    for i, event in enumerate(events)
                    if event["method"] == "DELETE"
                    and event["path"] == "/v2/network-volumes/testvolume"
                )
                self.assertLess(pod_delete, first_download)
                self.assertLess(first_download, volume_delete)
                self.assertTrue(
                    any(
                        event["path"] == "/s3/list_objects_v2"
                        for event in events[first_download:volume_delete]
                    )
                )
                if transient:
                    self.assertFalse(services.get_failures)
                    self.assertEqual(services.s3_failures, 0)
                    self.assertTrue((folder / "startup-observation.json").is_file())
                    release = next(
                        i
                        for i, event in enumerate(events)
                        if event["path"] == "/s3/put_object"
                        and (event["key"] or "").endswith("/start.json")
                    )
                    self.assertTrue(
                        any(
                            event["path"] == "/v2/pods/testpod" and event["status"] == 503
                            for event in events[:release]
                        )
                    )
                    self.assertTrue(
                        any(
                            event["path"] == "/s3/get_object" and event["status"] == 503
                            for event in events[:release]
                        )
                    )
                for handle in services.handles.values():
                    while handle.running() and time.monotonic() < deadline - 2:
                        time.sleep(0.05)
                    self.assertFalse(
                        handle.running(), "CLI/watchdog remained alive after successful cleanup."
                    )
            finally:
                stop_private_processes(controller, workspace, services, deadline - 1)
                server.shutdown()
                server.server_close()
                thread.join(timeout=0.5)
        self.assertLess(
            time.monotonic() - started, 30, "CLI integration case exceeded its 30-second bound."
        )

    def test_cli_success_with_real_windows_watchdog(self):
        self.run_case(transient=False)

    def test_cli_detach_then_the_real_watchdog_finishes_alone(self):
        """launch --detach exits once the workload is released; the workload ends only after that, so the
        real watchdog process alone deletes the Pod, recovers and checks the results and deletes the volume."""
        started = time.monotonic()
        deadline = started + 50
        controller = None
        with tempfile.TemporaryDirectory(
            prefix="wondomon-cli-detach-", ignore_cleanup_errors=True
        ) as temporary:
            workspace = Path(temporary)
            services = FakeServices(workspace, transient=False)
            released = []
            services.s3.on_start = released.append  # the workload runs on; it finishes later
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(services))
            server.daemon_threads = True
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            thread.start()
            try:
                private_copy(workspace, f"http://127.0.0.1:{server.server_port}")
                allowed = {
                    "SYSTEMROOT",
                    "WINDIR",
                    "PATH",
                    "TEMP",
                    "TMP",
                    "USERPROFILE",
                    "COMSPEC",
                    "PATHEXT",
                    "APPDATA",
                    "LOCALAPPDATA",
                    "PROGRAMDATA",
                    "PROGRAMFILES",
                    "PSMODULEPATH",  # Keep CIM module lookup; no account credentials.
                }
                environment = {
                    key: value for key, value in os.environ.items() if key.upper() in allowed
                }
                environment.update(
                    PYTHONNOUSERSITE="1",
                    PYTHONDONTWRITEBYTECODE="1",
                    AWS_EC2_METADATA_DISABLED="true",
                    NO_PROXY="127.0.0.1",
                )
                controller = subprocess.Popen(
                    [
                        str(PYTHON),
                        "-B",
                        str(workspace / "managed_run.py"),
                        "launch",
                        "--detach",
                        "--state-dir",
                        str(workspace / "output"),
                        "--env-file",
                        str(workspace / ".env"),
                        str(workspace / "config.json"),
                    ],
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                stdout, stderr = controller.communicate(timeout=30)
                # Exit code 9 indicates a detached launch.
                self.assertEqual(controller.returncode, 9, stdout + stderr + repr(services.errors))
                self.assertNotIn(DUMMY_KEY, stdout + stderr)
                self.assertTrue(released)
                folder = services.folder()
                self.assertTrue((folder / "detached.json").is_file())
                self.assertIn("testpod", services.api.pods)
                proof = json.loads((folder / "watchdog-isolation.json").read_text())
                with services.lock:
                    watchdog = services.handles.get(proof["pid"]) or ProcessHandle(proof["pid"])
                    services.handles[proof["pid"]] = watchdog
                self.assertTrue(
                    watchdog.running(), "the watchdog must outlive its detached controller"
                )
                with services.lock:
                    services.finish(released[0])  # the workload ends now, with no controller left
                while watchdog.running() and time.monotonic() < deadline - 3:
                    time.sleep(0.1)
                self.assertFalse(watchdog.running(), "the watchdog did not finish the job")
                self.assertFalse(services.errors)
                summary = json.loads((folder / "summary.json").read_text())
                self.assertEqual(summary["training_outcome"], "success")
                self.assertTrue(
                    summary["pod_termination_verified"] and summary["volume_deletion_verified"]
                )
                self.assertTrue(services.delete_checked)
                self.assertEqual(
                    (services.api.pods, services.api.volumes, services.s3.objects), ({}, {}, {})
                )
                verify_local_backup(
                    json.loads((folder / "job.json").read_text()),
                    "testvolume",
                    folder / "downloads",
                )
            finally:
                stop_private_processes(controller, workspace, services, deadline - 1)
                server.shutdown()
                server.server_close()
                thread.join(timeout=0.5)
        self.assertLess(
            time.monotonic() - started, 50, "the detach case exceeded its 50-second bound"
        )

    def test_cli_retries_startup_rest_and_s3_observation_failures(self):
        self.run_case(transient=True)
