# MetaFusion

MetaFusion inventories selected Plex libraries, retrieves metadata and artwork
from TMDb, and prepares the result for either Kometa or Plex. It is not a Plex
scanner or metadata agent, and it never modifies video or audio files.

## What MetaFusion does

- Reads movies, shows, seasons, Specials/Season 0, and episodes from Plex.
- Generates Kometa YAML and assets, or writes Plex-compatible local artwork.
- Can optionally fill selected Plex metadata fields through the Plex API.
- Skips unchanged items between periodic reconciliation scans.
- Rechecks movie, series, and season artwork on separate schedules.
- Preserves manual artwork through ownership and checksum safeguards.
- Handles multiple movie editions when Plex edition names are unique.
- Runs once or as a long-running Docker scheduler on AMD64 and ARM64.

## Start here

Choose the guide for your platform:

- [Docker Compose installation and upgrades](docs/docker-compose.md)
- [Unraid installation and permissions](docs/unraid.md)
- [Complete environment-variable and `config.yml` reference](docs/configuration.md)
- [Policy behavior and safety rules](docs/policies.md)
- [Scheduling, maintenance, state, and troubleshooting](docs/operations.md)

Required connections are a reachable Plex server, Plex token, TMDb API key,
and exact Plex library names. Kometa mode also needs a writable Kometa output
path. Plex artwork mode needs writable media mappings.

## Choose an operation mode

`RUN_MODE` selects one of two distinct workflows:

| Goal | Required setting | Output | How Plex changes |
| --- | --- | --- | --- |
| Generate files for Kometa | `RUN_MODE=kometa` | YAML under `/kometa/metadata` and artwork under `/kometa/assets` | Kometa applies the generated files during a later Kometa run. MetaFusion never edits Plex metadata directly. |
| Use Plex local artwork only | `RUN_MODE=plex` and `PLEX_METADATA_UPDATES=False` | Artwork beside mapped media files; no Kometa YAML | Plex discovers the local artwork. MetaFusion does not edit metadata fields. |
| Use local artwork and cautious API enrichment | `RUN_MODE=plex` and `PLEX_METADATA_UPDATES=True` | Local artwork plus selected direct Plex API updates; no Kometa YAML | MetaFusion applies only supported fields allowed by the selected metadata policy. |

Artwork and metadata are independent. In Plex mode you may disable every
artwork option and use direct metadata enrichment only, or leave direct
metadata updates disabled and generate artwork only.

### Kometa mode

```text
Plex inventory + TMDb -> MetaFusion -> Kometa YAML/assets -> Kometa -> Plex
```

- `RUN_BASIC=True` generates core movie, show, season, and episode metadata.
- `RUN_ENHANCED=True` adds Kometa-supported director, writer, and producer
  fields. Cast and character roles remain with Plex's online provider.
- `RUN_POSTER`, `RUN_SEASON`, and `RUN_BACKGROUND` control generated artwork.
- Deleting either generated metadata file causes the next eligible run to
  rebuild it, provided `RUN_BASIC=True`.
- MetaFusion preserves unknown/manual YAML fields and known-good output when a
  partial TMDb request fails.

Kometa must be configured to read the generated YAML and asset directories and
must run after MetaFusion. `PLEX_METADATA_UPDATES=True` is invalid in Kometa
mode.

### Plex mode

```text
Plex inventory + TMDb -> MetaFusion -> local artwork and/or Plex API -> Plex
```

- MetaFusion never creates or updates Kometa YAML.
- Local artwork is written beside media using Plex-compatible filenames.
- `PLEX_PATH_MAPPINGS` translates paths returned by Plex to container paths;
  matching writable Docker mappings are still required.
- Direct Plex metadata enrichment is disabled unless
  `PLEX_METADATA_UPDATES=True`.
- Successful API edits appear immediately. Local artwork appears when Plex
  next discovers it according to its library scan and local-assets settings.
- MetaFusion does not trigger a metadata refresh because that could replace
  unlocked values supplied by Plex's provider.

MetaFusion does not generate episode artwork in either mode.

## Minimum configuration

These values identify the connection, libraries, and output workflow:

```text
RUN_MODE=kometa                  # or plex
PLEX_URL=http://plex:32400
PLEX_TOKEN=your-token
PLEX_LIBRARIES=Movies,TV Shows
TMDB_API_KEY=your-key
```

For an environment-only installation, no `config.yml` is created or required.
The container maintains a value-free `/config/config_template.yml`; copy it to
`/config/config.yml` only when YAML configuration is wanted. Never edit the
managed template itself.

Configuration priority, from lowest to highest, is:

1. Built-in defaults.
2. `/config/config.yml`.
3. Secret files.
4. Non-empty environment variables.

A missing or blank environment variable falls back to the next available
source. Secrets are redacted from MetaFusion logs, but Docker and Unraid
administrators can still inspect environment values. See the
[configuration reference](docs/configuration.md) for every supported setting.

## Policy map

MetaFusion has three separate policy controls. They do not change one another:

| Setting | Scope | Default |
| --- | --- | --- |
| `ASSET_UPDATE_POLICY` | Existing poster, background, and season-poster files | `managed` |
| `PLEX_METADATA_POLICY` | Supported direct Plex API metadata fields when API updates are enabled | `fill_missing` |
| `KOMETA_TAG_POLICY` | Supported tags written to Kometa YAML | `append` |

`RUN_CLEANUP` is a separate, disabled-by-default reconciliation operation. It
does not make `overwrite` less safe or make `fill_missing` more aggressive.

### Artwork update policy

All artwork policies create a file when the destination is missing. Their
difference is what may happen when a file already exists:

| Existing artwork | `fill_missing` | `managed` | `overwrite` |
| --- | --- | --- | --- |
| Unchanged file previously written by MetaFusion | Preserve | Eligible for a quality upgrade | Eligible for a quality upgrade |
| MetaFusion file changed afterward | Preserve | Preserve because its checksum changed | Eligible for replacement |
| Manual or third-party file with no ownership record | Preserve | Preserve | Eligible for replacement |

`managed` is the recommended default. MetaFusion replaces an existing file
only when its path and current SHA-256 checksum still match the ownership
record saved when MetaFusion wrote it. If that record is absent or lacks a
checksum, MetaFusion can adopt the file only when its bytes exactly match the
currently selected TMDb image. Adoption records ownership without rewriting
the file or changing its owner, permissions, or timestamps; different artwork
remains protected. `fill_missing` never replaces an existing file. `overwrite`
bypasses ownership protection but still does not blindly rewrite artwork:
identical sources are skipped and quality safeguards reject stale candidates
with lower dimensions or lower TMDb vote scores.

No artwork policy bypasses destination-collision protection. Different Plex
items cannot silently overwrite the same path unless they resolve to the same
TMDb identity and exact TMDb artwork. Cleanup also retains its own checksum
safeguards even when the update policy is `overwrite`.

### Direct Plex metadata policy

These policies apply only when both `RUN_MODE=plex` and
`PLEX_METADATA_UPDATES=True`:

- `fill_missing` fills empty supported scalar fields and appends missing
  supported tags. It does not replace values, remove tags, or cross an
  existing Plex lock.
- `managed` begins with the same safe behavior, then may update or remove only
  values recorded as MetaFusion-owned. Manual changes are retained as
  conflicts.
- `overwrite` makes selected supported fields match TMDb, including removals.
  It requires `PLEX_METADATA_ALLOW_OVERWRITE=True`.

Begin with `fill_missing`, a low `PLEX_METADATA_MAX_WRITES_PER_RUN`, and
`DRY_RUN=True`. Direct metadata dry-runs write only a redacted audit report.
See [Policy behavior and safety rules](docs/policies.md) for the supported
fields, lock behavior, reports, restoration commands, and limitations.

### Kometa tag policy

- `append` preserves provider/user tags and adds missing supported TMDb tags.
- `sync` makes supported generated tag fields match TMDb and should be used
  only when TMDb is intended to be authoritative for those tags.

This setting affects generated Kometa YAML only. It does not control Plex API
metadata or artwork files.

### Cleanup policy

`RUN_CLEANUP=False` is the default. Cleanup runs only during a complete,
successful reconciliation scan. Missing libraries, incomplete inventories,
processing failures, invalid YAML, and write failures prevent cleanup.

In Kometa mode, cleanup can remove stale generated YAML entries and artwork
whose exact path and checksum still prove MetaFusion ownership. Modified,
unmanaged, symbolic-link, and unverifiable artwork is preserved. In Plex mode,
cleanup removes stale MetaFusion state only; it never removes local artwork,
Kometa output, or media files.

Shared canonical artwork used by multiple Plex editions is evaluated once and
is removable only when the current checksum matches a recorded owner. A legacy
file that is already orphaned cannot be safely adopted and remains a manual
cleanup decision.

Always test cleanup with `DRY_RUN=True`. The full checklist is in
[Policy behavior and safety rules](docs/policies.md#cleanup-and-deletion-safety).

## Scheduling and incremental processing

The container normally remains running and executes at `RUN_TIMES` in the
configured `TZ`. `RUN_ON_START` controls an immediate scheduler-start job;
`SCHEDULE_CATCH_UP` uses durable job history to recover a recently missed
scheduled slot after a restart. Scheduling is based on saved timestamps, not
container uptime.

`INCREMENTAL=True` skips successfully processed unchanged items.
`FULL_SCAN_INTERVAL_HOURS` controls the maximum time between complete
reconciliation scans. Artwork refresh intervals independently make unchanged
items eligible for reconsideration:

```text
MOVIE_IMAGE_UPGRADE_DAYS=30
SERIES_IMAGE_UPGRADE_DAYS=15
SEASON_IMAGE_UPGRADE_DAYS=15
```

These intervals use saved upgrade timestamps, not filesystem modification
times. `0` disables timed rechecks for that media type. The artwork update
policy still decides whether an existing file may be replaced.

See [Scheduling and operations](docs/operations.md) for one-shot runs,
targeted repairs, catch-up behavior, health checks, and diagnostics.

## Identity and edition safety

MetaFusion prefers verified Plex external IDs. Optional title search is
conservative and rejects ambiguous title/year matches. Alternate TMDb episode
groups are used only when one group uniquely covers Plex's episode inventory.

Every same-title/year movie copy should have a unique Plex edition name.
Duplicate or blank edition identities stop safely by default;
`ALLOW_AMBIGUOUS_EDITIONS=True` opts into the risk of matching the wrong copy.
Editions in one physical folder can share canonical artwork only when their
TMDb identity and selected image are identical. Use separate folders when
editions need different local artwork.

## Output and persistent state

Kometa mode writes:

```text
/kometa/metadata/movie_metadata.yml
/kometa/metadata/tv_metadata.yml
/kometa/metadata/.metafusion-backups/*.bak
/kometa/assets/movie/...
/kometa/assets/tv/...
```

Both modes use `/config` for configuration, reports, logs, and SQLite state:

```text
/config/config_template.yml
/config/logs/metafusion.log
/config/cache/meta_db.sqlite3
/config/cache/tmdb_cache.sqlite3
/config/reports/artwork-gaps-*.txt
/config/reports/plex-metadata-*.txt       # direct Plex metadata runs only
```

The TMDb response cache is disposable. Durable inventory, scan, job, and
artwork-ownership state remains isolated in `meta_db.sqlite3`. The live
heartbeat is stored in `/tmp/metafusion-status.json` to avoid persistent writes
every 30 seconds. Back up SQLite files while MetaFusion is stopped.

## Docker image tags and rollback

| Tag | Purpose |
| --- | --- |
| `1.2.3` | Exact stable release; recommended for production |
| `1.2.3-rc.1` | Exact release candidate for testing |
| `sha-<full-commit>` | Immutable diagnostic or rollback build |
| `develop` | Moving test build; never updates `latest` |
| `main`, `latest` | Moving production builds from `main` |

Production installations should pin an exact release. Change only the image
tag to roll back; keep `/config`, `/kometa`, permissions, and media mappings
unchanged. Platform-specific update steps are in the
[Docker Compose](docs/docker-compose.md#update-or-roll-back) and
[Unraid](docs/unraid.md#update-or-roll-back) guides.

## Support and diagnostics

Useful commands inside the container are:

```bash
python metafusion.py --doctor
python metafusion.py --status
python metafusion.py --support-report
```

Reports redact connector secrets. Do not publish `config.yml`, Docker
inspection output, tokens, API keys, or unredacted host paths. For operational
checks and common symptoms, see
[Scheduling, maintenance, state, and troubleshooting](docs/operations.md).

## References

- [Kometa metadata files](https://kometa.wiki/en/latest/files/metadata/)
- [Finding a Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- [Plex local TV artwork names](https://support.plex.tv/articles/200220717-local-media-assets-tv-shows/)
- [Python-PlexAPI edit and lock methods](https://python-plexapi.readthedocs.io/en/latest/modules/mixins.html)
- [TMDb API documentation](https://developer.themoviedb.org/docs)
