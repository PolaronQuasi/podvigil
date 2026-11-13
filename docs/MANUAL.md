# Command line reference

```text
podvigil [--state-dir FOLDER] [--env-file FILE] <command> [arguments]
podvigil --version
podvigil --help
```

Three ways to run it, all the same command line:

- `podvigil <command>`, once the package is installed;
- `python -m podvigil <command>`;
- `python managed_run.py <command>` in a checkout, without installing.

The advice in the runner's messages names the form you used: `podvigil`, `python -m podvigil` or
`managed_run.py`, for example `Run: podvigil recover <ledger>`. The watchdog's log names the form
that started the job. A line can be pasted as it is, with two exceptions: `managed_run.py` needs
`python` in front of it, and a ledger path that contains spaces needs quotes.

`--state-dir` and `--env-file` may come before or after the command name, and apply to that one
command. `podvigil <command> --help` shows a command's arguments.

**Naming a job.** Every command that works on one job takes a `JOB` argument, which is one of:

- the job's ledger folder, as printed on the `Job ledger:` line;
- `latest`, the newest job in the state folder;
- the start of the job's ID, at least four characters, for example `3f9a`; it must match exactly one
  job in the state folder.

A folder path wins over an ID. `latest` and IDs are looked up in the state folder of this command
(see [where the runner keeps things](#where-the-runner-keeps-things)); a ledger elsewhere, such as
one from a version before 0.1.0, is named by its path. When the job was named by `latest` or an ID,
the command prints `Job <id>: <folder>` on standard error first.

## Commands at a glance

<!-- prettier-ignore -->
| Command | Creates or deletes cloud resources | Needs credentials | Safe while a job runs |
| --- | --- | --- | --- |
| [`plan`](#plan) | no | no | yes |
| [`run-local`](#run-local) | no | no | yes |
| [`survey`](#survey) | no | API key | yes |
| [`launch`](#launch) | creates, then deletes | API key and S3 keys | not applicable |
| [`list`](#list) | no | no | yes |
| [`status`](#status) | no | no | yes |
| [`logs`](#logs) | no | no | yes |
| [`cost`](#cost) | no | no | yes |
| [`cancel`](#cancel) | through the watchdog: asks a running Pod to finish, then deletes the job's Pod and, when verified, its volume | no; the watchdog it may start does | it stops the job |
| [`recover`](#recover) | deletes the job's Pod and, when verified, its volume | API key and S3 keys | **no: it stops the job at once** |
| [`export-partial`](#export-partial) | deletes the job's Pod; never the volume | API key and S3 keys | **no: it stops the job** |
| [`export-unresolved`](#export-unresolved) | no | API key and S3 keys | only after the deadline plus five minutes |
| [`reattach`](#reattach) | deletes the job's resources when the job ends | API key and S3 keys | yes |
| [`resolve-creation`](#resolve-creation) | no | API key | yes |
| [`hand-off`](#hand-off) | no | no | yes, but monitoring ends |
| [`resume-monitoring`](#resume-monitoring) | no; the watchdog it wakes may | no; the watchdog it starts does | yes |
| `watchdog` | | | internal (below) |

Every command that takes a `JOB` finds its ledger first and creates nothing when it cannot: a wrong
path fails with "No job ledger at ... (job.json is missing). Nothing was created or changed.", and
an unknown or ambiguous ID with a message that says so (see
[troubleshooting](OPERATIONS.md#naming-a-job)).

## plan

```text
podvigil plan <job file>
```

Checks the job file and prints, as JSON, what `launch` would do. Reads no credentials, writes no
file, creates nothing, contacts nobody.

The output holds: `cloud`, `gpu`, `gpu_count`, `image`, `data_center`,
`maximum_pod_lifetime_seconds`, `maximum_compute_rate_usd_per_hour`, `compute_budget_usd`,
`new_network_volume_gb`, `pod_request` (the measured size of the Pod request against its limit; the
number of inputs does not change it, see [how many inputs fit](JOB-FILE.md#how-many-inputs-fit)),
`inputs` (the expanded input list with sizes and SHA-256) and `"paid_resources_created": false`.
With candidates it also lists them in launch order, with each one's limits. See
[the job file reference](JOB-FILE.md).

## run-local

```text
podvigil run-local <job file> [--workdir FOLDER] [--timeout SECONDS] [--python PATH]
```

Runs the job's command on this computer, laid out as the Pod lays it out, to check the workload
before you pay. It makes no cloud call, reads no credentials, and gives no isolation: the command
runs as you, with this computer's programs, and perhaps without a GPU. With launch candidates, the
first candidate's limits apply.

- **Layout**, below the work folder: the inputs in `inputs/<remote name>`, each checked by size and
  SHA-256 and made read-only while the command runs (as far as this computer's file modes allow);
  the output folder `jobs/local/artifacts`, which is the working directory and gets `train.log`; and
  a private `home/` with `cache/` and `tmp/`.
- **Environment**: the variables the Pod gives a workload, built by the Pod's own code (see
  [the workload contract](WORKLOAD-CONTRACT.md#environment-variables)), with local folders in place
  of the Pod's. On Windows a few system variables that programs need to start are added.
- **Time**: the workload's share of the job, the lifetime minus `finalize_grace_seconds`, unless
  `--timeout SECONDS` says otherwise. At the limit the command and everything it started are
  stopped. On Linux it gets up to ten seconds to finish after SIGTERM, then remaining descendants
  are killed. On Windows and Linux, remaining descendants are also stopped when the main command
  exits normally or fails, before results are recorded. This process cleanup does not sandbox the
  workload.
- **Result**: it writes `result.json` as the Pod would, checks `required_artifacts`, and prints a
  JSON result with the workload's outcome and exit code (`workload_outcome`, `workload_exit_code`),
  the folders, the outputs, any missing artifacts and warnings. The command's own outcome is
  `success` (exit code 0) when the workload succeeded and left every required artifact,
  `workload_failed` (2) otherwise, and `interrupted` (130) after Ctrl+C.

<!-- prettier-ignore -->
| Option | Meaning |
| --- | --- |
| `--workdir FOLDER` | a new or empty folder for the run; the default is a new temporary folder, which is kept afterwards |
| `--timeout SECONDS` | stop the command after this many seconds |
| `--python PATH` | run a `python3` or `python` command with this Python |

The command must exist here. A bare name such as `python3` is looked up on the workload's `PATH`; on
Windows the Microsoft Store's app alias for `python3` is refused, so name a real Python with
`--python`. Start scripts from the inputs as `python3 -u -m <module>`: the path
`/workspace/inputs/...` exists only in the Pod. When the command cannot be found, or the work folder
is not empty, nothing runs and no folder is left behind.

## survey

```text
podvigil survey [--gpu-count N] [--min-cuda-version X.Y] [--json]
```

Read-only. Lists the Secure Cloud GPU types that report capacity now in the data centers this runner
can use, cheapest first, with the price per hour for `N` GPUs, the GPU memory, and the lowest vCPU
count and RAM the catalog reports. Then it lists the usable data centers, the data centers without
STANDARD volumes, and those missing from the catalog. Creates nothing.

<!-- prettier-ignore -->
| Option | Default | Meaning |
| --- | --- | --- |
| `--gpu-count N` | 1 | GPUs per Pod, 1 to 8; prices are for all of them |
| `--min-cuda-version X.Y` | the runner's CUDA floor: `PODVIGIL_MIN_CUDA_VERSION`, else 12.8 | the lowest host CUDA version; it cannot be below the floor |
| `--json` | off | print JSON instead of the table |

Capacity is a hint, not a reservation: a launch can still find none. The survey sends about sixteen
read requests to Runpod's API (one per data center on the runner's list, and one for the GPU
catalog).

## launch

```text
podvigil launch <job file> [--detach]
```

Runs the job: checks, ledger, watchdog, volume, upload, Pod, supervision, recovery. This is the only
command that creates billed resources. [Concepts](CONCEPTS.md#how-a-job-runs) describes the steps.

It prints its progress on standard error:

- `Trying candidate <n> of <m> (<gpu> in <data center>).` when the job file lists more than one
  candidate;
- `Job ledger: <folder>` as soon as the ledger exists;
- `No capacity for candidate <n> of <m> (...): <answer> ... Trying the next candidate.` when a
  candidate has no capacity;
- when it ends, the two [closing lines](OPERATIONS.md#the-closing-lines).

When it ends, standard output gets the job's summary as one JSON document, with the command's
`outcome` and `exit_code` added (see [outcomes and exit codes](#outcomes-and-exit-codes)).

It prints nothing while the job runs; use `status` in another terminal.

**`--detach`** returns once the workload runs, instead of at the end of the job. `launch` first
releases the workload as usual, then waits until the watchdog can be left alone with the job: its
process lives, its last check ended armed within the last 30 seconds (or three polls, if longer),
and no stop was requested. Until then it supervises as without `--detach`, and says once on standard
error why: `Not detaching yet: <reason>. Supervising until the watchdog can take over.` Then it
writes `detached.json`, prints a JSON result with `"outcome": "detached"`, the ledger, the Pod and
volume IDs, the watchdog's process ID and the deadline, and exits with code 9.

- From then on the watchdog alone, together with the Pod's own guard, enforces the same deadline;
  the watchdog deletes the Pod, recovers and checks the results, and deletes the volume. Nothing
  extends the deadline.
- Your computer must stay on and awake until the job ends: the watchdog runs there.
- Follow the job with `status`, `logs` and `cost`, and stop it early with `cancel`. `summary.json`
  appears when the watchdog has finished.
- A job file with `self_termination_probe: true` is refused before anything is created: "launch
  --detach cannot run the self-termination probe (self_termination_probe): its result is known only
  at the end of the job. Nothing was created." The `hello-gpu` example has the probe on.

**Stopping a launch.** It depends on whether the workload was released yet:

- **Before the workload runs** (the volume, the upload, the Pod starting), Ctrl+C or
  `podvigil cancel` from another terminal stops the job at once: the Pod, if one exists, is deleted,
  and the volume is deleted after its audit.
- **Once the workload runs**, `cancel` from another terminal asks the Pod to finish first, as
  described under [cancel](#cancel). Ctrl+C in a foreground `launch` does the same when the job's
  watchdog is running: `launch` records the cancel (`cancel-request.json`, with source
  `interrupted launch`), deletes nothing itself, and exits; the watchdog then asks the Pod to
  finish, deletes it if needed, and recovers the results. Without a running watchdog, Ctrl+C deletes
  the Pod at once, and outputs not yet copied to the volume are lost.

After Ctrl+C the command prints "Interrupted; cleanup requested. Check the job ledger before
assuming billing has ended." and exits with code 130. A foreground `launch` stopped by `cancel` from
another terminal waits for the end, as usual. It ends with `cancelled` (10) when the Pod stopped the
workload and published its outputs, with `outputs_lost` (12) when the workload never published
anything, and with `storage_retained` (8) when the volume had to be kept, for example because only
part of the outputs reached it.

If instead the `launch` process is killed or crashes, what happens depends on the moment:

- **After the workload was released**, the job goes on. The watchdog keeps the deadline, deletes the
  Pod when the job is over, and recovers the results. `reattach` restores a controller.
- **Before that**, the workload is never started, because only the controller releases it. The job
  ends at its startup limit and is cleaned up, unless you run `reattach` in time.

## list

```text
podvigil list [--json]
```

Lists the jobs in the state folder, newest first: the start of each ID, when it was created (UTC),
its phase, the workload's outcome, the state of its Pod and its volume, and its GPU. The text names
the jobs folder by its real location on disk, the same path the `Job ledger:` line prints. A ledger
that cannot be read shows the phase `unreadable`. With `--json` it prints
`{"jobs_dir": ..., "jobs": [...]}`, with full IDs, ledger paths and `training_outcome`. It reads
only the ledgers: no credentials, no lock, nothing created. Ledgers outside the state folder are not
listed.

## status

```text
podvigil status JOB
```

Reads the ledger and prints a summary as JSON. It reads no credentials, contacts nobody, takes no
lock, keeps no file open, and changes nothing, so it is safe while the job runs.

<!-- prettier-ignore -->
| Field | Meaning |
| --- | --- |
| `phase` | `preparing`, `starting`, `running`, `stopping`, `pod creation unresolved`, `recovering results`, `finished` or `failed` (see [troubleshooting](OPERATIONS.md#status-phases)) |
| `detached` | `true` when `launch --detach` handed the job to the watchdog |
| `pod`, `volume` | `{"state": ..., "id": ...}`; the state is `not_created`, `rejected`, `unknown`, `exists` or `deleted` |
| `deadline_utc`, `seconds_to_deadline` | the job's deadline and the seconds left |
| `startup_deadline_utc`, `preparation_deadline_utc` | the other two time limits |
| `price_quote` | the catalog price checked at launch |
| `compute_ceiling_usd_per_hour`, `compute_budget_usd` | from the job file |
| `estimated_compute_cost` | the quoted price times the time from the Pod's creation until its verified deletion, or until now; no storage, no taxes |
| `training_outcome` | the workload's outcome, from `summary.json` once the job is finished |
| `failure` | from `launch-failure.json`: phase, error type, redacted message, HTTP status |
| `watchdog` | the watchdog's last state, process ID, message and heartbeat age |
| `watchdog_credentials` | whether the watchdog found the same credentials as the controller |

While a job runs and its watchdog was heard from in the last five minutes, the closing lines say
that the Pod "runs and bills", give the deadline, and point to `cancel` and `status`. After a
`cancel`, while the Pod finishes, they say that it "was asked to finish and publish its outputs" and
give the time by which the watchdog deletes it. Otherwise, without a recent sign of the watchdog or
once a stop was requested, they say that the Pod "may still exist and bill" and suggest `recover`;
on a job that is still running, `recover` stops it at once.

## logs

```text
podvigil logs JOB [--tail LINES] [--json]
```

Shows what the ledger holds now that tells what happened:

- the watchdog's log (`watchdog.log`);
- the launch failure record (`launch-failure.json`), the watchdog's recovery record
  (`recovery-handoff.json`), and why `recover` kept the volume of a job that published nothing
  (`volume-kept.json`), when they exist;
- the Pod's own records `guard-failure.json` and `finalization-incomplete.json`, once recovery or a
  partial export has copied them from the volume;
- the workload's log (`train.log`), once the results are downloaded, or from a partial export;
- the latest events from `events.jsonl` (see [the event log](OPERATIONS.md#the-event-log)).

The workload's log stays on the Pod while the job runs; `logs` then says it is not available yet.
`--tail LINES` shows the last lines of each log (default 100; `0` shows all of the last MiB). Each
file is read at once and closed, so `logs` is safe while the job runs. It reads no credentials and
creates nothing.

## cost

```text
podvigil cost JOB [--json]
```

Shows the estimated compute cost: the price quoted by Runpod's catalog at launch, times the time
from the Pod's creation receipt until its verified deletion, or until now while the Pod exists. It
also shows the run time, the price ceiling and the budget. A job whose Pod was never created
shows 0. It is an estimate, not a bill: Runpod bills by its own clock and rates, and the volume's
storage and taxes are not included. It reads only the ledger.

## cancel

```text
podvigil cancel JOB [--wait SECONDS]
```

Stops a job early. It writes `cancel-request.json` and the usual stop request (reason "cancel
requested by the operator"), which also closes the job's creation fence. It calls no API and deletes
nothing itself: the job's watchdog acts on the request. If no watchdog is running, `cancel` starts
one, as `resume-monitoring` does.

What the watchdog does depends on whether the workload was released:

- **Before the release** (a `launch` still preparing the job, or a Pod still starting), it deletes
  the Pod at once, confirms its absence and handles the volume, as at the deadline. A `launch` that
  is still preparing the job stops at its next check.
- **Once the workload runs**, it first asks the Pod to finish. It writes `jobs/<job id>/cancel.json`
  to the job's volume, naming this job and this Pod. The Pod checks for that file every 2 seconds.
  When it finds it, it stops the workload as at its own time limit (SIGTERM, then SIGKILL 10 seconds
  later), copies the outputs to the volume with the workload's outcome `cancelled`, and deletes
  itself. A workload that had already ended keeps its own outcome; the Pod finishes as usual. The
  watchdog waits for that at most `finalize_grace_seconds` plus 30 seconds, and never past the job's
  deadline; it keeps the end of the wait in `cancel-grace.json`, so a restarted watchdog does not
  wait longer. Then it confirms the Pod's absence, or deletes the Pod if it still exists, recovers
  and checks the results, and deletes the volume, as at the end of any job.
- **The Pod keeps billing while it finishes.** The deadline still applies during the wait, and so do
  the watchdog's checks for a frozen or failed Pod. When the request cannot be written to the
  volume, or the Pod has not ended when the wait is over, the watchdog deletes the Pod at once.
  Outputs the Pod had not copied to the volume by then are lost: see [recover](#recover) for what
  happens to the volume.

Its options and results:

- `--wait SECONDS`: how long `cancel` waits for the end, from 0 to 3600 seconds. The default is the
  job's `finalize_grace_seconds` plus 150 seconds, at most 3600. `0` returns at once.
- It prints a JSON result whose `state` is `compute_stopped` (the Pod is gone and so is the volume),
  `storage_retained` (the Pod is gone, the volume is kept), `pending` (the Pod's deletion is not
  verified yet; follow with `status`), `recovering` (the Pod is gone and the watchdog still recovers
  the results when the wait ends; whether the volume is deleted or kept is not decided yet; follow
  with `status`) or `already_finished` (the job had ended; nothing was written), with the workload's
  `training_outcome` once the job is finished: `cancelled` when the Pod stopped the workload and
  published its outputs.
- Exit codes: 0 (`compute_stopped` or `already_finished`), 8 (`storage_retained`), 11 (`pending` or
  `recovering`) and 12 (`outputs_lost`: the Pod and the volume are gone, but the workload never
  published its outputs).
- While the Pod finishes, the closing lines say so:
  `Pod <id>: was asked to finish and publish its outputs, and bills until it ends; its deletion is NOT verified yet. The watchdog deletes it if it has not ended by <time>. Follow: podvigil status <ledger>`.
  Do not run `recover` then: it would delete the Pod at once, before it publishes.
- The watchdog picks the request up at its next check, normally within `poll_seconds`. Until then,
  for example right after `cancel --wait 0` or Ctrl+C, the closing lines say
  `Pod <id>: a cancel was requested; the watchdog is about to ask it to finish and publish its outputs. It bills until it ends; its deletion is NOT verified yet. Follow: podvigil status <ledger>`.
  Do not run `recover` then either. If no watchdog has been heard from for five minutes, nobody will
  ask the Pod, and the lines go back to suggesting `recover`.
- It refuses a job that was handed off: "An operator took over this job's monitoring (hand-off), so
  no watchdog acts on a stop request. Run resume-monitoring first, then cancel again. Nothing was
  changed."

## recover

```text
podvigil recover JOB [--accept-partial]
```

Brings a job to its end: deletes the job's Pod if it still exists, then downloads the results,
checks every file against the manifest, audits the whole volume, deletes the volume, confirms each
deletion, and writes `summary.json`. Prints the summary as JSON and the closing lines.

- **On a running job it stops the job.** The Pod is deleted at once; the workload does not get to
  finish or copy its outputs. The same holds while a cancelled job's Pod is finishing. To stop a
  running job and keep its outputs, use [cancel](#cancel).
- Safe to repeat. On a finished job it checks the local results again and returns the earlier
  summary.
- It keeps the volume, and says why, whenever the volume holds something it cannot account for.
- Runs one at a time per job; a second `recover` waits up to 30 minutes for the first (with
  `--accept-partial`, 5 seconds).
- It refuses to continue with a different account key than the job was launched with: "Job
  credential changed; verify account identity before rebinding cleanup. Absence is not verified."

**A job that never published its outputs.** The workload's outputs stay on the Pod's own disk until
the Pod copies them to the volume, with the manifest last. If the workload was released but the Pod
is gone without publishing anything (it died, or it was deleted before it began to copy its
outputs), the outputs are lost, and waiting for them would keep the volume billed for ever.
`recover` then deletes the volume only when all of this holds:

- the Pod's deletion is verified;
- the Pod's own status record for this job and this Pod is on the volume;
- the audit of the whole volume finds nothing but your unchanged inputs (by the hashes recorded at
  launch), the input manifest (byte for byte), and the records that the release, a cancel request
  and this job's Pod write, each checked to be exactly as written, for this job and this Pod.

It copies those records to `<ledger>/downloads/volume-audit-<random>/` first. The summary says
`"training_outcome": "outputs_lost"`, and the command ends with the outcome `outputs_lost` (exit
code 12) and this line on standard error: "OUTPUTS LOST: the workload ran, but it never published
its outputs and they are lost with the Pod; the Pod and the volume are deleted, the Pod's records
kept." Any other object on the volume (an output, part of one, a changed input or record, an unknown
file) keeps the volume. The reason is saved in `volume-kept.json`, and `logs` shows it.

**Incomplete outputs.** When the volume holds outputs but no complete manifest (for example after a
cancel whose copy did not finish in time, or a Pod that died while copying), `recover` keeps the
volume and says: "No valid complete artifact manifest. Keep the volume for recovery. The workload's
outputs are incomplete. export-partial copies what is there and deletes nothing; recover
--accept-partial copies every object with its bytes checked, then deletes the volume."

**`--accept-partial`** is that second way, and only you can choose it. It first recovers as usual:
complete outputs are recovered and checked against their manifest exactly as without the option.
Only when recovery stops at the missing or incomplete manifest does it go on, and only when the
workload was released, the Pod's deletion is verified and the volume is the job's own. Otherwise it
deletes nothing and says why:

- "The workload was never released, so the volume holds no outputs to accept. Nothing was deleted."
- "The Pod's deletion is not verified. Nothing was deleted."
- "Result volume ownership/availability is not verified. Nothing was deleted."

Then it audits the whole volume in a mode that copies: every object is read once, copied to
`<ledger>/partial-downloads/volume-audit-<random>/` under an opaque name (`00000000.bin`, ...), and
the copy checked against the bytes read. `objects.json` there maps each key to its file, with its
size, SHA-256 and kind (`partial_output` for an output, `control` for one of the runner's own
records, `input`, `input_manifest`, `directory_marker`). Inputs that are still unchanged on your
computer are checked against their hashes and not copied again; `objects.json` names your own file
for them. The inputs and the input manifest must still match what `launch` uploaded, the volume's
owner record must name the job, and the listing must not change during the copy. An object that the
outputs' link index names is never read: it keeps the volume ("S3 exposes an object the link index
names; retain volume without reading its target."). Any failure keeps the volume. Then the volume is
deleted and its absence confirmed, as always.

The summary then says `"training_outcome": "partial_outputs"`, `workload_outcome` (the workload's
own outcome from the early copy of `result.json` the Pod writes when it starts finishing, if one was
copied; otherwise `null`), `copy_directory` (the folder with the copies) and `object_count` (the
objects on the volume). The outcome is `partial_outputs`, exit code 13. When the volume held only
what a job that published nothing leaves (above), `--accept-partial` ends as plain `recover` does,
with `outputs_lost`.

## export-partial

```text
podvigil export-partial JOB
```

For a job whose results could not be recovered in full. Deletes the job's Pod if it still exists,
then copies every readable object on the job's volume to
`<ledger>/partial-downloads/partial-export-<random>/`, never deleting the volume.

- `objects/` holds the copies under opaque names (`00000000.bin`, ...), so that names Windows cannot
  store still arrive.
- `objects.jsonl` maps each original key to its file. Each key gets a `pending` line before its copy
  and a `saved` (with size and SHA-256), `failed` or `skipped_symlink` line after it; a
  `listing_failed` line means the listing of the volume broke off.
- `export-summary.json` and the ledger's `partial-export.json` sum it up. `complete` is always
  `false`; `storage_retained` is `true`.

Links are not followed; their text is kept in the bundle metadata. A partial export never allows the
volume to be deleted. When the outputs are incomplete, [`recover --accept-partial`](#recover) copies
every object with its bytes checked and then deletes the volume. Otherwise, when you have what you
need, delete the volume yourself in the Runpod console. Its ID is in `volume-receipt.json` and in
the closing line.

## export-unresolved

```text
podvigil export-unresolved JOB
```

For a job whose Pod creation is still unresolved (the watchdog reports `creation_unresolved` or
`operator_resolution_needed`). After the original deadline plus five minutes, it copies the readable
objects of the job's volume to `<ledger>/unresolved-downloads/` without claiming that the Pod is
gone. It never deletes anything, and the watchdog stays active. The result
(`unresolved-export.json`) says `complete: false` and `billing_unverified: true`, because a Pod
nobody can see might still be writing. Before that time it refuses with "Unresolved export requires
the original deadline plus five minutes to elapse."

## reattach

```text
podvigil reattach JOB
```

Restores supervision of a running job, for example after your computer restarted. It keeps the
original deadline. It starts a new watchdog if none is running, supervises the job until the Pod is
gone, then recovers the results as `recover` does.

- It needs the job's Pod and volume receipts and its deadline: "Reattach requires reconciled
  Pod/volume receipts and the original deadline." For a lost creation answer, use `resolve-creation`
  first.
- If the new watchdog finds different credentials, `reattach` still starts it, because the existing
  resources need a guard, and prints a `WARNING:` line on standard error. Results recovery by that
  watchdog may then fail.

## resolve-creation

```text
podvigil resolve-creation JOB --pod-id ID
podvigil resolve-creation JOB --volume-id ID
podvigil resolve-creation JOB --audit
```

For a create request whose answer was lost. Give exactly one option.

- `--pod-id` or `--volume-id` binds a resource you can see in the Runpod console to this job, after
  checking that it matches the job's unique name (and, for a Pod, its job ID; for a volume, its data
  center) and that it did not exist before the job. It refuses anything else: "The supplied resource
  ID cannot be verified as belonging to this job."
- `--audit` searches Runpod's audit log for this job's create records and binds what it finds. An
  empty or partial answer proves nothing and is reported as such.

It never creates or deletes anything. Continue with `reattach` (the job may still run) or `recover`.

## hand-off

```text
podvigil hand-off JOB --note "why you take over" --acknowledge-unverified-billing
```

You take over responsibility for the job's resources. This closes the job's creation fence (no new
resource for this job, ever) and lets the watchdog stop monitoring. It records your note in
`operator-handoff.json`. It does not claim any cleanup: compute and storage may still bill, and
deleting them is now your task. Both options are required. The note must be short (at most 1,000
characters) and must not contain anything that looks like a credential. No automatic path ever runs
this command.

## resume-monitoring

```text
podvigil resume-monitoring JOB
```

Undoes a `hand-off`. It marks the hand-off inactive and asks the watchdog to confirm, starting a new
watchdog if none is running, within 40 seconds. It prints the state (`monitor_armed`,
`existing_monitor_active` or `monitor_acknowledged_and_exited`), the watchdog's process ID, and
`"original_deadline_unchanged": true`. The creation fence stays closed.

## watchdog

Internal. `launch`, `reattach`, `resume-monitoring` and `cancel` start it as a separate process with
the ledger and the same `--state-dir` and `--env-file`; its log names the command form of the
process that started it. Do not run it yourself.

## Outcomes and exit codes

Every command ends with one named outcome, and the process exits with its code. The result JSON on
standard output carries both, as `outcome` and `exit_code`. A name means the same for every command.

<!-- prettier-ignore -->
| Code | Outcome | Meaning |
| --- | --- | --- |
| 0 | `success` | the command did what was asked; for `launch`: the workload succeeded, its results are verified and nothing of the job remains |
| 1 | `error` | the command failed and no other outcome applies; the closing lines say what exists |
| 2 | `workload_failed` | the job ended and its cleanup is verified, but the workload's outcome is not `success` (for example `training_failed`, `time_limit` or `not_started`); for `run-local`: the command failed or a required artifact is missing |
| 3 | `probe_failed` | `launch`: the workload succeeded, but the self-termination probe did not pass |
| 4 | `no_capacity` | no candidate had Secure Cloud capacity; nothing of the job remains |
| 5 | `rejected` | Runpod refused a create request for another reason; nothing of the job remains |
| 6 | `ambiguous` | a create request's outcome is unknown: a Pod or volume may exist and bill; the watchdog keeps reconciling |
| 7 | `cleanup_unverified` | a Pod may still exist and bill: its deletion is not verified |
| 8 | `storage_retained` | compute has ended, but the volume is kept and billed as storage: its results are not all recovered (`recover`, `export-partial`) |
| 9 | `detached` | `launch --detach` returned while the workload runs; the watchdog guards it |
| 10 | `cancelled` | `launch`: the job was stopped by `cancel` before its workload finished; what the workload published is recovered, and nothing remains |
| 11 | `pending` | `cancel`: the stop request is written; the Pod's deletion is not verified yet, or the watchdog still recovers the results |
| 12 | `outputs_lost` | the workload ran, but it never published its outputs and they are lost with the Pod; the Pod and the volume are deleted, the Pod's records kept (`launch`, `recover`, `reattach`, `cancel`) |
| 13 | `partial_outputs` | `recover --accept-partial`: the workload's outputs were incomplete; every object on the volume was copied to the ledger with its bytes checked, then the volume was deleted; the Pod's deletion is verified |
| 64 | `usage_error` | the command line is wrong; the usage is printed on standard error |
| 130 | `interrupted` | interrupted with Ctrl+C; check the job before assuming billing has ended |

When a command that creates or deletes resources fails (`launch`, `recover`, `reattach`, `cancel`,
`export-partial`, `export-unresolved`, `resume-monitoring`), what its ledger shows decides the
outcome first: a create request with no known outcome gives `ambiguous`, an unverified Pod deletion
`cleanup_unverified`, a kept volume `storage_retained`. Then come `cancelled`, `no_capacity`,
`rejected` and `error`.

The codes from 0 to 4 and 130 mean what they meant before this table existed. A usage error was 2
before; it is 64 now.

The `outcome` and `exit_code` fields are always the command's own. The workload's outcome has its
own names: `training_outcome` in `summary.json`, `status` and `list`, and `workload_outcome` in the
result of `run-local`.

## Output

**Standard output** carries the result alone: one JSON document with `outcome` and `exit_code`
added. When a command fails, the document holds the outcome, the exit code, the error and, when a
job is involved, its ledger and the state of its Pod and volume. `list`, `logs`, `cost` and `survey`
print text instead, unless given `--json`. The watchdog prints no result; its output is its log.

**Standard error** carries everything meant for people: progress (`Trying candidate ...`,
`Job ledger: ...`, `No capacity for ...`, `Not detaching yet: ...`), the `Job <id>: <folder>` line
of a job named by `latest` or an ID, `WARNING:` lines, `RUN FAILED: <message>`, the parser's usage
errors, the `OUTPUTS LOST: ...` line of the outcome `outputs_lost`, and last the two closing lines.

So `podvigil launch job.json > result.json` keeps the result in a file while the progress and the
closing lines stay on the screen. `plan`, `run-local`, `survey` and `list` print no closing lines;
every other command does, once it has found its job, and `launch` always does.

Error messages never contain credentials. Errors from other libraries are reported by type name
only, to keep their details (which can contain request data) out of logs.

## Where the runner keeps things

**State.** New ledgers go to `<state folder>/jobs/<job id>`, and the API pacing files to
`<state folder>/api-rate-limit-<fingerprint>.json`, one per API key. The state folder is, first
match wins:

1. the `--state-dir` option;
2. the `PODVIGIL_STATE_DIR` environment variable;
3. the per-user folder: `%LOCALAPPDATA%\podvigil` on Windows,
   `~/Library/Application Support/podvigil` on macOS, `$XDG_STATE_HOME/podvigil` elsewhere (default
   `~/.local/state/podvigil`).

The `Job ledger:` line shows where the ledger really is on disk. From a shell inside a packaged
Windows app that is the app's private copy of `%LOCALAPPDATA%`; see
[the quickstart](QUICKSTART.md#running-from-a-packaged-windows-app).

Each API key has its own pacing file. Its name holds a short one-way fingerprint of the key (the
first 16 hexadecimal digits of a SHA-256 over a fixed label and the key); the key itself is never
written. A new key starts with fresh pacing, and the processes that use the same key and state
folder share its file. The single `api-rate-limit.json` of earlier builds is no longer read; a
watchdog started by such a build keeps using it until it ends. When the pacing file cannot be used,
ordinary requests are held back with "API pacing deferred" and the cause (see
[troubleshooting](OPERATIONS.md#credentials-and-setup)).

**Credentials.** One file, first match wins:

1. the `--env-file` option;
2. the file the `PODVIGIL_ENV_FILE` environment variable names;
3. the per-user `credentials.env`: `%LOCALAPPDATA%\podvigil\credentials.env` on Windows,
   `~/Library/Application Support/podvigil/credentials.env` on macOS,
   `$XDG_CONFIG_HOME/podvigil/credentials.env` elsewhere (default
   `~/.config/podvigil/credentials.env`);
4. the `.env` file at the root of a checkout, when the runner runs from that checkout.

A file named by the option or the variable must exist; the runner never skips it for another. The
file holds `NAME=value` lines; quotes around a value are removed.

<!-- prettier-ignore -->
| Name | Used by | Notes |
| --- | --- | --- |
| `RUNPOD_API_KEY` | everything that talks to Runpod's API | read from the file only, never from the environment; exactly one, without spaces |
| `RUNPOD_S3_ACCESS_KEY`, `RUNPOD_S3_SECRET_KEY` | `launch` and every command that reads or deletes the volume | the environment wins over the file; on Windows keep them in the file (see [the quickstart](QUICKSTART.md#2-store-your-credentials)) |

The watchdog receives the chosen state folder and credentials file on its command line, and at start
it proves, with salted hashes and never the values, that it found the same credentials.

**Environment variables the runner reads:**

<!-- prettier-ignore -->
| Variable | Meaning |
| --- | --- |
| `PODVIGIL_STATE_DIR` | the state folder (above) |
| `PODVIGIL_ENV_FILE` | the credentials file (above) |
| `PODVIGIL_MIN_CUDA_VERSION` | the CUDA floor for new jobs and `survey` (default 12.8); jobs already launched keep theirs |
| `LOCALAPPDATA`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME` | where the per-user folders are |
| `RUNPOD_S3_ACCESS_KEY`, `RUNPOD_S3_SECRET_KEY` | the S3 keys, before the credentials file |

The prefix `PODVIGIL_` follows the tool's name. The runner also sets variables for its own processes
(`PODVIGIL_ARM_TOKEN` and `PODVIGIL_ARM_FOLDER` for the watchdog's start, and `PODVIGIL_JOB_ID` and
`PODVIGIL_JOB_SPEC` on the Pod); do not set them yourself.

## The bundle tool

When the outputs could not be copied as plain files (see
[the workload contract](WORKLOAD-CONTRACT.md#outputs)), the download holds
`downloads/output-bundle/<id>/archive.tar` and `metadata.json`. To unpack them:

```text
python -m podvigil.podrt.bundle <archive.tar> <metadata.json> <new folder> [--restore-links]
```

It checks every member of the archive against its size and SHA-256, never follows a stored link, and
writes into a folder that must not exist yet:

- `objects/` holds every file under an opaque name, and `mapping.json` maps the original paths,
  modes and link texts to them. This works on every system.
- With `--restore-links`, on Linux and macOS only, `tree/` also rebuilds the original names,
  folders, modes and hard links, and the links whose target stays inside the tree.

It prints a JSON object with the folder and the mapping file.
