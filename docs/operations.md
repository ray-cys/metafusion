# Scheduling, maintenance, state, and troubleshooting

This guide covers day-to-day operation after the container has been configured.
Platform installation is documented separately for
[Docker Compose](docker-compose.md) and [Unraid](unraid.md).

## Scheduler behavior

The normal long-running service uses:

```text
METAFUSION_RUN=False
RUN_SCHEDULE=True
RUN_ON_START=False
```

`RUN_TIMES` is a comma-separated list of daily `HH:MM` times interpreted in
`TZ`. The scheduler writes successful and failed job timestamps to durable
SQLite state. Timers are not based on container uptime.

Stopping MetaFusion for an Unraid appdata backup therefore does not reset the
incremental, full-scan, artwork, or metadata recheck ages. With
`SCHEDULE_CATCH_UP=True`, startup runs the most recent missed slot once when it
is no older than `SCHEDULE_CATCH_UP_MAX_HOURS` and no later successful job
already covers it.

`RUN_ON_START=True` is different: it requests a job on every scheduler start,
regardless of whether a scheduled slot was missed.

## Run one job

With Docker Compose:

```bash
docker compose run --rm -e METAFUSION_RUN=True metafusion
```

From an existing container console:

```bash
python metafusion.py --metafusion_run
```

Do not leave `METAFUSION_RUN=True` on the long-running service unless a new
one-shot run after every container restart is intentional.

## Validate and inspect

```bash
# Validate configuration and show value sources without contacting connectors
python metafusion.py --doctor

# Show live scheduler state, effective build, library counts, and recent jobs
python metafusion.py --status

# Write a value-free diagnostic report under /config/reports
python metafusion.py --support-report
```

The support report contains image version/commit, configuration binding names,
state and cache health, platform details, and validation status. It does not
include configuration values, tokens, API keys, or metadata summaries.

## Targeted repairs

Target one or more exact Plex libraries and rating keys:

```bash
# Metadata only
python metafusion.py --metafusion_run \
  --library Movies --rating-key 12345 --metadata-only

# Enabled artwork only
python metafusion.py --metafusion_run \
  --library "TV Shows" --rating-key 12345 --asset-only

# Explain why an item is or is not due without processing it
python metafusion.py --library Movies --rating-key 12345 --explain-selection
```

`--library` and `--rating-key` may be repeated or contain comma-separated
values. Targeted runs disable cleanup. Add `--full-scan` to bypass incremental
selection for the selected scope.

## Incremental and full scans

`INCREMENTAL=True` skips successfully processed unchanged items. An item is
selected again when its Plex update marker changes, a metadata/artwork recheck
becomes due, required generated output is missing, or a full reconciliation is
required.

`FULL_SCAN_INTERVAL_HOURS` is evaluated from the saved last successful full
scan for each Plex server/library, not the current Docker runtime. The default
`168` is seven days. Restarting the container does not force or postpone this
deadline.

Cleanup is considered only on a complete reconciliation. Incremental and
targeted runs explicitly report that cleanup was skipped; they do not print a
misleading `0 Titles Removed` result.

## Artwork refresh timing

Unchanged artwork can become due independently for movies, series, and
seasons:

```text
IMAGE_UPGRADE_DAYS=30
MOVIE_IMAGE_UPGRADE_DAYS=30
SERIES_IMAGE_UPGRADE_DAYS=15
SEASON_IMAGE_UPGRADE_DAYS=15
```

Blank type-specific values inherit `IMAGE_UPGRADE_DAYS`. Decimal values are
supported; `0.5` means 12 hours. `0` disables timed refreshes for that type.
The saved MetaFusion upgrade timestamp is used—there is no media-tree scan to
calculate artwork age from filesystem dates.

At the first scheduled job after an interval expires, MetaFusion evaluates a
candidate. [Artwork policy](policies.md#artwork-update-policies) and quality
rules determine whether the file may actually be replaced.

Under `managed`, a run can also report `Adopted` artwork. This is a one-time
ownership verification for an existing file that exactly matches the selected
TMDb bytes; it does not rewrite the destination or alter filesystem ownership
or permissions. The first verification run can therefore take longer and use
more TMDb/network I/O than later runs.

Per-library timing overrides are documented in
[Configuration](configuration.md#per-library-overrides).

## Cleanup dry run

Before enabling cleanup, follow the full
[cleanup safety checklist](policies.md#cleanup-and-deletion-safety). A complete
one-time preview uses:

```text
INCREMENTAL=False
RUN_CLEANUP=True
DRY_RUN=True
```

Run one job and review the final report. Restore normal values afterward. A
dry run does not update YAML, artwork, cache, or durable state. Direct Plex
metadata dry runs retain one redacted audit report as the deliberate exception.

## Persistent state and I/O

MetaFusion uses two separate SQLite databases:

| Path | Purpose | Recovery behavior |
| --- | --- | --- |
| `/config/cache/meta_db.sqlite3` | Durable media state, artwork ownership, per-library full scans, schedules, and job history | Back up with appdata while the container is stopped. Do not treat as disposable. |
| `/config/cache/tmdb_cache.sqlite3` | Compressed successful TMDb responses | Disposable; corruption or deletion causes a clean cache rebuild. |

Rows are read and updated individually rather than loading and rewriting a
large JSON cache. TMDb cache expiry or pruning cannot remove durable scan or
artwork ownership state.

Obsolete `meta_cache.json`, `incremental_state.json`,
`tmdb_response_cache.json`, and `.bak` files are ignored. There is no JSON
migration path in the public SQLite-only configuration; obsolete files can be
removed manually after confirming the SQLite deployment.

The live heartbeat is `/tmp/metafusion-status.json`. It is intentionally
ephemeral, avoiding a persistent appdata write every 30 seconds. A file left at
`/config/metafusion-status.json` is obsolete and can be removed after changing
`STATUS_FILE` back to its `/tmp` default.

## Generated output and reports

Kometa mode writes:

```text
/kometa/metadata/movie_metadata.yml
/kometa/metadata/tv_metadata.yml
/kometa/metadata/.metafusion-backups/*.bak
/kometa/assets/movie/...
/kometa/assets/tv/...
```

Plex mode never writes those YAML files. Enabled artwork is written beside the
mapped media. Direct Plex metadata updates are sent through the API.

Shared reports and logs are:

```text
/config/logs/metafusion.log
/config/reports/artwork-gaps-YYYYMMDD-HHMMSS.txt
/config/reports/plex-metadata-YYYYMMDD-HHMMSS.txt
/config/reports/metafusion-support-*.txt
```

Artwork-gap reports identify missing/rejected artwork and identity failures.
Plex metadata reports identify fields and outcomes. They are bounded and safe
for support when host paths in accompanying logs have also been reviewed.

## Container health and shutdown

Inspect Compose logs and health with:

```bash
docker compose logs --tail=200 metafusion
docker inspect --format '{{json .State.Health}}' metafusion
```

A failed scheduled job appears in live status and durable job history. By
default `HEALTH_FAIL_ON_JOB_ERROR=False`, so a content/API failure does not
create a Docker restart loop. Set it true only when external monitoring should
treat the latest failed job as an unhealthy container.

Keep `SHUTDOWN_TIMEOUT` lower than Docker's stop timeout
(`STOP_GRACE_PERIOD` in Compose). The supplied defaults are 15 and 20 seconds.
The application stops accepting new work, cancels pending work, closes its
connections and state, then exits. Do not bypass the image entrypoint or init
process.

## Common problems

| Symptom | What to check |
| --- | --- |
| `/config` or `/kometa` is not writable | Confirm the host directory is writable by `PUID:PGID` (`99:100` on standard Unraid) and no Docker user override is set. |
| Plex mode does not write artwork | Add read/write media mappings and confirm every `PLEX_PATH_MAPPINGS` destination matches a container path. |
| Direct Plex metadata is unchanged | Confirm API opt-in, item field locks, policy, field allowlist, write cap, and the latest metadata report. |
| Kometa output is missing | Confirm `RUN_MODE=kometa`, `RUN_BASIC=True`, `KOMETA_PATH`, and the writable `/kometa` mapping. |
| Kometa output changed during a run | Stop other writers and rerun; MetaFusion refuses to overwrite concurrently changed YAML. |
| Another job is active | Wait for the process holding `/config/.metafusion-run.lock`; operating-system locking, not stale file existence, controls access. |
| TMDb artwork exists but is skipped | Inspect the artwork-gap report, language fallbacks, `ARTWORK_ALLOW_ANY_LANGUAGE`, update policy, and destination mapping. |
| Episodes remain unmapped | `not available yet` is a harmless future-episode notice. For `could not safely map`, check the exact SxxExx list and Plex ordering; existing YAML is preserved. |
| Scheduled runs do not start | Check `RUN_SCHEDULE`, `RUN_TIMES`, `TZ`, current `--status`, and whether the container remains running. |
| Container is slow to stop | Restore the supplied entrypoint/init settings and keep the Docker stop timeout above `SHUTDOWN_TIMEOUT`. |

When opening a GitHub issue, attach the latest relevant redacted report,
support report, Plex server version, and only the necessary log section. Never
attach `config.yml`, tokens, API keys, Docker inspection output, or unredacted
host paths.
