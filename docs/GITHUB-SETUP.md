# GitHub setup

The GitHub repository is [PolaronQuasi/podvigil](https://github.com/PolaronQuasi/podvigil) and is
currently private. Gitea holds a separate copy. The workflows below run on GitHub when triggered;
local checks do not establish a successful hosted Actions run.

## Workflow layout

<!-- prettier-ignore -->
| Workflow | Trigger | Result |
| --- | --- | --- |
| `ci.yml` | Branch push, pull request, manual dispatch | Reusable validation suite |
| `verify.yml` | Called by CI/release, or manually | Quality, four OS/Python lanes, containers, packages |
| `release.yml` | `v<version>` tag, or manual dispatch on that tag | Full validation, then a draft GitHub release |

CI uses hosted Ubuntu 24.04 and Windows 2025 runners. The test matrix is Linux Python 3.10 without
SDK dependencies, Linux 3.12 and 3.14 with the runtime lock, and Windows 3.12 with a project
`.venv`. Container checks add Linux root coverage with and without injected init and exercise the
actual Pod guard in isolated containers. Every required job must succeed; a skipped or cancelled
lane cannot pass the aggregate gate.

## Repository settings

1. Keep `main` as the default branch and preserve the current visibility unless the owner explicitly
   chooses to publish it.
2. Enable Actions with read-only default permissions and hosted runners. The workflow pins
   third-party actions to commit SHAs and disables persisted Git credentials. Do not add Runpod or
   S3 credentials as repository secrets.
3. Require the aggregate **All checks passed** check on `main` after its first successful run.
   Confirm the displayed reusable-workflow check name before selecting it. Block force pushes and
   deletion; require reviewed pull requests.
4. Keep `.github/CODEOWNERS` valid for `@PolaronQuasi`. Enable dependency alerts and review
   Dependabot updates. Before a public release, enable the private reporting route described below.
5. Configure the `github-release` environment, restrict it to release tags and add a required
   reviewer where the account plan supports it. Restrict who can create or change release tags. The
   automatic token only gains `contents: write` inside the draft-release job.
6. Inspect the first complete hosted run. Local workflow lint and matching local test commands do
   not establish that hosted services or runner permissions work.

Fork PRs use `pull_request`, not `pull_request_target`. They receive no cloud credentials and cannot
invoke the tag-only draft job. The workflows have finite job timeouts; concurrent branch CI is
cancelled when superseded.

## Security reporting without email

The current private repository accepts reports from collaborators through its issue tracker, as
described in [SECURITY.md](../SECURITY.md). GitHub private vulnerability reporting is available for
public GitHub repositories; it is not active while this repository is private.

If the owner makes the repository public, enable **Settings → Advanced Security → Private
vulnerability reporting**. Confirm that **Security and quality → Report a vulnerability** opens the
private reporting form before directing users there. Adding `SECURITY.md` does not enable the
feature automatically.

Use GitHub's built-in report form and advisory discussion to receive and answer reports. No public
contact email or custom reporting service is needed. See
[GitHub's setup instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).

## Delivery

The release workflow reruns verification for the exact tag, checks its version and ancestry on
`origin/main`, verifies package checksums, and uploads the wheel, source distribution and checksum
file to a **draft** release. Review and complete [the release process](RELEASING.md) before
publishing. An existing draft causes a rerun to stop rather than overwrite assets; inspect an
interrupted draft manually. There is no PyPI upload, production deployment or paid Runpod job in
this pipeline.

The existing `.gitea/workflows/ci.yml` remains separate. Its runner registration and labels are
local Gitea configuration, not GitHub configuration.
