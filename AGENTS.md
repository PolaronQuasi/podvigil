# Runpod policy for this project

- The user requires **Secure Cloud only** for every future Pod. Send `cloud: "SECURE"` explicitly in
  REST v2 (`cloudType: "SECURE"` for tools using the older field). Never use Community Cloud or fall
  back to it because of price or availability.
- Use the runner (`managed_run.py` at the repository root, or the installed `podvigil` command) for
  training jobs. Do not bypass its deadline, detached watchdog, Pod supervisor, ownership ledger,
  durable output volume, or read-back checks.
- Local work never authorizes a paid smoke test or training deployment; only an explicit request to
  run a cloud job does. Do all read-only preparation and local checks first.
- Terminate/delete the job's Pod on success, failure, cancellation, startup failure or runtime
  deadline. Stopping a Pod alone is not complete cleanup.
- Only delete resources recorded as created by the job. Do not modify existing unrelated Pods,
  volumes, account keys or experiments.
- Never rely on `--stop-after` or `--terminate-after`: the official plugin documents that they were
  not enforced.
- Workloads write results to `PODVIGIL_OUTPUT_DIR` on local Pod disk. The supervisor snapshots them
  to the owned network volume. Terminate compute before retrieving results through S3. Delete the
  owned volume only after manifest and SHA-256 verification; retain and identify it if recovery is
  incomplete.
- Never say all billing stopped while a network volume remains or Pod deletion is unverified. A
  platform/network failure can delay termination; the timeout is not a provider-enforced spending
  cap.
- Keep credentials in the ignored `.env` or the per-user credentials file (see README); never print
  keys or authorization headers. The supervisor uses the platform-provided Pod-scoped key; do not
  forward the account API key to the Pod.
- Before trusting a new version with a long job, run a short paid smoke test (only when explicitly
  requested) and verify actual Pod/volume deletion.
- Keep existing job ledgers and result folders intact.
