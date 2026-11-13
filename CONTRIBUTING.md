# Contributing to PodVigil

Read [CONTRACT.md](CONTRACT.md), [SECURITY.md](SECURITY.md) and
[the safety model](docs/SAFETY-MODEL.md) before changing lifecycle, ownership, credentials or
recovery. PodVigil is maintained by [PolaronQuasi](https://github.com/PolaronQuasi).

## Working on a change

Start a focused branch from `main`, preserve existing work, and open a pull request in the
repository hosting your checkout. Explain the concrete problem, resulting behavior, validation and
remaining operational limits. The package version is recorded in `src/podvigil/__init__.py`.

Use a separate checkout when a watchdog still uses the existing source directory. Do not replace
files or move state beneath a running job. Never commit credentials, private paths, ledgers,
checkpoints, downloads or virtual environments.

## Fixed rules

- Secure Cloud only, explicitly selected; no Community fallback.
- Local development, CI and package builds do not authorize paid cloud jobs.
- Do not repeat an ambiguous create or delete resources without ownership evidence.
- Compute termination precedes result retrieval; storage deletion follows verified backup.
- Preserve data and report uncertainty when cleanup cannot be verified.
- Never print keys or authorization headers.

## Validation

Create `.venv` with `requirements.lock.txt`, then run `python -B tools/ci.py test` using that
environment. Use the separate `requirements-ci.txt` environment for lint, workflow checks and
package builds. See [Development](docs/DEVELOPMENT.md) for exact commands and platform coverage.

Keep Pod runtime modules flat, standard-library only and within the request-size test. Share
fixtures through `tests/support/`, not imports between test modules. Add regressions for changed
failure behavior and report skipped platform tests. Passing local fakes is not proof of live billing
or provider deletion behavior.

## Documentation

User-visible changes update the relevant guide: [commands](docs/MANUAL.md),
[job fields](docs/JOB-FILE.md), [states and messages](docs/OPERATIONS.md), or
[workload behavior](docs/WORKLOAD-CONTRACT.md). The operations-page test checks literal states and
messages against the code. Keep diagrams and their Mermaid sources in sync. Use the same plain,
explicit terminology throughout the project.
