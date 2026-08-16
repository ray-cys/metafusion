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
- True dry-run behavior for generated metadata, assets, cache, and logs
- Bounded item concurrency, HTTP timeouts, and maximum image download size
- Non-root, read-only Docker runtime with a scheduler health check
- Graceful `SIGTERM` cancellation and cache flushing

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

The image runs as an unprivileged user. `config` and `kometa` must be writable
by the configured `PUID` and `PGID`. On Linux, either set those values to your
host user IDs or change the directory ownership before starting:

```bash
id -u
id -g
sudo chown -R 10001:10001 config kometa
```

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

## Configuration

Configuration is resolved independently for every option: built-in default,
then `/config/config.yml`, then a non-empty environment variable. Environment
variables therefore keep priority, while missing or blank variables fall back
to `config.yml`. Compose no longer injects application defaults, so a YAML-only
deployment works without `.env`. In Unraid, remove an unwanted variable or
leave its value blank; any non-empty template variable intentionally overrides
the corresponding YAML value.

If a non-empty environment configuration is present and `config.yml` is
absent, MetaFusion uses safe defaults without generating a template that could
replace environment values. For YAML configuration, copy
`config_template.yml` to `/config/config.yml`.

Core options:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `PLEX_URL` | `http://10.0.0.1:32400` | Plex server URL |
| `PLEX_TOKEN` | required | Plex authentication token |
| `PLEX_LIBRARIES` | `Movies,TV Shows` | Exact Plex library names |
| `TMDB_API_KEY` | required | TMDb API key |
| `RUN_MODE` | `kometa` | `kometa` or `plex` output mode |
| `RUN_SCHEDULE` | `True` | Enable scheduled operation |
| `RUN_TIMES` | `06:00,18:30` | Daily scheduler times |
| `DRY_RUN` | `False` | Calculate and log without generated writes/deletes |
| `RUN_PROCESS` | `False` | Enable guarded orphan cleanup |
| `MAX_CONCURRENCY` | `8` | Maximum items processed concurrently |
| `REQUEST_TIMEOUT` | `30` | Total HTTP request timeout in seconds |
| `CONNECT_TIMEOUT` | `10` | HTTP connection timeout in seconds |
| `PLEX_TIMEOUT` | `10` | Maximum duration of each blocking Plex request |
| `SHUTDOWN_TIMEOUT` | `15` | Graceful shutdown deadline before forced exit |
| `STOP_GRACE_PERIOD` | `20s` | Compose/Docker outer stop deadline |
| `MAX_IMAGE_MB` | `25` | Maximum accepted artwork response size |
| `ALLOW_AMBIGUOUS_EDITIONS` | `False` | Permit unsafe duplicate edition matching |
| `PUID` / `PGID` | `10001` | Container runtime user/group |

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
assets/movie/...
assets/tv/...
```

Logs and operational state are stored in `/config`:

```text
logs/metafusion.log
cache/meta_cache.json
metafusion-status.json
```

The Docker health check verifies that the process heartbeat is current and
marks the container unhealthy when the last scheduled run failed. Inspect it
with:

```bash
docker inspect --format '{{json .State.Health}}' metafusion
docker compose logs --tail=200 metafusion
```

One-shot failures return a non-zero exit code. Scheduled failures remain in the
status file and health state until a later run succeeds.

### Restart behavior

`SIGTERM` immediately wakes an idle scheduler and cancels an active async job.
Plex calls are bounded to `PLEX_TIMEOUT` (10 seconds by default), and cache
changes are flushed during normal cancellation. If a blocked filesystem,
network driver, or worker still prevents exit, the process watchdog exits after
`SHUTDOWN_TIMEOUT` (15 seconds). Docker's default 20-second grace period is the
final boundary. YAML and cache replacement is atomic, so a forced exit can lose
the current in-memory batch but cannot leave a partially written output file.

Keep `SHUTDOWN_TIMEOUT` lower than `STOP_GRACE_PERIOD`. The health-check startup
window is 20 seconds and does not delay the container process itself.

## Development and validation

Install the development lock and run the same checks as CI:

```bash
python -m pip install --require-hashes -r requirements-dev.lock
ruff check --select F,E9 .
pytest -q --cov=. --cov-report=term --cov-fail-under=50
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

MetaFusion is released under the MIT License.
