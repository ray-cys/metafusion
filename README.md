# MetaFusion

MetaFusion reads the existing items in selected Plex libraries and obtains
metadata and artwork from TMDb. It is not a Plex scanner or metadata agent.
Instead, it prepares output for either Kometa or Plex according to `RUN_MODE`.

## What it does

- Reads movies, shows, seasons, Specials/Season 0, and episodes from Plex.
- Retrieves additional metadata and artwork from TMDb.
- Generates Kometa YAML and assets, or writes Plex-compatible local artwork.
- Can optionally fill selected missing Plex metadata fields through the Plex API.
- Skips unchanged Plex items between periodic reconciliation scans.
- Supports separate movie, series, and season artwork refresh intervals.
- Preserves manually managed assets during guarded, opt-in cleanup.
- Handles multiple movie editions when Plex edition names are unique.
- Runs once or as a long-running Docker scheduler.

## Requirements

- A reachable Plex server and Plex token.
- A TMDb API key.
- Docker Compose v2 or an Unraid Docker installation.
- A writable Kometa path when `RUN_MODE=kometa`.

## Choose how MetaFusion operates

Choose one workflow before configuring the container:

| Your goal | Settings | MetaFusion output | Who changes Plex metadata? |
| --- | --- | --- | --- |
| Use MetaFusion with Kometa | `RUN_MODE=kometa` | YAML under `/kometa/metadata` and artwork under `/kometa/assets` | Kometa, when Kometa next reads the generated files |
| Use Plex without direct metadata edits | `RUN_MODE=plex` and `PLEX_METADATA_UPDATES=False` | Local artwork beside the media files; no YAML | Plex's existing metadata provider; MetaFusion makes no metadata edits |
| Use Plex with TMDb metadata gap-filling | `RUN_MODE=plex` and `PLEX_METADATA_UPDATES=True` | Local artwork plus optional direct Plex API edits; no YAML | MetaFusion fills or manages only the configured supported fields |

The important distinction is:

- `RUN_MODE=kometa` never edits Plex metadata directly. MetaFusion writes files,
  and a separate Kometa run applies them to Plex.
- `RUN_MODE=plex` never creates Kometa YAML. It writes local artwork beside the
  media. Direct metadata editing is a separate opt-in controlled by
  `PLEX_METADATA_UPDATES` and is disabled by default.
- Artwork controls (`RUN_POSTER`, `RUN_SEASON`, and `RUN_BACKGROUND`) are
  independent of direct Plex metadata editing.

If you already run Kometa, choose Kometa mode. If you do not use Kometa,
choose Plex mode and begin with direct metadata updates disabled.

## Quick start

Set `RUN_MODE` for the workflow selected above. Plex mode also needs writable
media mappings when any artwork option is enabled. Kometa mode needs the
`/kometa` mapping but does not need media-directory mappings.

Start with one of these combinations:

```text
# Existing Kometa installation: generate files for Kometa to consume
RUN_MODE=kometa
PLEX_METADATA_UPDATES=False

# No Kometa: write local artwork but leave Plex metadata unchanged
RUN_MODE=plex
PLEX_METADATA_UPDATES=False

# No Kometa: write local artwork and cautiously fill missing Plex metadata
RUN_MODE=plex
PLEX_METADATA_UPDATES=True
PLEX_METADATA_POLICY=fill_missing
DRY_RUN=True
```

The third example is initially a preview. Review its report before setting
`DRY_RUN=False`.

### Environment-variable configuration

```bash
cp .env.example .env
mkdir -p config kometa
# Add your Plex token, TMDb key, library names, and host paths to .env.
docker compose up -d
docker compose logs -f metafusion
```

### `config.yml` configuration

The container maintains a current, value-free reference at
`/config/config_template.yml`. To switch to YAML configuration, create a
separate `config.yml` from that reference:

```bash
mkdir -p config kometa
docker compose up -d
cp config/config_template.yml config/config.yml
# Edit config/config.yml. A .env file is not required.
docker compose restart metafusion
```

`config_template.yml` is managed by the container and refreshed when the image
contains a newer template, so do not store settings in it. MetaFusion never
copies environment values or secrets into either YAML file and does not create
`config.yml` when environment configuration is present. Existing `config.yml`
files are never overwritten by template maintenance.

Configuration priority is:

1. Built-in defaults.
2. `/config/config.yml`.
3. Secret files.
4. Non-empty environment variables.

Environment variables have the highest priority. A missing or blank variable
falls back to a secret file, `config.yml`, or the built-in default. When
environment configuration is supplied without `config.yml`, MetaFusion does
not generate a file containing those values.

## Unraid permissions

Use Unraid's standard `nobody:users` identity:

```text
PUID=99
PGID=100
```

The mapped `/config` directory and, in Kometa mode, `/kometa` directory must be
writable by `99:100`. New files are created as `0664` and owned by
`nobody:users`; existing metadata and artwork retain their ownership and
permissions. Container startup adjusts only MetaFusion's managed state under
`/config`, not the complete Kometa or media tree.

Keep the image entrypoint unchanged and do not set a Docker user. An explicit
Compose `user:` or `docker run --user` setting overrides `PUID` and `PGID` and
can create files with the wrong owner.

### Unraid template

The repository includes a ready-to-import
[Unraid Docker template](unraid/metafusion.xml). It exposes every supported
container environment variable. Common settings remain visible in the basic
view, while tuning and safety controls are available under **Advanced View**.

The template requires only the connection, library, and output-mode values:

- `RUN_MODE`
- `PLEX_URL`
- `PLEX_TOKEN`
- `PLEX_LIBRARIES`
- `TMDB_API_KEY`

The `/config` mapping is also required for persistent application state and the
managed `/config/config_template.yml` reference. Duplicate that file as
`config.yml` only when YAML configuration is wanted; environment-only setups
can leave `config.yml` absent.

`/kometa` is needed only in Kometa mode. In Plex mode, add writable media path
mappings for every artwork destination. If Plex reports different paths, set
`PLEX_PATH_MAPPINGS` to translate them. Keep the image entrypoint and Docker
user settings unchanged so `PUID=99` and `PGID=100` can take effect.

## Running MetaFusion

The Docker service defaults to scheduler mode. `RUN_TIMES` uses `TZ`.

Common commands:

```bash
# Run one job now
docker compose run --rm -e METAFUSION_RUN=True metafusion

# Validate configuration without contacting Plex or TMDb
docker compose run --rm metafusion python metafusion.py --doctor

# Show scheduler status and recent jobs
docker compose exec metafusion python metafusion.py --status

# Create a value-free diagnostic file for a GitHub issue
docker compose run --rm metafusion python metafusion.py --support-report
```

Do not leave `METAFUSION_RUN=True` on the long-running service unless a new
one-shot run after every container restart is intentional.

For a targeted repair, supply a Plex library and rating key:

```bash
# Repair metadata only
docker compose run --rm metafusion python metafusion.py \
  --metafusion_run --library Movies --rating-key 12345 --metadata-only

# Repair artwork only
docker compose run --rm metafusion python metafusion.py \
  --metafusion_run --library "TV Shows" --rating-key 12345 --asset-only
```

Targeted runs disable cleanup. `--library` and `--rating-key` may be repeated or
comma-separated. Add `--full-scan` to ignore incremental state, or
`--explain-selection` to report why an item is due without processing it.

## What happens during a run

Every job starts the same way:

1. MetaFusion connects to Plex and inventories only the names in
   `PLEX_LIBRARIES`.
2. It uses the Plex matches to retrieve metadata and artwork from TMDb.
3. It skips unchanged items when incremental processing says they are not due.
4. It writes different output according to `RUN_MODE`.
5. It stores cache and processing state under `/config`.

MetaFusion never replaces Plex's scanner, never modifies video/audio media
files, and never deletes movies or episodes.

### `RUN_MODE=kometa`: hand files to Kometa

```text
Plex inventory + TMDb -> MetaFusion -> /kometa YAML and assets -> Kometa -> Plex
```

MetaFusion's job ends after writing Kometa-compatible files:

- `RUN_BASIC=True` writes the core metadata to
  `/kometa/metadata/movie_metadata.yml` and `tv_metadata.yml`.
- `RUN_ENHANCED=True` adds the supported cast and crew metadata to those files.
- `RUN_POSTER`, `RUN_SEASON`, and `RUN_BACKGROUND` control artwork under
  `/kometa/assets`.

MetaFusion does not edit Plex metadata or place artwork beside media in this
mode. Kometa must be configured to read the generated YAML/assets and must run
afterward before Plex changes. `PLEX_METADATA_UPDATES=True` is rejected in
Kometa mode because direct Plex edits and Kometa output are separate workflows.

### `RUN_MODE=plex`: work directly with a Plex library

```text
Plex inventory + TMDb -> MetaFusion -> local artwork and/or Plex API -> Plex
```

Plex mode never creates or updates Kometa YAML. It provides two independent
functions:

| Function | How to enable it | Result |
| --- | --- | --- |
| Local artwork | Enable `RUN_POSTER`, `RUN_SEASON`, or `RUN_BACKGROUND` | MetaFusion writes Plex-compatible filenames beside the media. Writable media mounts and correct path mappings are required. |
| Direct metadata enrichment | Set `PLEX_METADATA_UPDATES=True` | MetaFusion compares supported Plex fields with TMDb and applies the selected safety policy through the Plex API. No media mount is needed for metadata-only use. |

With `PLEX_METADATA_UPDATES=False`, Plex mode is artwork-only and MetaFusion
does not change any Plex metadata field. `RUN_BASIC` and `RUN_ENHANCED` affect
direct Plex metadata only after that opt-in is enabled. Artwork settings remain
independent of those metadata settings.

Plex receives successful API metadata edits immediately. Local artwork appears
when Plex next discovers it according to the library's scan and local-assets
settings. MetaFusion does not force a metadata refresh, because a refresh can
also replace unlocked metadata supplied by another provider.

### Direct Plex metadata scope

Direct Plex metadata is intentionally narrower than Kometa metadata because it
changes the live Plex database. Supported fields are:

| Plex item | Basic fields and tags | Enhanced additions |
| --- | --- | --- |
| Movie | Original title, release date, content rating, studio, tagline, summary, countries, genres | Directors, writers, producers |
| Show | Original title, first-air date, content rating, network/studio, tagline, summary, countries, genres | None |
| Season | Missing title and summary | None |
| Episode | Missing title, summary, and air date | Directors and writers |

`RUN_BASIC` controls the basic columns above. `RUN_ENHANCED` adds only the
listed crew fields and requires `RUN_BASIC=True`. MetaFusion intentionally
leaves matching IDs, cast and character roles, audience/critic ratings, labels,
collections, playback data, extras, recommendations, and provider-specific
artwork choices to Plex's online provider or the user.

### Direct Plex metadata safety

Direct metadata updates rely on Plex's HTTP API through the community
Python-PlexAPI client. This is more aggressive than generating Kometa YAML:
Plex API behavior can vary with Plex Media Server releases, library agents,
field locks, token ownership, and existing manual edits.

Use an owner/admin Plex token, back up the Plex database, and begin with:

```text
RUN_MODE=plex
PLEX_METADATA_UPDATES=True
PLEX_METADATA_POLICY=fill_missing
PLEX_METADATA_MAX_WRITES_PER_RUN=25
DRY_RUN=True
```

Review `/config/reports/plex-metadata-*.txt`, then disable dry-run and increase
the write limit gradually. A dry-run never edits Plex, YAML, artwork, cache, or
SQLite; the text audit report is its only persistent output.

The policies are:

- `fill_missing`: fills empty scalar fields and appends missing supported tags.
  It never replaces existing values, removes tags, or crosses an existing
  Plex field lock.
- `managed`: starts with the same safe behavior, then updates or removes only
  values recorded as MetaFusion-owned. A manual change causes a conflict and
  MetaFusion leaves that field alone.
- `overwrite`: makes selected supported fields match TMDb, including removal
  of other values. It requires `PLEX_METADATA_ALLOW_OVERWRITE=True` and should
  be limited with `PLEX_METADATA_FIELDS` and a low write cap.

MetaFusion does not trigger a Plex metadata refresh after direct edits; a
refresh can immediately let the online provider replace unlocked values.
Unchanged values cause no API write and no new lock. Existing locked fields are
skipped by `fill_missing` and `managed` unless the ownership ledger shows that
MetaFusion created the lock. `PLEX_METADATA_LOCK_MERGED_TAGS=True` locks the
whole merged tag field, including tags supplied by Plex or the user, so leave
it off unless this effect is intentional. Turning direct updates off does not
unlock fields or revert earlier writes.

Use targeted maintenance when needed:

```bash
# Preview restoring only values still equal to MetaFusion's last write
docker compose run --rm -e DRY_RUN=True metafusion python metafusion.py \
  --plex-metadata-restore --library Movies --rating-key 12345

# Restore prior values and lock states
docker compose run --rm metafusion python metafusion.py \
  --plex-metadata-restore --library Movies --rating-key 12345

# Keep values, but remove only locks recorded as MetaFusion-created
docker compose run --rm metafusion python metafusion.py \
  --plex-metadata-unlock --library Movies --rating-key 12345
```

Both commands require `RUN_MODE=plex` and at least one `--rating-key`; they
refuse a library-wide operation. If a current value differs from MetaFusion's
ownership ledger, it is treated as a manual change and retained.

Plex metadata reports contain field names and outcomes, not summaries, tokens,
or API keys. Reports distinguish fills, additions, unchanged values, locks,
manual conflicts, failures, and write-limit skips. This provides a compact
GitHub issue attachment without exposing the metadata itself.
Use `--support-report` to add environment-binding names, database health,
platform details, and configuration-validation status without their values.

Plex installations vary widely, so user testing should focus on one or two
representative items before increasing the write cap. If behavior differs from
the policies above, open the repository's **Plex metadata issue** form and
attach the latest metadata report, support report, Plex server version, and
redacted relevant logs. Never attach `config.yml`, Docker inspection output,
tokens, API keys, or unredacted host paths.

### Plex media path mapping

Plex returns paths from the Plex server's filesystem. If those paths differ
inside the MetaFusion container, use semicolon-separated longest-prefix
translations:

```text
PLEX_PATH_MAPPINGS=/mnt/user/media=>/media;/mnt/disks/archive=>/archive
```

This setting does not create Docker mounts. Add matching writable bind mounts
for `/media`, `/archive`, or each chosen destination. With no mappings,
MetaFusion uses Plex's path unchanged. A translated media directory must
already exist and be writable; MetaFusion will not create a missing media
directory that may indicate a bad mount. Movie parts must resolve to one movie
folder, every season must resolve to one real season folder, and all seasons
must share one show folder; ambiguous layouts are skipped rather than written
to a guessed path. Specials use Plex's documented
`season-specials-poster.jpg` name in their actual `Season 00` or `Specials`
folder.

## Environment variables

The tables below list every supported user-configurable Docker variable.

### Connections, libraries, and output mode

| Variable | Default | Purpose |
| --- | --- | --- |
| `PLEX_URL` | `http://10.0.0.1:32400` | Complete Plex server URL. |
| `PLEX_TOKEN` | required | Plex authentication token. |
| `PLEX_TOKEN_FILE` | unset | File containing the Plex token; direct token wins. |
| `PLEX_LIBRARIES` | `Movies,TV Shows` | Comma-separated exact Plex library names. |
| `PLEX_PATH_MAPPINGS` | unset | Plex-artwork mode only. Semicolon-separated `PLEX_PATH=>CONTAINER_PATH` translations; bind mounts are still required. |
| `TMDB_API_KEY` | required | TMDb API key. |
| `TMDB_API_KEY_FILE` | unset | File containing the TMDb key; direct key wins. |
| `TMDB_LANGUAGE` | `en-US` | TMDb metadata language and primary artwork language. |
| `TMDB_LANGUAGE_FALLBACK` | `zh,ja` | Ordered artwork-only language fallbacks; these never change metadata text. |
| `TMDB_REGION` | `US` | Metadata release/certification region, with US as the fallback. |
| `RUN_MODE` | `kometa` | Select the output workflow: `kometa` writes YAML/assets for Kometa; `plex` never writes Kometa YAML. |
| `KOMETA_PATH` | `/kometa` | Kometa mode only. Container path for generated YAML and assets. |

Tokens and API keys are redacted from MetaFusion logs. Environment values
remain visible to Docker/Unraid administrators. Use the `*_FILE` options when
your deployment supports protected secret mounts.

### Scheduling and processing

| Variable | Default | Purpose |
| --- | --- | --- |
| `METAFUSION_RUN` | `False` | Run once instead of waiting for the scheduler. |
| `RUN_SCHEDULE` | `True` | Enable the long-running scheduler. |
| `RUN_ON_START` | `False` | Run one job when scheduler mode starts, then continue normally. |
| `RUN_TIMES` | `06:00,18:30` | Comma-separated daily run times. |
| `TZ` | `UTC` | Container timezone used by the scheduler. |
| `DRY_RUN` | `False` | Calculate without edits/deletions; direct Plex metadata dry-runs still write a redacted audit report. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `RUN_BASIC` | `True` | Kometa mode: write core YAML fields. Plex mode: enable core API fields only when `PLEX_METADATA_UPDATES=True`. |
| `RUN_ENHANCED` | `True` | Add supported cast/crew YAML in Kometa mode or limited crew API fields in opted-in Plex mode. Requires `RUN_BASIC=True`. |
| `PLEX_METADATA_UPDATES` | `False` | Plex mode only. Opt in to direct Plex API metadata enrichment; when false, MetaFusion leaves Plex metadata unchanged. |
| `PLEX_METADATA_POLICY` | `fill_missing` | `fill_missing`, `managed`, or acknowledged `overwrite`. |
| `PLEX_METADATA_LOCK_WRITES` | `False` | Lock scalar fields written through Plex; use cautiously. |
| `PLEX_METADATA_LOCK_MERGED_TAGS` | `False` | Lock whole merged tag fields after additions. |
| `PLEX_METADATA_ALLOW_OVERWRITE` | `False` | Required acknowledgement for overwrite policy. |
| `PLEX_METADATA_RECHECK_DAYS` | `30` | Recheck interval for unchanged Plex metadata; `0` disables timed rechecks. |
| `PLEX_METADATA_MAX_WRITES_PER_RUN` | `100` | Maximum Plex item/season/episode API writes in one job. |
| `PLEX_METADATA_REPORT_RETENTION` | `10` | Audit reports retained under `/config/reports`. |
| `PLEX_METADATA_FIELDS` | all safe fields | Optional comma-separated direct-write allowlist. |
| `RUN_POSTER` | `True` | Write movie/show posters to Kometa assets or beside Plex media, according to `RUN_MODE`. |
| `RUN_SEASON` | `True` | Write season posters, including Specials, to the output selected by `RUN_MODE`. |
| `RUN_BACKGROUND` | `False` | Write movie/show backgrounds to the output selected by `RUN_MODE`. |
| `RUN_CLEANUP` | `False` | Enable guarded mode-specific cleanup. Test with dry-run first; media files are never deleted. |

### Runtime and Plex reliability

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAX_CONCURRENCY` | `8` | Maximum media items processed concurrently. |
| `REQUEST_TIMEOUT` | `30` | Total TMDb/image request timeout in seconds. |
| `CONNECT_TIMEOUT` | `10` | HTTP connection timeout in seconds. |
| `PLEX_TIMEOUT` | `10` | Timeout for each blocking Plex request. |
| `PLEX_RETRIES` | `3` | Plex startup connection attempts. |
| `PLEX_RETRY_DELAY` | `1` | Base Plex retry delay in seconds. |
| `SHUTDOWN_TIMEOUT` | `15` | Internal graceful-shutdown deadline in seconds. |
| `STOP_GRACE_PERIOD` | `20s` | Docker/Compose stop deadline; keep above `SHUTDOWN_TIMEOUT`. |
| `MAX_IMAGE_MB` | `25` | Maximum accepted artwork download size. |
| `PUID` | `10001` | Runtime user ID; use `99` on Unraid. |
| `PGID` | `10001` | Runtime group ID; use `100` on Unraid. |

### Incremental processing, artwork cadence, cache, and health

| Variable | Default | Purpose |
| --- | --- | --- |
| `INCREMENTAL` | `True` | Skip successfully processed unchanged items. |
| `FULL_SCAN_INTERVAL_HOURS` | `168` | Maximum interval between reconciliation scans. |
| `IMAGE_UPGRADE_DAYS` | `30` | Default timed artwork refresh interval; `0` disables it. |
| `MOVIE_IMAGE_UPGRADE_DAYS` | inherited | Movie poster/background interval. |
| `SERIES_IMAGE_UPGRADE_DAYS` | inherited | Show poster/background interval. |
| `SEASON_IMAGE_UPGRADE_DAYS` | inherited | Season-poster interval. |
| `TMDB_CACHE_ENABLED` | `True` | Persist successful TMDb responses in SQLite. |
| `TMDB_CACHE_TTL_HOURS` | `24` | TMDb response lifetime. |
| `TMDB_CACHE_MAX_ENTRIES` | `5000` | Maximum persisted TMDb responses. |
| `TMDB_CACHE_MAX_MB` | `0` | Optional compressed-payload limit in MiB; `0` disables it. |
| `VALIDATE_OUTPUT` | `True` | Kometa mode only. Validate YAML before replacing known-good output. |
| `OUTPUT_BACKUP_COUNT` | `3` | Kometa mode only. Metadata backups retained per YAML file. |
| `ALLOW_AMBIGUOUS_EDITIONS` | `False` | Allow unsafe duplicate edition matching. |
| `HEALTH_FAIL_ON_JOB_ERROR` | `False` | Mark the container unhealthy after a failed job. |
| `HEALTH_MAX_HEARTBEAT_AGE` | `120` | Maximum health heartbeat age in seconds. |

### Artwork selection (advanced)

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTER_MAX_WIDTH` | `2000` | Preferred maximum poster width. |
| `POSTER_MAX_HEIGHT` | `3000` | Preferred maximum poster height. |
| `POSTER_MIN_WIDTH` | `1000` | Minimum preferred poster width. |
| `POSTER_MIN_HEIGHT` | `1500` | Minimum preferred poster height. |
| `POSTER_PREFER_VOTE` | `5.0` | Preferred TMDb vote score. |
| `POSTER_VOTE_RELAXED` | `3.5` | Relaxed fallback vote score. |
| `POSTER_VOTE_THRESHOLD` | `5.0` | Score used when deciding artwork upgrades. |
| `SEASON_MAX_WIDTH` | `2000` | Preferred maximum season-poster width. |
| `SEASON_MAX_HEIGHT` | `3000` | Preferred maximum season-poster height. |
| `SEASON_MIN_WIDTH` | `1000` | Minimum preferred season-poster width. |
| `SEASON_MIN_HEIGHT` | `1500` | Minimum preferred season-poster height. |
| `SEASON_PREFER_VOTE` | `5.0` | Preferred TMDb vote score. |
| `SEASON_VOTE_RELAXED` | `0.5` | Relaxed fallback vote score. |
| `SEASON_VOTE_THRESHOLD` | `3.0` | Score used when deciding season-poster upgrades. |
| `BG_MAX_WIDTH` | `3840` | Preferred maximum background width. |
| `BG_MAX_HEIGHT` | `2160` | Preferred maximum background height. |
| `BG_MIN_WIDTH` | `1920` | Minimum preferred background width. |
| `BG_MIN_HEIGHT` | `1080` | Minimum preferred background height. |
| `BG_PREFER_VOTE` | `5.0` | Preferred TMDb vote score. |
| `BG_VOTE_RELAXED` | `3.5` | Relaxed fallback vote score. |
| `BG_VOTE_THRESHOLD` | `5.0` | Score used when deciding background upgrades. |

### Paths used by Docker Compose

| Variable | Default | Purpose |
| --- | --- | --- |
| `METAFUSION_IMAGE` | `ghcr.io/ray-cys/metafusion:main` | Compose image tag or digest; pin an exact version for rollback. |
| `CONFIG_PATH` | `./config` | Host path mounted at `/config`. |
| `KOMETA_HOST_PATH` | `./kometa` | Host path mounted at `/kometa`. |
| `CONFIG_DIR` | `/config` | Container configuration/state directory. |
| `STATUS_FILE` | `/tmp/metafusion-status.json` | Ephemeral container heartbeat and health-status file. |

`CONFIG_DIR` and `STATUS_FILE` are normally left at their image defaults.
Existing Unraid installations that retained the older
`/config/metafusion-status.json` value should change `STATUS_FILE` to
`/tmp/metafusion-status.json` to stop persistent heartbeat writes.

## Artwork refresh intervals

Timed artwork refreshes work with incremental processing. On every scheduled
run, MetaFusion selects an otherwise unchanged item only when an enabled
artwork type is due.

Example: movies every 30 days, series and seasons every 15 days:

```text
IMAGE_UPGRADE_DAYS=30
MOVIE_IMAGE_UPGRADE_DAYS=30
SERIES_IMAGE_UPGRADE_DAYS=15
SEASON_IMAGE_UPGRADE_DAYS=15
```

Blank per-type values inherit `IMAGE_UPGRADE_DAYS`. Decimal values are
supported (`0.5` is 12 hours), and `0` disables timed refreshes for that type.
The interval is a minimum age: work starts at the first `RUN_TIMES` execution
after the interval expires.

Movie settings apply to every configured movie library. Series and season
settings apply to every configured TV library unless a `config.yml` library
override is present. MetaFusion does not generate episode artwork.

For different cadences within the same media type, use advanced per-library
overrides in `config.yml`. Global environment variables remain the defaults:

```yaml
library_overrides:
  Movies 4K:
    image_upgrades:
      movie_days: 60
  Anime:
    image_upgrades:
      series_days: 7
      season_days: 7
```

`image_upgrades` and `plex_metadata` are accepted inside a library override.
Library names must exactly match Plex. `--doctor` validates the override
structure.

## TMDb response cache

MetaFusion stores successful TMDb responses as compressed rows in
`/config/cache/tmdb_cache.sqlite3`. It reads and updates individual
responses instead of loading and rewriting one large JSON document. The cache
is disposable: database corruption causes a clean cache rebuild and does not
remove generated metadata or artwork.

No JSON response import is attempted because those entries expire after
`TMDB_CACHE_TTL_HOURS` and importing a large document would recreate the memory
and I/O spike this backend avoids. MetaFusion leaves the obsolete
`tmdb_response_cache.json` and `.bak` files untouched; they can be removed
manually after the SQLite cache has been tested.

`TMDB_CACHE_MAX_ENTRIES` remains the primary bound. Set `TMDB_CACHE_MAX_MB` to
an optional compressed-payload limit when appdata space matters; its default
of `0` preserves the existing entry capacity without a byte cap.

## Durable application state

MetaFusion stores media state, per-season artwork state, per-library scan
history, and recent completed jobs in `/config/cache/meta_db.sqlite3`.
Changed media and season rows are committed together after a job instead of
loading and rewriting one large JSON cache. Full-scan timing is tracked per
Plex server and library, so one library cannot incorrectly represent the scan
state of every configured library.

This pre-release branch uses SQLite as its only application-state backend.
Obsolete `meta_cache.json`, `incremental_state.json`, and their `.bak` files
are ignored and may be removed manually. There is no JSON migration path. Dry
runs can read existing SQLite state but never create or update the database.

The disposable TMDb response cache remains in its separate SQLite file. This
keeps cache expiry, pruning, or corruption recovery from affecting durable
scan and artwork state. Back up SQLite files while MetaFusion is stopped, as
the standard Unraid appdata backup workflow already does.

## Cleanup safety

Cleanup is disabled by default. Before setting `RUN_CLEANUP=True`:

1. Confirm every `PLEX_LIBRARIES` name exactly matches Plex.
2. Back up existing Kometa metadata and assets.
3. Run once with `DRY_RUN=True` and inspect the logs.
4. Enable cleanup only after the dry-run result is correct.

Cleanup runs only during a complete reconciliation scan and only after every
configured library of that media type completes successfully. A missing
library, failed scan, incomplete season/episode inventory, malformed YAML
file, or write failure aborts cleanup. Incremental and targeted runs report
that cleanup was skipped instead of showing a misleading zero.

In Kometa mode, cleanup reconciles MetaFusion's generated metadata files,
removing Plex titles, seasons, and episodes that no longer exist. Artwork is
deleted only when its exact path and content checksum match the file
MetaFusion previously created. A manually replaced file, symbolic link,
unmanaged file, or legacy cache record without a checksum is preserved.
Disabling an artwork feature also disables cleanup for that artwork type.
Season 0/Specials are retained whenever they remain in Plex. Plex mode cleans
stale MetaFusion cache entries only; it does not remove YAML or artwork.
Neither mode deletes Plex media files.

The final report separates title, season, episode, and artwork counts. A dry
run reports the same proposed counts without changing cache, YAML, or assets.
For a one-time complete dry-run test, set `INCREMENTAL=False`,
`RUN_CLEANUP=True`, and `DRY_RUN=True`, then restore your normal settings.

## Multiple editions

Give every same-title/year movie copy a unique Plex edition name. Two blank
editions or duplicate edition names cannot be matched safely by Kometa, so
MetaFusion stops and identifies the affected movies. Setting
`ALLOW_AMBIGUOUS_EDITIONS=True` restores permissive behavior but can update the
wrong copy and is not recommended.

## Versioned Docker releases and rollback

Pin an exact release for production and soak testing. `main` and `latest` move
whenever the default branch is updated, while version and SHA tags identify a
specific build. Published images support AMD64 and ARM64 and are signed.

| Image tag | Behavior |
| --- | --- |
| `1.2.3` | Exact release; recommended production pin. |
| `1.2.3-rc.1` | Exact prerelease; intended for testing. |
| `1.2` | Moves to the newest patch in the `1.2` line. |
| `1` | Moves to the newest release in the `1` line. |
| `sha-<full-commit>` | Immutable build for exact recovery or diagnosis. |
| `develop` | Moves with successful testing builds from `develop`; never updates `latest`. |
| `main`, `latest` | Move with successful production builds from `main`. |

For Docker Compose, pin a tested version in `.env`:

```text
METAFUSION_IMAGE=ghcr.io/ray-cys/metafusion:1.2.3
```

Then apply it without changing the mounted configuration or media data:

```bash
docker compose pull metafusion
docker compose up -d metafusion
```

To roll back, change only `METAFUSION_IMAGE` to the previous exact release or
an immutable `sha-...` tag and run the same two commands. Do not delete or
recreate `/config` or `/kometa` during rollback.

On Unraid, set the container's **Repository** field to an exact image such as
`ghcr.io/ray-cys/metafusion:1.2.3`. Roll back by selecting the prior exact tag,
applying the template, and restarting the container. Keep `PUID=99`,
`PGID=100`, and all existing path mappings unchanged.

For pre-release testing, use `ghcr.io/ray-cys/metafusion:develop`. Return to
`latest` or a tested version tag after the changes are promoted to `main`.

## Output locations, health, and troubleshooting

### Kometa mode output

With `RUN_MODE=kometa`, MetaFusion writes below `KOMETA_PATH`:

```text
metadata/movie_metadata.yml
metadata/tv_metadata.yml
metadata/.metafusion-backups/*.bak
assets/movie/...
assets/tv/...
```

It does not write into the Plex media directories or directly edit Plex.

### Plex mode output

With `RUN_MODE=plex`, MetaFusion never creates the Kometa paths above. Enabled
artwork is written beside the corresponding media using Plex-compatible local
asset names. When direct metadata updates are enabled, changes are sent through
the Plex API, a redacted audit is written under `/config/reports`, and field
ownership is recorded in `meta_db.sqlite3`.

### Shared state and diagnostics

Both modes store persistent diagnostics and state under `/config`:

```text
config_template.yml
logs/metafusion.log
cache/meta_db.sqlite3
cache/tmdb_cache.sqlite3
reports/plex-metadata-YYYYMMDD-HHMMSS.txt  # direct Plex metadata runs only
```

The live heartbeat is intentionally stored at
`/tmp/metafusion-status.json`, avoiding a persistent appdata write every 30
seconds. Completed job history is retained in `meta_db.sqlite3`. View the
combined current status and recent history with `python metafusion.py
--status` inside the container.

Inspect logs and container health with:

```bash
docker compose logs --tail=200 metafusion
docker inspect --format '{{json .State.Health}}' metafusion
```

A scheduled-job failure is recorded in the live status and durable job history and shown in
the health message. By default, the scheduler remains healthy so a failed job
does not create a restart loop. Set `HEALTH_FAIL_ON_JOB_ERROR=True` if Docker
should mark the container unhealthy after a job failure.

Common problems:

| Symptom | Check |
| --- | --- |
| `/config` or `/kometa` permission error | Confirm the host path is writable by `PUID:PGID` (`99:100` on Unraid). |
| Plex mode does not write artwork | Add writable media bind mounts and configure `PLEX_PATH_MAPPINGS` when Plex and container paths differ. |
| Direct Plex metadata is unchanged | Confirm `PLEX_METADATA_UPDATES=True`, inspect field locks, the policy, field allowlist, write cap, and the latest report. |
| Kometa output is missing | Confirm `RUN_MODE=kometa`, `KOMETA_PATH`, and the writable `/kometa` mapping. |
| Scheduled runs do not start | Check `RUN_SCHEDULE`, `RUN_TIMES`, and `TZ`. |
| Container is slow to stop | Keep `SHUTDOWN_TIMEOUT` lower than `STOP_GRACE_PERIOD` and do not bypass the image entrypoint. |

## References

- [Kometa metadata files](https://kometa.wiki/en/latest/files/metadata/)
- [Finding a Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- [Plex local TV artwork names](https://support.plex.tv/articles/200220717-local-media-assets-tv-shows/)
- [Python-PlexAPI edit and lock methods](https://python-plexapi.readthedocs.io/en/latest/modules/mixins.html)
- [TMDb API documentation](https://developer.themoviedb.org/docs)
