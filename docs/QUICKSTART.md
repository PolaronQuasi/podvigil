# Quickstart

This page takes you from a fresh computer to your first verified GPU job. Setting up takes about
five minutes. The job itself then takes a few more, most of it waiting for the Pod to start.

The job is `examples/hello-gpu`: it checks that CUDA works, multiplies two matrices on the GPU and
saves a small `metrics.json`. Its job file plans at most 0.20 USD of compute, plus a few minutes of
a 10 GB network volume.

## What you need

- A computer with Windows or Linux, Python 3.10 or newer, and git. Real jobs have so far been
  launched from Windows only; on Linux the test suite passes. macOS may work but is untested.
- A Runpod account with some credit.
- A Runpod API key. Create it in the Runpod console under your account settings.
  [SECURITY.md](../SECURITY.md#the-runpod-api-key) lists the permissions the runner uses.
- A Runpod S3 access key and its secret. These are separate from the API key; create them on the
  credentials page of the Runpod console (S3 API keys). The runner uses them to upload your inputs
  to the network volume and to download your results.

## 1. Install

Windows (PowerShell):

```powershell
git clone https://github.com/PolaronQuasi/podvigil.git
cd podvigil
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps .
$env:Path = "$PWD\.venv\Scripts;$env:Path"   # lets you type podvigil in this terminal
podvigil --version
```

Linux (bash):

```bash
git clone https://github.com/PolaronQuasi/podvigil.git
cd podvigil
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install --no-deps .
source .venv/bin/activate
podvigil --version
```

The last command prints `podvigil 0.1.0`.

- `requirements.lock.txt` pins the one dependency, `boto3` (the Amazon S3 client library), and what
  it needs.
- pip downloads setuptools to build the package. If setuptools 77 or newer is already installed in
  the environment, add `--no-build-isolation` to build without that download.
- `python -m podvigil` works too. In the checkout, `python managed_run.py` runs the same command
  line without installing anything.

## 2. Store your credentials

The runner reads its credentials from one file. The simplest place is the per-user file:

| System  | File                                                                                  |
| ------- | ------------------------------------------------------------------------------------- |
| Windows | `%LOCALAPPDATA%\podvigil\credentials.env`                                             |
| Linux   | `~/.config/podvigil/credentials.env` (or `$XDG_CONFIG_HOME/podvigil/credentials.env`) |
| macOS   | `~/Library/Application Support/podvigil/credentials.env`                              |

Create it with exactly these three lines and your values:

```dotenv
RUNPOD_API_KEY=<your Runpod API key>
RUNPOD_S3_ACCESS_KEY=<your Runpod S3 access key>
RUNPOD_S3_SECRET_KEY=<your Runpod S3 secret>
```

Windows:

```powershell
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\podvigil" | Out-Null
notepad "$env:LOCALAPPDATA\podvigil\credentials.env"
```

Linux:

```bash
mkdir -p ~/.config/podvigil
install -m 600 /dev/null ~/.config/podvigil/credentials.env
nano ~/.config/podvigil/credentials.env
```

Keep the file readable only by your account. Never put keys on a command line, in a job file or in a
chat. Other places for the file (an option, an environment variable) are described in
[the command line reference](MANUAL.md#where-the-runner-keeps-things).

On Windows, keep the S3 keys in this file even if you also have them in environment variables. The
watchdog is started through Windows' management service and does not inherit your terminal's
environment. `launch` checks that the watchdog found the same keys and refuses to start otherwise.

## 3. Check the job without spending anything

```text
podvigil plan examples/hello-gpu/job.json
```

`plan` reads no credentials and creates nothing. It validates the job file and prints what `launch`
would do, as JSON: the GPU and data center of the first candidate, the maximum Pod lifetime (600
seconds here), the price ceiling, the compute budget, the input files with their SHA-256, the size
of the Pod request, and `"paid_resources_created": false`. It ends with the order in which `launch`
would try the candidates.

A mistake in the job file stops here with `RUN FAILED:` and a message that names the problem. See
[the job file reference](JOB-FILE.md).

## 4. Find a GPU that can be placed now

```text
podvigil survey
```

`survey` uses your API key, reads Runpod's catalog and creates nothing. It prints a table of the
Secure Cloud GPU types that report capacity now, cheapest first, with the price per hour and the
data centers that have them, and then the data centers the runner can use. A data center is usable
only when it is on the runner's list of data centers with S3 access _and_ offers STANDARD network
volumes. On 12 November 2026 that was seven: EU-CZ-1, EU-RO-1, EUR-IS-1, EUR-NO-1, US-IL-1, US-NC-1
and US-NE-1.

Open `examples/hello-gpu/job.json` and make its `candidates` match what the survey shows: up to
eight pairs of `gpu_id` and `data_center_id`, in your order of preference, each with a price ceiling
(`max_compute_usd_per_hour`) at or above the listed price.

Capacity is a hint, not a reservation. "LOW" in particular often turns into "no capacity" a few
seconds later. That is why the example lists several candidates.

## 5. Launch

```text
podvigil launch examples/hello-gpu/job.json
```

This creates billed resources. What you see:

```text
Trying candidate 1 of 3 (NVIDIA GeForce RTX 4090 in EU-RO-1).
Job ledger: <state folder>/jobs/<job id>
```

Then nothing for a few minutes, while the volume is created, the input is uploaded, the Pod starts,
the job runs and the results come back. In a second terminal you can watch the job without
disturbing it:

```text
podvigil status latest
podvigil logs latest
```

`latest` names the newest job; the path from the `Job ledger:` line, or the first characters of the
job ID, work too. `status` shows the phase, the deadline and an estimated cost so far. `logs` shows
the watchdog's log; the workload's own log arrives with the results.

To stop a job early, run `podvigil cancel latest`. Once the workload runs, the watchdog asks the Pod
to stop it as at its time limit, copy its outputs to the volume, and delete itself; then the results
come back as usual, with the workload's outcome `cancelled`, and `launch` ends with the outcome
`cancelled` (exit code 10). The Pod bills until it ends. If it has not ended within
`finalize_grace_seconds` plus 30 seconds, the watchdog deletes it, and outputs not yet copied are
lost. See [cancel](MANUAL.md#cancel).

For later jobs, `podvigil launch --detach <job file>` returns as soon as the workload runs and
leaves the rest to the watchdog (exit code 9); the computer must still stay on. It does not work
with the self-termination probe, which this example uses.

After a detached launch, `podvigil wait <returned ledger> --timeout 30` waits for the final job
outcome using only its local ledger. It returns the foreground launch's exit code, or `wait_timeout`
(14) when 30 minutes pass. Ctrl+C stops this reader, not the job. A stale watchdog also ends waiting
and reports the recorded resources. See [the manual](MANUAL.md).

When `launch` is done it prints the job's summary as JSON, with `"outcome": "success"` and
`"exit_code": 0`, and then the two closing lines:

```text
Pod <pod id>: deleted; its absence was verified.
Volume <volume id>: deleted; its absence was verified. Nothing from this job remains.
```

The JSON goes to standard output and everything else to standard error, so
`podvigil launch examples/hello-gpu/job.json > result.json` keeps the summary in a file.

The exit code is 0. It is 3 (`probe_failed`) if the Pod did not delete itself within a minute and
the watchdog had to do it; the job file asks for that test (`self_termination_probe`), and the Pod
is deleted either way. [The command line reference](MANUAL.md#outcomes-and-exit-codes) lists every
outcome.

If a candidate had no capacity, you see a line such as `No capacity for candidate 1 of 3 (...)`
ending with `Trying the next candidate.`. If none had capacity, the run ends with
`RUN FAILED: None of the 3 candidates had capacity. Last answer: ...` and exit code 4
(`no_capacity`), and nothing is left in your account. Wait a little, run `survey` again, adjust the
candidates, and launch again.

To make that wait part of the command, add `--wait-for-capacity 15` to `launch`. It runs the initial
round normally, then polls the catalog between additional rounds for up to 15 minutes. It starts
another real attempt only after a definite no-capacity answer, verified absence, and fresh catalog
capacity. Ambiguous creates and other failures stop the command. The result includes
`capacity_rounds` and `attempt_ledgers`; the successful job still gets its own full runtime limit.
The option also works with `--detach`, with capacity waiting in the foreground.

Keep the computer on and online until `launch` returns. If it sleeps, read
[what happens then](SAFETY-MODEL.md#what-the-runner-cannot-make-sure-of).

## 6. Read the verified results

Everything about the job is in its ledger folder, the path from the `Job ledger:` line. By default
that is `%LOCALAPPDATA%\podvigil\jobs\<job id>` on Windows, `~/.local/state/podvigil/jobs/<job id>`
on Linux and `~/Library/Application Support/podvigil/jobs/<job id>` on macOS.

<!-- prettier-ignore -->
| File in the ledger | What it is |
| --- | --- |
| `downloads/artifacts/metrics.json` | the file the job wrote |
| `downloads/artifacts/train.log` | everything the job printed |
| `downloads/artifacts/result.json` | the outcome (`success`) and the exit code, written by the runner |
| `downloads/artifact-manifest.json` | every result file with its size and SHA-256, made in the Pod |
| `downloads/backup-verified.json` | the receipt: this job, this volume, the manifest's SHA-256, the file count |
| `pod-deleted.json`, `volume-deleted.json` | the deletion evidence |
| `summary.json` | the final record: outcome and both verified deletions |

`podvigil status latest` now shows `"phase": "finished"`, both resources as `deleted` and
`"training_outcome": "success"`. `podvigil logs latest` shows the workload's log, and
`podvigil cost latest` the estimated compute cost: the quoted price times the time from the Pod's
creation to its verified deletion. It does not include storage or taxes; Runpod's billing page shows
what was actually charged. `podvigil list` shows all your jobs.

## Where to go next

- Run a script with your own files: [examples/script-with-inputs](../examples/script-with-inputs)
  and [the job file reference](JOB-FILE.md). Try it first on your own computer with
  `podvigil run-local <job file>`; it makes no cloud call.
- Long jobs: read [the workload contract](WORKLOAD-CONTRACT.md) first. Outputs reach the volume only
  when the job ends, so a long job needs checkpoints and a plan for chaining jobs, as in
  [examples/checkpoints](../examples/checkpoints).
- Anything unexpected: [troubleshooting](OPERATIONS.md).

## Running from a packaged Windows app

If your terminal was started from inside a packaged Windows app (for example an app installed from
the Microsoft Store), Windows keeps new folders that the runner creates under `%LOCALAPPDATA%` in
that app's private copy (`...\AppData\Local\Packages\<app>\LocalCache\Local\...`). Programs outside
the app do not see them at the usual path, and the watchdog runs outside the app.

This works without extra options. The runner gives the watchdog the real location of the ledger and
of the credentials file, and the `Job ledger:` line shows that real location; `podvigil list` shows
the jobs folder by its real location too. On 12 November 2026 a launch from such a shell, without
`--state-dir`, put its ledger in the app's private copy of the per-user folder, and the watchdog
armed and guarded the job. Use the printed path in later commands from terminals outside the app;
there, `latest` and job IDs are looked up in the state folder as that terminal sees it, which may
not hold these ledgers.

Should the watchdog still not see the ledger, `launch` stops before it creates anything, with a
message that starts "The watchdog cannot see the job ledger." and names both paths. Then follow the
message and use folders outside `%LOCALAPPDATA%`:

```powershell
podvigil launch --state-dir "$HOME\podvigil-state" --env-file "$HOME\podvigil-keys\credentials.env" examples\hello-gpu\job.json
```

The environment variables `PODVIGIL_STATE_DIR` and `PODVIGIL_ENV_FILE` do the same for a whole
terminal session.
