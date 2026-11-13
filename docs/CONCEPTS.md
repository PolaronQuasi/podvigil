# Concepts

This page explains the four ideas the runner is built on: the ledger, the three guards, the one
deadline, and the evidence. Read it once before you run long or expensive jobs.

## The words used here

- **Pod**: a Runpod machine slot with one or more GPUs, running one container. Billed per second
  while it exists.
- **Network volume**: Runpod storage that lives apart from any Pod. It keeps billing until it is
  deleted. The runner creates a new one for every job and deletes it at the end.
- **Secure Cloud**: Runpod's data-center tier. The runner uses nothing else. It never falls back to
  Community Cloud.
- **S3 API**: Runpod's Amazon-S3-compatible access to network volumes. The runner uploads inputs and
  downloads results through it, with your S3 keys. It exists only in some data centers.
- **Job file**: the JSON file that describes one job. See [the job file reference](JOB-FILE.md).
- **Workload**: your command, as it runs inside the Pod.
- **Controller**: the `podvigil launch` process on your computer.

## How a job runs

1. **Checks.** `launch` validates the job file, makes sure it has the S3 keys and the `boto3`
   library, reads the live price and capacity from Runpod's catalog, and records which Pods and
   volumes already exist in the account. Those are never touched.
2. **Ledger.** It creates the job's ledger folder and prints its path (`Job ledger: ...`).
3. **Watchdog.** It starts the watchdog and waits until the watchdog proves that it runs on its own
   and found the same credentials.
4. **Volume.** It creates one network volume, checks that it can write and read it over S3, and
   uploads your inputs. Then it writes the list of inputs, with each file's size and SHA-256 (the
   input manifest), to the volume and reads it back by its hash.
5. **Pod.** It checks capacity and price again, fixes the deadline, and creates one Pod. The Pod
   request names the input manifest by its size and SHA-256.
6. **In the Pod.** The Pod guard starts, locks the volume away from the workload, checks the input
   manifest against the Pod request, copies your inputs to the Pod's own disk, checks their hashes
   and the Pod's identity and cloud, and reports "ready". The controller checks the Pod's data
   center, GPU and price, and only then releases the workload. The workload runs once.
7. **Finishing.** When the workload ends, its time is up, or the job is cancelled, the guard stops
   it, hashes every output file, copies the outputs and their list (the manifest) to the volume, and
   deletes its own Pod.
8. **Recovery.** On your computer, the controller and the watchdog confirm that the Pod is gone,
   download the results, check each file against the manifest, check every object on the volume,
   delete the volume, confirm that it is gone, and write `summary.json`.

If anything fails along the way, the same path runs from where it stopped: the Pod is deleted, and
the volume is deleted only on one of the grounds under [the evidence](#the-evidence).

## The ledger

The ledger is one folder per job on your computer: `<state folder>/jobs/<job id>`. The job ID is a
random 32-character hexadecimal name. The state folder is `%LOCALAPPDATA%\podvigil` on Windows,
`~/.local/state/podvigil` on Linux and `~/Library/Application Support/podvigil` on macOS, unless you
choose another one (see [the command line reference](MANUAL.md#where-the-runner-keeps-things)).

The ledger is the runner's memory, and its authority. The runner deletes a Pod or a volume only when
this job's ledger records that this job created it. Every job command (`status`, `logs`, `recover`,
and the others) takes the ledger folder as its argument, or `latest`, or the start of the job ID;
`podvigil list` shows the jobs in the state folder. Keep ledgers until you no longer need the
results; the runner never deletes them.

Main files:

<!-- prettier-ignore -->
| File | Written when | What it holds |
| --- | --- | --- |
| `job.json` | before anything is created | the validated job, the resource names, the Pods and volumes that existed before, the input hashes, the price quote, the deadlines, and, once written, the input manifest's size and SHA-256 |
| `pod-intent.json`, `volume-intent.json` | just before a create request | what is about to be created |
| `pod-dispatch.json`, `volume-dispatch.json` | just before the request is sent | that a request went out; without it, none did |
| `pod-receipt.json`, `volume-receipt.json` | when the ID is known | the resource ID: the authority to delete it |
| `pod-rejected.json`, `volume-rejected.json` | when Runpod refused a create request | the HTTP status; nothing was created |
| `training-released.json` | when the workload was allowed to start | the time |
| `stop-request.json` | when cleanup was requested | the reason; no new resource may be created after it |
| `cancel-request.json`, `cancel-grace.json` | when the job was cancelled | who asked, and until when the watchdog waits for the Pod to finish |
| `pod-deleted.json`, `volume-deleted.json` | after a verified deletion | the evidence (see below) |
| `downloads/` | during recovery | the results, the manifest and the receipt |
| `summary.json` | at the very end | the outcome and both verified deletions |
| `launch-failure.json` | when `launch` fails | the phase, the error type, a redacted message, the HTTP status |
| `watchdog.json`, `watchdog.log` | while the watchdog runs | its state, and one line per event |
| `events.jsonl` | by every process that acts on the job | one JSON line per event, redacted: the job's story |

[Troubleshooting](OPERATIONS.md) lists every file and state a ledger can hold.

Ledgers from versions before 0.1.0 live under `output/jobs` in a checkout. They stay readable: give
their path to any job command.

## The three guards

Three separate processes can end the job, so that no single failure leaves a Pod running:

<!-- prettier-ignore -->
| Guard | Where | What it does |
| --- | --- | --- |
| Controller | the `launch` process on your computer | creates the resources, releases the workload, watches the deadline, recovers the results |
| Watchdog | a separate process on your computer | started before anything is created; survives the controller; deletes the Pod at the deadline or when the job is over; recovers the results and deletes the volume |
| Pod guard | the command the runner gives the Pod's container, with a child process (the supervisor) that runs the workload | stops the workload in time, copies the outputs to the volume, and deletes its own Pod with the key Runpod gives each Pod |

- The watchdog runs detached from the controller. On Windows it is started through Windows'
  management service (WMI), outside the controller's process tree, so that closing the terminal does
  not kill it. On Linux it runs in its own session.
- A dead controller does not by itself stop a healthy job: the watchdog and the Pod guard keep the
  original deadline. Only a controller releases the workload, though: if `launch` dies before that,
  the job ends at its startup limit unless `reattach` takes over in time.
- `launch --detach` uses this on purpose: it returns once the workload runs and the watchdog has
  shown that it guards the job. From then on two guards, the watchdog and the Pod guard, keep the
  same deadline.
- The Pod guard is the strongest guard: it needs neither your computer nor your account key. On 12
  November 2026 it deleted its own Pod on Runpod (DELETE answered 204; the next read answered 401).
  If its key is refused, it falls back to Runpod's "stop" action, which according to Runpod's
  documentation ends compute billing but not storage billing, and it records that the deletion is
  not verified.
- Runpod starts the container command under its own init process, so the guard is not process 1
  there. It registers as the reaper of its own descendants instead and collects every process the
  workload leaves behind.

## The deadline

There is one deadline per job, fixed once and never extended.

- It is set just before the Pod is requested, from Runpod's clock (the `Date` header of its API
  answers), not your computer's clock.
- Its length is the smaller of `max_runtime_seconds` and the time the compute budget buys at the
  price ceiling: `max_compute_usd / max_compute_usd_per_hour` hours.
- It covers everything the Pod does: starting, copying inputs, the workload, copying outputs.
- The workload is stopped `finalize_grace_seconds` before the deadline, so that the outputs can be
  hashed and copied in the time that is left.
- The workload must be released within `startup_timeout_seconds` of the Pod request.
- Before the Pod request, a separate preparation limit applies (`preparation_timeout_seconds`, 30
  minutes by default, counted from the ledger's creation). After it, nothing new may be created.

The Pod guard measures the deadline with the Pod's own monotonic clock, anchored to Runpod's time.
The watchdog and the controller do the same on your computer. Whichever acts first ends the Pod.

## The evidence

The runner says "deleted" only with evidence, and keeps storage when it is unsure.

- **A verified deletion** is a deletion request (or a resource found already gone), followed by two
  reads at least one second apart that show the resource absent (or, for a Pod, terminated), and a
  complete listing of the account's Pods or volumes without it. Runpod accepting a DELETE is not
  enough. The evidence goes to `pod-deleted.json` and `volume-deleted.json`.
- **Verified results** start in the Pod: the guard writes a manifest that lists every output file
  with its size and SHA-256. After download, the runner checks every file against it and writes the
  receipt `downloads/backup-verified.json`, which binds the job, the volume and the manifest's hash.
- **The volume audit** runs before the volume is deleted. It lists every object on the volume and
  accounts for each one: a result in the manifest, an unchanged input, the unchanged input manifest,
  or a known control record. Anything else (an unknown file, a changed input, a missing manifest
  entry) keeps the volume, which then keeps billing as storage, and names the objects.
  `volume-audit.json` holds the audit; objects without a local copy are copied to
  `downloads/volume-audit-*/` first.
- **Honest closing lines.** Every launch and job command ends with two lines that state what the
  ledger proves about the Pod and the volume. See
  [troubleshooting](OPERATIONS.md#the-closing-lines).

The runner deletes a job's volume on one of four grounds, each after a verified deletion of the Pod
(or proof that no Pod was created) and an audit of every object on the volume:

1. **The results are recovered.** Every result was downloaded and matched the manifest, and nothing
   else on the volume is unaccounted for. This is the normal end of a job.
2. **Nothing ran.** The workload was never released, and the volume holds only your unchanged
   inputs, the input manifest, and the runner's and the guard's own records. The outcome is
   `not_started`.
3. **Nothing was published.** The workload was released, but the Pod is gone and published nothing:
   its outputs were on its own disk and are lost with it. The volume may be deleted only when it
   holds nothing but your unchanged inputs, the unchanged input manifest, the volume's owner record,
   and the records that the release, a cancel request and this job's Pod write, each checked to be
   exactly as written for this job and this Pod; the Pod's own status record must be among them. The
   records are copied to the ledger first. The outcome is `outputs_lost`, and the command says
   "OUTPUTS LOST". Any other object keeps the volume, and `volume-kept.json` says why.
4. **You accepted partial outputs.** The outputs are incomplete, and you ran
   `recover --accept-partial`. Every object on the volume was read once, copied to the ledger, and
   the copy checked against the bytes read; the inputs and the input manifest still match their
   originals, and the owner record names the job; and the listing did not change during the copy. An
   object that the outputs' link index names is never read and keeps the volume. The outcome is
   `partial_outputs`. No automatic path takes this step: only the command with the option does.

Grounds 3 and 4 are new in this version. They are tested locally against fakes of Runpod's API and
S3, and have not yet run on Runpod itself. See
[the safety model](SAFETY-MODEL.md#what-has-been-checked-and-how).

## Stopping a job early

`podvigil cancel` stops a job early. It deletes nothing itself: it writes a stop request, and the
job's watchdog acts on it.

- **Before the workload is released**, the watchdog deletes the Pod at once, as at the deadline, and
  handles the volume.
- **Once the workload runs**, the watchdog first asks the Pod to finish: it writes a request,
  `cancel.json`, to the job's volume. While the workload runs, the Pod looks for it every 2 seconds;
  when it finds it, it stops the workload as at its own time limit, copies the outputs to the volume
  with the workload's outcome `cancelled`, and deletes itself. The watchdog then confirms that the
  Pod is gone, recovers and checks the results, and deletes the volume.
- **The wait is bounded.** The watchdog waits at most `finalize_grace_seconds` plus 30 seconds, and
  never past the deadline; a restarted watchdog never waits longer. The Pod keeps billing while it
  finishes. If the Pod has not ended by then, or the request could not be written, the watchdog
  deletes the Pod as before, and outputs the Pod had not copied to the volume are lost.

Ctrl+C in a foreground `launch` does the same once the workload runs and the job's watchdog is
running: `launch` records the cancel and exits, and the watchdog does the rest. Before the release,
or without a running watchdog, Ctrl+C makes `launch` clean up at once: it deletes the Pod, if one
exists, without waiting for the workload. `recover` on a running job, `export-partial`, and the
watchdog's own checks (a frozen supervisor, a wrong price, a Pod that stopped or failed) also delete
the Pod at once.

On 12 November 2026 one cancel of a running job on Runpod went this way: the Pod stopped the
workload within 2 seconds of the request reaching the volume, published its outputs with the outcome
`cancelled` and deleted itself, the watchdog recovered and checked the results, and 25 seconds after
the cancel nothing of the job remained. A cancel whose copy takes longer than the wait, and Ctrl+C
after the release, have been tested only locally, against fakes.

## Single attempts and ambiguous answers

Every create request is sent once. If Runpod refuses it with a definite answer (an HTTP status such
as 400 or 403), nothing was created and the ledger records the refusal. If the answer is ambiguous
(a timeout, a server error, a lost connection after sending), the runner cannot know whether a
resource was created, because Runpod offers no way to make a create request safe to repeat. It then
never sends the request again. Instead the watchdog keeps looking for a resource with the job's
unique name and job ID, and later in Runpod's audit log. Until it finds one, or you resolve it with
`resolve-creation`, the job reports `creation_unresolved` and then `operator_resolution_needed`.

## Launch candidates

A job file can list up to eight GPU and data-center pairs. `launch` tries them in order. It moves on
only after a definite "no capacity" answer, and only after the failed attempt's ledger shows that no
Pod exists and that its volume deletion was verified. Each attempt is its own job, with its own
ledger and watchdog. Any other error stops the launch. See
[the job file reference](JOB-FILE.md#launch-candidates).

## The pacing file

Runpod limits how many API requests an account may make. The runner tracks the limits it is told
about in a pacing file in the state folder, one per API key:
`<state folder>/api-rate-limit-<fingerprint>.json`. The fingerprint is a short one-way hash of the
key; the key itself is never written. Every controller and watchdog that uses the same state folder
and the same key shares that file. Teardown requests get reserved room, and still go through when
the file cannot be used. Two projects on one computer share the pacing only when they use the same
state folder and the same key; a new key starts with fresh pacing.
