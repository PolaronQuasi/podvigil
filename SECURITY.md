# Security

The runner holds a key that can create and delete resources in your Runpod account and spend your
money, and it runs code you give it on machines you rent. This page says what it protects, against
whom, what it does not protect, and which permissions its keys need.

## Reporting a problem

Security reports and follow-up discussion stay in the repository. No contact email is required. Keep
vulnerability details out of public issues.

- **Current private repository:** collaborators can open an issue titled `[Security]` followed by a
  short description. Confirm that the repository is marked private before including details. These
  issues are visible to everyone who has access to the repository's issue tracker.
- **If the repository becomes public on GitHub:** use **Security and quality → Report a
  vulnerability** to submit a private report and continue the discussion in its advisory thread.
  This route requires GitHub's private vulnerability reporting setting; this policy file alone does
  not enable it. See the
  [GitHub setup guide](docs/GITHUB-SETUP.md#security-reporting-without-email).
- **If neither private route is available:** open an issue titled `Security reporting request`
  asking for a private reporting space, with no vulnerability details. The maintainer can arrange a
  separate private repository and invite your account before you share the report.

In the private report, include the version (`podvigil --version`), a minimal reproduction, what
happened and the potential impact. Never include credentials, raw ledgers or results containing
account identifiers.

Only the newest 0.x version receives fixes.

## Threat model

**What is protected:**

- your Runpod account API key and your S3 keys;
- your money: no Pod or volume of a job should keep billing unnoticed;
- the other resources in your account: the runner must never touch them;
- your inputs and results, and the evidence that they are complete.

**Against whom, and how:**

<!-- prettier-ignore -->
| Who or what | What could go wrong | What the runner does |
| --- | --- | --- |
| The workload, or a library it pulls in | reads keys, changes the guard's records, fakes results, keeps running, reaches the storage | runs as an unprivileged user without Linux capabilities; never receives the account key, the S3 keys or the Pod's key; cannot read the guard's records, code or memory; cannot reach the network volume; outputs are hashed by root after the workload is stopped; the Pod is deleted whatever the workload does |
| A mistake in a job file | Community Cloud, an unbounded runtime, a credentials file uploaded as an input | the job file is validated before anything is created; Secure Cloud only; every limit is finite; credentials files and links are refused as inputs |
| A lost or ambiguous API answer | a duplicate or unseen billed resource | each create request is sent once; ambiguity is reconciled, never retried |
| A coding agent operating the runner | deletes unrelated resources, falls back to cheaper clouds, retries until something bills | no command deletes by name pattern or across the account; deletion needs the job's own receipt; Secure Cloud only; no repeated creation |
| Logs and error messages | a key ends up in a terminal, a log or a ledger | errors are redacted; errors from other libraries are shown by type name only; every line of the event log passes one redaction point that also removes the process's own credential values; ledgers never hold credentials or request bodies |
| The network | requests read or redirected | HTTPS to Runpod's API and S3 endpoints; the API client follows no redirects, so the key never goes to another host; plain HTTP is accepted only to `127.0.0.1`, for the tests |

**What is not protected:**

- **The container image is trusted.** The guard runs as root with the image's own `python3`. A
  malicious image controls the guard: it can read the Pod's key, fake the guard's records and keep
  the Pod alive until the watchdog deletes it. Your account and S3 keys never reach the Pod. Use
  images you trust. A digest (`name@sha256:...`) names one exact image; a tag can be moved by its
  publisher.
- **The workload has network access.** It can send its inputs or outputs anywhere. Do not give
  untrusted code data it must not leak.
- **Isolation is not a virtual machine.** It is Linux user separation inside a container; a kernel
  flaw or a provider fault can cross it.
- **`run-local` has no isolation at all.** It runs the workload on your computer, as you, with your
  files and your network. Run it only with code you would run yourself.
- **Your computer is trusted.** Anyone who can read your credentials file has your keys. Anyone who
  can write your state folder can change ledgers, and a ledger is the runner's authority to delete.
  Protect both like the keys themselves.
- **Runpod is trusted** to run the Pod you asked for and to answer truthfully about it.
- **Ledgers hold account identifiers** (Pod and volume IDs, the IDs of resources that existed before
  the job, prices). Do not publish them.

## The Runpod API key

The configured client uses the operations below. See [CONTRACT.md](CONTRACT.md) and the
[Runpod API documentation](https://docs.runpod.io/api-reference/overview) for the provider boundary:

<!-- prettier-ignore -->
| Scope | Why | Requests |
| --- | --- | --- |
| `catalog:read` | price, capacity and storage types before launch; `survey` | `GET /catalog/gpus`, `GET /catalog/gpus/{id}`, `GET /catalog/datacenters/{id}` |
| `pods:read` | the inventory of existing Pods; reading the job's Pod; proving its deletion | `GET /pods`, `GET /pods/{id}` |
| `pods:write` | creating and deleting the job's Pod | `POST /pods`, `DELETE /pods/{id}` |
| `volumes:read` | the inventory of existing volumes; reading the job's volume; proving its deletion | `GET /network-volumes`, `GET /network-volumes/{id}` |
| `volumes:write` | creating and deleting the job's volume | `POST /network-volumes`, `DELETE /network-volumes/{id}` |
| `audit:read` (optional) | only to resolve a create request whose answer was lost: the watchdog five minutes after the deadline, and `resolve-creation --audit` | `GET /audit-logs` |

Nothing else is used: no billing, templates, secrets, Serverless, clusters or membership.

By command: `plan`, `run-local`, `list`, `status`, `logs` and `cost` use no key (`cost` estimates
from the stored price; it does not read Runpod's billing); `cancel` uses none itself, but the
watchdog that acts on it needs the Pod and volume scopes; `survey` needs `catalog:read`; `launch`
needs all but `audit:read`; `recover`, `export-partial` and `reattach` need the Pod and volume
scopes; `resolve-creation` needs the read scopes, and `audit:read` with `--audit`.

Be aware:

- **Not tested yet.** No key restricted to exactly this list has run a job. Whether the Runpod
  console offers exactly these restrictions has not been checked either.
- **A key without `pods:write` costs time.** Runpod answers its `POST /pods` with HTTP 403, which
  the runner treats as "no access to this GPU pool". With launch candidates it then tries every
  candidate, creating and deleting a volume each time, and ends with exit code 4.
- **A key without the write scopes cannot clean up.** The runner reports that the deletion is not
  verified; the resources stay until a key that can delete them is used.
- **Each key has its own pacing file** in the state folder, named by a short one-way fingerprint of
  the key (the first 16 hexadecimal digits of a SHA-256 over a fixed label and the key). The key
  itself is never written there. Every job still checks that its key has not changed since it was
  launched, and refuses another key.
- **The Pod has its own key.** Runpod gives each Pod a key for that Pod; the guard uses it to delete
  its own Pod. You do not configure it, and your account key is never sent to the Pod.

## The S3 keys

`RUNPOD_S3_ACCESS_KEY` and `RUNPOD_S3_SECRET_KEY` are separate from the API key and are created in
the Runpod console. The runner uses them for the job's own volume only: write and read a small owner
record, upload the inputs and their list (the input manifest), write the request to finish after a
`cancel`, list the volume, and download the results and records. Whether Runpod lets you limit these
keys to one volume is not known to this project; treat them as access to all your network volumes.

## Storing the keys

- Keep them in one credentials file readable only by your account (see
  [the command line reference](docs/MANUAL.md#where-the-runner-keeps-things)), never in a job file,
  on a command line, in a chat or in the repository. `.env` and `.env.*` files are ignored by git.
- On Windows keep the S3 keys in the file, not only in the environment: the watchdog does not
  inherit the environment, and `launch` refuses to start when the watchdog's keys differ.
- `workload_env` must not carry secrets: names that look like secrets are refused, and the job file
  is stored in the ledger and sent to the Pod.
