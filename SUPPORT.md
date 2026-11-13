# Getting help with PodVigil

Start with the [installation guide](README.md), [examples](docs/EXAMPLES.md),
[operations guide](docs/OPERATIONS.md) and [diagrams](docs/diagrams/README.md). PodVigil is
maintained by [PolaronQuasi](https://github.com/PolaronQuasi).

Use the [issue tracker](https://github.com/PolaronQuasi/podvigil/issues/new/choose) for ordinary
bugs and feature requests. Security reports follow the private reporting instructions in
[SECURITY.md](SECURITY.md), without a contact email. Keep vulnerability details out of ordinary
issues. No separate support service, response-time commitment or service level agreement is
provided. Account, capacity and billing questions belong with Runpod. Read
[DISCLAIMER.md](DISCLAIMER.md) before running a job.

## Useful bug reports

Include the PodVigil version, OS, Python version, installation method, a minimal job file, the
command, exit code, expected behavior and observed behavior. Include the two closing Pod/volume
status lines and relevant sanitized logs.

Do not publish credentials, raw ledgers, account identifiers, private paths or complete output
directories. Use fake identifiers and remove workload data. Report credential exposure or unsafe
deletion privately under [SECURITY.md](SECURITY.md).

If cleanup is uncertain, preserve the ledger and recover the existing job. Do not launch another job
just to reproduce an unresolved billed-resource problem.

## Updating or removing an installation

Finish or recover active jobs before replacing the code their watchdogs use. Update into a separate
environment or checkout while old jobs remain active. Removing the Python package does not delete
cloud resources or local ledgers. Keep the original code and state until cleanup is verified. See
[compatibility and state paths](docs/COMPATIBILITY.md).
