# Troubleshooting

Every state name and failure message the runner can write is on this page, with what it means and
what to do. The texts are copied from the code; `<...>` marks the parts that vary.

## Start here

1. **Read the two closing lines.** They say what still exists.
   [The closing lines](#the-closing-lines) lists every variant.
2. **Run `podvigil status <job>` and `podvigil logs <job>`.** Both change nothing and need no
   credentials; `<job>` is the ledger folder, `latest`, or the start of the job ID. In `status` look
   at `phase`, `pod`, `volume`, `failure` and `watchdog`. `logs` shows the watchdog's log, the
   failure records, and the workload's log once it has been downloaded.
3. **Look into the ledger folder** (the path from the `Job ledger:` line):
   - `events.jsonl`: one line per event, from every process that acted on the job;
   - `launch-failure.json`: why `launch` failed, in which phase, with the HTTP status if any;
   - `watchdog.log`: one line per watchdog event; its last line says what exists;
   - `watchdog.json`: the watchdog's current state;
   - `recovery-handoff.json`: whether the watchdog's recovery finished;
   - `volume-kept.json`: why `recover` kept the volume of a job whose workload published nothing.
4. **For a failure inside the Pod**, read the guard's record, see
   [failures inside the Pod](#failures-inside-the-pod).
5. **When in doubt, look at the Runpod console.** The job's resources are named `wondomon-<job id>`
   (the Pod) and `wondomon-results-<job id>` (the volume).

`RUN FAILED: <message>` on standard error is how every command reports an error; standard output
then holds `{"outcome": ..., "exit_code": ..., "error": "<message>"}`, plus the ledger and the Pod
and volume states when a job is involved. Messages the runner writes itself are shown in full.
Errors from other libraries show only their type name, such as `RUN FAILED: ClientError`, because
their details can contain request data.

`Interrupted; cleanup requested. Check the job ledger before assuming billing has ended.` means you
pressed Ctrl+C. Before the workload runs, or without a running watchdog, the job was stopped at
once. Once it runs, and while its watchdog runs, `launch` only recorded a cancel: the watchdog asks
the Pod to finish and publish, then cleans up (see [the command line reference](MANUAL.md#launch)).
The closing lines say what is left; in that case follow with `status` rather than `recover`, which
would delete the Pod before it publishes.

`OUTPUTS LOST: the workload ran, but it never published its outputs and they are lost with the Pod; the Pod and the volume are deleted, the Pod's records kept.`
comes with the outcome `outputs_lost` (exit code 12): nothing bills any more, but the workload's
outputs are gone. See [recovery](#recovery-and-the-volume-audit).

`The command line is wrong; its usage is on standard error.` is the error of the outcome
`usage_error` (exit code 64): the parser's message above it says what is wrong.

## Command outcomes

Every command ends with a named outcome and exit code;
[the command line reference](MANUAL.md#outcomes-and-exit-codes) explains each. What to do:

- **`success` (0)**

  nothing

- **`error` (1)**

  read the `RUN FAILED:` line and the closing lines

- **`workload_failed` (2)**

  the cleanup is verified; read `train.log` and the workload's outcome below

- **`probe_failed` (3)**

  the Pod did not delete itself in time and the watchdog did it; check the Pod's key permissions if
  it repeats

- **`no_capacity` (4)**

  try other candidates or later; see [capacity and price](#capacity-and-price)

- **`rejected` (5)**

  see the HTTP status in the error and in `launch-failure.json`

- **`ambiguous` (6)**

  see [unresolved creation](#unresolved-creation)

- **`cleanup_unverified` (7)**

  run `status`; then `recover`, or check the Runpod console

- **`storage_retained` (8)**

  see [recovery](#recovery-and-the-volume-audit)

- **`detached` (9)**

  nothing; follow the job with `status`, `logs` and `cost`

- **`cancelled` (10)**

  nothing; `cancel` stopped the job, what the workload published was recovered, and nothing remains

- **`pending` (11)**

  the stop request is written; follow with `status` until the Pod is gone and the results are
  recovered

- **`outputs_lost` (12)**

  nothing bills; the outputs are gone. The Pod's records were copied to `downloads/volume-audit-*/`,
  and `logs` shows its failure records from there; they may say why the Pod ended before it
  published. Stop a running job with `cancel`, not `recover`, so that it can publish first

- **`partial_outputs` (13)**

  read the copies in `copy_directory` (the summary names it); `objects.json` there maps each key to
  its file

- **`usage_error` (64)**

  fix the command line

- **`interrupted` (130)**

  read the closing lines; run `status`

## Naming a job

Job commands take a ledger folder, `latest`, or the start of a job ID. When they cannot find the job
they say so and change nothing:

- **`No job ledger at <folder> (job.json is missing). Nothing was created or changed.`**

  - **Meaning** the argument is neither a ledger folder nor something that looks like an ID
  - **What to do** check the path; `podvigil list` shows the jobs

- **`No job in <jobs folder> has an ID that starts with <text>, and there is no job ledger at <path>. Nothing was created or changed.`**

  - **Meaning** no job in this state folder starts with those characters
  - **What to do** check `--state-dir`; name a ledger elsewhere by its path

- **`<n> jobs in <jobs folder> start with <text>: <ids>. Give more of the ID.`**

  - **Meaning** the start matches several jobs
  - **What to do** give more characters

- **`No job ledgers in <jobs folder>, so there is no latest job. Nothing was created or changed.`**

  - **Meaning** `latest` found no job in this state folder
  - **What to do** check `--state-dir` or `PODVIGIL_STATE_DIR`

- **`Job <id>: <folder>` (on standard error)**

  **Meaning** not an error: the job that `latest` or the ID named

An ID needs at least four characters; shorter text is taken as a folder path.

## Read-only views

<!-- prettier-ignore -->
| Message | Command | Meaning |
| --- | --- | --- |
| `unreadable` in the `PHASE` column, with an `error` in `--json` | `list` | that ledger could not be read; open it with `status` to see why |
| `No jobs in <jobs folder>.` | `list` | the state folder has no ledgers; check `--state-dir`. The folder is shown by its real location on disk, as the `Job ledger:` line shows ledgers |
| `Not available yet: watchdog.log: the watchdog has written nothing yet.` | `logs` | the watchdog has not logged anything so far |
| `Not available yet: The workload log stays on the Pod while the job runs; it is downloaded with the results when the Pod has ended (recover, or the watchdog's recovery).` | `logs` | normal while the job runs |
| `--tail takes 0 (everything) or a positive number of lines.` | `logs` | fix the option |
| `No Pod was created, so no compute was billed.` | `cost` | the estimate is 0 |
| `The Pod's create request has no known outcome, so a Pod may exist and bill; there is nothing to estimate from. See status.` | `cost` | see [unresolved creation](#unresolved-creation) |
| `This ledger has no price quote or no Pod creation time to estimate from.` | `cost` | a ledger from an early stage, or from an older version |

## Running locally

Messages of `run-local`, which runs a job's command on your own computer. Nothing reaches Runpod.

- **`The job's command '<name>' is not on this computer's PATH. For a python3 or python command, name the Python to use with --python. Nothing was run.`**

  - **Meaning** the program is not installed here
  - **What to do** install it, or use `--python`

- **`The job's command '<name>' is only a Microsoft Store app alias here (<path>). For a python3 or python command, name the Python to use with --python. Nothing was run.`**

  - **Meaning** on Windows, `python3` often opens the Store instead of running
  - **What to do** use `--python` with a real Python

- **`The job's command '<path>' does not exist on this computer (<path>). For a python3 or python command, name the Python to use with --python. Nothing was run.`**

  - **Meaning** the command is a path that does not exist here, such as one below
    `/workspace/inputs`
  - **What to do** start scripts as `python3 -u -m <module>`

- **`--workdir must be a new or empty folder: <folder>. Nothing was run.`**

  - **Meaning** the work folder holds files
  - **What to do** choose a new folder

- **`Input <remote> changed while it was copied. Nothing was run.`**

  - **Meaning** an input file changed during the copy
  - **What to do** retry when nothing writes to it

- **`Input paths collide as files or directories.`**

  - **Meaning** an input name is also used as another input's parent directory
  - **What to do** give each file a distinct path, with directories used only as directories

- **`Local workload processes did not stop.`**

  - **Meaning** the local process supervisor could not confirm that every workload process ended
  - **What to do** inspect the local process tree before retrying; this run is not certified
    complete

- **`Cannot supervise local workload descendants.`**

  - **Meaning** Linux could not enable descendant supervision before starting the workload
  - **What to do** check the host's process restrictions; the workload was not started

- **`The time limit must be more than 0 seconds and at most seven days.`**

  - **Meaning** `--timeout`, or the job's own time, is outside that range
  - **What to do** fix `--timeout`

- **warning `The workload wrote result.json; the Pod replaces it with its own record, as here.`**

  - **Meaning** your command wrote a file the runner owns
  - **What to do** write your results under another name

- **warning
  `The job succeeded but required artifacts are missing; recovery would keep the volume and report the job incomplete.`**

  - **Meaning** a `required_artifacts` file is missing
  - **What to do** write it, or fix the list

- **outcome `training_failed` with `No module named ...` or `can't open file` in `train.log`**

  - **Meaning** the command started but failed here, for example without `torch` or with a
    `/workspace/...` path
  - **What to do** install what it needs; use `-m` for scripts

## The closing lines

`launch` and every command that takes a ledger end with two lines on standard error: the Pod, then
the volume. They are worked out from the ledger alone. `<command>` in them is the command you ran:
`podvigil`, `python -m podvigil` or `managed_run.py` (run that one as `python managed_run.py`). In
the watchdog's log it is the command that started the job. The ledger path is not quoted: put it in
quotes when it contains spaces.

- **`Pod <id>: deleted; its absence was verified.`**

  - **Meaning** the Pod is gone, with evidence
  - **What to do** nothing

- **`Volume <id>: deleted; its absence was verified.`**

  - **Meaning** the volume is gone, with evidence
  - **What to do** nothing

- **`... Nothing from this job remains.` (added to the second line)**

  - **Meaning** neither resource exists: deleted, refused, or never requested
  - **What to do** nothing

- **`Pod <id>: runs and bills; the watchdog guards it and deletes it by the deadline <time> at the latest. Stop it early: <command> cancel <ledger>`**

  - **Meaning** the job is running, no stop was requested, and its watchdog was heard from in the
    last five minutes
  - **What to do** nothing; `cancel` stops the job early

- **`Volume <id>: holds the job's inputs and results and bills as storage; the watchdog recovers the results and then deletes it. Follow: <command> status <ledger>`**

  - **Meaning** the same job's volume, also while a cancelled job's Pod finishes
  - **What to do** nothing; follow with `status`

- **`Pod <id>: was asked to finish and publish its outputs, and bills until it ends; its deletion is NOT verified yet. The watchdog deletes it if it has not ended by <time>. Follow: <command> status <ledger>`**

  - **Meaning** after a `cancel` (or Ctrl+C on `launch`) of a running workload, the watchdog asked
    the Pod to stop the workload, publish its outputs and delete itself; the watchdog was heard from
    in the last five minutes, and `<time>` is the end of its wait
  - **What to do** wait and follow with `status`. Do not run `recover` now: it would delete the Pod
    before it publishes

- **`Pod <id>: a cancel was requested; the watchdog is about to ask it to finish and publish its outputs. It bills until it ends; its deletion is NOT verified yet. Follow: <command> status <ledger>`**

  - **Meaning** the moment after a `cancel` (or Ctrl+C on `launch`) of a running workload, before
    the watchdog has acted on it; the watchdog was heard from in the last five minutes
  - **What to do** do not run `recover`: it would delete the Pod before it is asked to publish.
    Follow with `status`; the watchdog acts within about `poll_seconds`

- **`Pod <id>: may still exist and bill; its deletion is NOT verified. Run: <command> recover <ledger>`**

  - **Meaning** the Pod was created and its deletion is not proven, and either no watchdog was heard
    from recently or a stop was requested that the lines above do not cover (the workload had not
    started, or the time the Pod was given to finish is over)
  - **What to do** if the job may still be running, check `status` first: `recover` stops a running
    job. Otherwise run `recover`

- **`Volume <id>: still exists and is billed as storage until deleted. Results and cleanup: <command> recover <ledger>`**

  - **Meaning** the volume exists, and the job is not running under a recently heard watchdog (or it
    has a stop request)
  - **What to do** while the watchdog is still recovering, wait; then run `recover`; if it keeps
    refusing, see [recovery](#recovery-and-the-volume-audit)

- **`Pod: none was created (Runpod refused the request with HTTP <status>).`**

  - **Meaning** Runpod refused the create request; nothing exists
  - **What to do** see the failure; for 400 or 403 usually no capacity

- **`Volume: none was created (Runpod refused the request with HTTP <status>).`**

  - **Meaning** the same for the volume
  - **What to do** check the key's permissions and your account's storage

- **`Pod: none was created (the request was not sent).` (or `Volume: ...`)**

  - **Meaning** the request failed before it left your computer
  - **What to do** check the network; retry

- **`Pod: the create request's outcome is unknown, so a pod may exist and bill. Check: <command> status <ledger>`**

  - **Meaning** an ambiguous answer: Runpod may have created a Pod that nobody can see yet
  - **What to do** see [unresolved creation](#unresolved-creation)

- **`Volume: the create request's outcome is unknown, so a volume may exist and bill. Check: <command> status <ledger>`**

  - **Meaning** the same for the volume
  - **What to do** see [unresolved creation](#unresolved-creation)

- **`Pod: none was created.` / `Volume: none was created.`**

  - **Meaning** no create request was ever sent
  - **What to do** nothing

- **`Pod: none was created.` and `Volume: none was created. Nothing from this run remains.`**

  - **Meaning** `launch` stopped before it made a ledger
  - **What to do** fix the reported problem

The three command forms do the same; a line from one form can be followed with any of them.

## Status phases

`podvigil status` reports one `phase`:

<!-- prettier-ignore -->
| Phase | Meaning |
| --- | --- |
| `preparing` | no Pod request yet: the volume, the upload |
| `starting` | the Pod exists; the workload is not released yet |
| `running` | the workload was released |
| `stopping` | a stop was requested and the Pod is not yet proven deleted; after a `cancel` of a running workload this includes the time the Pod is given to finish |
| `pod creation unresolved` | the Pod create request had an ambiguous answer |
| `recovering results` | the Pod's deletion is verified; results and the volume are being handled |
| `finished` | `summary.json` exists; read `outcome` and the closing lines |
| `failed` | `launch` recorded a failure in `launch-failure.json`; read `failure` and the closing lines |

`failed` describes the controller, not necessarily the resources: the watchdog may still be
finishing the cleanup. The closing lines and `watchdog` tell you.

## Resource states

`status` shows `pod.state` and `volume.state`:

<!-- prettier-ignore -->
| State | Meaning |
| --- | --- |
| `not_created` | no create request was sent |
| `rejected` | Runpod refused the request (`http_status` says how), or it was not sent; nothing exists |
| `unknown` | a request was sent and its answer was lost or ambiguous |
| `exists` | the ID is known and the deletion is not verified (yet) |
| `deleted` | the deletion is verified |

## Watchdog states

`watchdog.json` holds the watchdog's `state`, shown by `status` under `watchdog`:

- **`armed`**

  - **Meaning** watching the job
  - **What to do** nothing

- **`pod_deleted`**

  - **Meaning** it deleted the Pod, or confirmed its deletion, and is recovering
  - **What to do** wait

- **`observation_or_cleanup_unverified`**

  - **Meaning** its last check or cleanup step failed; it retries every poll; `message` says why
  - **What to do** if it lasts, read `watchdog.log`

- **`no_pod_created`**

  - **Meaning** the job ended before any Pod existed (refused or never sent), and the volume was
    handled
  - **What to do** nothing

- **`complete`**

  - **Meaning** recovery finished after the Pod was found gone: results and volume handled
  - **What to do** nothing

- **`storage_retained`**

  - **Meaning** recovery failed; the volume was kept
  - **What to do** read `recovery-handoff.json`, then [recovery](#recovery-and-the-volume-audit)

- **`creation_unresolved`**

  - **Meaning** an ambiguous create answer; it keeps looking for the resource
  - **What to do** see [unresolved creation](#unresolved-creation)

- **`operator_resolution_needed`**

  - **Meaning** still unresolved five minutes after the deadline; it now checks every 60 seconds and
    also reads Runpod's audit log
  - **What to do** see [unresolved creation](#unresolved-creation)

- **`operator_took_over`**

  - **Meaning** someone ran `hand-off`; monitoring has ended and billing is not verified
  - **What to do** the operator owns the cleanup; `resume-monitoring` undoes it

`recovery-handoff.json` says `complete` or `storage_retained`. With `storage_retained` it also holds
the error, the result of a partial export, and the message "Compute is terminated. Storage may
remain billed; run recover or export partial outputs." (or, when no Pod existed, "Recovery
unresolved; inspect ledger and retain storage.").

## Workload outcomes

The workload's own outcome, apart from the command's: `training_outcome` in `summary.json`, `status`
and `list`; `outcome` in `downloads/artifacts/result.json`; `workload_outcome` in the result of
`run-local`:

- **`success`**

  the command exited with code 0

- **`training_failed`**

  the command exited with another code; read `train.log`

- **`time_limit`**

  the command was stopped at the end of its time; its outputs were still recovered

- **`cancelled`**

  the command was stopped because the job was cancelled (`cancel`, or Ctrl+C on `launch` once the
  workload ran), as at its time limit; its outputs were still recovered

- **`interrupted`**

  the guard stopped the supervisor (a stop signal to the container, a missing heartbeat, the startup
  limit)

- **`agent_failed`**

  the supervisor failed; see [failures inside the Pod](#failures-inside-the-pod)

- **`agent_preflight_failed`**

  the supervisor ended before it recorded any other outcome

- **`not_started`**

  no Pod was created, or the Pod stopped before its supervisor began the job; the volume was deleted
  after an audit

- **`outputs_lost`**

  in `summary.json` only: the workload was released, but the Pod ended without publishing anything;
  the volume held only the inputs and checked records and was deleted (see
  [recovery](#recovery-and-the-volume-audit))

- **`partial_outputs`**

  in `summary.json` only: the outputs were incomplete, and `recover --accept-partial` copied every
  object on the volume to the ledger, checked, before it deleted the volume

- **`unknown`**

  the downloaded `result.json` had no outcome (not expected)

`summary.json` also holds `pod_termination_verified` and `volume_deletion_verified` (both `true`
when it exists), the evidence, and, for jobs that never ran, `backup_not_required: true`. With
`outputs_lost` it also holds `outputs_lost: true`, an `explanation`, and `guard_records`, the folder
with the copies of the Pod's records. With `partial_outputs` it holds `partial_outputs: true`, an
`explanation`, `workload_outcome` (the workload's own outcome from the early copy of `result.json`,
if one was on the volume), `copy_directory` (the folder with the copies) and `object_count` (the
objects on the volume).

## Job file errors

From `plan` and `launch`, before anything is created. Fix the job file and run `plan` again. The
[job file reference](JOB-FILE.md) has the rules.

- **`Missing/unknown job fields: [<names>]`**

  the listed fields are required and missing, or not known; the list mixes both

- **`A job file holds one JSON object.`**

  the file is valid JSON but not an object

- **`Only SECURE cloud is permitted. There is no Community fallback.`**

  `cloud` is not `"SECURE"`

- **`Only STANDARD network volumes are permitted.`**

  `volume_type` is not `"STANDARD"`

- **`self_termination_probe must be a boolean.`**

  use `true` or `false`, without quotes

- **`workload_env must be an object of string values.`**

  `workload_env` is not a JSON object

- **`Unsafe declared workload environment variable.`**

  a `workload_env` name or value breaks the rules

- **`<field> must be nonempty.`**

  `image`, `gpu_id`, `data_center_id` or `min_cuda_version` is empty or not a string

- **`Pin the container image to a specific tag or digest; latest is forbidden.`**

  add a tag such as `:1.0.2` or a digest

- **`min_cuda_version must be major.minor.`**

  for example `"12.8"`

- **`min_cuda_version <version> is below this runner's CUDA floor <floor> (the PODVIGIL_MIN_CUDA_VERSION setting).`**

  raise `min_cuda_version`, or change the setting

- **`PODVIGIL_MIN_CUDA_VERSION must be major.minor, for example 12.8; it is '<value>'.`**

  fix the environment variable

- **`Select a documented Runpod S3 data center; update the reviewed list if needed.`**

  `data_center_id` is not one of the fifteen listed data centers

- **`<field> must be a positive integer.`**

  a whole-number field is missing a value, is `0`, negative, or written with a decimal point

- **`GPU count or volume size is outside this runner's bounds.`**

  `gpu_count` above 8, or `volume_gb` outside 10 to 4096

- **`Runtime must be between five minutes and seven days.`**

  `max_runtime_seconds` outside 300 to 604800

- **`Preparation timeout must be between one minute and one day.`**

  `preparation_timeout_seconds` outside 60 to 86400

- **`Self-termination grace must be between one second and one hour.`**

  `self_termination_grace_seconds` outside 1 to 3600

- **`Reserve at least 30 seconds and less than half the runtime for finalization.`**

  `finalize_grace_seconds`

- **`Startup timeout must be shorter than the total runtime.`**

  `startup_timeout_seconds` (default 900) is not below `max_runtime_seconds`

- **`Polling must be between 2 and 30 seconds.`**

  `poll_seconds`

- **`Heartbeat grace must be at least six polling intervals.`**

  `controller_stale_seconds` or `agent_stale_seconds` below 6 times `poll_seconds`

- **`<field> must be a finite positive number.`**

  `max_compute_usd` or `max_compute_usd_per_hour`

- **`command must be a nonempty argv array, not a shell string.`**

  write the command as a list of strings

- **`Compute budget leaves no training time after startup/finalization allowances.`**

  the lifetime (the smaller of `max_runtime_seconds` and the budget's time at the ceiling) must
  exceed `startup_timeout_seconds + finalize_grace_seconds`

- **`required_artifacts must be a list of paths relative to the output directory.`**

  write a list

- **`This job file lists several candidates; plan and launch take them in order.`**

  only from code that reads a job file as one configuration; the command line accepts candidates

- **`Unsafe artifact path.`**

  an artifact or input `remote` name is empty, absolute, or contains `\`, `:`, `.`, `..` or an empty
  part

- **`Duplicate artifact path.`**

  an artifact is listed twice, or you listed `result.json` or `train.log`

- **`Unsafe Windows artifact path.`**

  a name contains `< > : " \ | ? *` or a control character, or ends with a space or a dot

- **`Reserved or unsupported Windows artifact path.`**

  a reserved name such as `CON`, `NUL`, `COM1`; a short alias such as `ABCDEF~1.TXT`; or a name over
  255 characters

- **`Colliding artifact path.`**

  two names differ only in letter case or Unicode form

- **`inputs must be a list.` / `inputs must be a list of explicit files.`**

  `inputs` is not a list

- **`Each input must contain only local and remote paths.`**

  an input entry has other keys; folder and pattern entries use their own keys

- **`Duplicate input or credential file is forbidden.`**

  two inputs share a `remote` name, or a `remote` name starts with `.env`

- **`Input source must be a nonempty local path.`**

  `local` is empty or not a string

- **`Never upload a .env file.`**

  an input's file name starts with `.env`

- **`Never upload a .env file or an alias of it.`**

  an input is one of the runner's credentials files, under another name or through a link

- **`Input source cannot traverse a symlink or reparse point.`**

  a folder on the input's path is a link or a junction; move the file to a plain folder

- **`Input must be a regular file, not a symlink or reparse point.`**

  the input itself is a link or not a file

- **`[Errno 2] No such file or directory: '<path>'` (Windows:
  `[WinError 2] The system cannot find the file specified: '<path>'`)**

  an input file does not exist; relative paths start at the job file's folder

- **`A directory input has exactly "directory" and "remote".`**

  fix the entry's keys

- **`A pattern input has "pattern" and optionally "remote" and "base".`**

  fix the entry's keys

- **`Input <origin> needs a nonempty folder path.`**

  `directory` or `base` is empty

- **`Input <origin> names a folder that does not exist: <folder>`**

  fix the path

- **`Input <origin> is not a plain folder (links are never followed): <folder>`**

  the folder is a link or a file

- **`Input <origin> has a folder that cannot be read: <folder>`**

  a permission problem below the folder

- **`Input <origin> reaches a link or reparse point, which is never followed: <path>`**

  remove the link from the folder, or list the files you want

- **`Input <origin> reaches a special file: <path>`**

  a pipe or device file below the folder

- **`Input <origin> contains a credential file, which is never uploaded: <path>`**

  a `.env`-like file below the folder; move it out

- **`Input <origin> has a file whose name cannot be an input name: <path>`**

  a file name containing `:` or `\`

- **`Input <origin> holds no files.` / `Input <origin> matched no files below <folder>.`**

  an empty folder or a pattern that matches nothing

- **`Input <origin> holds more than 10,000 files.` /
  `Input <origin> matches more than 10,000 files.` /
  `The inputs expand to more than 10,000 files.`**

  far beyond what fits; pack the files into an archive

- **`Input <origin> must be relative, separated by '/', without '.', '..' or '**' (use a directory input for a whole tree).`**

  fix the pattern

- **`With candidates, give gpu_id and data_center_id in each candidate, not at the top level.`**

  move them into the candidates

- **`candidates must be a list of 1 to 8 entries.`**

  fix the list

- **`Candidate <n> needs gpu_id and data_center_id and may also set only max_compute_usd_per_hour and max_compute_usd.`**

  fix that candidate

- **`Candidate <n> repeats an earlier GPU and data center.`**

  remove the duplicate

- **`The Pod request would be <n> bytes; this runner sends at most 100,000 (Runpod refuses more than 102,400). The request holds the Pod's code and the job's settings; the input list is not in it. Nothing was created.`
  (with `, and the <n> Pods already in the account add about 25 bytes each` before
  `; the input list`, or ending `The Pod was not requested.` when it is found at the Pod request)**

  the job's settings (for example a very long `command`, `workload_env` or `required_artifacts`), or
  a very large number of Pods in the account, leave no room in the request; the number of inputs
  does not count. See [how many inputs fit](JOB-FILE.md#how-many-inputs-fit)

- **`Expecting value: line <n> column <n> (char <n>)` and similar**

  the job file is not valid JSON

`<origin>` is the folder or pattern entry, for example `directory "data"`.

## Credentials and setup

- **`The Runpod .env must contain one nonempty RUNPOD_API_KEY without whitespace.`**

  - **Meaning** the credentials file has no `RUNPOD_API_KEY` line, more than one, an empty value, or
    a space in it
  - **What to do** fix the file; the name says `.env`, but it means whichever file the lookup found

- **`The credentials file named by --env-file or PODVIGIL_ENV_FILE does not exist: <path>`**

  - **Meaning** the option or the variable names a missing file
  - **What to do** fix the path; the runner does not fall back to another file

- **`Set Runpod's separate S3 access/secret keys in .env before launch; no resources created.`**

  - **Meaning** `RUNPOD_S3_ACCESS_KEY` or `RUNPOD_S3_SECRET_KEY` is missing
  - **What to do** add both to the credentials file

- **`Install requirements.txt in this folder's own venv before launch.`**

  - **Meaning** `boto3` is not installed in the Python that runs the runner
  - **What to do** `pip install -r requirements.lock.txt` in that environment

- **`[Errno 2] No such file or directory: '<path ending in .env>'`**

  - **Meaning** no credentials file was found: no option, no variable, no per-user file, so the
    runner tried its last fallback
  - **What to do** create the per-user file (see
    [the quickstart](QUICKSTART.md#2-store-your-credentials))

- **`The watchdog resolved different credentials than this controller: <names>. On Windows the watchdog starts through WMI and does not inherit this shell's environment variables, so keys set only in the environment are missing there. Put the keys in the .env file.`**

  - **Meaning** the watchdog would not have the same keys
  - **What to do** put all three keys in the credentials file; nothing was created

- **`WARNING: <the same text> The watchdog still guards the existing resources, but its result recovery may fail.`**

  - **Meaning** from `reattach` or `resume-monitoring`: the watchdog started anyway
  - **What to do** fix the credentials file, then run `recover` when the job ends

- **`Job credential changed; verify account identity before rebinding cleanup. Absence is not verified.`**

  - **Meaning** the API key differs from the one the job was launched with
  - **What to do** use the original key; a key of another account could not prove anything about
    this job

- **`Runpod <method> <path>: HTTP network failure (request not sent): API pacing deferred; caller must retain supervision`
  (or `HTTP <status>: API pacing deferred; ...` when Runpod's answer itself asked for the wait)**

  - **Meaning** the runner held back an API request: Runpod's rate limit asks for a wait longer than
    the runner may wait, or the request ran out of time while waiting
  - **What to do** retry a little later; the watchdog retries by itself

- **`Runpod <method> <path>: HTTP network failure (request not sent): API pacing deferred: another process held the pacing file; try again`**

  - **Meaning** another process that uses the same key and state folder held the pacing file at that
    moment
  - **What to do** try again

- **`Runpod <method> <path>: HTTP network failure (request not sent): API pacing deferred: this key's pacing file in the state folder is unusable (<error type>); when no job runs, delete the api-rate-limit files there`**

  - **Meaning** this key's pacing file could not be read or written: it is damaged (for example
    `ValueError`, `JSONDecodeError` or `KeyError`), or the folder refuses access (`PermissionError`
    or another `OSError`)
  - **What to do** when no job of this state folder runs, delete the files whose names start with
    `api-rate-limit` in the state folder (each pacing file and its `.lock` file), then retry;
    cleanup requests still go through meanwhile

**API keys and pacing.** Each API key has its own pacing file in the state folder,
`api-rate-limit-<fingerprint>.json`, named by a short one-way fingerprint of the key; the key itself
is never written. So after you change your API key, or when two accounts share one computer, the new
key starts with fresh pacing. Cleanup requests go through even when the pacing file cannot be used:
they fall back to pacing inside their own process. The single `api-rate-limit.json` of earlier
builds is no longer read; a watchdog started by such a build uses it until it ends, and the file can
be deleted after that. A job still refuses a key other than the one it was launched with (see the
"Job credential changed" row above).

## Capacity and price

- **`No confirmed Secure Cloud capacity in the selected data center (<gpu> in <data center>). No Community fallback.`**

  - **Meaning** the catalog shows no capacity for that pair. At the first check nothing exists yet;
    at the second check, after the upload, the job's new volume is deleted again
  - **What to do** try later, or add candidates (exit code 4)

- **`Runpod POST /pods: HTTP 400: <title>; <detail>` with "no longer any instances available" or a
  similar text**

  - **Meaning** capacity vanished between the catalog check and the Pod request; the volume was
    created, then deleted
  - **What to do** the same (exit code 4)

- **`Runpod POST /pods: HTTP 403 ...`**

  - **Meaning** treated as no access to that GPU pool; also what a key without Pod permission gets
  - **What to do** check the key's permissions if it happens for every candidate

- **`No capacity for <candidate>: <answer> Nothing was created. Trying the next candidate.`**

  - **Meaning** printed between candidates
  - **What to do** nothing

- **`No capacity for <candidate>: <answer> No Pod was created and the volume deletion was verified. Trying the next candidate.`**

  - **Meaning** printed between candidates
  - **What to do** nothing

- **`None of the <n> candidates had capacity. Last answer: <answer>`**

  - **Meaning** every candidate answered "no capacity" (exit code 4)
  - **What to do** run `survey`, adjust the candidates, retry

- **`No capacity for <candidate>, but its volume deletion is not verified, so the next candidate is not tried. Ledger: <folder>`**

  - **Meaning** the failed attempt's volume was not proven deleted within 60 seconds
  - **What to do** run `status` and, if needed, `recover` on that ledger

- **`Live Secure Cloud price exceeds the configured ceiling.`**

  - **Meaning** the catalog price times `gpu_count` is above `max_compute_usd_per_hour`; this stops
    the launch
  - **What to do** raise the ceiling or choose another GPU

- **`A valid Secure Cloud GPU price is unavailable.`**

  - **Meaning** the catalog has no Secure Cloud price for that GPU type
  - **What to do** check `gpu_id` against `survey`

- **`Selected data center lacks STANDARD network volumes; no premium fallback.`**

  - **Meaning** the data center is on the S3 list but offers no STANDARD volumes
  - **What to do** use a data center that `survey` lists as usable

- **`Runpod GET /catalog/gpus/<gpu>: HTTP <status> ...`**

  - **Meaning** the catalog request failed; 404 usually means an unknown GPU type, 401 or 403 a
    problem with the key
  - **What to do** copy `gpu_id` from `survey`; check the key

- **`The GPU count must be an integer from 1 to 8.`**

  - **Meaning** `survey --gpu-count`
  - **What to do** fix the option

- **`This runner's CUDA floor is <floor> (the PODVIGIL_MIN_CUDA_VERSION setting); a survey cannot go below it.`**

  - **Meaning** `survey --min-cuda-version`
  - **What to do** fix the option, or change the setting

- **`A CUDA version must be major.minor, for example 12.8.`**

  - **Meaning** `survey --min-cuda-version`
  - **What to do** fix the option

- **`Malformed GPU catalog response; the survey has no result. Nothing was created.`**

  - **Meaning** Runpod's answer had an unexpected shape
  - **What to do** retry later

## Launch failures

After the `Job ledger:` line, a failure also lands in `launch-failure.json` with the `phase`:
`watchdog_arming`, `volume_create`, `volume_first_write`, `input_upload`, `input_manifest`,
`capacity_recheck`, `pod_create`, `supervise` or `recover`. The runner then deletes what it created,
when it can prove ownership, and the closing lines say what is left. After Ctrl+C on a running
workload with a live watchdog, the record says `handed_to_watchdog: true` instead: `launch` recorded
a cancel, and the watchdog does the cleanup.

- **`Independent watchdog failed to arm; no new paid resource may be created. Cause: <reason>`**

  - **Meaning** the watchdog did not start or did not prove itself; nothing was created
  - **What to do** read the cause (below) and `watchdog.log`

- **`Windows service-mediated watchdog launch failed; see watchdog-launch-error.json`**

  - **Meaning** PowerShell could not ask Windows' management service (WMI) to start the watchdog
  - **What to do** read `watchdog-launch-error.json`; WMI or PowerShell may be blocked by a policy

- **`Windows refused watchdog process creation: <code>`**

  - **Meaning** WMI refused to create the process
  - **What to do** the code is WMI's return value

- **`Watchdog intermediary failed or did not exit` /
  `Watchdog intermediary failed; inspect watchdog.log`**

  - **Meaning** the helper that starts the watchdog failed
  - **What to do** read `watchdog.log`

- **`Watchdog isolation failed: exit=<code>, in_job=<bool>, controller=<pid>, ancestors=<pids>`**

  - **Meaning** the watchdog exited at once, or (on Windows) it still runs inside the controller's
    process tree or job object, so it would not survive the controller
  - **What to do** read `watchdog.log`; run from an ordinary terminal session

- **`The watchdog cannot see the job ledger. This controller wrote it at <path> [(on disk: <real path>)]; the watchdog looked for it at <path> and found no job.json there. On Windows a shell inside a packaged app keeps new folders under %LOCALAPPDATA% in the app's private copy, which other processes do not see at the usual path; give --state-dir a folder outside %LOCALAPPDATA%.`
  (or `...; the watchdog's starter, outside this process's tree, cannot read it there. ...`)**

  - **Meaning** the watchdog, which runs outside the controller's process tree, cannot reach the
    ledger folder; nothing was created
  - **What to do** follow the message: run with `--state-dir` (and `--env-file`) outside
    `%LOCALAPPDATA%`

- **`Watchdog did not acknowledge its arming nonce. If it cannot see the job ledger: this controller wrote it at <path>, and the watchdog runs outside this process's tree; see watchdog.log there.`**

  - **Meaning** the watchdog did not report back within 15 seconds
  - **What to do** read `watchdog.log` in the ledger; if the cause is the ledger's location, see the
    row above

- **`Controller did not accept watchdog isolation` / `Watchdog arming cancelled` /
  `Invalid watchdog arming nonce` / `Unverifiable watchdog process ancestry`**

  - **Meaning** the arming handshake failed
  - **What to do** retry; read `watchdog.log`

- **`Returned volume does not match the creation intent.`**

  - **Meaning** Runpod answered the volume request with a volume that is not this job's
  - **What to do** read the ledger; check the console

- **`Storage round-trip check failed.`**

  - **Meaning** the test file written to the new volume did not read back the same
  - **What to do** check the S3 keys and the data center; the volume is deleted

- **`The input manifest on the volume does not read back as written.`**

  - **Meaning** the list of inputs that `launch` wrote to the volume
    (`jobs/<job id>/control/inputs.json`) did not read back with the size and SHA-256 recorded in
    `job.json`; no Pod was requested (phase `input_manifest`)
  - **What to do** the closing lines say whether the volume was deleted; a changed manifest keeps it
    (see [recovery](#recovery-and-the-volume-audit)). Retry; if it repeats, check the network and
    the S3 keys

- **`Runpod POST /network-volumes: HTTP <status> ...`**

  - **Meaning** the volume request failed
  - **What to do** 401/403: the key; 402: no credit; others: retry later

- **`RUN FAILED: ClientError`, `S3UploadFailedError`, `EndpointConnectionError` or another type
  name, with `provider_code` (such as `AccessDenied`) in `launch-failure.json` when S3 gave one**

  - **Meaning** an S3 error from the storage library; the first write to a new volume is retried for
    90 seconds on `AccessDenied` and `NoSuchBucket` only
  - **What to do** check the S3 keys and the network; retry

- **`Preparation deadline expired; no new resource may be created.`**

  - **Meaning** the upload took longer than `preparation_timeout_seconds`
  - **What to do** raise it, or upload less

- **`Controller heartbeat is stale; no new resource may be created.`**

  - **Meaning** the computer slept or the controller stalled longer than `controller_stale_seconds`
  - **What to do** keep the computer awake during `launch`

- **`Watchdog exited before Pod creation.` / `Local watchdog exited unexpectedly.`**

  - **Meaning** the watchdog died
  - **What to do** read `watchdog.log`; the closing lines say what is left

- **`Cleanup was requested; no new resources may be created.` / `Job creation fence is closed.`**

  - **Meaning** a stop was requested for this job, so nothing new may be created
  - **What to do** the job is ending; use `status`

- **`Created Pod identity does not match this job.` /
  `Returned Pod is not Secure Cloud; immediate termination required.` /
  `Pod is in a different data center.` / `Allocated GPU does not match the request.` /
  `Actual Pod price is missing or exceeds the configured ceiling.`**

  - **Meaning** the created Pod failed a check and is deleted
  - **What to do** report it; for the price, raise the ceiling

- **`Pod supervisor did not become ready within startup allowance.`**

  - **Meaning** the Pod did not report "ready" within `startup_timeout_seconds`
  - **What to do** raise it (image download, input copy); look for `guard-failure.json`

- **`A creation receipt requires a definitive resource ID.`**

  - **Meaning** Runpod's answer to a create request had no ID
  - **What to do** treat as [unresolved creation](#unresolved-creation)

- **`Provider Date clock has not been established.`**

  - **Meaning** the runner has not yet read Runpod's clock; it never falls back to your computer's
    clock
  - **What to do** retry

- **`launch --detach cannot run the self-termination probe (self_termination_probe): its result is known only at the end of the job. Nothing was created.`**

  - **Meaning** `--detach` with a job file that asks for the probe
  - **What to do** set `self_termination_probe` to `false`, or launch without `--detach`

- **`Not detaching yet: <reason>. Supervising until the watchdog can take over.` (on standard
  error)**

  - **Meaning** not an error: `launch --detach` keeps supervising until the watchdog can guard the
    job alone; `<reason>` is `the watchdog process is not running`, `a stop was requested`,
    `the watchdog's last loop did not end armed` or
    `the watchdog has not written its heartbeat recently`
  - **What to do** wait; if it lasts, read `watchdog.log`

- **`"outcome": "detached"` in the result, exit code 9**

  - **Meaning** not an error: `launch --detach` returned while the workload runs
  - **What to do** follow the job with `status`, `logs` and `cost`

## During the run

<!-- prettier-ignore -->
| Message or record | Meaning | What to do |
| --- | --- | --- |
| `watchdog.log`: `stopping Pod <id>: <reason>` | the watchdog is ending the job | read the reason (below) |
| `watchdog.log`: `Pod <id> finished and its absence verified; recovering results` | the normal end | nothing |
| `watchdog.log`: `Pod <id> deleted and its absence verified; recovering results` | the watchdog ended the Pod | read the reason above it |
| `watchdog.log`: `observation or cleanup not verified, retrying: <type>: <message>` | a check or a cleanup step failed; it retries | if it repeats, look at the message |
| `watchdog.log`: `no Pod to stop (<reason>); recovering the volume`, then `recovery finished: <state>` | the job ended before a Pod existed | nothing |
| `watchdog.log`: `recovery complete` | results and volume done | nothing |
| `watchdog.log`: `recovery failed, storage retained: <type>: <message>; partial export saved` (or `; no partial export`) | see [recovery](#recovery-and-the-volume-audit) | |
| `watchdog.log`: `watchdog armed`, `watchdog stopped; last state <state>. <closing lines>` | start and end of the watchdog | |
| `self-termination-probe.json`: `passed: false` | the Pod did not delete itself within `self_termination_grace_seconds`, or the watchdog had to help (`local_fallback: true`) | `launch` exits with 3; the Pod is deleted either way |

Stop reasons, in `stop-request.json` and the watchdog's messages:

<!-- prettier-ignore -->
| Reason | Who | Meaning |
| --- | --- | --- |
| `job finished` | watchdog | the Pod reported "done"; the watchdog deletes it (when the probe is off) |
| `self-termination probe timeout` | watchdog | the Pod did not delete itself in time; the watchdog does it |
| `absolute runtime limit` | watchdog | the deadline passed |
| `controller runtime limit` | controller | the deadline passed |
| `preparation time limit` | watchdog | no Pod request within `preparation_timeout_seconds` |
| `startup/agent timeout` | watchdog | the startup limit passed and the workload was never released |
| `Pod supervisor repeatedly observed frozen` | watchdog | the Pod's heartbeat stopped for `agent_stale_seconds` |
| `Pod stopped or failed` | watchdog | Runpod reports the Pod as `EXITED` or `ERROR` |
| `Pod cloud/identity/price validation failed` | watchdog | the Pod no longer matches the job, is not Secure Cloud, or reports a price above the ceiling |
| `controller exit/failure` | controller | `launch` failed or was interrupted |
| `cancel requested by the operator` | `cancel`, or `launch` on Ctrl+C | someone ran `cancel`, or pressed Ctrl+C in a foreground `launch` once the workload ran; for a running workload the watchdog first asks the Pod to finish, and records this reason again when it deletes a Pod that has not ended in time |
| `recover/finish cleanup` | `recover` | someone ran `recover` |
| `partial output recovery` | `export-partial` | someone ran `export-partial` |
| `unresolved creation export; compute status unknown` | `export-unresolved` | someone ran `export-unresolved` |
| `operator taking responsibility for unresolved billing` | `hand-off` | someone ran `hand-off` |

## Unresolved creation

The Pod or volume create request got an ambiguous answer (a timeout, a server error, a lost
connection after sending). Runpod may have created the resource. The runner never sends the request
again.

- **`Creation response was ambiguous. Absence is not confirmed; watchdog will reconcile.`**

  no Pod can be seen yet after an ambiguous Pod request

- **`Early Pod absence is inconclusive; retain guards through startup visibility window.`**

  a new Pod may simply not be visible yet

- **`Startup cleanup request not accepted; continue reconciling without declaring absence.`**

  a delete during startup was refused; it keeps trying

- **`No positive proof that a Pod was never dispatched. Keep volume; reconcile the job ledger.`**

  `recover` cannot prove that no Pod exists, so it keeps the volume

- **`Volume creation is unresolved; keep reconciling its identity.`**

  the same for the volume

- **`Missing positive volume creation/rejection evidence; cleanup cannot be certified.`**

  the ledger cannot show what happened to the volume request

- **`No volume receipt; inspect creation reconciliation before declaring completion.`**

  a Pod exists but the volume's ID is unknown

- **`Multiple owned resources found. Do not create again; inspect the ledger.`**

  two resources match this job's identity

- **`Invalid Pod identity beacon; retain storage for inspection.` /
  `Volume beacon points to an unrelated Pod; no deletion authority.`**

  the record the Pod left on the volume does not identify a Pod of this job

- **`Incomplete audit response; creation remains unresolved.` /
  `Multiple matching creation audit records; operator inspection required.` /
  `Creation audit conflicts with the current resource identity.`**

  the audit-log search proved nothing

What to do:

1. Run `status`. The watchdog keeps looking (`creation_unresolved`, later
   `operator_resolution_needed`); often it finds the resource and finishes alone.
2. Look in the Runpod console for a Pod named `wondomon-<job id>` or a volume named
   `wondomon-results-<job id>`. If you find it, bind it:
   `podvigil resolve-creation <ledger> --pod-id <id>` (or `--volume-id <id>`), or let the runner
   search the audit log with `--audit`. Then `reattach` (if the job may still run) or `recover`.
3. To save files from the volume meanwhile, wait until the deadline plus five minutes and run
   `export-unresolved`.
4. If you are sure nothing exists, or you will clean up by hand, run `hand-off` with a note. The
   watchdog then stops. Billing is your responsibility from then on.

`resolve-creation` can also say: `Invalid resource ID.` (IDs are letters, digits, `_` and `-`),
`Select exactly one Pod ID, volume ID, or audit reconciliation.`,
`No matching creation intent exists in this ledger.` (the job never tried to create that kind of
resource), `Refusing to replace a different resource identity already bound to this job.`, and
`The supplied resource ID cannot be verified as belonging to this job.`

## Recovery and the volume audit

The volume is deleted only on evidence, after an audit of every object on it:

- after every result was downloaded and verified, and every other object is accounted for;
- for a job whose workload never ran, when it holds only the inputs and the guard's records;
- for a job whose workload ran but never published anything, when it holds only the unchanged
  inputs, the input manifest and the checked records of the release, a cancel request and this job's
  Pod. The outputs are lost: the outcome is `outputs_lost` (exit code 12), and the Pod's records are
  copied to `downloads/volume-audit-*/` first;
- after you ran `recover --accept-partial` and every object was copied to the ledger and checked:
  the outcome is `partial_outputs` (exit code 13).

For `not_started` after a Pod existed, `outputs_lost`, and `partial_outputs`, recovery writes
`recovery-checkpoint.json` before requesting volume deletion. If the controller exits after deletion
but before writing its summary, a later `recover` verifies the saved job and resource identities,
audit, copied bytes and independent deletion evidence before finishing. It does not need the deleted
volume to be available through S3. A completed summary is checked against those saved copies too.

`Special recovery evidence is missing, changed or invalid; completion is not verified.` or
`Saved volume audit is missing, changed or invalid; completion is not verified.` means those local
records or copies cannot prove completion. Keep the ledger intact, inspect `status` and the resource
IDs, and restore the original records from a trusted backup if available. A volume may already be
deleted; these messages do not establish that storage still exists or that recovery succeeded.

Each message below keeps the volume, and it keeps billing as storage. Most end with "Keep the
volume."

- **`No valid complete artifact manifest. Keep the volume for recovery. The workload's outputs are incomplete. export-partial copies what is there and deletes nothing; recover --accept-partial copies every object with its bytes checked, then deletes the volume.`**

  the workload ran, and the volume holds more than a job that published nothing leaves (usually part
  of the outputs, or the early copy of `result.json`), but no complete manifest: the Pod died or was
  deleted while it copied, or its time ran out. `volume-kept.json` (and `logs`) name what was found;
  see "What to do" below

- **`No valid complete artifact manifest. Keep the volume for recovery.`**

  the Pod never published a complete manifest: it died, its time ran out during the copy, a required
  artifact was missing, the outputs could not be snapshotted, or the job was stopped hard. For a job
  whose workload ran, the message goes on with the advice above

- **`The Pod's own status record for this job is not on the volume. Keep the volume.` /
  `The Pod's own status record is not on the volume. Keep the volume.` (in `volume-kept.json`)**

  nothing on the volume proves what this job's Pod left: its `agent.json`, naming this job and this
  Pod, is missing or wrong

- **`Volume record "<name>" is not one this job's Pod leaves. Keep the volume.` (in
  `volume-kept.json`)**

  a record on the volume is not exactly as the release, a cancel request or this job's Pod writes
  it: for example it names another Pod, or it says that outputs were salvaged

- **`Input manifest ledger entry is malformed.`**

  the `input_manifest` entry in `job.json` is damaged, so the input manifest on the volume cannot be
  checked; do not edit ledgers

- **`The workload was never released, so the volume holds no outputs to accept. Nothing was deleted.`**

  `recover --accept-partial` on a job whose workload never ran; plain `recover` handles such a
  volume

- **`The Pod's deletion is not verified. Nothing was deleted.`**

  `recover --accept-partial`: the ledger holds no verified deletion of the job's Pod
  (`pod-receipt.json`, `pod-deleted.json`); run `status`, then retry

- **`Result volume ownership/availability is not verified. Nothing was deleted.`**

  `recover --accept-partial`: the volume is gone or no longer matches the job

- **`S3 exposes an object the link index names; retain volume without reading its target.`**

  `recover --accept-partial` found an object under a name that the outputs' link index lists; such
  an object may expose the target of a link, so it is never read. `export-partial` skips those
  objects

- **`Successful job is missing required artifacts. Keep the volume.`**

  the outcome is `success` but a `required_artifacts` file is missing

- **`Unexpected volume objects. Keep the volume. Keys [<keys>]`**

  the volume holds files the runner cannot account for

- **`Remote content differs from its original checksum. Keep the volume. Key <key>`**

  an input on the volume, or the input manifest (`control/inputs.json`), changed

- **`Original input is absent from both the volume and a verified local source. Keep the volume. Key <key>`
  / `Input has no verified local source or backup. Keep the volume. Key <key>`**

  an input cannot be accounted for

- **`Original local input changed during the audit. Keep the volume.`**

  an input file on your computer changed while the audit ran

- **`Volume is missing manifest objects. Keep the volume.`**

  a file in the manifest is not on the volume

- **`Remote manifest differs from the verified local manifest. Keep the volume.`**

  the manifest changed

- **`Volume owner probe does not match the job. Keep the volume.`**

  the volume's owner record is not this job's

- **`Volume inventory changed during its audit. Keep the volume.`**

  something wrote to the volume during the audit

- **`S3 exposes a symlink object; retain volume without reading its target.`**

  a link in the outputs became visible as an object; its target is never read

- **`S3 artifact inventory does not match the manifest. Keep the volume.` /
  `S3 original inventory does not match bundle coverage. Keep the volume.`**

  the result files on the volume differ from the manifest

- **`Artifact download failed. Keep the volume.` /
  `Artifact size or checksum mismatch. Keep the volume.` /
  `Cannot read volume object <key>. Keep the volume.` /
  `Volume object size changed <key>. Keep the volume.`**

  a download failed or did not match its hash

- **`S3 artifact inventory failed. Keep the volume.` /
  `Malformed S3 artifact inventory. Keep the volume.` /
  `Malformed S3 artifact inventory entries. Keep the volume.` /
  `Inconsistent S3 artifact inventory count. Keep the volume.` /
  `Malformed or duplicate S3 artifact inventory object. Keep the volume.` /
  `Invalid S3 artifact inventory pagination. Keep the volume.`**

  listing the volume over S3 failed or gave an incomplete answer

- **`Storage read failed; check connectivity and S3 credentials.` /
  `Storage metadata is malformed or exceeds 16 MiB.`**

  reading a record over S3 failed

- **`Missing job result or log. Keep the volume.` / `Invalid job result. Keep the volume.` /
  `Local artifact missing or checksum mismatch. Keep the volume.` /
  `Local backup metadata or job result is missing.` /
  `Local backup metadata or job result is malformed.` /
  `Local backup receipt does not match this job, volume, or manifest.`**

  the downloaded files in `downloads/` are incomplete or were changed

- **`Bundle result/log differs from manifest copies. Keep the volume.` /
  `Bundle snapshot identity mismatch.` / `Invalid output bundle descriptor. Keep the volume.` /
  `Bundle manifest must contain exactly its archive, metadata, result and log.` /
  `Archive member checksum mismatch.` / `Archive lacks metadata content.` /
  `Unexpected or unsafe archive member.`**

  the bundle archive does not match its metadata

- **`Invalid artifact manifest timestamp. Keep the volume.` /
  `Artifact manifest files must be a nonempty list. Keep the volume.` /
  `Invalid artifact manifest file entry. Keep the volume.` / `Duplicate JSON member.` /
  `Non-finite JSON number.` / `Artifact metadata exceeds 16 MiB.`**

  the manifest is malformed

- **`Unsafe existing path in the artifact destination.` / `Local artifact destination is missing.` /
  `Artifact path must be beneath artifacts/.`**

  something in `downloads/` is a link or not where it should be

- **`Volume audit could not durably verify local data. Keep the volume.` /
  `Volume audit local copy verification failed. Keep the volume.` /
  `Volume audit local copy changed. Keep the volume.`**

  writing or re-reading the audit's local copies failed; check the disk

- **`Volume absence was not independently confirmed; retry recovery.`**

  the volume seems gone, but the second check did not confirm it

- **`Cleanup NOT verified: <reason>`**

  a deletion could not be confirmed; `<reason>` is "resource is still present", "resource presence
  or inventory still prevents confirmation", or the API error

- **`Ownership mismatch; refusing to delete this resource.`**

  the resource behind the ID no longer matches the job; it is not deleted

- **`Malformed inventory; tombstone is not verified.` /
  `Malformed list response; refusing an incomplete resource inventory.` /
  `Malformed resource response; absence is not verified.`**

  Runpod's answer was incomplete; the runner proves nothing from it

- **`Creation fence not confirmed; storage deletion is deferred.`**

  another process of this job held its control lock; retry

- **`Another process holds this job lock.`**

  another command works on this job (for example the watchdog's recovery); wait and retry

What to do when the volume is kept:

1. Run `podvigil recover <ledger>` again. Many causes are passing (network, S3). For a job whose
   workload ran, `volume-kept.json` (shown by `logs`) may say what kept the volume.
2. If it keeps failing, save what is on the volume: `podvigil export-partial <ledger>`. The copies
   land in `<ledger>/partial-downloads/`, with `objects.jsonl` mapping each original key to its
   file. The watchdog may already have made one. A partial export never deletes the volume.
3. When the message says that the outputs are incomplete, and you accept that, run
   `podvigil recover --accept-partial <ledger>`. It copies every object on the volume to
   `<ledger>/partial-downloads/volume-audit-<random>/`, checks each copy, and then deletes the
   volume; it ends with `partial_outputs` (exit code 13). See [recover](MANUAL.md#recover).
4. The runner never deletes a volume it cannot account for in one of these ways. Otherwise, when you
   have what you need, delete the volume yourself in the Runpod console; its ID is in the closing
   line and in `volume-receipt.json`.

`export-partial` can also say: `No known result volume is available.` (the job has no volume ID),
`Result volume ownership/availability is not verified.` (the volume is gone or does not match), and
`Malformed link safety index; retain source without exporting link targets.` The last one can come
from `recover --accept-partial` too; it keeps the volume.

## Operator commands

- **`Reattach requires reconciled Pod/volume receipts and the original deadline.`**

  - **Command** `reattach`
  - **Meaning** the job has no Pod or volume ID yet, or never got a deadline; use `resolve-creation`
    or `recover`

- **`Job ownership ledger not found.` / `Missing job ownership ledger.`**

  - **Command** any
  - **Meaning** `job.json` is empty

- **`Provide a short, nonempty handoff note without credentials.` /
  `The handoff note must not contain credentials.` /
  `Explicit acknowledgement of unverified compute/storage billing is required.`**

  - **Command** `hand-off`
  - **Meaning** fix the note; text such as `rpa_`, `Bearer`, `secret:` or `api_key=` is refused

- **`Creation fence unavailable; cannot hand off monitoring safely.`**

  - **Command** `hand-off`
  - **Meaning** another process held the job's control lock; retry

- **`Watchdog exited before acknowledging resume; monitoring is unverified.` /
  `Watchdog did not acknowledge resume; monitoring is unverified. Retry or inspect the job ledger.`**

  - **Command** `resume-monitoring`
  - **Meaning** read `watchdog.log` and retry

- **`Unresolved export requires the original deadline plus five minutes to elapse.` /
  `Creation fence unavailable; unresolved export deferred.` /
  `Resolve the owned volume identity before exporting it.` / `Volume ownership is unverified.`**

  - **Command** `export-unresolved`
  - **Meaning** wait, retry, or resolve the volume first

- **`Creation fence unavailable; continuing reconciliation.`**

  - **Command** watchdog
  - **Meaning** it retries

- **`An operator took over this job's monitoring (hand-off), so no watchdog acts on a stop request. Run resume-monitoring first, then cancel again. Nothing was changed.`**

  - **Command** `cancel`
  - **Meaning** follow the message

- **`The cancel wait must be between 0 and 3600 seconds.`**

  - **Command** `cancel`
  - **Meaning** fix `--wait`

`cancel` reports one of five states: `compute_stopped` (exit code 0: the Pod and the volume are
gone; exit code 12, outcome `outputs_lost`, when the workload never published its outputs),
`already_finished` (exit code 0: the job had ended; nothing was written), `storage_retained` (exit
code 8: the Pod is gone, the volume is kept; see [recovery](#recovery-and-the-volume-audit)),
`pending` (exit code 11: the stop request is written and the Pod's deletion is not verified yet, for
example because the Pod is still finishing; follow with `status`) or `recovering` (exit code 11 too:
the Pod is gone and the watchdog still recovers the results when the wait ends, so whether the
volume is deleted or kept is not decided yet; follow with `status`). Its `training_outcome` is
`cancelled` when the Pod stopped the workload on the request and published its outputs.

`resume-monitoring` reports one of three states: `monitor_armed` (it started a new watchdog),
`existing_monitor_active` (a watchdog was already running and confirmed), or
`monitor_acknowledged_and_exited` (a watchdog confirmed and then ended, for example because the job
is done).

## Failures inside the Pod

The Pod is deleted at the end, and its own log with it. The guard therefore leaves its reasons on
the volume, and recovery copies them to your computer:

- After a recovery that deleted the volume, also one that ended with `outputs_lost`,
  `downloads/volume-audit-<random>/objects.json` lists every object of the volume; the guard's
  records (keys ending in `control/guard-failure.json`, `agent.json`, `control/self-delete.json`,
  `control/finalization-incomplete.json`) are copied next to it as `<number>.bin` files.
- After `export-partial`, they are in `partial-downloads/.../objects/`, mapped in `objects.jsonl`.
- After `recover --accept-partial`, they are in `partial-downloads/volume-audit-<random>/`, with
  every other object of the volume, mapped in `objects.json` there. `logs` reads the mapped workload
  log and guard records from this folder too.

`guard-failure.json` holds the error type, the message, the place, and `actor` (`pod_guard` or
`pod_supervisor`). For an ownership or mode error it also holds a probe of what the filesystem did.
While a Pod still exists, its log in the Runpod console shows the guard's lines as well.

- **`Workload isolation requires a trusted Linux root supervisor.`**

  - **Meaning** the container does not run as root
  - **What to do** use an image that starts as root

- **`Required Linux process protection failed` / `Supervisor memory protection was not applied.` /
  `Supervisor memory protection unavailable.`**

  - **Meaning** the kernel refused a protection call (`prctl`)
  - **What to do** use another image or host; the guard refuses to run the workload

- **`The Pod guard could not become the reaper of its descendants.`**

  - **Meaning** the kernel refused the subreaper flag
  - **What to do** the same

- **`The Pod guard must run on Linux.` / `Only Secure Cloud is permitted.` /
  `Secure Cloud identity validation failed.`**

  - **Meaning** the Pod is not what the job asked for
  - **What to do** report it

- **`Missing platform-scoped credential.` (Pod log only)**

  - **Meaning** Runpod did not give the Pod its own key, so the guard cannot start
  - **What to do** report it; the watchdog deletes the Pod

- **`Expected one job spec in the Pod's environment; found <n>.` (Pod log only)**

  - **Meaning** the Pod's environment holds no job description, or more than one for different jobs
  - **What to do** report it; the watchdog deletes the Pod

- **`Filesystem does not enforce required ownership/mode. <path>: uid <n>, gid <n>, mode <m>; required uid <n>, gid <n>, mode <m>.`**

  - **Meaning** a folder the guard must protect ignores ownership or modes
  - **What to do** read the probe in the record; the Pod's own disk must support them

- **`The network volume needs a private parent folder.` /
  `The network volume is not mounted at <path>.` /
  `The network volume is also reachable at <path>; the lock would not hold.`**

  - **Meaning** the volume is not mounted as requested, or a second time elsewhere
  - **What to do** report it

- **`The workspace and the network volume must be separate folders.` /
  `Control directory is outside the job workspace.`**

  - **Meaning** an internal layout check
  - **What to do** report it

- **`Isolation path is a link, alias, or special file.` / `Isolation path changed while opening it.`
  / `Supervisor code must have an absolute private location.` / `Supervisor code is not root owned.`
  / `Supervisor module is not root owned.`**

  - **Meaning** a protected path is not what the guard expects
  - **What to do** report it

- **`Workload retains Linux capabilities.` / `Workload identity/drop verification failed.` /
  `Unsupported Linux capability range.` / `Capability clearing failed` /
  `Workload could access a protected supervisor resource.` / `Invalid supervisor identity.`**

  - **Meaning** the switch to the unprivileged user could not be proven
  - **What to do** the workload is not started; report it

- **`Workload isolation failed: <type>` (in `train.log`; the helper exits with code 126)**

  - **Meaning** the user switch or its checks failed
  - **What to do** read `guard-failure.json`

- **`Workload isolation checks did not pass.` / `Supervisor did not release isolated workload.` /
  `Supervisor closed workload launch IPC.` / `Invalid workload launch frame.` /
  `Workload launch data exceeds limit.`**

  - **Meaning** the launch handshake with the helper failed
  - **What to do** report it

- **`The input manifest on the volume is not the one the Pod request names.`**

  - **Meaning** the list of inputs on the volume (`control/inputs.json`) does not have the size and
    SHA-256 that the Pod request carries; the Pod stops before the workload (outcome `agent_failed`)
  - **What to do** retry; if it repeats, report it

- **`The input manifest on the volume does not belong to this job.` /
  `The input manifest has a malformed entry.`**

  - **Meaning** the list matches its hash but names another job, has another number of entries, or
    an entry is not a name, a size and a SHA-256; the Pod stops before the workload (outcome
    `agent_failed`)
  - **What to do** report it

- **`Input '<name>' was not copied and checked: <reason>`**

  - **Meaning** an input could not be copied from the volume, or did not match its hash
  - **What to do** retry; if it repeats, report it

- **`Input verification failed.`**

  - **Meaning** an input on the Pod's disk does not match its hash
  - **What to do** the same

- **`The Pod's input folder is a link or special file.` /
  `The Pod's input folder already holds files; only checked inputs may.`**

  - **Meaning** `/workspace/inputs` already existed in the image
  - **What to do** do not ship files at `/workspace/inputs` in the image

- **`Durable output volume is missing.` / `Pod does not advertise a terminate action.`**

  - **Meaning** the Pod's description lacks the mounted volume or the terminate action
  - **What to do** report it

- **`Controller did not release training within the startup limit.`**

  - **Meaning** only the controller (`launch` or `reattach`) releases the workload, and it did not:
    it was not running when the Pod became ready, or the Pod failed one of its checks
  - **What to do** keep `launch` running until the job ends; read the ledger

- **`No startup time remains.`**

  - **Meaning** the guard started after the startup limit
  - **What to do** raise `startup_timeout_seconds`

- **`Provider Date header unavailable; refusing local-clock fallback.` /
  `Provider clock unavailable; refusing local-wall-clock deadlines.`**

  - **Meaning** the Pod could not read Runpod's clock
  - **What to do** report it

- **`Container restart detected; preserve the first attempt and terminate.` /
  `Unclaimed outputs exist; preserve them for recovery.`**

  - **Meaning** the container restarted; the workload never runs twice
  - **What to do** the volume keeps the first attempt's files; use `recover` or `export-partial`

- **`PID-1 guard refused or lost the handshake.` / `PID-1 guard did not acknowledge readiness.` /
  `Guard closed scoped-key IPC.` / `Invalid scoped-key IPC.` / `Invalid scoped-key length.` /
  `Invalid supervisor handshake.` / `Supervisor heartbeat failed.` / `Cancellation requested.`**

  - **Meaning** the guard and its supervisor lost contact, or the guard stopped the job
  - **What to do** read the record; the job ends as `interrupted` or `agent_failed`

- **`Processes did not quiesce; no complete manifest is safe.` /
  `Process reaping requires the private container's PID 1 or the armed guard.`**

  - **Meaning** processes could not be stopped before the snapshot
  - **What to do** look for processes that ignore SIGKILL (stuck in the kernel)

- **`Missing regular result or log.` / `Invalid result identity.` /
  `Missing required successful-job artifact.`**

  - **Meaning** the snapshot found `result.json` or `train.log` missing or changed, or a required
    artifact missing
  - **What to do** write the required artifacts; leave `result.json` and `train.log` alone

- **`Reserved record is a link or special file.` / `Reserved log is a link or special file.` /
  `Output directory traverses a link or special file.`**

  - **Meaning** the workload replaced a reserved name with a link
  - **What to do** leave `result.json` and `train.log` alone

- **`Special output file cannot be snapshotted; retain source.` /
  `Separately mounted output cannot be snapshotted.` /
  `Hardlink leaves the output tree; never read its target.` /
  `Unsupported reparse point; retain source.`**

  - **Meaning** the outputs hold something that cannot be copied safely (see
    [the workload contract](WORKLOAD-CONTRACT.md#outputs))
  - **What to do** remove pipes, sockets, mounts and outside hard links from the outputs

- **`Output snapshot cutoff; retain raw outputs.`**

  - **Meaning** the time ran out while hashing or copying
  - **What to do** raise `finalize_grace_seconds`, or write smaller outputs

- **`Output tree changed during snapshot; retain source.` / `Output changed before content read.` /
  `Output changed during content read.` / `Output directory changed during inventory.` /
  `Hash worker did not quiesce before snapshot.`**

  - **Meaning** something kept writing during the snapshot
  - **What to do** make sure every process the workload starts ends with it

- **`Copy destination already exists; nothing is replaced.` /
  `Copy source differs from its recorded size and SHA-256.` / `Copy source is larger than recorded.`
  / `The volume already holds a manifest for this job.` /
  `The local manifest differs from the snapshot taken.` / `Bundle metadata and manifest disagree.` /
  `Unsafe copy directory.` / `Unsafe snapshot directory.` /
  `Snapshot directory must belong to the supervisor.` /
  `Bundle parent must be supervisor-owned and private.`**

  - **Meaning** copying the outputs to the volume failed a check
  - **What to do** the volume is kept; use `export-partial`

- **`Pod deletion not yet verified; retrying. External watchdog remains necessary.` (Pod log)**

  - **Meaning** the guard's self-delete is not confirmed yet; it retries every 15 seconds
  - **What to do** nothing; the watchdog also acts

- **`SELF CLEANUP UNVERIFIED: DELETE accepted, but absence verification denied. External verification is required.`**

  - **Meaning** the Pod's key could delete but not read; the watchdog must confirm
  - **What to do** the watchdog does this; check the closing lines

- **`SELF CLEANUP UNVERIFIED: authentication denied; stop fallback attempted. External cleanup is required; storage may remain billable.`**

  - **Meaning** the Pod's key was refused; the guard asked Runpod to stop the Pod
  - **What to do** the watchdog deletes the Pod; check the closing lines

- **`Self-cleanup refuses an ID other than RUNPOD_POD_ID.` /
  `Pod-scoped authentication denied a cleanup request.` /
  `Own-Pod DELETE has no explicit accepted HTTP response.`**

  - **Meaning** self-deletion could not proceed
  - **What to do** the watchdog deletes the Pod

- **`Guard failure: <type>` / `Supervisor failure: <type>` (Pod log)**

  **Meaning** the type of the failure; the details are in `guard-failure.json`

- **`Guard memory protection unavailable; refusing workload.` (Pod log)**

  **Meaning** see the protection messages above

- **`Process shutdown incomplete; terminating Pod with unfinalized outputs.` /
  `Artifact finalization incomplete; outcome retained for partial recovery.` /
  `Artifact finalization incomplete; retain network volume for recovery.` (Pod log)**

  - **Meaning** finalization did not complete
  - **What to do** the volume is kept; use `export-partial`

### Records the guard leaves on the volume

<!-- prettier-ignore -->
| Record | Field | Values |
| --- | --- | --- |
| `agent.json` | `phase` | `preflight`, `ready`, `training`, `finalizing`, `done` |
| `agent.json` | `cleanup_state` (from the guard, at the end) | `absence_verified`, `verification_denied`, `auth_denied` |
| `agent.json`, `control/*.json` | `actor` | `pod_guard`, `pod_supervisor` |
| `control/self-delete.json` | `state` | `starting`, `delete_attempted`, `delete_accepted`, `delete_failed`, `delete_response_unverified`, `delete_unverified`, `read_back`, `read_failed`, `inventory_failed`, `absence_unproven`, `absence_verified`, `verification_denied`, `auth_denied`, `stop_attempted`, `stop_accepted`, `stop_failed` |
| `control/self-delete.json` | `absence_basis` | `accepted_self_delete`, `owned_terminated` |
| `control/self-delete.json` | `request_outcome` | `unknown`, `accepted`, `rejected` |
| `control/finalization-incomplete.json` | `stage` | `outcome`, `quiesce`, `inventory_and_hashing`, `volume_copy`; `state` is `failed` or `complete` |
| `control/identity.json` | `source` | `RUNPOD_POD_ID` |

Two more records on the volume come from your computer: `control/inputs.json`, the input manifest
that `launch` writes (`job_id` and the list of `inputs`, each with `remote`, `size` and `sha256`),
and, after a `cancel` of a running workload, `cancel.json`, the watchdog's request to finish
(`job_id`, `pod_id`, `requested_utc`, `grace_seconds`).

## The event log

`events.jsonl` in the ledger gets one line per thing that happened to the job, from every process
that acts on it, and `podvigil logs` shows the latest ones. Each line is a JSON object with `utc`,
`event`, `pid`, `command` (`launch`, `watchdog`, `cancel`, ...) and the event's own fields. Every
value passes one redaction point: IDs stay readable, and other text is redacted like the API errors,
with this process's own credential values removed. Writing an event never stops a cleanup and never
creates a ledger.

<!-- prettier-ignore -->
| Event | Meaning |
| --- | --- |
| `job_created` | the ledger was made |
| `watchdog_started`, `watchdog_armed`, `watchdog_stopped` | a watchdog started; `launch` accepted it; it ended, with its last `state` |
| `create_requested` | a create request for the Pod or the volume (`kind`) is about to be sent |
| `created` | Runpod answered with the resource's ID |
| `create_refused` | Runpod refused the request (`http_status`) |
| `create_outcome_unknown` | the answer was lost or ambiguous |
| `inputs_uploaded` | the inputs are on the volume (`files`, `bytes`) |
| `workload_released` | the controller released the workload |
| `detached` | `launch --detach` handed the job to the watchdog |
| `cancel_requested` | a cancel was recorded (`source`: `cancel` for the command, `interrupted launch` for Ctrl+C on a running workload; `fence_closed`) |
| `asked_pod_to_finish` | the watchdog wrote the request to finish on the volume (`pod_id`, `grace_seconds`: how long it waits) |
| `monitoring_handed_off` | the watchdog stopped after a `hand-off` |
| `stopping_pod` | the watchdog is deleting the Pod (`reason`) |
| `pod_finished` | the Pod ended by itself (`source`: `tombstone` or `self_delete`); after `asked_pod_to_finish` this is the graceful end of a cancel |
| `stopping_without_pod` | a stop with no Pod to delete (`reason`) |
| `pod_deletion_verified`, `volume_deletion_verified` | a verified deletion; the volume's event names the audit's `mode` (`audit`) and its number of `objects`. `pod_deletion_verified` can appear twice for one Pod: each check of the deletion is recorded |
| `recovered` | recovery finished (`training_outcome`, which can also be `outputs_lost` or `partial_outputs`) |
| `recovery_failed` | recovery failed; the volume is kept (`partial_export` says whether one was made) |
| `partial_export` | a partial export was made (`files`, `failed`) |
| `launch_failed` | `launch` failed (`phase`, the error) |

## Other records in the ledger

- **`pod-creation-state.json`, `volume-creation-state.json`**

  - **Field** `state`
  - **Values and meaning** `never_dispatched` (no request sent), `dispatching` (a request was being
    sent)

- **`pod-dispatch.json`, `volume-dispatch.json`**

  - **Field** `state`
  - **Values and meaning** `dispatching`

- **`pod-receipt.json`, `volume-receipt.json`**

  - **Field** `established_by`
  - **Values and meaning** `create_response` (from Runpod's answer), `reconciled` (found later by
    name and job ID), `self_deletion_observed` (a Pod that deleted itself before it was seen; never
    grants deletion)

- **`pod-audit-observation.json`, `volume-audit-observation.json`**

  - **Field** `state`
  - **Values and meaning** `identity_resolved`, `inconclusive`, `unknown`

- **`beacon-observation.json`, `startup-observation.json`**

  - **Field** `state`
  - **Values and meaning** `unknown`: a read failed; nothing was concluded

- **`pod-cleanup-attempt.json`**

  - **Field** `outcome`
  - **Values and meaning** `accepted` or `unverified`; `phase` is `startup_visibility_guard` or
    `sampled_owned_pod`

- **`terminal-observed.json`**

  - **Field** `source`
  - **Values and meaning** `owned_tombstone` (Runpod listed the Pod as TERMINATED) or
    `accepted_self_delete`

- **`pod-deleted.json`, `volume-deleted.json`**

  - **Field** evidence
  - **Values and meaning** `delete_attempts[].outcome`: `accepted`, `uncertain`, `not_found`,
    `error`; `observations[].type`: `point_read`, `inventory`, `api_error`; `observations[].state`:
    `present`, `absent`, `terminated`; an inventory's `source`: `confirm_absent` or `api.listing`;
    `actor`: `local_controller_or_watchdog` (in the Pod's own records: `pod_guard`);
    `deleted_by: unknown` when the volume was found already gone

- **`volume-audit.json`**

  - **Field** `mode`
  - **Values and meaning** `verified_backup` (after a complete download), `pristine_inputs` (no Pod
    ever ran), `unreleased_pod` (the Pod stopped before the workload was released),
    `released_unpublished` (the workload ran but never published anything; outcome `outputs_lost`),
    `partial_outputs_copied` (`recover --accept-partial` copied every object); `objects[].kind`:
    `artifact`, `input`, `input_manifest` (the list of inputs on the volume), `archived_original`,
    `control`, `partial_output` (an object that `recover --accept-partial` copied as part of the
    incomplete outputs), `directory_marker`; `control_backup_directory`: the folder with the copies

- **`self-termination-probe.json`**

  - **Field** `passed`, `local_fallback`
  - **Values and meaning** whether the Pod deleted itself without the watchdog's help

- **`watchdog-isolation.json`**

  - **Field** `credentials`
  - **Values and meaning** `checked`, `same`, and the names that `different`

- **`operator-handoff.json`**

  - **Field** `active`
  - **Values and meaning** `true` after `hand-off`, `false` after `resume-monitoring`; `next` says
    "Operator owns reconciliation; use resume-monitoring or recover with this ledger."

- **`partial-export.json`, `unresolved-export.json`**

  - **Field** `complete`
  - **Values and meaning** always `false`; `unresolved-export.json` also says "Snapshot may race an
    unobserved writer; it never authorizes deletion. Watchdog remains active."

- **`partial-downloads/.../objects.jsonl`, `unresolved-downloads/.../objects.jsonl`**

  - **Field** `status`
  - **Values and meaning** `pending` (before a copy), `saved`, `failed`, `skipped_symlink` (a link;
    its target is never read), `listing_failed` (the listing broke off)

- **`downloads/volume-audit-*/objects.json`, `partial-downloads/volume-audit-*/objects.json`**

  - **Field** `objects`
  - **Values and meaning** the audit's list of every object on the volume before its deletion:
    `key`, `kind` (as in `volume-audit.json`), `size`, `sha256`, and `local_path` for the objects
    with a local copy or source; the second folder comes from `recover --accept-partial`

- **`pod-audit-observation.json`, `volume-audit-observation.json`**

  - **Field** `message`
  - **Values and meaning** "No complete matching create identity; absence was not inferred."

- **`watchdog.json`**

  - **Field** `reason`
  - **Values and meaning** a stop reason (above), or "Ambiguous creation; no repeat POST or assumed
    absence.", or "terminal status and absence"

- **`watchdog-arm-*.json`, `*.launch.json`, `*.accepted`, `*.cancel`, `*.unseen.json`,
  `watchdog-launch-error.json`, `watchdog-resume-request.json`, `watchdog-resume-ack.json`**

  **Values and meaning** the watchdog's start-up and resume handshakes; `*.unseen.json` records
  where a watchdog looked when it could not see the ledger

- **`controller-heartbeat.json`**

  - **Field** `epoch`
  - **Values and meaning** the controller's heartbeat, every 5 seconds

- **`cancel-request.json`**

  - **Field** `utc`, `pid`, `source`
  - **Values and meaning** when a cancel was recorded, by which process, and how: `cancel` (the
    command) or `interrupted launch` (Ctrl+C on a running workload)

- **`cancel-grace.json`**

  - **Field** `until_epoch`, `asked`, `pod_id`
  - **Values and meaning** after a cancel of a running workload: until when the watchdog waits for
    the Pod to finish by itself; `asked: false` and no time when the request could not be written to
    the volume, so the Pod was deleted at once

- **`volume-kept.json`**

  - **Field** `reason`
  - **Values and meaning** why `recover` kept the volume of a job whose workload ran but published
    nothing: the error type and message

- **`launch-failure.json`**

  - **Field** `handed_to_watchdog`
  - **Values and meaning** `true` when Ctrl+C on a running workload was handed to the watchdog as a
    cancel

- **`events.jsonl`, `events.lock`**

  **Values and meaning** the event log (above), and the lock that keeps two writers from mixing
  their lines

- **`detached.json`**

  - **Field** `controller_pid`, `watchdog_pid`, `deadline_epoch`
  - **Values and meaning** when `launch --detach` handed the job to the watchdog

- **`*.lock`**

  **Values and meaning** operating-system locks; never delete them while a command runs

## Messages that indicate a bug

These guard the code's own assumptions. If you see one, keep the ledger and report it:
`Only SECURE cloud may cross the Pod creation boundary.`,
`A persistent ownership ledger is required before creation.`,
`A launch needs at least one candidate configuration.`, `Unknown resource kind.`,
`Resume acknowledgement timeout must be between zero and 120 seconds.`,
`A credential check needs a credential_proof function.`,
`Watchdog command must use the current Python environment`, `Unknown Runpod S3 data center.`,
`Invalid job ID for artifact storage.`, `Volume audit requires a valid job identity.`,
`Volume audit job configuration is malformed.`, `Volume audit identity does not match the job.`,
`Pristine input authorization must be a boolean.`,
`Unreleased-Pod authorization must be a boolean used with pristine inputs.`,
`Completed-job volume deletion requires a verified backup directory.`,
`Unreleased-Pod volume deletion requires a backup directory for the guard's records.`,
`Released-Pod volume deletion needs the Pod's ID, pristine inputs and a backup directory for the Pod's records.`,
`Copying every object needs pristine inputs and a backup directory, and no other mode.`,
`Original input ledger is malformed.`, `Original input ledger entry is malformed.`,
`Duplicate original input ledger entry.`, `Configured input sources are malformed.`,
`Clock observation does not match the owned Pod; refusing deletion.`,
`Inventory confirmation must return a boolean.`, `Unsupported API origin.`,
`List path must be a relative API path without a fragment.`,
`One account/origin must use one pacing state path`,
`Pacing deadline must be a finite monotonic time`,
`Pacing max_wait must be finite and between 0 and 60 seconds`,
`Pacing max_wait must be between 0 and 60 seconds`, `read_retries must be between 0 and 3`,
`Unsafe job identifier.`, `Quiesce all writers before finalization.`,
`Wrong output hash cache or worker is not stopped.`,
`Hash worker already started or invalid interval.`,
`Only a complete manifest of this job is published.`, `Invalid snapshot path.`,
`Snapshot paths must be relative without traversal.`, `Linked source cannot be opened as content.`,
`Snapshot metadata too large.`, `Duplicate snapshot metadata member.`,
`Invalid bundle metadata identity/schema.`, `Invalid filesystem metadata.`,
`Invalid ownership metadata.`, `Invalid root metadata.`, `Invalid bundle entry type.`,
`Invalid bundle entry schema.`, `Duplicate bundle path.`, `Invalid bundle content checksum.`,
`Invalid/duplicate opaque archive member.`, `Invalid link text.`,
`Bundle path crosses a missing directory or link.`, `Invalid hardlink identity/content.`,
`Checkpoint source must be a private regular file.`, `Unreachable request retry state`,
`Pod allocation/price has not been confirmed; workload cannot start.` (handled inside the runner: it
keeps polling).

Handled inside the runner and never shown: the rate-limit header checks (`Invalid quota header`,
`Invalid quoted escape`, `Invalid quota members`, `Invalid quota number`,
`Invalid zero quota window`, `Duplicate or invalid quota parameter`,
`Invalid quota parameter value`, `Repeated quota policy`, `Repeated quota window`,
`Inconsistent quota`, `Timestamp requires timezone`), the pacing file checks
(`Oversized pacing state`, `Pacing state belongs to a different credential/origin`,
`Invalid pacing state`, `Invalid pacing window`, `Invalid pacing numbers`; they surface as the
"pacing file in the state folder is unusable (ValueError)" message above),
`Object exceeds its listed size.`, `Partial export local verification failed.`,
`Hash worker stopped.`, and the watchdog's internal reasons `Pod ownership no longer matches`,
`Non-Secure Pod returned` and `Pod price invalid/exceeds ceiling` (they surface as the stop reason
"Pod cloud/identity/price validation failed").

In code the runner does not call today: `Unsafe output filename.`, `Reserved output filename.`,
`Linked or separately mounted output is unsafe.`,
`Output filenames collide on the backup filesystem.`,
`Output must contain only directories and unlinked regular files.`
