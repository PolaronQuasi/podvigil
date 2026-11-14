# Safety model

The runner is built so that a job leaves nothing billing in your Runpod account, and so that it
tells you plainly when it cannot prove that. Its deadline is enforced by the runner, not by Runpod:
**it is not a provider-enforced spending cap.**

This page separates what the runner makes sure of, by design and checked by tests, from what it
cannot make sure of. "Tested" means the project's local tests, which run against fakes of Runpod's
API and storage. "Observed" means seen on Runpod itself; so far that is a handful of short jobs on
12 November 2026.

## What the runner makes sure of

**Secure Cloud only.** The job file must say Secure Cloud (or leave the default). The Pod request
carries `cloud: "SECURE"`, the returned Pod is checked, and the guard checks again inside the Pod. A
missing Secure Cloud capacity ends the round; optional capacity waiting remains Secure Cloud only.
There is no Community Cloud fallback. This rule is also structural: the network volumes the runner
needs exist only in Secure Cloud.

**One deadline, three guards.** The deadline is fixed once, from Runpod's clock, just before the Pod
is requested, and never extended. The Pod guard stops the workload in time to copy its outputs and
deletes its own Pod by the deadline; the watchdog and the controller on your computer delete the Pod
at the deadline if it still exists. Each of the three can end the Pod without the others. After
`launch --detach`, or when the controller dies, the other two remain. See
[concepts](CONCEPTS.md#the-three-guards).

**Read-only waiting.** `wait` reads the existing ledger, including the shared status phases and
watchdog heartbeat. It does not supervise, cancel or recover the job. Timeout and Ctrl+C stop the
reader only. A stale watchdog stops waiting and reports what the ledger says remains; it is not
proof that the watchdog process or billing ended.

**Proven-empty capacity retries.** With `--wait-for-capacity`, another round requires the existing
definite-no-capacity classification and proof that no Pod was created and no volume remains. The
last candidate must pass that check too. Catalog-only polling avoids repeated allocations and
uploads while no capacity is advertised. Each actual attempt keeps its ledger, rechecks limits, and
gets a deadline from its own start. Missing evidence, ambiguous creates and other errors stop the
command.

**Only its own resources.** The runner deletes a Pod or a volume only when the job's ledger records
that this job created it: from Runpod's answer to the create request, or found later by the job's
unique name together with its job ID (for a Pod) or its data center (for a volume), and never
anything that existed before the job started. There is no command that deletes resources by name
pattern or across the account.

**No repeated creation.** Each create request is sent once. When the answer is lost or ambiguous,
the runner does not send it again, because a second request could create a second, unseen, billed
resource. It reconciles instead, and says so until it is resolved.

**Deletion is proven, not assumed.** "Deleted" means: a deletion request (or the resource found
gone), then two reads at least a second apart showing it absent (or, for a Pod, terminated), and a
complete listing of the account without it. A DELETE that Runpod accepted is not counted as proof.

**Results before storage.** The volume is deleted only after a verified deletion of the Pod (or
proof that none was created) and an audit of every object on the volume, and only on one of these
grounds (see [concepts](CONCEPTS.md#the-evidence)):

- every result file was downloaded and matched its SHA-256 from the manifest made in the Pod, and
  the audit found nothing else it cannot account for;
- the workload never ran, and the volume holds only your unchanged inputs, the unchanged list of
  inputs, and the runner's and the guard's own records;
- the workload ran but published nothing, and the volume holds only your unchanged inputs, the
  unchanged list of inputs, and records that the release, a cancel request and this job's Pod write,
  each checked against this job and this Pod. The outputs were on the Pod's own disk and are lost;
  the runner copies the records to the ledger and says "OUTPUTS LOST" (outcome `outputs_lost`);
- you asked for it with `recover --accept-partial` after the outputs were found incomplete, and
  every object on the volume was copied to the ledger and each copy checked against the bytes read
  (outcome `partial_outputs`). No automatic path does this.

Otherwise the volume is kept, it keeps billing as storage, and the runner says so and names the
objects, or saves the reason in `volume-kept.json`.

**A cancel lets the workload finish, within limits.** `cancel`, or Ctrl+C on a foreground `launch`
with a running watchdog, does not delete a running workload's Pod at once. The watchdog asks the
Pod, through a request on the volume, to stop the workload as at its time limit, copy its outputs,
and delete itself. It waits at most `finalize_grace_seconds` plus 30 seconds, never past the
deadline, and a restarted watchdog never waits longer. The Pod keeps billing during that time. Then
the watchdog deletes the Pod if it still exists and checks its absence, as at the deadline. The
checks for a frozen or failed Pod still apply while it waits, and when the request cannot be written
to the volume, the Pod is deleted at once. Before the workload is released, a cancel deletes the Pod
at once.

**Keys stay where they belong.** Your account API key and your S3 keys stay on your computer. The
Pod uses the key Runpod creates for that Pod; the guard passes it to its supervisor over a private
channel, not through the environment, and the workload never receives it. The watchdog proves at
start, with salted hashes and never the values, that it found the same credentials as the
controller; `launch` refuses to create anything otherwise.

**The workload is contained, within limits.** It runs as an unprivileged user with no Linux
capabilities. It cannot read the guard's code, records or memory, and it cannot reach the network
volume, which is locked below a folder only root may enter. Its environment is a short allow-list.

**Honest last lines.** Every launch and every job command ends with two lines that state what the
ledger proves about the Pod and the volume. When something may still bill, the line says so and
names the command to run, in the form you used to run the runner.

## What the runner cannot make sure of

**Billing can continue when the runner cannot act.** For example:

- Runpod's API is down or unreachable, or rejects the runner's requests, while the deadline passes;
- your API key is revoked or lacks a permission it needs;
- the Pod's own key is refused, so the guard falls back to Runpod's "stop" action, which according
  to Runpod's documentation keeps storage billed, and your computer is off;
- the whole Pod or its host fails while your computer is off.

In those cases a Pod can keep running past its deadline, and a volume can stay, until the runner
gets through or someone deletes them in the Runpod console.

**Your computer must stay on and online.** The volume is deleted, and the results downloaded, by the
controller or the watchdog on your computer. If your computer sleeps or shuts down, the Pod guard
still ends the Pod by its deadline (when its key works), but the volume stays and bills as storage
until you run `recover`. The watchdog does not survive a restart; `reattach` or `recover` take over
after one.

**An ambiguous creation can leave something unseen.** Runpod offers no way to make a create request
safe to repeat, and no promise of when a new resource becomes visible. After a lost answer the
watchdog keeps looking, then reports `operator_resolution_needed`. You may have to check the console
yourself.

**Outputs can be lost.** The workload's outputs stay on the Pod's own disk until it ends, because
the network volume keeps no ownership or permissions. A Pod that dies hard loses every output not
yet copied, and so does a running job whose Pod is deleted from outside at once (`recover`, the
watchdog's checks for a frozen or failed Pod). A cancel asks the Pod to copy its outputs first, but
if the copy takes longer than the wait, the Pod is deleted and what was not copied is lost. If the
copy at the end runs out of time, the volume is kept with partial files. See
[the workload contract](WORKLOAD-CONTRACT.md#the-cost-of-the-local-disk-design).

**Capacity is never promised.** A launch often ends with "no capacity", even when the catalog said
"LOW" a moment earlier. The runner tries the next candidate only after a definite no-capacity answer
and verified absence. Optional capacity rounds do not reserve a GPU. A positive catalog read can
still be followed by a refusal after a volume was created and inputs uploaded.

**The container is trusted.** The workload's isolation is ordinary Linux user separation inside the
container image you choose. It is not a virtual machine. The image, the Linux kernel and Runpod are
part of what you trust. Code you do not trust should not run with this tool.

**Costs are estimates.** The price ceiling is checked against Runpod's catalog and against the price
the Pod reports. `status` and `cost` show an estimate: the quoted price times the Pod's lifetime.
Storage and taxes are not included, and Runpod bills what it bills.

**Runpod can change.** The runner was built against Runpod's API as it was documented and observed
in November 2026. A change in wording, timing or behaviour can make it stop where it went on before.
Where it can tell, it then stops on the safe side: it keeps storage and reports.

## What has been checked, and how

<!-- prettier-ignore -->
| Check | Status |
| --- | --- |
| Lifecycle, ownership, deletion evidence, volume audit, candidate fallback, failure paths | tested locally against fakes of Runpod's API and S3, on Windows and Linux |
| `wait`: local-only reads, foreground outcomes, terminal phases, stale watchdog, timeout and Ctrl+C | tested locally with simulated ledger transitions; no live job run for this feature |
| Bounded capacity rounds: catalog-only polling, verified cleanup, fresh price and budget checks, refusal to repeat ambiguous creates | tested locally against fake catalog, API and S3 responses; no live job run for this feature |
| The detached Windows watchdog surviving a killed controller | tested locally on Windows |
| The Pod guard, as process 1 and under an injected init, with the workload as another user | tested locally in network-less Linux containers |
| A volume that ignores `chmod` and refuses `chown` | tested locally with a model of the real volume |
| The Pod's key deleting its own Pod | observed: DELETE answered 204, the next read 401 |
| The Pod guard under Runpod's injected init | observed |
| The network volume ignoring modes and refusing ownership changes | observed and measured (MooseFS through FUSE) |
| The first real jobs, a few minutes each, with verified deletion of Pod and volume and hash-checked results | observed |
| A launch from a shell inside a packaged Windows app without `--state-dir`: the ledger in the app's private copy of the per-user folder, the watchdog armed and guarding the job | observed once |
| Candidate fallback: the first candidate had no capacity, nothing was created, the second was launched | observed once |
| The input list as a manifest on the volume, read by the Pod | observed once; 300 inputs through the real Pod code in a container and 600 end to end against fakes tested locally only |
| `launch --detach`: outcome `detached` (exit code 9) 21 seconds after the Pod was created; `status`, `logs`, `cost` and `list` on the running job | observed once |
| A graceful `cancel` of a running job: the Pod stopped the workload within 2 seconds of the request reaching the volume, published its outputs with outcome `cancelled` and deleted itself; the watchdog sent no delete request, verified the absence, recovered the checked results and deleted the volume, 25 seconds after the cancel; `cancel` exited with 0 | observed once |
| A cancel whose copy takes longer than the wait; Ctrl+C on `launch` after the release | tested locally against fakes only |
| Deleting the volume of a job that published nothing (`outputs_lost`) | tested locally against fakes only |
| `recover --accept-partial` | tested locally against fakes only |
| Long jobs, long detached runs, a Pod dying hard, a provider outage, real copy speeds | not yet observed |
| The three example jobs exactly as written | not yet run on Runpod |
| A key with only the permissions in [SECURITY.md](../SECURITY.md) | not yet tested |
| Linux as the controller's system for a real job | not yet observed (the local suite passes on Linux) |
| macOS as the controller's system | Apple Silicon and Intel CI cover fake services, local processes and installed wheels; no real cloud launch observed. See the intended commit's [CI results](https://github.com/PolaronQuasi/podvigil/actions/workflows/ci.yml). |

## Fixed rules

These follow from the model. They are not settings:

- no Community Cloud, and no fallback to it;
- no repeat of an ambiguous create request;
- no deletion of anything the ledger does not record as this job's;
- no deletion of a volume before every object on it is accounted for or, after
  `recover --accept-partial`, copied to the ledger and checked;
- no silent extension of a deadline;
- no SSH or Jupyter in the Pod;
- the watchdog is required, on every system.
