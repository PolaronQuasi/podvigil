# Compatibility

The project version is **0.1.0**, defined once in `src/podvigil/__init__.py` and shown by
`podvigil --version`. Package metadata reads that same value.

Python 3.10 or later is required. The GitHub workflow declares Linux checks on Python 3.10, 3.12 and
3.14, and Windows checks on Python 3.12. The container checks use a pinned Python 3.13.7 image. A
declared matrix is not a claim that hosted CI has already run. See [GitHub setup](GITHUB-SETUP.md).

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
