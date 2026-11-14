# How it compares

If you want a hard stop on spending with the least effort, a platform that enforces the stop itself,
such as Runpod Serverless or Modal, is safer by construction than this runner. This runner's niche
is narrower: plain Runpod Pods, any image and any command, evidence that nothing is left billing,
and fixed rules that make it safe to hand to a coding agent.

Claims about other tools come from an analysis written on 12 November 2026. Those marked **(not
verified)** were read on the web and not checked by this project; check the tool's own documentation
before you rely on them.

<!-- prettier-ignore -->
| Alternative | Better than this runner | Worse than this runner |
| --- | --- | --- |
| `runpodctl` or the Runpod Python SDK, with your own scripts | works without extra tooling; interactive use | no deadline enforced on your behalf (Runpod's official plugin documentation says the old `--stop-after` and `--terminate-after` flags were never enforced; read in November 2026, not checked again for this page); no ownership record; no verified results |
| Runpod Serverless | the platform enforces the execution timeout (default 600 seconds, at most 7 days) **(not verified)**; finding capacity is the platform's problem | needs a worker image with a request handler, not any image with any command |
| SkyPilot | fails over across regions and clouds when there is no capacity; its automatic teardown runs on the cluster itself; has a Runpod network-volume type **(not verified)** | more setup; teardown without the receipts this runner keeps; on Windows it needs WSL, to the analysis author's knowledge **(not verified)** |
| dstack | retries on no capacity, a maximum duration, `dstack logs` and `attach` **(not verified)** | needs a running server; a 2 MB upload limit per entry **(not verified)** |
| Modal | the least effort; the provider enforces the stop | another provider; code written for its platform |
| SSH and your own scripts | nothing new to learn | everything this runner exists for: deadline, cleanup, evidence |

## Where this runner is really better

- **Proof instead of trust.** It reports a deletion only after reading it back, and deletes the
  volume only after the results were downloaded and hash-checked and every object on the volume was
  accounted for.
- **One deadline, three independent guards**, one of them inside the Pod.
- **No server and no open port.** Nothing runs in the cloud except your Pod; the Pod has no SSH, no
  Jupyter and no ports.
- **Hash-checked results**, with a receipt tied to the job and the volume.
- **A native Windows controller.** Linux passes the test suite too, but has not launched a real job
  yet.
- **Rules a coding agent cannot talk its way around:** Secure Cloud only, no repeated ambiguous
  create, no deletion of anything the job did not create.

## Where it is really worse

- **One provider.** Runpod only, Secure Cloud only. Up to eight GPU and data-center candidates per
  job, but no other cloud.
- **Little visibility while a job runs.** `status`, `logs` and `cost` show the phase, the deadline,
  the watchdog's log and an estimated cost; the workload's own log arrives only with the results.
- **Your computer must stay on** until the job ends.
- **Every real attempt starts fresh:** a new volume and a full upload. Optional capacity waiting
  uses catalog-only polls between attempts; another attempt requires fresh capacity and proof that
  the previous one left nothing. It does not reuse an earlier volume or upload.
- **The stop is enforced by the client**, not by Runpod.
- **Outputs reach storage only at the end**, so a Pod that dies hard loses them.
- **Young.** The first real jobs ran on 12 November 2026; long jobs are not yet proven.
