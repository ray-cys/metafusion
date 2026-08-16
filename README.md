# MetaFusion

MetaFusion reads selected Plex libraries, enriches their metadata with TMDb,
and manages posters, backgrounds, and season artwork. It can create
Kometa-compatible metadata files or place artwork beside Plex media.

## What it does

- Generates movie, show, season, Specials/Season 0, and episode metadata.
- Downloads and upgrades movie/show posters, backgrounds, and season posters.
- Supports Kometa metadata output and Plex-side artwork output.
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

## Quick start

### Environment-variable configuration

```bash
cp .env.example .env
mkdir -p config kometa
# Add your Plex token, TMDb key, library names, and host paths to .env.
docker compose up -d
docker compose logs -f metafusion
```

### `config.yml` configuration

```bash
mkdir -p config kometa
cp config_template.yml config/config.yml
# Edit config/config.yml. A .env file is not required.
docker compose up -d
```

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
writable by `99:100`. The container prepares only its managed `/config`,
`/config/logs`, and `/config/cache` paths. It does not recursively change the
ownership of a large Kometa tree, which keeps container startup fast.

When replacing an existing metadata YAML or artwork file, MetaFusion preserves
that file's UID, GID, and permission mode. New files are created as `0664` and
owned by the configured runtime identity. MetaFusion also refuses to process as
UID `0`, preventing an accidentally bypassed entrypoint from creating
root-owned files. On Unraid, keep `PUID=99`, `PGID=100`, and do not override the
image entrypoint.

No Docker extra parameter is required. An explicit Compose `user:` or
`docker run --user` setting takes precedence over `PUID` and `PGID`.

## Running MetaFusion

The Docker service defaults to scheduler mode. `RUN_TIMES` uses `TZ`.

Run one job:

```bash
docker compose run --rm -e METAFUSION_RUN=True metafusion
```

Validate configuration without contacting Plex or TMDb and without creating
files:

```bash
docker compose run --rm metafusion python metafusion.py --doctor
```

Run a targeted metadata repair:

```bash
docker compose run --rm metafusion python metafusion.py \
  --metafusion_run --library Movies --rating-key 12345 --metadata-only
```

`--library` and `--rating-key` may be repeated or comma-separated. Targeted
runs always disable cleanup. Add `--full-scan` to bypass incremental skipping.

Run a targeted artwork repair without regenerating metadata:

```bash
docker compose run --rm metafusion python metafusion.py \
  --metafusion_run --library "TV Shows" --rating-key 12345 --asset-only
```

Explain why an item would be selected without processing or writing:

```bash
docker compose run --rm metafusion python metafusion.py \
  --metafusion_run --library "TV Shows" --rating-key 12345 --explain-selection
```

Print the current scheduler status and recent job history:

```bash
docker compose exec metafusion python metafusion.py --status
```

Do not leave `METAFUSION_RUN=True` on the long-running service unless a new
one-shot run after every container restart is intentional.

## Environment variables

The tables below list every supported user-configurable Docker variable.

### Connections, libraries, and output mode

| Variable | Default | Purpose |
| --- | --- | --- |
| `PLEX_URL` | `http://10.0.0.1:32400` | Complete Plex server URL. |
| `PLEX_TOKEN` | required | Plex authentication token. |
| `PLEX_TOKEN_FILE` | unset | File containing the Plex token; direct token wins. |
| `PLEX_LIBRARIES` | `Movies,TV Shows` | Comma-separated exact Plex library names. |
| `TMDB_API_KEY` | required | TMDb API key. |
| `TMDB_API_KEY_FILE` | unset | File containing the TMDb key; direct key wins. |
| `TMDB_LANGUAGE` | `en-US` | TMDb metadata language and primary artwork language. |
| `TMDB_LANGUAGE_FALLBACK` | `zh,ja` | Ordered artwork-only language fallbacks; these never change metadata text. |
| `TMDB_REGION` | `US` | Metadata release/certification region, with US as the fallback. |
| `RUN_MODE` | `kometa` | `kometa` or `plex`. Plex mode does not create Kometa YAML. |
| `KOMETA_PATH` | `/kometa` | Container path for Kometa metadata and assets. |

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
| `DRY_RUN` | `False` | Calculate and log without writing generated data or deleting files. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `RUN_BASIC` | `True` | Generate core metadata. Required by enhanced metadata, but not artwork-only runs. |
| `RUN_ENHANCED` | `True` | Generate extended cast/crew metadata. |
| `RUN_POSTER` | `True` | Manage movie and show posters. |
| `RUN_SEASON` | `True` | Manage season posters, including Specials. |
| `RUN_BACKGROUND` | `False` | Manage movie and show backgrounds. |
| `RUN_CLEANUP` | `False` | Enable guarded orphan cleanup. Test with dry-run first. |

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
| `TMDB_CACHE_ENABLED` | `True` | Persist successful TMDb JSON responses. |
| `TMDB_CACHE_TTL_HOURS` | `24` | TMDb response lifetime. |
| `TMDB_CACHE_MAX_ENTRIES` | `5000` | Maximum persisted TMDb responses. |
| `VALIDATE_OUTPUT` | `True` | Validate Kometa YAML before replacing known-good output. |
| `OUTPUT_BACKUP_COUNT` | `3` | Metadata backups retained per output file. |
| `ALLOW_AMBIGUOUS_EDITIONS` | `False` | Allow unsafe duplicate edition matching. |
| `HEALTH_FAIL_ON_JOB_ERROR` | `False` | Mark the container unhealthy after a failed job. |
| `HEALTH_MAX_HEARTBEAT_AGE` | `120` | Maximum health heartbeat age in seconds. |

### Poster selection

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTER_MAX_WIDTH` | `2000` | Preferred maximum poster width. |
| `POSTER_MAX_HEIGHT` | `3000` | Preferred maximum poster height. |
| `POSTER_MIN_WIDTH` | `1000` | Minimum preferred poster width. |
| `POSTER_MIN_HEIGHT` | `1500` | Minimum preferred poster height. |
| `POSTER_PREFER_VOTE` | `5.0` | Preferred TMDb vote score. |
| `POSTER_VOTE_RELAXED` | `3.5` | Relaxed fallback vote score. |
| `POSTER_VOTE_THRESHOLD` | `5.0` | Score used when deciding artwork upgrades. |

### Season-poster selection

| Variable | Default | Purpose |
| --- | --- | --- |
| `SEASON_MAX_WIDTH` | `2000` | Preferred maximum season-poster width. |
| `SEASON_MAX_HEIGHT` | `3000` | Preferred maximum season-poster height. |
| `SEASON_MIN_WIDTH` | `1000` | Minimum preferred season-poster width. |
| `SEASON_MIN_HEIGHT` | `1500` | Minimum preferred season-poster height. |
| `SEASON_PREFER_VOTE` | `5.0` | Preferred TMDb vote score. |
| `SEASON_VOTE_RELAXED` | `0.5` | Relaxed fallback vote score. |
| `SEASON_VOTE_THRESHOLD` | `3.0` | Score used when deciding season-poster upgrades. |

### Background selection

| Variable | Default | Purpose |
| --- | --- | --- |
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
| `STATUS_FILE` | `/config/metafusion-status.json` | Container health/status file. |

`CONFIG_DIR` and `STATUS_FILE` are normally left at their image defaults.

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

Only `image_upgrades` is accepted inside a library override. Library names
must exactly match Plex. `--doctor` validates the override structure.

Incremental selection keeps separate reasons for metadata, posters,
backgrounds, and season posters. An artwork-only cadence does not regenerate
episode metadata or execute artwork types that are not due. A known TMDb image
source is marked checked without downloading it again while its managed file
still exists.

## Cleanup safety

Cleanup is disabled by default. Before setting `RUN_CLEANUP=True`:

1. Confirm every `PLEX_LIBRARIES` name exactly matches Plex.
2. Back up existing Kometa metadata and assets.
3. Run once with `DRY_RUN=True` and inspect the logs.
4. Enable cleanup only after the dry-run result is correct.

Cleanup runs only during a complete reconciliation scan and only after every
configured library of that media type completes successfully. A missing
library, failed scan, malformed YAML file, or write failure aborts cleanup.
Only assets previously recorded in MetaFusion's cache are eligible for
deletion; manually managed assets are preserved. Disabling an artwork feature
also disables cleanup for that artwork type.

## Multiple editions

Give every same-title/year movie copy a unique Plex edition name. Two blank
editions or duplicate edition names cannot be matched safely by Kometa, so
MetaFusion stops and identifies the affected movies. Setting
`ALLOW_AMBIGUOUS_EDITIONS=True` restores permissive behavior but can update the
wrong copy and is not recommended.

## Versioned Docker releases and rollback

`main` and `latest` are moving public tags built from the default branch. A
Git tag such as `v1.2.3` runs the complete tests, dependency audit, image scan,
and multi-architecture build before publishing these signed GHCR tags:

| Image tag | Behavior |
| --- | --- |
| `1.2.3` | Exact release; recommended production pin. |
| `1.2` | Moves to the newest patch in the `1.2` line. |
| `1` | Moves to the newest release in the `1` line. |
| `sha-<full-commit>` | Immutable build for exact recovery or diagnosis. |
| `main`, `latest` | Move with successful builds from `main`. |

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

## Output, health, and troubleshooting

Kometa mode writes below `KOMETA_PATH`:

```text
metadata/movie_metadata.yml
metadata/tv_metadata.yml
metadata/.metafusion-backups/*.bak
assets/movie/...
assets/tv/...
```

Plex mode places artwork beside media and does not create Kometa metadata YAML.

Operational files are stored under `/config`:

```text
logs/metafusion.log
cache/meta_cache.json
cache/meta_cache.json.bak
cache/tmdb_response_cache.json
cache/tmdb_response_cache.json.bak
cache/incremental_state.json
cache/incremental_state.json.bak
metafusion-status.json
```

Inspect logs and container health with:

```bash
docker compose logs --tail=200 metafusion
docker inspect --format '{{json .State.Health}}' metafusion
```

A scheduled-job failure is recorded in `metafusion-status.json` and shown in
the health message. By default, the scheduler remains healthy so a failed job
does not create a restart loop. Set `HEALTH_FAIL_ON_JOB_ERROR=True` if Docker
should mark the container unhealthy after a job failure.

The status file keeps the ten most recent job results. Cache and incremental
state writes retain a last-known-good backup and recover from it automatically
if the primary JSON file is damaged.

The container handles `SIGTERM`, cancels active work, and flushes cache changes.
Keep `SHUTDOWN_TIMEOUT` below `STOP_GRACE_PERIOD`. Metadata, cache, and artwork
replacement use validated/atomic writes so an interrupted run cannot replace a
known-good file with a partial download.

## References

- [Kometa metadata files](https://kometa.wiki/en/latest/files/metadata/)
- [Finding a Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- [TMDb API documentation](https://developer.themoviedb.org/docs)
