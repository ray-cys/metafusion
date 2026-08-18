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
