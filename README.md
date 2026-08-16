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
| `TMDB_LANGUAGE` | `en` | Preferred TMDb language. |
| `TMDB_LANGUAGE_FALLBACK` | `zh,ja` | Ordered fallback languages. |
| `TMDB_REGION` | `US` | TMDb release/certification region. |
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
| `RUN_TIMES` | `06:00,18:30` | Comma-separated daily run times. |
| `TZ` | `UTC` | Container timezone used by the scheduler. |
| `DRY_RUN` | `False` | Calculate and log without writing generated data or deleting files. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `RUN_BASIC` | `True` | Generate core metadata. Required by enhanced metadata and artwork. |
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
settings apply to every configured TV library. MetaFusion does not generate
episode artwork.

The first Phase 8 run processes selected libraries once to establish artwork
check timestamps. Later runs return to normal incremental behavior.

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
cache/tmdb_response_cache.json
cache/incremental_state.json
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

The container handles `SIGTERM`, cancels active work, and flushes cache changes.
Keep `SHUTDOWN_TIMEOUT` below `STOP_GRACE_PERIOD`. Metadata, cache, and artwork
replacement use validated/atomic writes so an interrupted run cannot replace a
known-good file with a partial download.

## References

- [Kometa metadata files](https://kometa.wiki/en/latest/files/metadata/)
- [Finding a Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- [TMDb API documentation](https://developer.themoviedb.org/docs)
