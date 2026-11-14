# PodVigil's provider and workload contract

PodVigil 0.1.0 is a Python client for Runpod's REST v2 and S3 interfaces. Provider responses are
treated as evidence to validate, not instructions to trust blindly. See
[the safety model](docs/SAFETY-MODEL.md) for limits and
[the workload contract](docs/WORKLOAD-CONTRACT.md) for application behavior.

## Resource ownership

Every new Pod explicitly requests Secure Cloud. A job owns only resources established by its ledger
and matching receipts. An ambiguous create request is reconciled without automatically sending
another create. A name match alone is not sufficient authority to delete an unrelated resource.

## Completion and billing

A workload exit and an accepted DELETE are distinct from verified resource absence. The runner
attempts compute termination on success, failure, cancellation or deadline and records what the
provider confirms. When the provider cannot be reached, cleanup remains unverified. The deadline is
not a provider-enforced spending cap.

## Durable outputs

Workloads write below `PODVIGIL_OUTPUT_DIR` on the Pod's local disk. The privileged supervisor
snapshots outputs to the owned network volume. Compute can then be terminated. Results are retrieved
through S3, their sizes and hashes checked, and the whole volume audited before deletion. Incomplete
recovery preserves storage and reports its possible ongoing billing.

## Local command and state

`podvigil`, `python -m podvigil` and the checkout's `managed_run.py` use the same command line. A
result is JSON on stdout; progress and the two closing resource status lines go to stderr. State is
separate from installed source. The ledger schema is an internal file format, not a second product
version.

`wait` is a local ledger reader. Its timeout and interruption do not alter the job. The terminal
status and job outcome are distinct from proof that no resources remain.

`launch --wait-for-capacity` adds rounds only after definite no capacity and verified absence.
Between attempts it uses read-only catalog checks with existing API pacing. Each real attempt keeps
its own ledger and rechecks price and limits. Its runtime deadline starts at its own Pod request;
waiting never authorizes an ambiguous create to be repeated.

## Trust boundary

The user's machine, container image, operating-system kernel and provider remain trusted. Workload
isolation is not a virtual machine. Local tests and simulated services do not establish current live
provider behavior. The [disclaimer](DISCLAIMER.md) and [license](LICENSE) apply.
