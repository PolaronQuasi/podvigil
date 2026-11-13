# Gitea runners

The workflow needs repository-scoped Linux and Windows runners. Register them with Gitea's
[act_runner procedure](https://docs.gitea.com/1.24/usage/actions/act-runner/). Keep the generated
`.runner` registration file and all credentials outside this checkout. The runners need no Runpod
credentials. These host runners execute trusted repository code; the Linux container lane needs
access to its local Docker socket.

## Linux

Build this Dockerfile as `podvigil-ci-runner:0.2.13`, and install `python:3.13.7-slim-bookworm`
separately. CI uses the installed Python image by immutable ID and never pulls it. Use
`config.example.yaml` as the configuration in the runner's external data folder. Register it with
labels `ubuntu-latest:host,linux-docker:host` and the repository's registration token.

Create a Docker volume named `podvigil-ci-work`. Inspect its actual mount point:

```bash
docker volume create podvigil-ci-work
docker volume inspect --format '{{.Mountpoint}}' podvigil-ci-work
```

Mount that volume at the **same absolute path inside the runner**, set `TMPDIR` to that path, and
set `host.workdir_parent` to its `jobs` subdirectory. The example assumes Docker's default mount
point; adapt it to the inspected value. This makes test directories available at the same paths to
sibling containers launched through the Docker socket. Use native Linux storage: Docker Desktop
shared host folders can report different device IDs for directories and their files, which the
output-safety checks correctly reject.

Run the runner with `--init` (required to reap detached watchdog children), a restart policy, its
data folder mounted at `/data`, and `/var/run/docker.sock` mounted at the same path. Persist
`/opt/hostedtoolcache` in a separate volume to retain the Python versions downloaded by
`actions/setup-python`. Configure networking so both the runner and its host-executed actions can
reach the Gitea instance URL. A local Gitea instance can use host networking when the Docker host
supports it.

The test containers receive source snapshots only, with `--pull=never --network none`. They do not
receive the runner's registration file, credentials, or Docker socket.

## Windows

Use the Windows act_runner binary with the `windows:host` label, Python 3.12, Git, and Node.js on
its PATH. Store its configuration, registration file, and job workspace outside this checkout. Clear
inherited Runpod/AWS credentials before starting the daemon. Run it under a dedicated trusted
account, or start it manually for validation; arrange service startup if unattended CI is required.
The workflow uses `cmd` for Python installation and test steps.
