# Configuration reference

MetaFusion supports environment variables, secret files, and
`/config/config.yml`. The supplied Docker Compose and Unraid templates expose
the same application settings. Choose the intended behavior first in
[Kometa and Plex operation modes](modes.md); use the
[documentation index](index.md) for task-oriented guides.

The exhaustive [generated configuration table](configuration.generated.md),
`.env.example`, `config_template.yml`, Docker Compose environment block, and
Unraid variables are generated from the canonical `config_schema.yml` file.
Maintainers update the schema and run `python tools/generate_config_surfaces.py`.

## Configuration priority

Values are merged in this order, with later sources taking priority:

1. Built-in defaults.
2. `/config/config.yml`.
3. `PLEX_TOKEN_FILE` and `TMDB_API_KEY_FILE`.
4. Non-empty environment variables.

Blank environment bindings are ignored, allowing `config.yml` or defaults to
supply the value. A non-empty direct token or API key takes priority over its
matching secret file.

The container maintains `/config/config_template.yml` as a value-free
reference. It never copies environment values or secrets into that file and
never creates `config.yml` automatically. Copy the template to `config.yml`
when YAML configuration is wanted, then edit only the copy. Image updates may
refresh the template but never replace an existing `config.yml`.

Run `python metafusion.py --doctor` inside the container to validate the
effective configuration without contacting Plex or TMDb.

## Connections, libraries, and mode

| Variable | Default | Purpose |
| --- | --- | --- |
| `RUN_MODE` | `kometa` | `kometa` writes Kometa YAML/assets; `plex` never writes Kometa YAML. |
| `PLEX_URL` | placeholder | Complete Plex server URL, including port. |
| `PLEX_TOKEN` | required | Plex authentication token. |
| `PLEX_TOKEN_FILE` | unset | Mounted file containing the Plex token; a non-empty direct token wins. |
| `PLEX_LIBRARIES` | `auto` | `auto` discovers every supported movie/show library; comma-separated exact names limit the scope. Do not combine `auto` and explicit names. |
| `PLEX_PATH_MAPPINGS` | unset | Semicolon-separated `PLEX_PATH=>CONTAINER_PATH` translations for Plex-mode artwork. Docker mappings are still required. |
| `TMDB_API_KEY` | required | TMDb API key. |
| `TMDB_API_KEY_FILE` | unset | Mounted file containing the TMDb key; a non-empty direct key wins. |
| `TMDB_LANGUAGE` | `en-US` | Metadata language and primary artwork language. |
| `TMDB_LANGUAGE_FALLBACK` | `zh,ja` | Ordered artwork-only fallbacks; metadata text continues to use `TMDB_LANGUAGE`. |
| `TMDB_REGION` | `US` | Metadata release/certification region, with US fallback behavior. |
| `ARTWORK_ALLOW_ANY_LANGUAGE` | `True` | Make one unfiltered image request when preferred artwork languages produce no usable result. |
| `TMDB_TITLE_SEARCH_FALLBACK` | `False` | Use conservative exact normalized title/year search when Plex has no usable external ID. |
| `TMDB_EPISODE_GROUP_FALLBACK` | `True` | Use an alternate TMDb episode group only when one mapping uniquely covers the Plex inventory. |
| `TMDB_SPLIT_SERIES_SHOW_POLICY` | `preserve` | Default top-level show policy for split-series mappings: `preserve` or `primary`. |
| `TMDB_SPLIT_SERIES_MAPPINGS` | `{}` | JSON provider-to-season mappings for verified series that Plex and TMDb group differently. |
| `TMDB_EPISODE_OVERRIDES` | `{}` | JSON deterministic Plex-episode to TMDb-episode corrections for verified numbering exceptions. |
| `KOMETA_PATH` | `/kometa` | Kometa-mode container output root. |

Tokens and user-supplied keys are redacted from MetaFusion logs. They remain
visible to Docker or Unraid administrators when supplied as environment
variables. Fanart.tv fallback uses MetaFusion's bundled application project
key and needs no user configuration. See [Artwork providers](artwork-providers.md)
for its source order, reliability behavior, and attribution.

Use Plex's guide for [finding an authentication
token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
and TMDb's [API documentation](https://developer.themoviedb.org/docs) when
creating the two required user credentials.

## Scheduling and processing

| Variable | Default | Purpose |
| --- | --- | --- |
| `METAFUSION_RUN` | `False` | Run once rather than staying in scheduler mode. Keep false on the long-running service. |
| `RUN_SCHEDULE` | `True` | Keep the container running and execute at `RUN_TIMES`. |
| `RUN_ON_START` | `False` | Run one job when the scheduler starts, then continue normally. |
| `SCHEDULE_CATCH_UP` | `True` | Run the latest recently missed scheduled slot after restart. |
| `SCHEDULE_CATCH_UP_MAX_HOURS` | `24` | Maximum age of a missed slot eligible for startup catch-up. |
| `RUN_TIMES` | `06:00,18:30` | Comma-separated daily `HH:MM` times. |
| `TZ` | `UTC` | Timezone used by the scheduler. |
| `DRY_RUN` | `False` | Calculate without normal writes/deletions. A direct Plex metadata dry-run still writes a redacted audit report. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `LOG_MAX_MB` | `10` | Rotate the active log before it exceeds this many MiB; `0` disables only size rotation. |
| `LOG_BACKUP_COUNT` | `14` | Number of daily/size-rotated log files retained. |
| `RUN_BASIC` | `True` | Generate core Kometa fields or enable core direct Plex fields after Plex API opt-in. |
| `RUN_ENHANCED` | `True` | Add supported director/writer/producer fields. Requires `RUN_BASIC=True`; cast remains with Plex's provider. |
| `RUN_POSTER` | `True` | Generate movie and show posters. |
| `RUN_SEASON` | `True` | Generate season posters, including Specials. |
| `RUN_BACKGROUND` | `False` | Generate movie and show backgrounds. |
| `RUN_CLEANUP` | `False` | Enable guarded full-scan cleanup. Always test with dry-run. |
| `CLEANUP_CONFIRMATION_SCANS` | `2` | Require this many separate authoritative full scans to confirm an absence before cleanup becomes eligible. |
| `CLEANUP_GRACE_HOURS` | `48` | Keep a cleanup candidate pending for at least this many hours after first detection. |
| `PLEX_CLEANUP_MANAGED_ARTWORK` | `False` | Plex mode only. Opt in to deleting exact checksum-proven MetaFusion-owned local artwork for confirmed stale items; state-only cleanup remains the default. |

## Policy controls

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASSET_UPDATE_POLICY` | `managed` | `fill_missing`, `managed`, or `overwrite` behavior for existing artwork files. |
| `KOMETA_TAG_POLICY` | `append` | `append` preserves existing tags; `sync` makes supported generated tag fields match TMDb. |
| `PLEX_METADATA_UPDATES` | `False` | Plex mode only. Opt in to supported direct metadata edits through the Plex API. |
| `PLEX_METADATA_POLICY` | `fill_missing` | `fill_missing`, `managed`, or acknowledged `overwrite` behavior for direct Plex fields. |
| `PLEX_METADATA_LOCK_WRITES` | `False` | Lock scalar Plex fields written by MetaFusion. |
| `PLEX_METADATA_LOCK_MERGED_TAGS` | `False` | Lock the complete merged tag field after additions, including tags from other sources. |
| `PLEX_METADATA_ALLOW_OVERWRITE` | `False` | Required acknowledgement for direct Plex metadata `overwrite`. Not required for artwork overwrite. |
| `PLEX_METADATA_RECHECK_DAYS` | `30` | Recheck unchanged direct Plex metadata after this many days; `0` disables timed rechecks. |
| `PLEX_METADATA_MAX_WRITES_PER_RUN` | `100` | Maximum Plex items or child objects written in one job. |
| `PLEX_METADATA_FIELDS` | all supported safe fields | Optional comma-separated allowlist. |

Supported values for `PLEX_METADATA_FIELDS` are:

```text
title,originalTitle,originallyAvailableAt,contentRating,studio,tagline,summary,
country,genre,director,writer,producer
```

Availability still depends on item type and `RUN_BASIC`/`RUN_ENHANCED`. See
[Policies](policies.md) before enabling direct Plex writes or cleanup.

## Runtime and Plex reliability

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAX_CONCURRENCY` | `0` | `0` enables cgroup-aware adaptive tuning. A positive value is an optional hard ceiling; Plex, TMDb, Fanart.tv, nested artwork, and item lanes retain their lower internal safety caps. |
| `REQUEST_TIMEOUT` | `30` | Total TMDb/image request timeout in seconds. |
| `CONNECT_TIMEOUT` | `10` | HTTP connection timeout in seconds; must not exceed `REQUEST_TIMEOUT`. |
| `PLEX_TIMEOUT` | `10` | Timeout for each blocking Plex request. |
| `PLEX_RETRIES` | `3` | Plex startup connection attempts. |
| `PLEX_RETRY_DELAY` | `1` | Base delay between Plex connection retries in seconds. |
| `SHUTDOWN_TIMEOUT` | `15` | Internal graceful-shutdown deadline in seconds. |
| `STOP_GRACE_PERIOD` | `20s` | Docker Compose stop deadline; keep above `SHUTDOWN_TIMEOUT`. |
| `MAX_IMAGE_MB` | `25` | Maximum accepted artwork download size in MiB. |
| `VALIDATE_MEDIA_MOUNTS` | `True` | Validate configured Plex-mode mapping destinations before artwork processing. |
| `MIN_FREE_SPACE_MB` | `256` | Explicit free-space floor. MetaFusion also applies a storage-aware automatic floor of 1% of the volume, bounded between 256 MiB and 2 GiB. |
| `PUID` | `10001` | Runtime user ID; use `99` on standard Unraid. |
| `PGID` | `10001` | Runtime group ID; use `100` on standard Unraid. |

Existing installations that explicitly saved `MAX_CONCURRENCY=8` remain
adaptive but cannot grow past eight. Change the value to `0`, or remove the
environment variable, to use the complete automatic range.

Do not use Compose `user:` or Docker `--user`; those options bypass
`PUID`/`PGID` startup handling.

## Incremental processing, cache, output, and health

| Variable | Default | Purpose |
| --- | --- | --- |
| `INCREMENTAL` | `True` | Skip successfully processed unchanged items. |
| `FULL_SCAN_INTERVAL_HOURS` | `168` | Maximum interval between complete reconciliation scans. |
| `METADATA_PENDING_RECHECK_HOURS` | `24` | Recheck interval for Plex episodes whose TMDb metadata is not published yet. |
| `IMAGE_UPGRADE_DAYS` | `30` | Default adaptive base interval; `0` disables timed rechecks. |
| `MOVIE_IMAGE_UPGRADE_DAYS` | inherited | Movie poster/background adaptive base interval. |
| `SERIES_IMAGE_UPGRADE_DAYS` | inherited | Show poster/background adaptive base interval. |
| `SEASON_IMAGE_UPGRADE_DAYS` | inherited | Season-poster adaptive base interval. |
| `TMDB_CACHE_ENABLED` | `True` | Persist successful TMDb and Fanart.tv responses in separate SQLite caches. |
| `TMDB_CACHE_TTL_HOURS` | `24` | TMDb and Fanart.tv response lifetime. |
| `TMDB_CACHE_NEGATIVE_TTL_HOURS` | `12` | Short lifetime for either provider's HTTP 404 results; 429 and 5xx responses are never cached. |
| `TMDB_CACHE_MAX_ENTRIES` | `0` | Per-provider maximum persisted responses; `0` chooses a storage-aware automatic limit. |
| `TMDB_CACHE_MAX_MB` | `0` | Per-provider compressed-payload limit; `0` chooses 2% of available storage, bounded from 64 MiB to 1 GiB. |
| `VALIDATE_OUTPUT` | `True` | Kometa mode only. Validate YAML before replacing known-good output. |
| `OUTPUT_BACKUP_COUNT` | `3` | Kometa metadata backups retained per file. |
| `REPORT_RETENTION` | `10` | Number of reports retained per report type under `/config/reports`, including Plex metadata and read-only diagnostics. |
| `ALLOW_AMBIGUOUS_EDITIONS` | `False` | Permit unsafe duplicate-edition matching. Leave false unless accepting that risk. |
| `COMPATIBILITY_PROFILE` | `auto` | Select the declared output contract from `RUN_MODE`; explicit `kometa-2.4` or `plex-api-v1` values must match the mode. |
| `HEALTH_FAIL_ON_JOB_ERROR` | `False` | Mark the container unhealthy after a failed job instead of only reporting it. |
| `HEALTH_MAX_HEARTBEAT_AGE` | `120` | Maximum scheduler heartbeat age in seconds. |

Artwork age comes from saved MetaFusion observations, not filesystem mtime.
Decimal day values are accepted (`0.5` is 12 hours). Missing artwork is
rechecked sooner and stable unchanged candidates back off automatically; these
settings are the bases and optional bounds for that behavior. A due interval
makes an item eligible for evaluation; `ASSET_UPDATE_POLICY` and quality rules
still determine whether an existing file can be replaced.

When the normal TMDb → Fanart.tv → Plex → best-available chain has no candidate,
MetaFusion automatically relaxes artwork language selection only for a missing
destination. This safety fallback has no setting and never replaces an existing
file, including under `ASSET_UPDATE_POLICY=overwrite`.

The two mapping environment variables must contain JSON objects. Equivalent
YAML can be placed under `tmdb` in `config.yml`:

```yaml
tmdb:
  split_series_show_policy: preserve
  split_series_mappings:
    "tvdb:345246":
      show_policy: preserve
      seasons:
        1: {tmdb_id: 72844, season_number: 1}
        2: {tmdb_id: 109958, season_number: 1}
  episode_overrides:
    "tvdb:12345":
      "S01E01": "S01E02"
```

Mapping keys may use `tmdb:`, `tvdb:`, or `imdb:`. Episode overrides change
only which TMDb episode supplies metadata; generated output remains under the
original Plex season and episode number. Add overrides only after confirming a
stable provider mismatch.

## Artwork selection

The `MAX` settings are preferred high-resolution thresholds, not hard download
caps. `MIN` values describe the preferred acceptable dimensions. Vote settings
guide candidate selection and upgrades.

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTER_MAX_WIDTH` | `2000` | Preferred poster width threshold. |
| `POSTER_MAX_HEIGHT` | `3000` | Preferred poster height threshold. |
| `POSTER_MIN_WIDTH` | `1000` | Minimum preferred poster width. |
| `POSTER_MIN_HEIGHT` | `1500` | Minimum preferred poster height. |
| `POSTER_PREFER_VOTE` | `5.0` | Preferred TMDb poster vote score. |
| `POSTER_VOTE_RELAXED` | `3.5` | Relaxed fallback poster vote score. |
| `POSTER_VOTE_THRESHOLD` | `5.0` | Poster upgrade vote threshold. |
| `SEASON_MAX_WIDTH` | `2000` | Preferred season-poster width threshold. |
| `SEASON_MAX_HEIGHT` | `3000` | Preferred season-poster height threshold. |
| `SEASON_MIN_WIDTH` | `1000` | Minimum preferred season-poster width. |
| `SEASON_MIN_HEIGHT` | `1500` | Minimum preferred season-poster height. |
| `SEASON_PREFER_VOTE` | `5.0` | Preferred TMDb season-poster vote score. |
| `SEASON_VOTE_RELAXED` | `0.5` | Relaxed fallback season-poster vote score. |
| `SEASON_VOTE_THRESHOLD` | `3.0` | Season-poster upgrade vote threshold. |
| `BG_MAX_WIDTH` | `3840` | Preferred background width threshold. |
| `BG_MAX_HEIGHT` | `2160` | Preferred background height threshold. |
| `BG_MIN_WIDTH` | `1920` | Minimum preferred background width. |
| `BG_MIN_HEIGHT` | `1080` | Minimum preferred background height. |
| `BG_PREFER_VOTE` | `5.0` | Preferred TMDb background vote score. |
| `BG_VOTE_RELAXED` | `3.5` | Relaxed fallback background vote score. |
| `BG_VOTE_THRESHOLD` | `5.0` | Background upgrade vote threshold. |

## Container and Compose paths

| Variable | Default | Purpose |
| --- | --- | --- |
| `METAFUSION_IMAGE` | `ghcr.io/ray-cys/metafusion:main` | Compose image tag or digest. Pin an exact version for production. |
| `CONFIG_PATH` | `./config` | Compose host path mounted at `/config`. |
| `KOMETA_HOST_PATH` | `./kometa` | Compose host path mounted at `/kometa`. |
| `CONFIG_DIR` | `/config` | Container configuration/state directory; normally fixed. |
| `STATUS_FILE` | `/tmp/metafusion-status.json` | Ephemeral scheduler heartbeat; normally fixed. |

The Unraid template defaults its image to `latest` and its config host path to
`/mnt/user/appdata/metafusion`. These are platform defaults rather than
application configuration.

## Per-library overrides

Environment artwork intervals are global defaults. `config.yml` can override
artwork cadence and direct Plex metadata settings for exact Plex library
names:

```yaml
library_overrides:
  Movies 4K:
    image_upgrades:
      movie_days: 60
  Anime:
    image_upgrades:
      series_days: 7
      season_days: 7
    plex_metadata:
      policy: fill_missing
      max_writes_per_run: 25
```

Only `image_upgrades` and `plex_metadata` are accepted inside a library
override. `ASSET_UPDATE_POLICY` remains global. Blank per-type artwork values
inherit `IMAGE_UPGRADE_DAYS`. `--doctor` rejects unknown override keys,
invalid policies, and library overwrite settings without acknowledgement.
