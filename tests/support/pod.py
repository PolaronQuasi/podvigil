"""Pod-side fixtures: a Pod job, a real guard run in a network-less container, and the staged fake API.

This module imports only the standard library and the
Pod's own code, because the container lane copies it into Linux containers (stage_sources).
"""

from pathlib import Path
import base64
import json
import shutil
import subprocess
import tempfile
import time
import uuid

from podvigil.podrt.records import VOLUME_MOUNT, read, save
from tests.support.paths import ROOT


# The files the controller ships into the Pod, their parent package, and the test packages
# needed for a container run.
POD_SOURCES = (
    "src/podvigil/__init__.py",
    *(
        "src/podvigil/podrt/" + name
        for name in (
            "__init__.py",
            "agent.py",
            "api.py",
            "bundle.py",
            "isolation.py",
            "records.py",
            "teardown.py",
        )
    ),
)
TEST_PACKAGES = (
    "tests/__init__.py",
    "tests/support/__init__.py",
    "tests/support/paths.py",
    "tests/unit/__init__.py",
)


def stage_sources(destination, *paths):
    """Copy an explicit allowlist of checkout files, plus the test packages, for a container run.

    Never the checkout itself or its credentials: the folder is mounted read-only into the container.
    """
    for name in (*TEST_PACKAGES, *paths):
        target = Path(destination) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)


def job(command=None):
    return {
        "job_id": "b" * 32,
        "pod_name": "review-pod",
        "prior_pod_ids": [],
        "volume_id": "testvolume",
        "inputs": [],
        "deadline_epoch": time.time() + 60,
        "startup_deadline_epoch": time.time() + 30,
        "config": {
            "cloud": "SECURE",
            "agent_stale_seconds": 10,
            "finalize_grace_seconds": 2,
            "required_artifacts": [],
            "command": command
            or [
                "python3",
                "-B",
                "-c",
                (
                    "from pathlib import Path;import os;"
                    "p=Path(os.environ['WONDOMON_OUTPUT_DIR'])/'run-count';"
                    "p.write_text(str(int(p.read_text())+1) if p.exists() else '1');"
                    "(Path(os.environ['WONDOMON_OUTPUT_DIR'])/'model.bin').write_bytes(b'checkpoint')"
                ),
            ],
        },
    }


class Container:
    """One real guard run in a network-less container.

    `workspace` is the host folder that plays the job's network volume: it is mounted at
    VOLUME_MOUNT, and the fake gives it the measured Runpod volume behavior. The container's
    own /workspace is the Pod's disk. `init=True` starts the guard under an injected init, as
    Runpod does, so the guard is not PID 1.
    """

    def __init__(
        self,
        image,
        case="success",
        config=None,
        workspace=None,
        pids=64,
        fake_source=None,
        init=False,
        memory="128m",
    ):
        self.temp = tempfile.TemporaryDirectory(prefix="pod-linux-review-")
        self.base = Path(self.temp.name)
        # Never below the bootstrap folder: that mount would expose the volume a second time.
        self.volume_temp = (
            None if workspace else tempfile.TemporaryDirectory(prefix="pod-linux-volume-")
        )
        self.workspace = workspace or Path(self.volume_temp.name)
        self.workspace.mkdir(exist_ok=True)
        self.job = config or job()
        save(
            self.workspace / "jobs" / self.job["job_id"] / "start.json",
            {"job_id": self.job["job_id"], "pod_id": "testpod"},
        )
        from podvigil.controller.request import pod_bootstrap, pod_sources  # host side only

        source = pod_sources()
        source["api.py"] = fake_source or (
            (ROOT / "tests/support/linux_review_api.py")
            .read_text()
            .replace(
                "self.case = os.environ.get", "self.key = key\n        self.case = os.environ.get"
            )
            .replace("raise ApiError(403)", "raise ApiError(503)")
            + """
Api.now = lambda self: time.time()
original_request = Api.request
original_get = Api.get_or_none
def observed_request(self, method, path, body=None):
    try:
        value = original_request(self, method, path, body)
    except ApiError as exc:
        self.last_response = {'method': method, 'path': path, 'status': exc.status}
        raise
    self.last_response = {'method': method, 'path': path, 'status': 204 if method == 'DELETE' else 200,
                          'response_date': 'Tue, 10 Nov 2026 20:00:00 GMT'}
    return value
def observed_get(self, path):
    value = original_get(self, path)
    if value is None:
        self.last_response = {'method': 'GET', 'path': path, 'status': 404}
    return value
Api.request = observed_request
Api.get_or_none = observed_get
"""
        )
        # Write the controller's bootstrap to a file to fit Windows' command-line length limit.
        (self.base / "bootstrap.py").write_text(pod_bootstrap(source), encoding="utf-8")
        self.name = "wondomon-pod-review-" + uuid.uuid4().hex[:12]
        args = [
            "docker",
            "run",
            "--pull=never",
            "--name",
            self.name,
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            memory,
            "--pids-limit",
            str(pids),
            *(["--init"] if init else []),
            "--mount",
            f"type=bind,source={self.workspace},target={VOLUME_MOUNT}",
            "--mount",
            f"type=bind,source={self.base},target=/review,readonly",
            "-e",
            "RUNPOD_API_KEY=DUMMY",
            "-e",
            "RUNPOD_POD_ID=testpod",
            "-e",
            "REVIEW_CASE=" + case,
            "-e",
            "WONDOMON_JOB_SPEC=" + base64.b64encode(json.dumps(self.job).encode()).decode(),
            "--entrypoint",
            "python3",
            image,
            "-B",
            "-u",
            "-c",
            "exec(compile(open('/review/bootstrap.py').read(),'/review/bootstrap.py','exec'))",
        ]
        self.process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        self.output = None

    def finish(self, timeout=25, expected_code=0, expect_deleted=True):
        self.output = self.process.communicate(timeout=timeout)[0]
        if self.process.returncode != expected_code:
            state = subprocess.run(
                ["docker", "inspect", "--format", "{{json .State}}", self.name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            raise AssertionError(
                f"Container exited {self.process.returncode}; expected {expected_code}.\n"
                + (state.stdout or state.stderr)
                + "\n"
                + self.output
            )
        if expect_deleted and not (self.workspace / "deleted").exists():
            raise AssertionError("No verified fake deletion: " + self.output)
        return self.output

    def events(self):
        return [
            json.loads(line)
            for line in (self.workspace / "delete-events.jsonl").read_text().splitlines()
        ]

    def result(self):
        return read(self.workspace / "jobs" / self.job["job_id"] / "artifacts/result.json")

    def wait_for(self, name, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if (self.workspace / name).exists():
                return
            if self.process.poll() is not None:
                raise AssertionError(self.process.communicate()[0])
            time.sleep(0.05)
        raise AssertionError("Timed out waiting for " + name)

    def wait_for_local(self, path, timeout=10):
        """Text of a file on the Pod's own disk, read as root inside the running container."""
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            found = subprocess.run(
                ["docker", "exec", "--user", "0", self.name, "cat", path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if found.returncode == 0 and found.stdout:
                return found.stdout
            if self.process.poll() is not None:
                raise AssertionError(self.process.communicate()[0])
            time.sleep(0.1)
        raise AssertionError("Timed out waiting for " + path)

    def close(self):
        subprocess.run(
            ["docker", "rm", "-f", self.name], capture_output=True, check=True, timeout=15
        )
        if self.output is None:
            self.process.communicate(timeout=10)
        self.temp.cleanup()
        if self.volume_temp is not None:
            self.volume_temp.cleanup()


def linux_fake_source():
    """Extend only the staged fake; never edit the shared API or fake module."""
    original = (ROOT / "tests/support/linux_review_api.py").read_text()
    return (
        original
        + r"""
import threading

BaseApi = Api
class Api(BaseApi):
    def __init__(self, key):
        super().__init__(key)
        self.key, self.last_response = key, None
        self.got_own = False
        main = sys.modules['__main__']
        if self.case == 'slow_telemetry' and os.getpid() != 1:
            save = main.save
            def delayed_save(path, value):
                if Path(path).name == 'agent.json' and threading.current_thread() is not threading.main_thread():
                    (self.root / 'telemetry-blocked').touch()
                    time.sleep(60)
                return save(path, value)
            main.save = delayed_save
        if self.case == 'hash_cutoff' and os.getpid() != 1:
            def blocked_hash(*args, **kwargs):
                (self.root / 'hash-started').touch()
                time.sleep(60)
                raise AssertionError('Guard failed to enforce cutoff')
            main.make_manifest = blocked_hash

    def now(self):
        if not self.got_own:
            raise AssertionError('Clock anchored before own Pod GET')
        return time.time()

    def request(self, method, path, body=None):
        assert path.startswith('/pods/testpod')
        self.last_response = {'method': method, 'path': path,
            'status': 204 if method == 'DELETE' else 200,
            'response_date': 'Tue, 10 Nov 2026 20:00:00 GMT'}
        with (self.root / 'api-events.jsonl').open('a') as stream:
            stream.write(json.dumps({'pid': os.getpid(), 'method': method, 'path': path,
                'scoped_key_in_environ': 'RUNPOD_API_KEY' in os.environ}) + '\n')
        if method == 'GET':
            self.got_own = True
            if self.case == 'get_denied':
                self.last_response['status'] = 403
                raise ApiError(403)
        if method == 'DELETE' and self.case in ('delete_denied', 'both_denied'):
            self.last_response['status'] = 403
            raise ApiError(403)
        if method == 'POST':
            assert body == {'action': 'stop'}
            if self.case == 'both_denied':
                self.last_response['status'] = 401
                raise ApiError(401)
            return {'id': 'testpod', 'status': 'EXITED'}
        return super().request(method, path, body)

    def get_or_none(self, path):
        if (self.root / 'deleted').exists():
            self.last_response = {'method': 'GET', 'path': path, 'status': 404,
                'response_date': 'Tue, 10 Nov 2026 20:00:00 GMT'}
            return None
        return self.request('GET', path)
"""
    )
