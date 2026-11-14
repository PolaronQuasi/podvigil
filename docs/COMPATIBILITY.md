# Compatibility

The project version is **0.1.0**, defined once in `src/podvigil/__init__.py` and shown by
`podvigil --version`. Package metadata reads that same value.

Python 3.10 or later is required. The GitHub workflow declares Linux checks on Python 3.10, 3.12 and
3.14, Windows checks on Python 3.12, and macOS 26 checks on Python 3.12 for Apple Silicon and Intel.
Both macOS architectures also install and test the wheel built by the distribution job. The
container checks use a pinned Python 3.13.7 image. A declared matrix is not a claim that hosted CI
has already run. Check the
[CI results](https://github.com/PolaronQuasi/podvigil/actions/workflows/ci.yml) for the intended
commit.

macOS controller and package checks use fake services and local processes; no real Runpod launch
from macOS is claimed. For `run-local`, macOS cleanup covers ordinary process-group children.
Children that deliberately start a different session are not covered by that cleanup; Windows job
objects and Linux's subreaper provide stronger descendant tracking. This limitation concerns local
workload rehearsal, not the Linux Pod guard used by cloud jobs.

## Commands and configuration

Use `podvigil`, `python -m podvigil`, or `python managed_run.py` in a checkout. Install from this
repository or a verified wheel; no public PyPI publication is claimed. New settings use
`PODVIGIL_STATE_DIR`, `PODVIGIL_ENV_FILE` and `PODVIGIL_MIN_CUDA_VERSION`.

The unreleased working name's `PODWARDEN_*` configuration variables remain fallback aliases.
Explicit command-line options win, followed by `PODVIGIL_*`, then those aliases. The prior per-user
`podwarden/credentials.env` remains readable after the new per-user credentials file and before a
checkout `.env`.

New per-user state goes under `podvigil`. Existing state is never moved or deleted by the rename. To
inspect or recover it, pass its original folder with `--state-dir`, or pass the full ledger path. To
continue sharing quota coordination with an old watchdog, use the same state directory; its
credential-specific pacing filename is unchanged. Do not replace files beneath a running watchdog.

Pod ownership recognizes `PODVIGIL_JOB_ID`, `PODWARDEN_JOB_ID` and `WONDOMON_JOB_ID`. New workloads
receive `PODVIGIL_OUTPUT_DIR` and `PODVIGIL_INPUT_DIR`, with the corresponding older prefixes as
aliases. Private supervisor settings never reach the workload. Existing ledger paths and stored
configurations remain readable.

`wait` resolves the same existing ledger paths, `latest` and ID prefixes as `status`. It does not
migrate or rewrite old ledgers. New launch-failure records store an outcome; older records are
classified from their recorded errors and resource evidence. Status adds `ended` and
`recovery_handoff`; existing fields remain available. The new `wait_timeout` outcome uses code 14;
existing codes keep their meanings.

Capacity retries are opt-in through `launch --wait-for-capacity MINUTES`; existing launch commands
keep their behavior. The option adds `capacity_rounds` and `attempt_ledgers` to the result without
changing the job-file format or version. It also works with `--detach`.
