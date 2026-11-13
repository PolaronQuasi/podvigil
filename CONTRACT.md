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

## Trust boundary

The user's machine, container image, operating-system kernel and provider remain trusted. Workload
isolation is not a virtual machine. Local tests and simulated services do not establish current live
provider behavior. The [disclaimer](DISCLAIMER.md) and [license](LICENSE) apply.
