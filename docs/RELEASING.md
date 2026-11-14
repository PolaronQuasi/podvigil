# Releasing PodVigil

The current version, **0.1.0**, is defined in `src/podvigil/__init__.py`. Package metadata and
command output derive from it. Describe the release in its reviewed release notes.

## Prepare

1. Confirm that private vulnerability reporting is enabled and the `github-release` environment is
   protected before publishing a release tag.
2. Review the intended commit on `main`, including the guides and diagrams. Run all local and hosted
   CI lanes for that commit, including macOS on Apple Silicon and Intel and the installed-wheel
   checks on both; report skips and platform coverage. Run the CI workflow manually from the Actions
   tab if automatic CI was skipped.
3. Confirm that package installation and all offline example plans pass.
4. Inspect the distributions and checksums; exclude credentials, ledgers and private paths. Read
   [DISCLAIMER.md](../DISCLAIMER.md).
5. Confirm that the
   [private reporting form](https://github.com/PolaronQuasi/podvigil/security/advisories/new) is
   available and that `github-release` allows version tags and requires the intended reviewer.
6. When explicitly requested by the account owner, run the supervised Secure Cloud smoke for the
   intended release commit and verify backup and actual resource deletion. Retain a sanitized record
   for the release. CI never authorizes this.

## Create a draft on GitHub

On GitHub, tag the reviewed commit `v0.1.0` and push that tag. `release.yml` checks that the tag
matches the package, the commit belongs to `main`, and all verification jobs pass, including macOS
tests and installation of the exact wheel that will be attached to the draft. The draft job then
waits for the `github-release` environment's approval. After approval it creates a draft with the
exact validated wheel, source archive and SHA-256 checksums. No public release or package registry
publication happens automatically.

If a draft already exists for the tag, a rerun stops instead of overwriting its assets. Inspect an
interrupted draft before retrying. Limit permission to create or change release tags to trusted
maintainers.

Review the assets, validation evidence, limitations and notes before publishing through the GitHub
release UI. A draft without the required live smoke evidence must not be represented as a
production-validated release.

An interrupted upload may leave a partial draft. Inspect it before any deliberate retry; the
workflow does not delete existing releases, replace assets or move tags.
