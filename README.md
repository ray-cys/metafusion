# MetaFusion

MetaFusion scans selected Plex libraries, enriches their metadata with TMDb,
and produces Kometa-compatible YAML and artwork. It can also place artwork
beside Plex media when `RUN_MODE=plex`.

The application is designed to run either once or as a long-running Docker
scheduler. Cleanup is opt-in and guarded by a complete-library inventory.

## Current capabilities

- Movie, show, season (including Specials/Season 0), and episode metadata
- Kometa-compatible `movie_metadata.yml` and `tv_metadata.yml`
- Poster, background, and season artwork selection and upgrades
- Stable Plex/cache identities for multiple editions
- Atomic YAML and cache writes with one cache flush per run
- Validated Kometa output with rotating known-good backups and rollback
- Incremental Plex processing with periodic full-library reconciliation
- Persistent, bounded, TTL-based TMDb response caching
- True dry-run behavior for generated metadata, assets, cache, and logs
- Bounded item concurrency, HTTP timeouts, and maximum image download size
- Non-root, read-only Docker runtime with a scheduler health check
- Graceful `SIGTERM` cancellation and cache flushing
- Non-writing configuration doctor and targeted library/rating-key runs

## Requirements

- Docker Compose v2, recommended; or
- Python 3.10+
- A reachable Plex server and Plex token
- A TMDb API key
- Kometa directories if using `RUN_MODE=kometa`

Dependencies are fully pinned with hashes in `requirements.lock`. For a local
installation:

```bash
python -m pip install --require-hashes -r requirements.lock
```

## Docker Compose quick start

Using environment variables:

```bash
cp .env.example .env
mkdir -p config kometa
# Edit .env and add PLEX_TOKEN, TMDB_API_KEY, paths, and library names.
docker compose config
docker compose up -d
docker compose logs -f metafusion
```

Using only `config.yml`:

```bash
mkdir -p config kometa
cp config_template.yml config/config.yml
# Edit config/config.yml. A .env file is not required.
docker compose up -d
```

The MetaFusion process runs as an unprivileged user. When Docker starts the
image normally, its entrypoint prepares MetaFusion's managed `/config` state
and drops privileges to the configured `PUID` and `PGID`. An explicit Compose
`user:` or `docker run --user` setting takes precedence and is used directly.

`config` and `kometa` must be writable by that identity. On Linux, set `PUID`
and `PGID` to your host user IDs or change the directory ownership before
starting:

```bash
id -u
id -g
sudo chown -R 10001:10001 config kometa
```

For Unraid's standard `nobody:users` ownership, add or update these container
variables; no Docker extra parameter is required:

```text
PUID=99
PGID=100
```

The entrypoint never recursively changes the Kometa tree, avoiding long starts
for large libraries. The mapped Kometa directory must therefore already be
writable by `99:100`, as normal Unraid appdata and share paths are. It only
repairs ownership for `/config`, `/config/logs`, `/config/cache`, and the
MetaFusion status file. Existing `config.yml` and secret files are not made
writable or rewritten.

The startup preflight exits with a clear error if `/config` or `/kometa` is not
writable. The container otherwise uses a read-only root filesystem, drops all
Linux capabilities, and enables `no-new-privileges`.

### Scheduler and one-shot modes

Compose defaults to scheduler mode. `RUN_TIMES` uses the timezone from `TZ`.

Run one job without the Compose restart policy:

```bash
docker compose run --rm -e METAFUSION_RUN=True metafusion
```

Do not set `METAFUSION_RUN=True` on the long-running service unless repeated
one-shot execution after every restart is intentional.

Validate configuration without connecting to Plex/TMDb or creating files:

```bash
docker compose run --rm metafusion python metafusion.py --doctor
```

Run a targeted repair without scanning every item:

```bash
docker compose run --rm metafusion python metafusion.py \
  --metafusion_run --library Movies --rating-key 12345 --metadata-only
```

`--library` and `--rating-key` may be repeated or comma-separated. Targeted
runs always disable cleanup. Use `--full-scan` to bypass incremental skipping.

## Configuration

Configuration is resolved independently for every option: built-in default,
then `/config/config.yml`, then a configured secret file, then a non-empty
environment variable. Direct environment variables therefore keep highest
priority, while missing or blank variables fall back to secret files or
`config.yml`. Compose no longer injects application defaults, so a YAML-only
deployment works without `.env`. In Unraid, remove an unwanted variable or
leave its value blank; any non-empty template variable intentionally overrides
the corresponding YAML value.

If a non-empty environment configuration is present and `config.yml` is
absent, MetaFusion uses safe defaults without generating a template that could
replace environment values. For YAML configuration, copy
`config_template.yml` to `/config/config.yml`.

### Secret handling

Plex tokens and TMDb API keys are redacted from MetaFusion log and error
messages. When they are supplied as environment variables, MetaFusion does not
write them to `/config/config.yml`.

For file-based secrets, mount each protected host file read-only into the
container, leave the corresponding direct value blank, and set its file option:

```text
PLEX_TOKEN_FILE=/run/secrets/plex_token
TMDB_API_KEY_FILE=/run/secrets/tmdb_api_key
```

If both forms are set, `PLEX_TOKEN` and `TMDB_API_KEY` win. Empty, missing, or
unreadable secret files fail configuration validation before a run starts.

Environment variables are still visible to users with permission to inspect
the Unraid container or Docker configuration. MetaFusion does not provide a web
login or configuration form, so browser password-manager prompts are controlled
by the Unraid template and the browser rather than by MetaFusion. Treat Unraid
and Docker administrative access as trusted access.

Core options:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `PLEX_URL` | `http://10.0.0.1:32400` | Plex server URL |
| `PLEX_TOKEN` | required | Plex authentication token |
| `PLEX_TOKEN_FILE` | unset | Protected file containing the Plex token |
| `PLEX_LIBRARIES` | `Movies,TV Shows` | Exact Plex library names |
| `TMDB_API_KEY` | required | TMDb API key |
| `TMDB_API_KEY_FILE` | unset | Protected file containing the TMDb API key |
| `RUN_MODE` | `kometa` | `kometa` or `plex` output mode |
| `RUN_SCHEDULE` | `True` | Enable scheduled operation |
| `RUN_TIMES` | `06:00,18:30` | Daily scheduler times |
| `DRY_RUN` | `False` | Calculate and log without generated writes/deletes |
| `RUN_PROCESS` | `False` | Enable guarded orphan cleanup |
| `MAX_CONCURRENCY` | `8` | Maximum items processed concurrently |
| `REQUEST_TIMEOUT` | `30` | Total HTTP request timeout in seconds |
| `CONNECT_TIMEOUT` | `10` | HTTP connection timeout in seconds |
| `PLEX_TIMEOUT` | `10` | Maximum duration of each blocking Plex request |
| `PLEX_RETRIES` | `3` | Bounded Plex startup connection attempts |
| `PLEX_RETRY_DELAY` | `1` | Base Plex retry delay in seconds |
| `SHUTDOWN_TIMEOUT` | `15` | Graceful shutdown deadline before forced exit |
| `STOP_GRACE_PERIOD` | `20s` | Compose/Docker outer stop deadline |
| `MAX_IMAGE_MB` | `25` | Maximum accepted artwork response size |
| `ALLOW_AMBIGUOUS_EDITIONS` | `False` | Permit unsafe duplicate edition matching |
| `INCREMENTAL` | `True` | Skip successfully processed unchanged Plex items |
| `FULL_SCAN_INTERVAL_HOURS` | `168` | Maximum time between reconciliation scans |
| `TMDB_CACHE_ENABLED` | `True` | Persist successful TMDb JSON responses |
| `TMDB_CACHE_TTL_HOURS` | `24` | TMDb response lifetime |
| `TMDB_CACHE_MAX_ENTRIES` | `5000` | Maximum persisted TMDb responses |
| `VALIDATE_OUTPUT` | `True` | Validate Kometa document structure before replacement |
| `OUTPUT_BACKUP_COUNT` | `3` | Known-good metadata backups retained per file |
| `HEALTH_FAIL_ON_JOB_ERROR` | `False` | Make health strict instead of liveness-only |
| `PUID` / `PGID` | `10001` | Runtime identity; use `99` / `100` on Unraid |

Artwork and metadata switches and all image-selection thresholds are documented
in `config_template.yml` and `docker-compose.yml`.

## Cleanup safety

Cleanup remains disabled by default. Before enabling it:

1. Confirm every `PLEX_LIBRARIES` name exactly matches Plex.
2. Run with `DRY_RUN=True` and inspect the logs.
3. Back up existing Kometa metadata and assets.
4. Enable `RUN_PROCESS=True` only after the dry-run is correct.

MetaFusion will clean a media type only when every detected Plex library of
that type was selected and completed successfully. For example, all movie
libraries must be scanned before movie cache, YAML, or managed movie assets can
be removed. A missing library, scan failure, malformed YAML file, or write
failure aborts cleanup and marks the job failed. Only asset files previously
recorded in MetaFusion's cache are eligible for deletion; manually managed
Kometa assets are preserved.

Disabling an artwork feature also disables cleanup for that artwork type.
Incremental runs never perform cleanup; cleanup is considered only during a
complete reconciliation scan. The first run is always full, and a missing
Kometa output file also forces a full scan.

## Incremental processing and caches

MetaFusion records each successfully processed Plex rating key, Plex update
timestamp, and a fingerprint of output-affecting configuration. An item is
skipped only when all three still match. Failed items, items without update
timestamps, changed configuration, explicit rating-key targets, and full scans
are always processed.

`cache/tmdb_response_cache.json` stores successful TMDb JSON responses with a
TTL and entry limit. It is never written during dry-run. The periodic full scan
detects removed Plex items and is the only scan eligible for orphan cleanup.

## Multiple editions and versions

Give every same-title/year movie copy a unique Plex edition name. MetaFusion
uses native Plex edition metadata and also recognizes `{edition-Name}` in the
media filename. Distinct edition names produce distinct cache and YAML entries.

Two blank editions, or two copies with the same edition name, cannot be matched
uniquely by Kometa. MetaFusion therefore fails safely and explains which items
need edition names. `ALLOW_AMBIGUOUS_EDITIONS=True` restores permissive legacy
behavior, but it can cause Kometa to update the wrong copy and is not
recommended.

## Output and health

In Kometa mode, output is written below `KOMETA_PATH`:

```text
metadata/movie_metadata.yml
metadata/tv_metadata.yml
metadata/.metafusion-backups/*.bak
assets/movie/...
assets/tv/...
```

Logs and operational state are stored in `/config`:

```text
logs/metafusion.log
cache/meta_cache.json
cache/tmdb_response_cache.json
cache/incremental_state.json
metafusion-status.json
```

The Docker health check verifies process liveness using the PID and heartbeat.
A failed scheduled job is recorded in `metafusion-status.json` and displayed by
the health-check message, but it does not make a healthy scheduler process
unhealthy. Set `HEALTH_FAIL_ON_JOB_ERROR=True` for strict legacy behavior.
Inspect state with:

```bash
docker inspect --format '{{json .State.Health}}' metafusion
docker compose logs --tail=200 metafusion
```

One-shot failures return a non-zero exit code. Scheduled failures remain in the
status file until a later run succeeds.

Before each Kometa YAML replacement, MetaFusion validates the stable documented
mapping structure for metadata, matches, seasons, and episodes. Existing output
is retained as a rotating backup. A validation or post-write verification
failure restores the prior known-good file.

### Restart behavior

`SIGTERM` immediately wakes an idle scheduler and cancels an active async job.
Plex calls are bounded to `PLEX_TIMEOUT` (10 seconds by default), and cache
changes are flushed during normal cancellation. If a blocked filesystem,
network driver, or worker still prevents exit, the process watchdog exits after
`SHUTDOWN_TIMEOUT` (15 seconds). Docker's default 20-second grace period is the
final boundary. YAML and cache replacement is atomic, so a forced exit can lose
the current in-memory batch but cannot leave a partially written output file.
Downloaded artwork is decoded and verified before an atomic staging write, so
an HTML error response, corrupt image, or failed replacement cannot overwrite a
known-good asset.

Keep `SHUTDOWN_TIMEOUT` lower than `STOP_GRACE_PERIOD`. The health-check startup
window is 20 seconds and does not delay the container process itself.

## Development and validation

Install the development lock and run the same checks as CI:

```bash
python -m pip install --require-hashes -r requirements-dev.lock
ruff check --select F,E9 .
pytest -q --cov=. --cov-report=term --cov-fail-under=75
pip-audit -r requirements.lock --require-hashes --disable-pip --no-deps
```

GitHub Actions tests Python 3.10 and 3.13, audits Python dependencies, scans the
built container for fixable critical vulnerabilities, builds `linux/amd64` and
`linux/arm64` images, and signs published images.

To update dependency locks after reviewing available releases:

```bash
uv pip compile --universal --python-version 3.10 --generate-hashes requirements.in -o requirements.lock
uv pip compile --universal --python-version 3.10 --generate-hashes requirements-dev.in -o requirements-dev.lock
```

## Resources

- [Kometa metadata documentation](https://kometa.wiki/en/latest/files/metadata/)
- [Plex token documentation](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- [Python PlexAPI](https://python-plexapi.readthedocs.io/)
- [TMDb API](https://developer.themoviedb.org/docs)
