# MetaFusion

MetaFusion inventories selected Plex libraries, retrieves metadata and artwork
from TMDb, and prepares the result for either Kometa or Plex. It is not a Plex
scanner or metadata agent, and it never modifies video or audio files.

## What MetaFusion does

- Generates Kometa metadata YAML and assets, or Plex-compatible local artwork.
- Selects artwork through a guarded TMDb → Fanart.tv → Plex fallback chain.
- Optionally enriches selected Plex metadata fields through the Plex API.
- Processes new and changed movies, shows, seasons, Specials, and episodes
  incrementally, including TMDb-only changes, with durable retries and periodic
  reconciliation.
- Qualifies each published upgrade before output writes and can verify Kometa
  results against live Plex after Kometa runs.
- Preserves manual artwork through ownership, checksum, quality, and collision
  safeguards.
- Adapts processing and provider concurrency to container resources and
  upstream health, retaining short provider cooldowns across scheduled jobs.
- Reloads validated YAML changes between scheduled jobs and retains bounded
  run-performance history with schedule-capacity guidance.
- Provides read-only plans, audits, item explanations, and maintenance tools.
- Runs once or as a Docker scheduler on AMD64 and ARM64.

## Quick start

Choose one installation guide:

- [Docker Compose](docs/docker-compose.md)
- [Unraid](docs/unraid.md)

The minimum connection and workflow settings are:

```text
RUN_MODE=kometa                  # or plex
PLEX_URL=http://plex:32400
PLEX_TOKEN=your-token
PLEX_LIBRARIES=auto              # or exact comma-separated names
TMDB_API_KEY=your-key
```

`PLEX_LIBRARIES=auto` discovers every supported Plex movie/show library.
Kometa mode also needs a writable Kometa output mapping; Plex artwork mode
needs writable media mappings. Fanart.tv artwork fallback uses MetaFusion's
bundled project key and requires no user setting.

Environment-only installations do not need a YAML file. For YAML-based setup,
the container maintains a conventional `/config/config_template.yml` plus
ready-to-copy `/config/examples/kometa.yml` and
`/config/examples/plex.yml` profiles. Copy a profile to `/config` without
renaming it, or copy the conventional template to `/config/config.yml`.
MetaFusion never creates an active configuration file or stores environment
secrets in one. You can view or download the packaged
[conventional template](config/config_template.yml),
[Kometa profile](config/examples/kometa.yml), or
[Plex profile](config/examples/plex.yml) directly from this repository. See the
[configuration reference](docs/configuration.md) for selection rules, source
priority, every setting, and per-library overrides.

## Choose an output mode

| Goal | Setting | Output and Plex effect |
| --- | --- | --- |
| Generate files for Kometa | `RUN_MODE=kometa` | Writes Kometa YAML/assets. A later Kometa run applies them to Plex; MetaFusion does not edit Plex directly. |
| Use Plex local artwork | `RUN_MODE=plex` and `PLEX_METADATA_UPDATES=False` | Writes artwork beside mapped media, creates no Kometa YAML, and does not edit Plex metadata fields. |
| Use local artwork and cautious API enrichment | `RUN_MODE=plex` and `PLEX_METADATA_UPDATES=True` | Writes local artwork and applies only supported fields allowed by the selected Plex metadata policy. |

Artwork and metadata are independent. Read [Kometa and Plex operation
modes](docs/modes.md) before choosing paths or enabling direct Plex metadata.

## Safe defaults

| Control | Default | Meaning |
| --- | --- | --- |
| `ASSET_UPDATE_POLICY` | `managed` | Upgrade only artwork that MetaFusion can verify it still owns. |
| `PLEX_METADATA_POLICY` | `fill_missing` | Fill empty supported Plex fields without replacing existing values or crossing locks. |
| `KOMETA_TAG_POLICY` | `append` | Add supported generated tags while preserving existing tags. |
| `RUN_CLEANUP` | `False` | Do not reconcile stale generated output until explicitly enabled and dry-run reviewed. |
| Cleanup confirmation | 2 scans plus 48 hours | Keep a missing item pending before any eligible state/output removal. |
| Cleanup quarantine | 14 days | Retain checksum-proven artwork removed by automatic cleanup so it can be restored before expiry. |

Keep these defaults for the first full run and use `DRY_RUN=True` before
enabling cleanup or aggressive metadata/artwork replacement. The
[policy and safety guide](docs/policies.md) explains ownership, locks, quality
scoring, editions, identity validation, and deletion safeguards.

## Documentation

Use the [documentation index](docs/index.md) to find installation,
configuration, mode, policy, provider, scheduling, diagnostics, maintenance,
support, and release guidance.

Common destinations:

- [TMDb rechecks, upgrade canary, and post-Kometa verification](docs/runtime-safeguards.md)
- [Artwork fallback and attribution](docs/artwork-providers.md)
- [Scheduling, incremental processing, SQLite, and troubleshooting](docs/operations.md)
- [Diagnostics, audits, and reports](docs/diagnostics.md)
- [Targeted lifecycle management, cleanup history, and recovery](docs/lifecycle-management.md)
- [Support and version policy](SUPPORT.md)

For production, pin an exact stable version. Use `develop` only for testing;
see the [release lanes and qualification process](docs/release-testing.md).

## Support and diagnostics

Run these inside the container before opening an issue:

```bash
metafusion --doctor
metafusion --preflight
metafusion --support-report
metafusion --dashboard-report
```

Automatic dashboard refresh is disabled by default. Enable it with
`output.dashboard_enabled: true` or `DASHBOARD_ENABLED=True`; the command above
always remains available for an on-demand report.

Use the [GitHub issue chooser](https://github.com/ray-cys/metafusion/issues/new/choose)
to select the form matching the problem. Run only the checks relevant to that
form, and explain when requested diagnostics are unavailable.

Attach only the relevant redacted log section and problem-specific report.
Never publish `config.yml`, Docker inspection output, tokens, API keys, or
unredacted private paths. The offline dashboard is intended for local review;
inspect it before sharing because library and media titles are included. The
[diagnostics guide](docs/diagnostics.md) explains
which report to use, and the [command-line reference](docs/operations.md#command-line-reference)
lists every public flag.

## TMDb attribution

<a href="https://www.themoviedb.org"><img src="asset/tmdb_logo.svg" alt="TMDB" width="220"></a>

This product uses the TMDB API but is not endorsed or certified by TMDB.
The logo is an unmodified [approved TMDB mark](https://www.themoviedb.org/about/logos-attribution)
and remains the property of TMDB.

## Copyright

Copyright (c) 2026 ray-cys. All rights reserved. No open-source licence is
granted; see [COPYRIGHT](COPYRIGHT).

The optional [Formula 1 extension](docs/formula1.md) is documented separately.
