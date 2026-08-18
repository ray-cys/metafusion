# Development and release testing

MetaFusion uses `develop` for public testing and `main` for release-ready
builds. Feature branches must start from the current `develop` commit and must
not publish container images.

## CI and image lanes

| Git ref | Tests and security image scan | Published image |
| --- | --- | --- |
| Pull request | Yes, including a non-publishing multi-architecture build | None |
| Local feature or phase branch | Run locally before integration | None; local branches are not pushed |
| `develop` | Yes | `develop` and immutable `sha-<commit>` |
| `main` | Yes | `main` and immutable `sha-<commit>` |
| Release-candidate tag | Yes | Exact RC tag and `sha-<commit>`; never `latest` |
| Stable `vX.Y.Z` tag | Yes | Semantic-version tags, `latest`, and `sha-<commit>` from one manifest |

Dependencies and GitHub Actions are opened against `develop`. Python 3.10 and
3.13 remain the supported test matrix for the first stable release; a base
Python upgrade is a separate compatibility change, not an automatic merge.

CI also validates representative generated YAML with the official Kometa
2.4.8 container pinned by digest. Release-tag guards accept supported semantic
or RC `v…` tags only when their commit is the exact current `main` HEAD; the tag
workflow's own test and security jobs must then pass before an image publishes.

Published images include SBOM and maximum-mode build-provenance attestations.
Registry publication is serialized so overlapping branch and tag workflows
cannot write image aliases concurrently. CI verifies every published alias,
the keyless cosign signature, SBOM, provenance, embedded commit, and published
AMD64/ARM64 manifests before a tag workflow creates a GitHub Release. Tag
releases use generated notes from `.github/release.yml`; RC tags are marked
pre-release and stable tags become both the latest GitHub Release and the only
workflow allowed to move the container image's `latest` alias.

Pull requests build both supported image architectures without registry login,
publication, signing, or post-publication verification. This makes the image
build safe to require in branch protection while ensuring pull requests never
create package versions.

The separate weekly GHCR retention workflow resolves every tagged or otherwise
retained multi-arch manifest before deleting anything. Release, branch, SHA,
cosign, recent, newest-retained, and referenced platform manifests are
protected; only untagged, unreferenced versions older than 30 days are eligible,
and the newest 50 untagged versions are retained. Manual runs default to
report-only mode.

## Required release gate

Before promoting `develop` to `main`:

1. All required CI tests and the critical-vulnerability image scan pass.
2. Test coverage stays at or above the enforced 85% floor. Raise the floor
   only with useful failure-path tests.
3. `python metafusion.py --preflight` passes in the intended deployment.
4. `python metafusion.py --release-check` passes and its redacted report is retained.
5. A full scan completes with no unexpected item failures and with cleanup
   disabled or dry-run reviewed.
6. An immediate unchanged incremental run processes only items that are due.
7. Scheduler restart/catch-up and graceful stop complete on Unraid.
8. `/config` SQLite state and any Kometa output are backed up while the
   container is stopped.
9. The exact commit is soaked on `develop`, promoted unchanged to `main`, and
   the `main` workflow is allowed to finish before creating the release tag.
10. The release tag is created only from the current `main` HEAD. For a stable
    release, confirm `latest`, the exact version, and `sha-<commit>` resolve to
    the manifest verified by the tag workflow.

The Phase 16 synthetic inventory test models 2,000 movies, 300 shows, 1,000
seasons, and 8,000 episodes. It is a deterministic regression fixture, not a
replacement for the Unraid soak test against a real Plex server.

## Fault-injection coverage

Automated tests preserve prior output or fail closed for interrupted atomic
writes and downloads, Plex ownership-ledger commit failure, partial library
inventory, cleanup permission failure, TMDb 404/429/5xx responses, bounded
shutdown, scheduler catch-up, timezone offsets, canonical artwork collisions,
and large inventory reconciliation. New write paths must add the corresponding
recovery test before the coverage floor is raised.

Adaptive-concurrency tests use deterministic resource, pressure, clock, and
provider signals. They cover cgroup ceilings, healthy growth, rate-limit and
resource-pressure reduction, circuit opening/half-open recovery, shared
in-flight TMDb requests, and cancellation-safe worker bounds.

Phase 20 additionally covers interrupted-item recovery, exponential retry and
parking, Plex update-marker resets, automatic supported-library discovery,
missing-library cleanup protection, adaptive artwork timing, learned identity
invalidation, path-mapping advice, slow-Plex lane reduction, write-cap
deferral, storage-aware cache limits, and low-space artwork deferral.

Phase 22 covers read-only combined plans, cross-mode library/artwork audits,
rating-key/TMDb-ID/media-type targeting, selective retry filters, deterministic
artwork scoring, compatibility-profile validation, and explicit SQLite health,
backup, optimization, checkpoint, free-space, and vacuum paths. Release soak
testing should run `--plan` and `--compatibility-check` before a normal job, and
use `--sqlite-maintenance check` after the soak without overlapping an active
job.

Phase 22 hardening also enforces focused coverage floors for the builders, TMDb
cache, logging/provider mappings, main orchestration, and durable state paths.
Fault tests prove that HTTP 429 responses are not cached, a later TMDb request
can recover, and temporary Plex disconnects retry without duplicating a
successful mutation. SQLite backups are opened independently, checked, copied
to a separate restore path, and read back before the release gate passes.

Static checks use Ruff's Bugbear, Comprehensions, Simplify, and Ruff-specific
rule groups in addition to the existing correctness/security selectors. Mypy
checks the storage, provider mapping, logging, state, and builder modules as the
first typed boundary; its scope should expand only after each newly included
module is clean.

Phase 23 adds checked, redacted provider-shaped replay fixtures for editions,
localized titles, year disambiguation, Specials, alternate episode groups,
split-series mappings, and missing artwork. It also covers read-only mapping
diagnosis, stable paginated Plex inventory with duplicate/change/incomplete-page
rejection, and component-level explanations for selected and rejected artwork.
Replay fixtures must pass the credential, private-host, local-path, and server
identifier sanitizer before they can be committed.
