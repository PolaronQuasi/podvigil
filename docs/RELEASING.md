# Releasing PodVigil

The project has one current version, **0.1.0**, defined in `src/podvigil/__init__.py`. Package
metadata and command output derive from it. There is no changelog file; describe a release in its
reviewed release notes.

## Prepare

1. Review the intended commit on `main`, including the guides and diagrams.
2. Run all local and hosted CI lanes; report skips and platform coverage.
3. Confirm that package installation and all offline example plans pass.
4. Inspect the distributions and checksums; exclude credentials, ledgers and private paths. Read
   [DISCLAIMER.md](../DISCLAIMER.md).
5. Before a public GitHub release, enable and verify the
   [private vulnerability reporting form](GITHUB-SETUP.md#security-reporting-without-email).
6. When explicitly requested by the account owner, run the supervised Secure Cloud smoke for the
   intended release commit and verify backup and actual resource deletion. Retain a sanitized record
   for the release. CI never authorizes this.

## Create a draft on GitHub

On GitHub, tag the reviewed commit `v0.1.0` and push that tag. `release.yml` checks that the tag
matches the package, the commit belongs to `main`, and all verification jobs pass. It then creates a
draft with the exact validated wheel, source archive and SHA-256 checksums. No public release or
package registry publication happens automatically.

Review the assets, validation evidence, limitations and notes before publishing through the GitHub
release UI. A draft without the required live smoke evidence must not be represented as a
production-validated release.

An interrupted upload may leave a partial draft. Inspect it before any deliberate retry; the
workflow does not delete existing releases, replace assets or move tags.
