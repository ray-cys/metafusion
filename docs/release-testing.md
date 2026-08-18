# Development and release testing

MetaFusion uses `develop` for public testing and `main` for release-ready
builds. Feature branches must start from the current `develop` commit and must
not publish container images.

## CI and image lanes

| Git ref | Tests and security image scan | Published image |
| --- | --- | --- |
| Pull request | Yes | None |
| Local feature or phase branch | Run locally before integration | None; local branches are not pushed |
| `develop` | Yes | `develop` and immutable `sha-<commit>` |
| `main` | Yes | `main`, `latest`, and immutable `sha-<commit>` |
| `vX.Y.Z` or release-candidate tag | Yes | Immutable semantic-version tags and `sha-<commit>` |

Dependencies and GitHub Actions are opened against `develop`. Python 3.10 and
3.13 remain the supported test matrix for the first stable release; a base
Python upgrade is a separate compatibility change, not an automatic merge.

CI also validates representative generated YAML with the official Kometa
2.4.8 container pinned by digest. Release-tag guards accept supported semantic
or RC `v…` tags only when their commit is already contained in `main`; the tag
workflow's own test and security jobs must then pass before an image publishes.

The separate weekly GHCR retention workflow resolves every tagged or otherwise
retained multi-arch manifest before deleting anything. Release, branch, SHA,
cosign, recent, newest-retained, and referenced platform manifests are
protected; only untagged, unreferenced versions older than 30 days are eligible,
and the newest 50 untagged versions are retained. Manual runs default to
report-only mode.

## Required release gate

Before promoting `develop` to `main`:

1. All required CI tests and the critical-vulnerability image scan pass.
2. Test coverage stays at or above the current 80% enforced floor. Raise the
   floor only with useful failure-path tests; the next target is 85%.
3. `python metafusion.py --preflight` passes in the intended deployment.
4. A full scan completes with no unexpected item failures and with cleanup
   disabled or dry-run reviewed.
5. An immediate unchanged incremental run processes only items that are due.
6. Scheduler restart/catch-up and graceful stop complete on Unraid.
7. `/config` SQLite state and any Kometa output are backed up while the
   container is stopped.
8. The exact commit is soaked on `develop`; release tags are created only from
   the matching `main` commit.

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
