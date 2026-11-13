# The job file

A job file is one JSON object that describes one job. `podvigil plan <job file>` checks it offline
and shows what `launch` would do; run it after every change. Unknown fields are refused, so a typing
mistake never passes silently.

## A minimal job file

```json
{
  "image": "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404",
  "gpu_id": "NVIDIA GeForce RTX 4090",
  "data_center_id": "EU-RO-1",
  "min_cuda_version": "12.8",
  "max_runtime_seconds": 1800,
  "max_compute_usd_per_hour": 1.0,
  "max_compute_usd": 0.50,
  "inputs": [
    {"local": "train.py", "remote": "train.py"}
  ],
  "command": ["python3", "-u", "-m", "train"]
}
```

These eight fields are required: `image`, `gpu_id`, `data_center_id`, `min_cuda_version`, `command`,
`max_runtime_seconds`, `max_compute_usd_per_hour` and `max_compute_usd`. With launch candidates,
`gpu_id` and `data_center_id` move into the candidates (see below). Everything else has a default.
The three folders under [`examples/`](../examples) hold complete, checked job files.

## Fields

Whole numbers must be JSON integers (`30`, not `30.0`).

<!-- prettier-ignore -->
| Field | Default | Allowed values | Meaning |
| --- | --- | --- | --- |
| `image` | required | a tag other than `latest`, or a digest (`name@sha256:...`) | the container image the Pod runs; see [the workload contract](WORKLOAD-CONTRACT.md#the-image) |
| `gpu_id` | required | a Runpod GPU type ID, as `survey` prints it (`survey --json` shows IDs longer than 46 characters in full) | the GPU type |
| `gpu_count` | `1` | 1 to 8 | GPUs in the Pod |
| `data_center_id` | required | one of the runner's S3 data centers (below) | where the Pod and the volume are created |
| `min_cuda_version` | required | `"major.minor"`, at least the runner's CUDA floor: `"12.8"`, or the `PODVIGIL_MIN_CUDA_VERSION` setting | the lowest CUDA version the host machine must support |
| `cloud` | `"SECURE"` | only `"SECURE"` | there is no Community Cloud fallback |
| `command` | required | a non-empty list of strings | the workload's command, as an argument list; no shell is involved |
| `inputs` | `[]` | a list of input entries (below) | files copied to the Pod before the workload starts |
| `required_artifacts` | `[]` | output paths, relative to the output folder | files a successful workload must leave |
| `workload_env` | `{}` | an object of string values (rules below) | extra environment variables for the workload |
| `max_runtime_seconds` | required | 300 to 604800 (five minutes to seven days) | the longest the Pod may exist, counted from the Pod request |
| `max_compute_usd_per_hour` | required | a positive number | the price ceiling for the whole Pod (GPU price times `gpu_count`) |
| `max_compute_usd` | required | a positive number | the compute budget; it shortens the lifetime (below) |
| `startup_timeout_seconds` | `900` | at least 1, less than `max_runtime_seconds` | from the Pod request until the workload must be released |
| `finalize_grace_seconds` | `120` | at least 30, less than half of `max_runtime_seconds` | reserved at the end for stopping the workload and copying the outputs |
| `preparation_timeout_seconds` | `1800` | 60 to 86400 | for everything before the Pod request: the volume, the uploads |
| `disk_gb` | `30` | a positive integer | the Pod's own disk, where the workload's files live |
| `volume_gb` | `20` | 10 to 4096 | the size of the job's new network volume |
| `volume_type` | `"STANDARD"` | only `"STANDARD"` | the network volume's storage type |
| `poll_seconds` | `10` | 2 to 30 | how often the controller and the watchdog check the job |
| `controller_stale_seconds` | `180` | at least 6 times `poll_seconds` | no new resource is created when the controller's heartbeat is older than this |
| `agent_stale_seconds` | `180` | at least 6 times `poll_seconds` | the job is stopped when the Pod's supervisor shows no sign of life for this long |
| `self_termination_probe` | `false` | `true` or `false` | test that the Pod deletes itself (below) |
| `self_termination_grace_seconds` | `60` | 1 to 3600 | how long the watchdog waits for that self-deletion |
| `candidates` | none | 1 to 8 entries | GPU and data-center pairs to try in order (below) |

The CUDA floor is `12.8` unless the environment variable `PODVIGIL_MIN_CUDA_VERSION` names another
`major.minor` version. It applies when a job file is read and at launch, and `survey` starts at it;
a job already launched keeps the floor it was launched with.

## Time and money

The Pod's lifetime is the smaller of two numbers:

- `max_runtime_seconds`, and
- the time the budget buys at the ceiling: `3600 * max_compute_usd / max_compute_usd_per_hour`
  seconds, rounded down.

`plan` prints the result as `maximum_pod_lifetime_seconds`. It must be longer than
`startup_timeout_seconds + finalize_grace_seconds`; otherwise the job file is refused with "Compute
budget leaves no training time after startup/finalization allowances."

The time your workload gets is the lifetime minus `finalize_grace_seconds` minus however long the
Pod took to start. With the defaults (900 seconds of startup allowance and 120 of finalization), a
job needs a lifetime of at least 1021 seconds, so short jobs set smaller values, as the examples do.

The price ceiling is checked against the live catalog price times `gpu_count` twice before the Pod
is requested, then against the price the created Pod reports, before the workload is released and
while it runs. A higher price stops the launch with "Live Secure Cloud price exceeds the configured
ceiling." or "Actual Pod price is missing or exceeds the configured ceiling.", or stops a running
job.

The budget and the ceiling plan the compute cost; they are not a provider-enforced spending cap.
Storage (the volume while it exists) and taxes are outside the budget. See
[the safety model](SAFETY-MODEL.md).

## The image

Pin the image to a specific tag or a digest; `latest` and untagged names are refused. The image
needs `python3` and must start as root. Its own entrypoint and command do not run.
[The workload contract](WORKLOAD-CONTRACT.md#the-image) has the details.

## The command

`command` is the argument list of the workload's process, for example
`["python3", "-u", "-m", "train", "--epochs", "3"]`. There is no shell, so `$VAR`, `~`, wildcards
and `&&` are not expanded. For shell features use `["bash", "-c", "your shell line"]`. The `-u`
makes Python print without buffering, so `train.log` stays current.

To start a script that is one of your inputs, prefer `-m` with its module name, as above: the input
folder is on `PYTHONPATH`, so this works in the Pod and with `run-local` on your own computer. The
path `/workspace/inputs/train.py` works in the Pod only.

## Inputs

Inputs are files on your computer. `launch` uploads them to the new volume; in the Pod the guard
copies them to `/workspace/inputs`, checks each one against its SHA-256, and makes them read-only.
The workload sees them below `/workspace/inputs`, the folder that `PODVIGIL_INPUT_DIR` names, and
Python finds modules there because the folder is also on `PYTHONPATH`.

Relative local paths start at the job file's folder. Each entry has one of three forms:

```json
"inputs": [
  {"local": "train.py", "remote": "train.py"},
  {"directory": "data", "remote": "data"},
  {"pattern": "*.json", "base": "configs", "remote": "settings"}
]
```

With a folder `data/` holding `a.csv` and `more/b.csv`, and `configs/` holding `run.json`, the Pod
gets `/workspace/inputs/train.py`, `/workspace/inputs/data/a.csv`,
`/workspace/inputs/data/more/b.csv` and `/workspace/inputs/settings/run.json`.

- **One file:** `local` is the file on your computer, `remote` its name below `/workspace/inputs`.
- **A folder:** every file below `directory`, recursively, named `<remote>/<path below the folder>`.
  Both keys are required.
- **A pattern:** the files that `pattern` matches below `base` (default: the job file's folder),
  named `<remote>/<path below base>`, or just `<path below base>` when `remote` is left out. Each
  `*` matches within one folder level. `**` is refused; use a folder input for a whole tree. The
  pattern must be relative and use `/`, without `.` or `..`. Folders that a pattern matches are
  skipped; only files are taken.

`plan` expands folders and patterns into the explicit list and prints it with sizes and SHA-256.
`launch` hashes the files again when it starts.

The runner refuses, and never skips silently:

- links, Windows junctions and other reparse points, anywhere in the path of an input;
- special files (pipes, devices);
- any file whose name starts with `.env`, and any file that is one of the runner's own credentials
  files, under whatever name;
- a folder or pattern that yields no files, or more than 10,000, and more than 10,000 input files in
  all;
- two inputs with the same `remote` name;
- a file name also used as a directory, such as `data` together with `data/train.csv`;
- `remote` names that are absolute, contain `\` or `:`, or contain `.`, `..` or empty parts.

Remote names cannot contain NUL and are case-sensitive on the Linux Pod. `run-local` additionally
rejects names that alias each other on the local filesystem; on Windows it checks Windows filename
rules before copying any input. Staging never overwrites an existing input file.

### How many inputs fit

Up to 10,000 files in all, after folders and patterns are expanded (the limit in the list above).
The size of the Pod request no longer limits the number of inputs:

- After the uploads, `launch` records the size and SHA-256 of the list of inputs (each file's name,
  size and SHA-256) in the ledger, writes the list, the input manifest, to the job's volume as
  `jobs/<job id>/control/inputs.json`, and reads it back by its hash before it requests a Pod.
- The Pod request carries only the manifest's size, SHA-256 and number of entries. In the Pod, the
  guard reads the manifest without following links and checks its size, SHA-256, job ID, count and
  every entry before it copies any input.

The Pod request still has a limit: Runpod refuses a request above 102,400 bytes, and this runner
sends at most 100,000. The Pod's own code and the job's settings take about 57,000 of them, and each
Pod already in your account adds about 25 bytes, because the request also carries the IDs of the
Pods that existed before the job. `plan` prints the exact numbers under `pod_request` (`bytes`,
`limit_bytes`, `headroom_bytes`, `input_files`), and `plan` and `launch` refuse an oversized request
before anything is created.

Each input is still uploaded, copied to the Pod's disk and checked on its own, so many inputs take
upload time within `preparation_timeout_seconds` and copy time within `startup_timeout_seconds` (see
[sizing for copies](#sizing-for-copies)). For many small files, one archive that your command
unpacks is still an option.

## Required artifacts

`required_artifacts` lists files, relative to the workload's output folder, that a successful
workload must leave, for example `["metrics.json", "checkpoints/latest.pt"]`. They must be regular
files. If the workload exits with code 0 but one is missing, the runner does not publish a complete
manifest: the volume is kept for recovery, and `recover` reports "No valid complete artifact
manifest. Keep the volume for recovery." The names follow the same rules as Windows file names,
because the results are downloaded to your computer: no `< > : " \ | ? *`, no trailing dot or space,
no reserved names such as `CON` or `NUL`. Do not list `result.json` or `train.log`; the runner
always includes them.

## Workload environment

`workload_env` adds environment variables for the workload, for example
`{"BATCH_SIZE": "8", "OMP_NUM_THREADS": "4"}`. Values must be strings. A name is refused if it:

- is not made of capital letters, digits and `_` (and starts with a letter or `_`);
- starts with `RUNPOD_`, `PODVIGIL_`, `WONDOMON_` or `AWS_`;
- contains `SECRET`, `TOKEN`, `PASSWORD`, `API_KEY` or `CREDENTIAL`;
- is `PYTHONPATH`, `PYTHONSTARTUP` or `LD_PRELOAD`.

The runner sets `HOME`, `TMPDIR` and the cache folders itself, so values you give for those are
replaced. Never pass secrets this way: the job file is stored in the ledger and sent to the Pod.

## Launch candidates

Instead of one `gpu_id` and `data_center_id`, a job file may list up to eight `candidates` in order
of preference:

```json
"candidates": [
  {
    "gpu_id": "NVIDIA GeForce RTX 4090",
    "data_center_id": "EU-RO-1",
    "max_compute_usd_per_hour": 1.0,
    "max_compute_usd": 0.20
  },
  {
    "gpu_id": "NVIDIA GeForce RTX 3090",
    "data_center_id": "EU-CZ-1",
    "max_compute_usd_per_hour": 0.60,
    "max_compute_usd": 0.12
  }
]
```

- Each candidate sets `gpu_id` and `data_center_id`, and may set its own `max_compute_usd_per_hour`
  and `max_compute_usd`. Top-level values of those two apply to candidates that do not set them.
  Every other field is shared.
- With candidates, `gpu_id` and `data_center_id` must not appear at the top level.
- The same GPU and data center may not appear twice.
- `launch` tries the candidates in order, each as its own job with its own ledger and watchdog. It
  moves to the next one only on a definite "no capacity" answer: the catalog shows no capacity, or
  Runpod refuses the Pod request with HTTP 400 "no longer any instances available" (or a similar
  capacity message) or with HTTP 403. It also waits, up to 60 seconds, until the failed attempt's
  ledger shows no Pod and a verified volume deletion.
- Anything else stops the launch: a timeout, a server error, HTTP 429, any other 400, a price above
  the candidate's ceiling, a data center without STANDARD volumes. An ambiguous creation is never
  repeated.

A 403 is treated as "no access to this GPU pool", following Runpod's own guidance. A key without
permission to create Pods also gets 403, so with such a key the runner tries every candidate, each
time creating and deleting a volume, before it gives up.

## Data centers

`data_center_id` must be one of the fifteen data centers on the runner's S3 list: EU-CZ-1, EU-RO-1,
EUR-IS-1, EUR-NO-1, US-CA-2, US-GA-2, US-IL-1, US-KS-2, US-MD-1, US-MO-1, US-MO-2, US-NC-1, US-NC-2,
US-NE-1 and US-WA-1. That is necessary, not sufficient: the data center must also offer STANDARD
network volumes, and `plan` cannot check that offline. On 12 November 2026 only seven did: EU-CZ-1,
EU-RO-1, EUR-IS-1, EUR-NO-1, US-IL-1, US-NC-1 and US-NE-1. `survey` shows the current answer, and
`launch` stops with "Selected data center lacks STANDARD network volumes; no premium fallback." for
the others.

## Sizing for copies

Your workload's files live on the Pod's own disk, and the outputs reach the volume only when the
workload ends ([why](WORKLOAD-CONTRACT.md#the-cost-of-the-local-disk-design)). Size the job for the
copies:

- `disk_gb` must hold the inputs, the outputs, and, when the outputs become an archive (a bundle), a
  second copy of the outputs.
- `volume_gb` must hold the inputs and the outputs (twice the outputs for a bundle: the archive and
  the original files).
- `startup_timeout_seconds` must cover starting the Pod, downloading the image, and copying all
  inputs from the volume to the disk with hash checks.
- `finalize_grace_seconds` must cover stopping the workload, hashing every output, and copying the
  outputs to the volume. If time runs out, there is no manifest and the volume is kept with partial
  files; see `export-partial` and `recover --accept-partial` in
  [the command line reference](MANUAL.md#recover). After a `cancel`, the watchdog gives the Pod
  `finalize_grace_seconds` plus 30 seconds for the same work, never past the deadline.

There are no measured copy speeds yet. Start with small outputs, look at the times in the ledger,
then grow.

## Watching and the self-termination probe

- `poll_seconds` sets how often the controller and the watchdog read the Pod's state. Each read is
  an API request, so very short intervals use up Runpod's rate limit faster.
- `agent_stale_seconds`: the Pod's supervisor writes a heartbeat to the volume every 5 seconds and
  sends one to the guard every second. If either stops for this long, the job is stopped.
- `self_termination_probe: true` tests that the Pod deletes itself. When the workload is done, the
  watchdog waits up to `self_termination_grace_seconds` for the Pod to disappear without the
  watchdog's help, and records the result in `self-termination-probe.json`. If the watchdog has to
  delete the Pod itself, the probe fails and `launch` exits with code 3, even when the workload
  succeeded. The `hello-gpu` example uses it to show that the Pod guard works with your account.
- `launch --detach` refuses a job file with `self_termination_probe: true`, before anything is
  created: "launch --detach cannot run the self-termination probe (self_termination_probe): its
  result is known only at the end of the job. Nothing was created." Set it to `false` for a detached
  launch.

## Checking a job file

```text
podvigil plan <job file>
```

`plan` needs no credentials, writes nothing, and creates nothing. It stops at the first problem with
`RUN FAILED:` and a message; [troubleshooting](OPERATIONS.md#job-file-errors) lists them all. It
does not check what only Runpod knows: prices, capacity, whether a data center offers STANDARD
volumes, and whether the GPU type exists. `survey` and `launch` check those.
