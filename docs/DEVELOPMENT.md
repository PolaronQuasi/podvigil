# Development

Read [CONTRIBUTING.md](../CONTRIBUTING.md) and [the contract](../CONTRACT.md) first. Local
development and CI never authorize paid Runpod resources.

## Layout

<!-- prettier-ignore -->
| Path | Purpose |
| --- | --- |
| `src/podvigil/` | Python package, CLI, configuration and provider client |
| `src/podvigil/controller/` | Ownership, launch, supervision, recovery and cleanup |
| `src/podvigil/podrt/` | Flat standard-library runtime sent into the Pod |
| `tests/` | Unit, fake-cloud, process, container and manual live lanes |
| `tools/` | CI checks, package verification and documentation tooling |
| `examples/`, `jobs/` | Workload examples and offline plans |
| `docs/`, `docs/diagrams/`, `assets/` | Guides, editable diagrams and project artwork |
| `.github/` | GitHub workflows, issue templates and maintainership |
| `.gitea/` | The separate Gitea Actions workflow |

The Pod request has a size limit. Keep `podrt` flat and dependency-free; its payload-size test must
pass. Pod code imports itself as `podrt` inside the container.

## Local environments

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
python -m venv .venv-ci
.\.venv-ci\Scripts\python.exe -m pip install -r requirements-ci.txt
.\.venv\Scripts\python.exe -B tools/ci.py test
.\.venv-ci\Scripts\python.exe tools/ci.py audit
.\.venv-ci\Scripts\python.exe tools/check_workflows.py
```

On Linux use `python3` and `.venv/bin/python` / `.venv-ci/bin/python`. Tests import this checkout's
`src/`, use dummy credentials and temporary state, and never load the manual live lane. Logs,
counts, skips and source hashes go under ignored `output/ci/`. A new run replaces that lane's local
record, so copy it first when needed. Generated validation logs stay out of published documentation.

`python -m tests.run_validation --output-dir output/validation` also records a validation report
with the Git commit, checkout dirtiness and hashes of source, tests, examples, documentation,
workflows and package inputs. Changes during the run invalidate the report. Generated files and
credentials are excluded.

## Formatting

From the checkout, install the pinned Node development dependencies and run the formatter with the
Python environment from [Local environments](#local-environments). On Windows:

```powershell
npm ci --ignore-scripts
.\.venv-ci\Scripts\python.exe tools/format_files.py --write
.\.venv-ci\Scripts\python.exe tools/format_files.py --check
```

Use `--write` to apply formatting or `--check` to report differences. On Linux, use
`.venv-ci/bin/python` for the same commands. The formatter reads only Git-tracked files, so stage
newly added files first.

- Python uses Ruff 0.17.5 with 100 columns, four-space indentation and double quotes.
- Markdown, JSON, YAML, JavaScript and SVG use Prettier 3.10.1 with 100 columns and two-space
  indentation.

The canonical `LICENSE` and `NOTICE`, CSV fixture bytes and npm-managed lockfile are preserved. Edit
Mermaid diagrams in their `.mmd` sources. The diagram renderer formats the SVG and its CSS.

CI checks Python formatting. The full local `--check` also covers text files and assets.

## Test lanes

GitHub runs CI for pull requests, pushes to `main`, and manual dispatches. Feature branches use the
pull-request checks, avoiding duplicate push and pull-request results for the required check.

`tools/ci.py test` runs all automatically collected tests. `--lane unit`, `--lane fakecloud`,
`--lane integration` and `--lane container` select a lane. Windows process tests need the project's
`.venv` and locked dependencies. Report skips; a Linux run does not replace the Windows process
checks.

GitHub's Windows lane uses the runner's full temporary-directory path. The CLI fixtures preserve
`PSModulePath` so PowerShell can find its CIM process-launch modules within the normal watchdog
startup deadline. The path tests still reject unsafe artifact aliases.

Windows record reads briefly retry sharing violations while another process atomically replaces the
JSON file. Persistent denial still raises an error; it never becomes a missing-record default.

For container checks, select an already-installed Python Linux image:

```powershell
$env:PODVIGIL_REVIEW_DOCKER_IMAGE = docker image inspect --format '{{.Id}}' python:3.13.7-slim-bookworm
.\.venv\Scripts\python.exe -B tools/ci.py test --lane container
.\.venv\Scripts\python.exe -B tools/ci.py container-suite --image $env:PODVIGIL_REVIEW_DOCKER_IMAGE
```

The tests use `--pull=never` and `--network none`. The clean source suite runs as root both directly
and under Docker's injected init. Only allowlisted tracked files enter the container; no checkout
credentials or Docker socket are mounted. GitHub prepares the digest-pinned image before starting
these tests.

On Linux, the host process running `--lane container` needs root privileges to inspect and clean up
the guard's private, root-owned fixture outputs. The GitHub container job uses `sudo` for that
harness command. The six real ownership and process-isolation unit tests also require Linux root;
ordinary-user test lanes report them as skipped, and both clean Linux root suites execute them.

The 256 MiB checkpoint-cache fixture has a 512 MiB memory limit to accommodate its file pages and
Python processes. Other fixtures, including the deliberate worker-OOM regression, retain 128 MiB.

The golden-ledger test pins lifecycle records and validates optional beacon diagnostics separately.
The operations-page test checks literal states and messages against [OPERATIONS.md](OPERATIONS.md);
review messages assembled from pieces manually too.

## Package checks

```powershell
.\.venv-ci\Scripts\python.exe -m build --no-isolation
.\.venv-ci\Scripts\python.exe -m twine check --strict dist/*.whl dist/*.tar.gz
.\.venv-ci\Scripts\python.exe -B tools/ci.py smoke
```

The smoke check installs the wheel into a new environment, runs both entry points outside the
checkout and plans every example with no credentials. It then runs
`tests/integration/wheel_smoke.py` against the installed package: input-module loading, successful
and failed workloads, missing artifacts, time limits and descendant-process cleanup. Network access
and credential reads are rejected; package contents and metadata must stay unchanged, and no state
folder may be created. The build also produces an sdist; checksums accompany both artifacts.

## Continuous integration

The GitHub workflows provide quality, platform tests, container tests, packages and a required
aggregate check. Inspect the
[CI run](https://github.com/PolaronQuasi/podvigil/actions/workflows/ci.yml) for the intended commit
before claiming that hosted CI passed. If automatic CI was skipped, run the CI workflow manually on
that branch from the Actions tab.

The separate Gitea workflow retains six jobs: lint, filenames-only secret scan, Linux tests (Python
3.10 with and without dependencies, and 3.12), network-disabled container tests with and without
init, Windows tests and installed-wheel checks. Its dedicated runners and preinstalled container
image are described in [the Gitea runner guide](../.gitea/runner/README.md). Keep runner
registrations, workspaces and credentials outside the repository. Neither CI system receives Runpod
credentials or runs paid jobs.

## Paid smoke: by explicit instruction only

`tests/live` has no `__init__.py`, so normal discovery does not collect it. A maintainer can
deliberately run it after the account owner requests the paid job:

```powershell
$env:PAID_SMOKE_JOB = 'examples/hello-gpu/job.json'
$env:PAID_SMOKE_ACCEPT_CHARGES = 'yes'
.\.venv\Scripts\python.exe -B -m unittest tests.live.test_live_smoke -v
```

This uses real credentials, creates billed resources and checks workload success, verified Pod and
volume absence, and verified backup. It is never an Actions step. Local tests cannot establish
current GPU, storage, API-permission or billing behavior.

For package delivery, see [Releasing](RELEASING.md). For diagram rendering, see
[diagrams](diagrams/README.md).
