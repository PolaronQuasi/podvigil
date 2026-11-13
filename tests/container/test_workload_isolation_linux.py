"""The workload's isolation from the guard, checked as real Linux processes in a container.

Set PODVIGIL_REVIEW_DOCKER_IMAGE to an already-installed Python
image ID; images are never pulled.
"""

from pathlib import Path
import hashlib
import subprocess
import tempfile
import unittest

from podvigil.podrt.records import VOLUME_MOUNT, read
from tests.container import review_image
from tests.support.pod import POD_SOURCES, Container, job, linux_fake_source, stage_sources


MALICIOUS_WORKLOAD = r"""
import ctypes, errno, json, os, signal
from pathlib import Path
out = Path(os.environ['WONDOMON_OUTPUT_DIR'])
job = out.parent
checks = {}
def denied(name, action):
    try:
        value = action()
    except OSError as exc:
        checks[name] = exc.errno in (errno.EACCES, errno.EPERM)
    else:
        checks[name] = False
        if isinstance(value, int):
            os.close(value)
parent = os.getppid()
for pid in {1, parent}:
    for name in ('environ', 'mem'):
        denied(f'{pid}_{name}', lambda: os.open(f'/proc/{pid}/{name}', os.O_RDONLY))
    denied(f'{pid}_fd', lambda: os.listdir(f'/proc/{pid}/fd'))
    denied(f'{pid}_fd0', lambda: os.open(f'/proc/{pid}/fd/0', os.O_RDONLY))
    denied(f'{pid}_signal', lambda: os.kill(pid, signal.SIGKILL))
    libc = ctypes.CDLL(None, use_errno=True)
    attached = libc.ptrace(16, pid, None, None)
    checks[f'{pid}_ptrace'] = attached == -1 and ctypes.get_errno() in (errno.EPERM, errno.EACCES)
    if attached == 0:
        libc.ptrace(17, pid, None, None)
denied('control_list', lambda: os.listdir(job / 'control'))
denied('control_read', lambda: os.open(job / 'control/identity.json', os.O_RDONLY))
denied('control_write', lambda: os.open(job / 'control/identity.json', os.O_WRONLY))
denied('control_create', lambda: os.open(job / 'control/forged.json', os.O_WRONLY | os.O_CREAT, 0o600))
denied('status_write', lambda: os.open(job / 'agent.json', os.O_WRONLY | os.O_CREAT, 0o600))
denied('start_write', lambda: os.open(job / 'start.json', os.O_WRONLY | os.O_CREAT, 0o600))
denied('job_replace', lambda: os.rename(out, job / 'moved'))
# The network volume keeps no modes (777 everywhere); only the lock folder above it protects it.
volume = Path('@VOLUME@')
remote = volume / 'jobs' / job.name
denied('volume_stat', lambda: os.stat(volume))
denied('volume_lock_list', lambda: os.listdir(volume.parent))
denied('volume_list', lambda: os.listdir(volume))
denied('volume_status_write', lambda: os.open(remote / 'agent.json', os.O_WRONLY))
denied('volume_start_write', lambda: os.open(remote / 'start.json', os.O_WRONLY | os.O_CREAT, 0o600))
denied('volume_control_read', lambda: os.open(remote / 'control/identity.json', os.O_RDONLY))
denied('volume_input_read', lambda: os.open(volume / 'inputs/immutable.py', os.O_RDONLY))
denied('volume_forge', lambda: os.open(volume / 'forged.json', os.O_WRONLY | os.O_CREAT, 0o600))
for pid in {1, parent}:  # Nor through a root process's view of the file system.
    denied(f'{pid}_root_volume', lambda: os.stat(f'/proc/{pid}/root{volume}'))
denied('input_write', lambda: os.open(Path(os.environ['WONDOMON_INPUT_DIR']) / 'immutable.py', os.O_WRONLY))
denied('input_create', lambda: os.open(Path(os.environ['WONDOMON_INPUT_DIR']) / 'injected.py', os.O_WRONLY | os.O_CREAT, 0o600))
checks['input_read'] = (Path(os.environ['WONDOMON_INPUT_DIR']) / 'immutable.py').read_text() == 'VALUE = 42\n'
denied('restore_uid', lambda: os.setuid(0))
denied('restore_groups', lambda: os.setgroups([0]))
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
checks['uid'] = os.getresuid() == (61000, 61000, 61000)
checks['gid'] = os.getresgid() == (61000, 61000, 61000)
checks['groups'] = os.getgroups() == []
checks['capabilities'] = all(int(status[n].strip(),16) == 0 for n in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))
checks['nnp'] = status['NoNewPrivs'].strip() == '1'
checks['key_absent'] = all(k not in os.environ for k in ('RUNPOD_API_KEY','WONDOMON_JOB_SPEC','REVIEW_CASE','HF_TOKEN','COMPANY_AUTH'))
home = Path(os.environ['HOME'])
(home / 'cache' / 'ordinary-cache').write_bytes(b'regenerable')
checks['private_home'] = str(home).startswith('/tmp/wondomon-workload-') and home.stat().st_uid == 61000
denied('root_home', lambda: os.listdir('/root'))
(out / 'checkpoint.bin').write_bytes(b'only checkpoint')
(out / 'isolation-result.json').write_text(json.dumps(checks))
assert all(checks.values()), [name for name, passed in checks.items() if not passed]
""".replace("@VOLUME@", VOLUME_MOUNT)


@unittest.skipUnless(
    review_image(), "Set an installed Python image ID for Linux isolation regressions"
)
class IsolationDockerTests(unittest.TestCase):
    def container(self, *args, **kwargs):
        instance = Container(review_image(), *args, fake_source=linux_fake_source(), **kwargs)
        self.addCleanup(instance.close)
        return instance

    def test_real_linux_unit_regressions(self):
        with tempfile.TemporaryDirectory(prefix="isolation-linux-units-") as tmp:
            source = Path(tmp)
            stage_sources(
                source,
                *POD_SOURCES,
                "tests/support/pod.py",
                "tests/unit/test_workload_isolation.py",
            )
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network",
                    "none",
                    "--cpus",
                    "1",
                    "--memory",
                    "192m",
                    "--pids-limit",
                    "64",
                    "--mount",
                    f"type=bind,source={source},target=/review,readonly",
                    "--workdir",
                    "/review",
                    "--entrypoint",
                    "python3",
                    review_image(),
                    "-B",
                    "-m",
                    "unittest",
                    "tests.unit.test_workload_isolation.IsolationLinuxUnitTests",
                    "-v",
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("skipped", result.stderr)

    def test_malicious_workload_cannot_access_root_processes_or_control_files(self):
        with tempfile.TemporaryDirectory(prefix="isolation-workspace-") as tmp:
            workspace = Path(tmp)  # The network volume, as the controller filled it.
            (workspace / "inputs").mkdir()
            module = b"VALUE = 42\n"
            (workspace / "inputs/immutable.py").write_bytes(module)
            config = job(["python3", "-c", MALICIOUS_WORKLOAD])
            config["inputs"] = [
                {
                    "remote": "immutable.py",
                    "size": len(module),
                    "sha256": hashlib.sha256(module).hexdigest(),
                }
            ]
            container = self.container(config=config, workspace=workspace)
            container.finish(timeout=35)
            self.assertEqual(container.result()["outcome"], "success")
            folder = workspace / "jobs" / container.job["job_id"]
            checks = read(folder / "artifacts/isolation-result.json")
            self.assertGreater(len(checks), 35)
            self.assertTrue(all(checks.values()), checks)
            self.assertEqual(container.events()[-1]["pid"], 1)
            self.assertEqual(container.events()[-1]["live_pids"], [1])
            evidence = read(folder / "control/self-delete.json")
            self.assertTrue(evidence["absence_verified"])
            self.assertEqual(evidence["delete_http_status"], 204)

    def test_integrated_256mib_background_cache_stops_and_avoids_final_rehash(self):
        fake = (
            linux_fake_source()
            + r"""
OriginalApi = Api
class Api(OriginalApi):
    def __init__(self, key):
        super().__init__(key)
        agent = sys.modules['__main__']
        if os.getpid() != 1:
            original = agent.make_manifest
            def measured(job, folder, cache=None, deadline=None):
                before = cache.bytes_hashed
                result = original(job, folder, cache=cache, deadline=deadline)
                (self.root / 'cache-proof.json').write_text(json.dumps({
                    'primed_bytes': before, 'final_bytes': cache.bytes_hashed-before,
                    'hits': cache.cache_hits, 'thread_started': cache._thread is not None,
                    'thread_stopped': not cache._thread.is_alive(), 'deadline_supplied': deadline is not None}))
                return result
            agent.make_manifest = measured
"""
        )
        command = [
            "python3",
            "-c",
            (
                "import os,time;from pathlib import Path;"
                "f=Path('checkpoint.bin').open('wb');chunk=b'c'*(4*1024*1024);"
                "[f.write(chunk) for _ in range(64)];f.flush();os.fsync(f.fileno());f.close();time.sleep(11)"
            ),
        ]
        container = Container(review_image(), config=job(command), fake_source=fake)
        self.addCleanup(container.close)
        container.finish(timeout=40)
        self.assertEqual(container.result()["outcome"], "success")
        proof = read(container.workspace / "cache-proof.json")
        self.assertGreaterEqual(proof["primed_bytes"], 256 * 1024 * 1024)
        self.assertLess(proof["final_bytes"], 1024 * 1024)
        self.assertGreaterEqual(proof["hits"], 1)
        self.assertTrue(
            proof["thread_started"] and proof["thread_stopped"] and proof["deadline_supplied"]
        )

    def test_process_dump_protection_is_set_before_api_receives_key(self):
        fake = (
            linux_fake_source()
            + r"""
OriginalApi = Api
class Api(OriginalApi):
    def __init__(self, key):
        agent = sys.modules['__main__']
        assert agent.isolation.prctl(agent.isolation.PR_GET_DUMPABLE) == 0
        super().__init__(key)
        (self.root / ('protected-' + str(os.getpid()))).write_text('dumpable=0')
"""
        )
        container = Container(review_image(), fake_source=fake)
        self.addCleanup(container.close)
        container.finish(timeout=30)
        self.assertEqual(container.result()["outcome"], "success")
        self.assertTrue((container.workspace / "protected-1").exists())
        self.assertEqual(len(list(container.workspace.glob("protected-*"))), 2)

    def test_volume_permission_failure_never_executes_workload_and_guard_deletes(self):
        fake = (
            linux_fake_source()
            + r"""
OriginalApi = Api
class Api(OriginalApi):
    def __init__(self, key):
        super().__init__(key)
        if os.getpid() != 1:
            def denied(*args, **kwargs):
                raise PermissionError('volume chown unsupported')
            sys.modules['__main__'].isolation.permissions = denied
"""
        )
        instance = Container(review_image(), fake_source=fake)
        self.addCleanup(instance.close)
        instance.finish(timeout=30)
        self.assertEqual(instance.result()["outcome"], "agent_failed")
        output = instance.workspace / "jobs" / instance.job["job_id"] / "artifacts"
        self.assertFalse((output / "run-count").exists())
        self.assertEqual(instance.events()[-1]["pid"], 1)
