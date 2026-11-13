# The workload contract

This page says what your command (the workload) sees inside the Pod, what it must do, and what the
design costs you.

In short: your command runs once, in the foreground, as an unprivileged Linux user, in the image you
name. It reads its inputs from `/workspace/inputs`, which is read-only, and writes its results into
the folder that `PODVIGIL_OUTPUT_DIR` names, which is also its working directory. What it prints
goes to `train.log`. When its time is up, or the job is cancelled, it gets SIGTERM, and SIGKILL 10
seconds later. Everything it writes stays on the Pod's own disk until it ends; then the runner
hashes the outputs and copies them to the network volume. If the Pod dies before that, the outputs
are lost.

## The image

- **Linux, with `python3` on the `PATH`.** The Pod's start command is replaced by
  `python3 -B -u -c <the guard's code>`. The guard uses only Python's standard library.
- **It must start as root** (the default for most images). The guard needs root to protect itself
  and to switch your command to an unprivileged user. As another user it stops at once with
  "Workload isolation requires a trusted Linux root supervisor."
- **Its own entrypoint and command do not run.** Anything they would set up, such as starting a
  service or activating an environment, does not happen. Put that into your command instead, for
  example `["bash", "-c", "source /opt/env/bin/activate && python -u -m train"]`.
- **No SSH, no Jupyter, no open ports.** The Pod is created with none of them.
- **Pin it.** A tag other than `latest`, or a digest.
- The examples use `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`, the image of the project's own
  smoke job (`jobs/smoke.example.json`).

## The process

- The command runs exactly once. If the container restarts, the guard sees the earlier attempt's
  marker on the volume and ends the job instead of running it again.
- It runs as user and group 61000 (named `wondomon-workload` in `USER`), with no supplementary
  groups, no Linux capabilities, and "no new privileges": `sudo` and set-user-ID programs do not
  work.
- It starts in a new session, in its output folder, with an empty standard input (`/dev/null`).
- It cannot read the guard's code, its records, its process memory or its key, and it cannot reach
  the network volume. This is ordinary Linux user separation inside a container, not a virtual
  machine. See [the safety model](SAFETY-MODEL.md).

## Files and folders

<!-- prettier-ignore -->
| Path | What it is | Your access |
| --- | --- | --- |
| `/workspace/inputs` (also `PODVIGIL_INPUT_DIR`) | your inputs, under their `remote` names, each checked against its SHA-256 | read and execute, no write |
| `/workspace/jobs/<job id>/artifacts` (also `PODVIGIL_OUTPUT_DIR`) | your outputs; the working directory | read and write |
| `HOME`: a new private folder, normally under `/tmp`, with `tmp/` and `cache/` inside | scratch space and caches; not copied back | read and write |
| the network volume | locked below a folder only root may enter | none |
| the rest of the image | as the image allows an ordinary user | usually read-only |

Everything in this table is on the Pod's own disk, whose size is `disk_gb`.

## Environment variables

Your command gets a small, fixed environment, not the container's:

- From the container, only these names, when they are set: `PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`,
  `TZ`, `LD_LIBRARY_PATH`, `LIBRARY_PATH`, `CPATH`, `CUDA_HOME`, `CUDA_PATH`,
  `CUDA_VISIBLE_DEVICES`, `CUDA_MODULE_LOADING`, `NVIDIA_VISIBLE_DEVICES`,
  `NVIDIA_DRIVER_CAPABILITIES`, `NVIDIA_REQUIRE_CUDA`, `OMP_NUM_THREADS`, `MKL_NUM_THREADS`,
  `OPENBLAS_NUM_THREADS`, `NCCL_DEBUG`, `NCCL_SOCKET_IFNAME`, `NCCL_IB_DISABLE`, `NCCL_P2P_DISABLE`,
  `PYTHONUNBUFFERED` and `PYTHONHASHSEED`.
- Then the variables from the job file's `workload_env`.
- Then, always set by the runner:

<!-- prettier-ignore -->
| Variable | Value |
| --- | --- |
| `PODVIGIL_OUTPUT_DIR`, `PODWARDEN_OUTPUT_DIR`, `WONDOMON_OUTPUT_DIR` | `/workspace/jobs/<job id>/artifacts` |
| `PODVIGIL_INPUT_DIR`, `PODWARDEN_INPUT_DIR`, `WONDOMON_INPUT_DIR` | `/workspace/inputs` |
| `PYTHONPATH` | `/workspace/inputs` |
| `HOME` | the private folder above |
| `USER`, `LOGNAME` | `wondomon-workload` |
| `TMPDIR` | `$HOME/tmp` |
| `XDG_CACHE_HOME` | `$HOME/cache` |
| `CUDA_CACHE_PATH`, `TORCH_HOME`, `HF_HOME`, `HUGGINGFACE_HUB_CACHE`, `TRANSFORMERS_CACHE`, `PIP_CACHE_DIR` | folders below `$HOME/cache` |
| `PYTHONDONTWRITEBYTECODE` | `1` |

Nothing else arrives. In particular the workload never sees `RUNPOD_API_KEY` (the Pod's key),
`RUNPOD_POD_ID`, other `RUNPOD_*` variables, or the job's own description.

The `PODWARDEN_` and `WONDOMON_` names remain aliases for existing workloads. Each set names the
same two folders. New workloads should use `PODVIGIL_`; see [compatibility](COMPATIBILITY.md) for
the corresponding local configuration and job-record behavior.

## Outputs

- Write everything you want back below `PODVIGIL_OUTPUT_DIR`. Relative paths land there, because it
  is the working directory.
- Standard output and standard error are appended to `train.log` in that folder.
- `result.json` and `train.log` are the runner's. It writes `result.json` itself at the end and
  replaces any file of that name. Leave both alone.
- Every file in `required_artifacts` must exist when your command exits with code 0. Otherwise no
  complete manifest is published and the volume is kept for recovery.
- **Plain outputs are copied as they are.** Ordinary files and non-empty folders with ordinary
  permissions and names that Windows can store arrive as the same files on your computer.
- **Anything else is packed into an archive (a bundle):** a link, an empty folder, an executable
  file, unusual permissions, or names Windows cannot store (for example with `:` or `?`, or two
  names that differ only in letter case). The download then holds `output-bundle/<id>/archive.tar`
  and `metadata.json` beside the plain files. The bundle needs a second copy of the outputs on the
  Pod's disk and on the volume, and more time to copy. Unpack it with
  `python -m podvigil.podrt.bundle` (see [the command line reference](MANUAL.md#the-bundle-tool)).
- **Some things make the snapshot fail:** pipes, sockets or device files in the output folder, a
  second filesystem mounted inside it, or hard links to files outside it. The volume is then kept
  with whatever plain files could be copied.
- Your command is stopped before the snapshot, together with every process it started, so no process
  may keep writing after it.

## Time

- The deadline counts from the Pod request. Your command starts after the Pod has started, the
  inputs are copied and the checks have passed, and it must be stopped `finalize_grace_seconds`
  before the deadline. See [concepts](CONCEPTS.md#the-deadline).
- **When the time is up**, the runner sends SIGTERM to your command's process group, waits 10
  seconds, sends SIGKILL, and then kills any process that is left. The same happens when the guard
  inside the Pod stops the job for another reason (a stop signal to the container, a lost heartbeat,
  the startup limit). Save what you need quickly on SIGTERM. If copying the outputs is still running
  at the deadline itself, every process is killed at once and the Pod is deleted.
- **A cancel is graceful, within limits.** After `cancel`, or Ctrl+C on a foreground `launch` while
  its watchdog runs, the Pod finds the watchdog's request on the volume within about 2 seconds and
  stops your command exactly as at its time limit: SIGTERM, SIGKILL 10 seconds later. Then it copies
  your outputs with the outcome `cancelled` and deletes itself. The watchdog waits for that at most
  `finalize_grace_seconds` plus 30 seconds, never past the deadline; after that it deletes the Pod,
  and outputs not yet copied to the volume are lost.
- **Other stops from outside the Pod are not graceful.** `recover` on a running job,
  `export-partial`, and the watchdog's own checks (a frozen supervisor, a wrong price, a Pod that
  stopped or failed) delete the Pod at once, and so does a cancel before the workload was released.
  What Runpod does to the container then is outside the runner's control; outputs not yet copied to
  the volume are lost.
- **Your command is not told its deadline.** If it should finish by itself, give it a time budget
  through `workload_env` that is safely below
  `max_runtime_seconds - finalize_grace_seconds - the startup time`.
  [examples/checkpoints](../examples/checkpoints) does this.

The workload's outcome is recorded in `result.json` and, as `training_outcome`, in the ledger's
`summary.json`:

<!-- prettier-ignore -->
| Outcome | Meaning |
| --- | --- |
| `success` | your command exited with code 0 |
| `training_failed` | your command exited with another code |
| `time_limit` | your command was stopped because its time was up |
| `cancelled` | your command was stopped because the job was cancelled, as at the time limit |
| `interrupted` | the guard stopped the supervisor (the container got a stop signal, a heartbeat went missing, or the startup limit passed), before or while your command ran |
| `agent_failed` | the supervisor failed: a check, the input copy, or starting your command; the reason is in `guard-failure.json` (see [troubleshooting](OPERATIONS.md#failures-inside-the-pod)) |
| `agent_preflight_failed` | the supervisor ended before it recorded any other outcome (rare) |

The ledger's `summary.json` can also say `not_started`: no Pod was created, or the Pod stopped
before its supervisor began the job; `outputs_lost`: your command ran, but the Pod ended without
publishing anything, so its outputs are lost; or `partial_outputs`: the outputs were incomplete, and
`recover --accept-partial` copied what was on the volume (see
[the command line reference](MANUAL.md#recover)).

A job that ends with `time_limit` still gets its outputs hashed, downloaded and verified, and its
volume deleted; `launch` then ends with the outcome `workload_failed` (exit code 2), because the
workload's outcome is not `success`. A job that ends with `cancelled` gets the same treatment, and a
foreground `launch` ends with the outcome `cancelled` (exit code 10).

## The cost of the local-disk design

The network volume keeps neither file ownership nor permissions. On Runpod it is a network
filesystem (MooseFS through FUSE): every file shows owner root and mode 777, `chmod` does nothing,
and `chown` is refused even for root. Measured on 12 November 2026. On such a volume the workload
could read and change the guard's records, so the runner does not let the workload near it. Instead:

- the volume is mounted at `/guard-volume/data`, below a folder that only root may enter;
- the guard copies your inputs from the volume to the Pod's own disk and checks them;
- your command works only on the Pod's own disk;
- at the end the guard hashes the outputs and copies them, and the manifest last, to the volume.

What that costs:

- **A Pod that dies hard loses every output not yet copied.** Until the end, the volume holds only
  your inputs, the list of inputs, the guard's records and the once-only marker (and, after a
  cancel, the watchdog's request to finish); an early copy of `result.json` is added when
  finalization starts. A host failure, an out-of-memory kill of the whole container, or a provider
  fault in a long job loses its work. There is no copying during the run. When nothing at all was
  published, `recover` deletes the volume on that evidence and reports the outcome `outputs_lost`.
- **`disk_gb` must hold the inputs and the outputs**, and a second copy of the outputs if they
  become a bundle.
- **`startup_timeout_seconds` must cover copying all inputs** from the volume to the disk, on top of
  starting the Pod and downloading the image.
- **`finalize_grace_seconds` must cover hashing and copying all outputs** to the volume. If time
  runs out there is no manifest, and the volume is kept with partial files.
- **All output bytes cross the network at the end**, twice for a bundle.

So: keep jobs short, keep outputs small, and chain jobs. A long training run becomes several jobs:
each saves a checkpoint into its outputs, and the next job lists the downloaded checkpoint as an
input, for example:

```json
{
  "local": "<ledger of the previous job>/downloads/artifacts/checkpoints/latest.pt",
  "remote": "resume/checkpoint.pt"
}
```

## GPUs and CPU cores

One GPU can be saturated by one small training process. On 12 November 2026 one process of a small
transformer fit took 6.7 ms per update on an RTX 4090, and 4, 8, 12 or 16 processes together gave
the same total of about 150 updates per second as one. For that kind of workload, more CPU cores or
more processes per GPU do not help; choose the GPU, not the core count. Measure your own workload
before you pay for a larger Pod.

## Trying the workload before you pay

`podvigil run-local <job file>` runs the job's command on your own computer, laid out as the Pod
lays it out: the inputs copied and hash-checked into an input folder, an empty output folder as the
working directory, `train.log`, and the same environment variables, built by the Pod's own code. At
the end it writes `result.json` and checks `required_artifacts`. It makes no cloud call and reads no
credentials. See [the command line reference](MANUAL.md#run-local).

It is not the Pod: your command runs as you, with your computer's programs instead of the image's,
without the separate user, and perhaps without a GPU. The command must exist on your computer;
`--python` names the Python to use for a `python3` or `python` command. Start scripts from the
inputs with `-m` (see [the job file](JOB-FILE.md#the-command)), because the path
`/workspace/inputs/...` exists only in the Pod.

## Checklist before you launch

- The image is pinned, has `python3`, starts as root, and has everything your command needs.
- The command is an argument list; a script from the inputs starts as `python3 -u -m <module>`.
- Everything you want back is written below `PODVIGIL_OUTPUT_DIR`, as plain files.
- The files in `required_artifacts` are written on success.
- The workload saves and exits within 10 seconds of SIGTERM, and ends every process it starts.
- It has a time budget below the job's lifetime, if it should end with `success`.
- `disk_gb`, `startup_timeout_seconds` and `finalize_grace_seconds` leave room for the copies.
- `podvigil plan` passes, and `podvigil run-local` ends with `success`.
