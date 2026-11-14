# How PodVigil works

Start with the architecture, then follow a job through finalization, result delivery and recovery.
These diagrams describe the current implementation; they are not a guarantee of live provider
behavior. Read the [manual](../MANUAL.md), [safety model](../SAFETY-MODEL.md) and
[disclaimer](../../DISCLAIMER.md) for the operational boundaries.

<!-- prettier-ignore -->
| Diagram | What it answers | Editable source |
| --- | --- | --- |
| [Architecture](01-architecture.svg) | What runs locally, in the Pod, and at the provider? | [Mermaid](01-architecture.mmd) |
| [Job lifecycle](02-job-lifecycle.svg) | What happens from launch through cleanup? | [Mermaid](02-job-lifecycle.mmd) |
| [Result delivery](03-result-delivery.svg) | When are outputs safe to recover and storage safe to delete? | [Mermaid](03-result-delivery.mmd) |
| [Safe recovery](04-safe-recovery.svg) | What happens after an uncertain or interrupted job? | [Mermaid](04-safe-recovery.mmd) |

## 1. Architecture

![Local controller, read-only wait and independent watchdog, an owned Secure Cloud Pod, and verified durable output recovery.](01-architecture.svg)

Blue boxes execute work, green boxes store data, and purple identifies the provider API. Labels
carry the same meaning without relying on color. The workload writes to Pod-local disk; the
supervisor publishes snapshots to the private network volume. A checkpoint not yet snapshotted can
be lost with the Pod.

`wait JOB` reads only the local ledger and returns the job's outcome. A timeout or Ctrl+C stops
waiting without changing the job. It does not contact the provider.

Optional capacity rounds belong to the controller. After a definite no-capacity answer and proof
that the attempt left no Pod or volume, it polls the catalog every 75 seconds. A new attempt needs
fresh capacity, price and budget checks. Each created attempt keeps its own ledger; capacity waiting
does not consume the next job's runtime allowance.

## 2. Job lifecycle

![Verified-empty capacity rounds precede a job's supervision, bounded finalization, compute cleanup and verified storage cleanup.](02-job-lifecycle.svg)

Finalization failure must not prevent a compute-deletion attempt. Conversely, compute deletion does
not authorize volume deletion without a complete backup. The configured deadline cannot force a
provider API to respond.

The capacity loop is optional. An ambiguous create or unverified absence cannot enter another round.
A catalog-only round creates no ledger or resources; a real attempt keeps its own ledger.

## 3. Result delivery

![The supervisor publishes results before compute termination; backup verification precedes volume deletion, and wait reads the resulting ledger.](03-result-delivery.svg)

An accepted DELETE is only a request acknowledgment. Separate read-back and inventory evidence
establish absence. Unknown or changed volume objects retain storage, which may continue to bill.

Completion and failure records are written to the job ledger. `wait` reads them; it does not
download outputs, validate backups or delete resources. A terminal status can still describe
retained storage or unverified cleanup.

## 4. Safe recovery

![Observe an existing job without changing it, or explicitly recover it using ownership evidence, compute cleanup and verified backup.](04-safe-recovery.svg)

Recovery operates on the existing job and its original deadline. It does not launch another Pod or
sweep unrelated account resources.

The observation branch is separate: `wait` can stop on a terminal phase, a watchdog no longer heard,
its timeout or Ctrl+C. None of those reader outcomes starts recovery. Invoke `recover` deliberately
when its resource actions are needed.

## Rebuild the diagrams

Node.js 22 or later is needed only for diagram tooling; PodVigil itself is Python. Run `npm ci` and
`npm run docs:diagrams`. The lockfile pins the renderer and its dependencies. To use an installed
Chrome, set `PUPPETEER_EXECUTABLE_PATH`; for an installation that skips browser downloads, use
`npm ci --ignore-scripts`.

`mermaid.json` defines the diagram palette and typography. Commit both `.mmd` sources and rendered
`.svg` files when changing a diagram.
