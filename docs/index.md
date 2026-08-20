# MetaFusion documentation

Use this index to find the guide for the task at hand. The root README remains
a short project overview and first-run entry point.

## Install and configure

| Task | Guide |
| --- | --- |
| Install, map storage, upgrade, or roll back with Docker Compose | [Docker Compose setup](docker-compose.md) |
| Install, map appdata/media, and run with Unraid ownership `99:100` | [Unraid setup](unraid.md) |
| Choose between Kometa output and Plex local artwork/API enrichment | [Kometa and Plex operation modes](modes.md) |
| Choose `config.yml`, `kometa.yml`, `plex.yml`, environment-only setup, secrets, and overrides | [Configuration reference](configuration.md) |
| Inspect every generated configuration variable and surface | [Generated configuration table](configuration.generated.md) |

## Understand behavior and safety

| Topic | Guide |
| --- | --- |
| Artwork, Plex metadata, Kometa tags, cleanup, identity, and edition safeguards | [Policy behavior and safety rules](policies.md) |
| TMDb → Fanart.tv → Plex artwork order, quality, failure behavior, and attribution | [Artwork providers](artwork-providers.md) |
| Scheduling, incremental/full scans, retries, SQLite, reports, shutdown, and troubleshooting | [Operations](operations.md) |
| Understand TMDb change rechecks, the published-build canary, and post-Kometa readback | [Runtime safeguards and application verification](runtime-safeguards.md) |

## Diagnose and maintain

| Task | Guide |
| --- | --- |
| Choose a local check, connector preflight, audit, plan, or item explanation | [Diagnostics and support reports](diagnostics.md) |
| Verify generated Kometa metadata and artwork after Kometa applies it | [Post-Kometa verification](runtime-safeguards.md#post-kometa-application-verification) |
| Find every supported command-line flag | [CLI reference](operations.md#command-line-reference) |
| Manage one item's output, exceptions, identity review, migrations, cleanup history, or recovery | [Lifecycle management](lifecycle-management.md) |
| Report a problem and confirm the supported release lane | [Support and version policy](../SUPPORT.md) |

## Develop and release

| Task | Guide |
| --- | --- |
| Understand CI, image lanes, provider contracts, soak gates, and stable publication | [Development and release testing](release-testing.md) |
| Review project source-use terms | [Copyright](../COPYRIGHT) |

Provider-specific external references and attribution are kept beside the
features that use them instead of duplicated here.
