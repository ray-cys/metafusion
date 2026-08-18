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

## Command-line reference

Enter every option with two ordinary ASCII hyphens (`--`). Copy names exactly:
the underscores in older options such as `--metafusion_run` and `--dry_run`
are part of the supported interface, while newer options use hyphens. Options
apply only to the current process; omitted options continue to use environment
or `config.yml` values.

| Category | Option | Value | Purpose and requirements |
| --- | --- | --- | --- |
| Help | `-h`, `--help` | None | Print the supported command-line options and exit. |
| Build | `--version` | None | Print the image version, commit, Python/architecture, and supported SQLite schema versions, then exit. |
| Run control | `--metafusion_run` | None | Request one MetaFusion job immediately. |
| Run control | `--schedule` | None | Enable the scheduler for the current process. |
| Run control | `--run_times` | Comma-separated `HH:MM` values | Override the daily schedule, for example `--run_times 06:00,18:30`. Times use `TZ`. |
| Run control | `--dry_run` | None | Enable dry-run behavior for the current process. |
| Configuration | `--mode` | `kometa` or `plex` | Override the configured output mode. |
| Configuration | `--run_basic` | None | Enable basic metadata processing. |
| Configuration | `--run_enhanced` | None | Enable enhanced metadata processing. |
| Configuration | `--run_poster` | None | Enable poster processing. |
| Configuration | `--run_season` | None | Enable season artwork processing. |
| Configuration | `--run_background` | None | Enable background processing. |
| Diagnostics | `--doctor`, `--check-config` | None | Validate configuration and show value sources without running a job. Both names perform the same action. |
| Diagnostics | `--preflight` | None | Check Plex, TMDb, selected libraries, mappings, and storage without processing content. |
| Diagnostics | `--release-check` | None | Run the complete read-only preflight and SQLite health checks, write a redacted release-qualification report, and exit nonzero when an automated gate fails. |
| Diagnostics | `--asset-audit` | None | Perform a read-only, full artwork selection and ownership/quality audit and write a report. |
| Diagnostics | `--metadata-audit` | None | Perform a read-only full metadata comparison against TMDb and write field-level proposed actions. Artwork and cleanup are disabled. |
| Diagnostics | `--plan` | None | Produce one read-only full-scan plan for metadata, artwork, and eligible cleanup. The report is the only deliberate output. |
| Diagnostics | `--library-audit` | None | Inventory selected and available Plex libraries and audit enabled artwork in either output mode without applying changes. |
| Diagnostics | `--mapping-diagnose` | None | Compare one or more Plex TV inventories with TMDb standard ordering, configured overrides, split-series mappings, and episode groups. Requires `--rating-key`; writes guidance only. |
| Diagnostics | `--identity-inspect` | None | Explain the current Plex-to-TMDb identity, active learned binding, bounded history, warning reasons, edition, and computed destinations. Requires `--rating-key`; writes only a report. |
| Diagnostics | `--compatibility-check` | None | Test connectors, paths, and the configured Kometa/Plex output contract, write a compatibility report, and exit. |
| Diagnostics | `--status` | None | Print current runtime status and recent durable job history as JSON, then exit. |
| Diagnostics | `--support-report` | None | Write a value-free diagnostic report under `/config/reports`, then exit. |
| Compatibility | `--compatibility-profile` | `auto`, `kometa-2.4`, or `plex-api-v1` | Override `COMPATIBILITY_PROFILE` for this command or run. An explicit profile must match `RUN_MODE`. |
| Targeting | `--library` | Plex library name | Process only the named library. Repeat the option or use comma-separated names. |
| Targeting | `--rating-key` | Plex rating key | Process only the named item. Repeat the option or use comma-separated keys. Targeted runs disable cleanup. |
| Targeting | `--tmdb-id` | Numeric TMDb ID | Process items whose existing Plex GUID list exposes the ID. Repeat the option or use comma-separated IDs. |
| Targeting | `--media-type` | `movie` or `show` | Limit processing to movie or show libraries. Repeat it to select both. |
| Targeting | `--metadata-only` | None | Process metadata without artwork or cleanup. It cannot be combined with `--asset-only` or `--asset-audit`. |
| Targeting | `--asset-only` | None | Process enabled artwork without metadata or cleanup. It cannot be combined with `--metadata-only`. |
| Targeting | `--full-scan` | None | Bypass incremental skipping and reconcile the selected scope. |
| Targeting | `--explain-selection` | None | Explain why selected items are or are not due without processing or writing. |
| Recovery | `--retry-failed` | None | Immediately process matching durable retry-queue entries, including parked entries when selected. Cleanup stays disabled. |
| Recovery | `--retry-status` | `all`, `pending`, `parked`, or `running` | Narrow `--retry-failed`; defaults to `all` and is invalid without that command. |
| SQLite | `--sqlite-maintenance` | `check`, `optimize`, `checkpoint`, `vacuum`, or `backup` | Run one standalone, explicit database operation. Only `check` is read-only. |
| SQLite | `--sqlite-target` | `all`, `state`, or `tmdb` | Limit `--sqlite-maintenance`; defaults to both databases and is invalid without that command. |
| Plex maintenance | `--plex-metadata-restore` | None | Restore MetaFusion-owned Plex fields for items selected by `--rating-key`. Cannot be combined with `--plex-metadata-unlock`. |
| Plex maintenance | `--plex-metadata-unlock` | None | Remove only MetaFusion-created Plex locks for items selected by `--rating-key`. Cannot be combined with `--plex-metadata-restore`. |

`--healthcheck` belongs to the container entrypoint and is reserved for the
image's Docker health check; it is not a public `metafusion.py` option.

## Validate and inspect

```bash
# Validate configuration and show value sources without contacting connectors
python metafusion.py --doctor

# Contact Plex and TMDb, verify selected libraries and inspect mapped storage
# without creating or changing configuration, metadata, artwork, or state
python metafusion.py --preflight

# Run connector, path, storage, architecture, and SQLite release qualification
python metafusion.py --release-check

# Perform a read-only full artwork selection and ownership/quality audit
python metafusion.py --asset-audit

# Compare TMDb with current Kometa/Plex metadata without applying changes
python metafusion.py --metadata-audit

# Preview metadata, artwork, and cleanup decisions in one report
python metafusion.py --plan

# Inventory every Plex movie/show library and enabled artwork destination
python metafusion.py --library-audit

# Explain difficult TV episode ordering without changing any mapping or metadata
python metafusion.py --mapping-diagnose --rating-key 12345

# Explain how a Plex item became associated with TMDb without changing it
python metafusion.py --identity-inspect --rating-key 12345

# Confirm the configured output contract and required connector/path support
python metafusion.py --compatibility-check

# Show live scheduler state, effective build, library counts, and recent jobs
python metafusion.py --status

# Write a value-free diagnostic report under /config/reports
python metafusion.py --support-report
```

Public CLI exit codes are `0` for success, `1` for an operational or connector
failure, and `2` for invalid configuration or command combinations. Docker can
forward public options directly, for example `docker run --rm IMAGE --help` or
`docker run --rm IMAGE --version`.

Preflight fails when authentication, explicitly selected library names, mapping
roots, or required storage are unavailable. With `PLEX_LIBRARIES=auto`, it
prints the discovered movie/show libraries. It samples only a few Plex media
paths and, when one visible container mount is an unambiguous suffix match,
prints a proposed `PLEX_PATH_MAPPINGS` value. This is guidance only: Docker
mounts and filesystem ownership remain manual safety decisions. Preflight does
not create missing directories or probe files. The asset audit contacts Plex
and TMDb and can take about as long as a full artwork evaluation, but it does
not update YAML, artwork, ownership, incremental state, or TMDb cache. Its
deliberate output is a value-safe `asset-audit-*.txt` report.

The metadata audit always uses a dry-run full scan and writes
`/config/reports/metadata-audit-*.txt`. In Kometa mode it compares generated
TMDb fields with current YAML; in Plex mode it compares supported TMDb
candidates with current Plex fields and reports locks, policy exclusions,
conflicts, missing source values, differences, and proposed actions. It does
not write Plex fields, Kometa YAML, artwork, cache entries, ownership records,
or incremental markers. Metadata values are omitted from the report.

`--plan` combines the metadata and artwork evaluations with cleanup candidate
calculation when cleanup is configured and the inventory is complete. It uses
the same selection, ownership, schema, policy, and cleanup gates as a real run,
but forces dry-run behavior. It does not write Kometa YAML, Plex fields,
artwork, caches, ownership, retry state, or incremental markers. Its only
intentional persistent output is `/config/reports/change-plan-*.txt`. A target
option disables cleanup in the plan because a partial library/item scope cannot
prove an orphan.

`--library-audit` works in Kometa and Plex modes. It lists discovered and
selected movie/show libraries, item counts, artwork ownership outcomes,
candidate dimensions, and the normalized 0-100 artwork quality score. It also
lists the highest-scoring rejected candidates, their score components, and
whether language priority, dimensions, vote threshold, aspect ratio, downgrade
protection against the existing file, or a deterministic tie-break determined
the reported action. It
writes `/config/reports/library-asset-audit-*.txt` and does not modify either
output mode. Like the asset audit, it can take about as long as full artwork
evaluation because it contacts Plex and TMDb.

`--mapping-diagnose` accepts one or more TV show rating keys. It compares the
complete Plex season/episode inventory with TMDb standard ordering, configured
`TMDB_EPISODE_OVERRIDES`, split-series mappings, and available TMDb episode
groups. When one complete one-step numbering offset is uniquely provable, the
report includes a proposed configuration snippet for review. It never applies
that proposal, changes a learned identity, writes Plex or Kometa metadata, or
downloads artwork. Results are written to
`/config/reports/mapping-diagnosis-*.txt`; an unresolved result is a successful
diagnostic outcome rather than a mutation failure.

`--identity-inspect` works for movies and shows in either output mode. For each
requested Plex rating key it reports Plex GUIDs and external IDs, localized and
original titles, year, edition, selected TMDb ID, resolution source, match
confidence, warning/rejection reasons, active learned binding, and the newest
50 binding-history events. It also computes the Kometa YAML entry or Plex API
target and poster, background, and season artwork destinations. Provider
response caching is disabled and SQLite is read in query-only mode. Its sole
deliberate write is `/config/reports/identity-inspection-*.txt`; it does not
touch bindings, caches, provider fields, YAML, artwork, ownership, incremental
markers, or cleanup. History begins when the identity-history extension is
installed. An older active binding remains visible, but earlier transitions
cannot be reconstructed. The extension remains schema-4 rollback compatible;
older MetaFusion images ignore its nullable columns and additional table.

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

# Target IDs Plex already exposes through TMDb GUIDs
python metafusion.py --metafusion_run \
  --library Movies --tmdb-id 550,551 --metadata-only

# Process every selected movie library but no show library
python metafusion.py --metafusion_run --media-type movie

# Explain why an item is or is not due without processing it
python metafusion.py --library Movies --rating-key 12345 --explain-selection
```

`--library` and `--rating-key` may be repeated or contain comma-separated
values. `--tmdb-id` has the same repeat/comma behavior and matches only TMDb
IDs already exposed in the Plex item's GUID list; it does not run a risky title
search. Use `--rating-key` when Plex does not expose the expected provider GUID.
Targeted runs disable cleanup. Add `--full-scan` to bypass incremental
selection for the selected scope. An explicitly requested rating key or TMDb
ID that is not found makes the job fail clearly instead of silently succeeding.

## Incremental and full scans

`INCREMENTAL=True` skips successfully processed unchanged items. An item is
selected again when its Plex update marker changes, a metadata/artwork recheck
becomes due, required generated output is missing, or a full reconciliation is
required.

For TV libraries, MetaFusion also fingerprints the show-level `childCount`,
`seasonCount`, and `leafCount` values returned with the normal Plex library
inventory. A new season or episode therefore selects its parent show even when
Plex leaves the show's `updatedAt` value unchanged. This adds no media-tree scan
and no per-episode Plex request to an otherwise skipped show. If a Plex server
omits all three counters, `updatedAt` and the periodic full scan remain the
fallbacks.

The first successful run after enabling this behavior selects existing shows
whose older SQLite records have no child fingerprint, establishing a baseline.
Later unchanged runs return to normal incremental skipping.

`--explain-selection` separates the trigger from the work that would run. Its
per-item causes include a new rating key, Plex `updatedAt` change, TV child
inventory change/baseline, configuration change, pending metadata recheck,
Plex metadata recheck, artwork interval, targeted rating key, or full scan.
Each library ends with selected, unchanged/not-due, and cause counts.

`FULL_SCAN_INTERVAL_HOURS` is evaluated from the saved last successful full
scan for each Plex server/library, not the current Docker runtime. The default
`168` is seven days. Restarting the container does not force or postpone this
deadline.

Cleanup is considered only on a complete reconciliation. Incremental and
targeted runs explicitly report that cleanup was skipped; they do not print a
misleading `0 Titles Removed` result.

Future episodes whose Plex records arrive before TMDb publishes episode data
are marked pending in SQLite. They are selected for metadata-only evaluation
after `METADATA_PENDING_RECHECK_HOURS` even if Plex's update timestamp is
unchanged; the marker clears automatically once every pending episode resolves.

### Durable failed-item recovery

Before processing, each real Plex item is marked `running` in
`meta_db.sqlite3`. A container stop therefore leaves interrupted work visible
and eligible on the next run. Transient failures use bounded exponential
delays of 15 minutes, 1 hour, 6 hours, then 24 hours; successful processing
clears the record. After repeated failures, or for deterministic identity/path
rejections, the deadline queue parks the item so one bad title cannot consume
every incremental run. A full scan, explicit targeted run, Plex marker change,
or configuration-triggered evaluation can still recheck it.

A changed Plex `updatedAt` marker resets the failure history and gives a parked
item a fresh attempt. Retry selection works during incremental mode and does
not enable cleanup. `python metafusion.py --status` shows pending, running, and
parked queue totals. Retry deadlines are evaluated at job start; MetaFusion
does not wake outside configured schedule times solely for a retry.

To retry a deliberate subset immediately:

```bash
# Retry every queued item in the currently selected libraries
python metafusion.py --retry-failed

# Retry only parked items in one library
python metafusion.py --retry-failed --retry-status parked --library Movies

# Retry one known queued item
python metafusion.py --retry-failed --library Movies --rating-key 12345
```

This command still performs the item's normally enabled metadata and artwork
work. A successful item is removed from the queue; another failure updates its
bounded retry record. It never enables cleanup and a request matching no queue
rows is a successful no-op with an explicit log message.

In-flight markers and successful removals are committed once per library, not
once per item. This preserves restart recovery without adding thousands of
SQLite fsyncs to a large full scan.

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
The values are adaptive bases, not a filesystem age scan:

- Missing candidates retry after 1, 3, 7, 14, 30, then 60 days, never later
  than the configured base.
- A repeatedly unchanged candidate doubles its base up to 180 days, while an
  explicitly longer base remains respected.
- A different candidate or successful upgrade resets the unchanged backoff.

Candidate identity and observations are saved in SQLite. MetaFusion does not
walk the media tree or read file mtimes to calculate artwork age.

## Log retention

The persistent log rotates at local midnight and whenever the active file
reaches `LOG_MAX_MB` (10 MiB by default). `LOG_BACKUP_COUNT` keeps the newest
14 rotated files by default. Set `LOG_MAX_MB=0` only when size-based rotation
must be disabled; daily rotation remains active.

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

## Adaptive concurrency and provider protection

MetaFusion uses separate item, TMDb, Plex, and nested artwork/season-work
lanes. With `MAX_CONCURRENCY=0` (the default), startup ceilings are calculated
from the CPU quota, CPU affinity, and memory limit visible inside the container.
The controller starts conservatively, increases one slot after healthy work,
and reduces only the affected lane after failures. CPU or memory pressure at
90% reduces item/nested work without cancelling operations already in flight.

TMDb HTTP 429 responses immediately reduce the TMDb lane and still honor
`Retry-After`. Repeated provider failures open only that provider's circuit;
new work is preserved or skipped during the cooldown, then a single half-open
probe decides whether the circuit closes. Identical concurrent TMDb requests
share one in-flight request, response, and cache write.

Successful Plex calls taking five seconds or longer reduce only the Plex lane,
preventing a slow server from accumulating requests even when it returns no
error. Direct Plex metadata writes that reach the per-run safety cap are
recorded as deferred and returned to the durable retry planner for a later job.

The log records the detected resources, initial and maximum limits, meaningful
adjustments, circuit transitions, and final limits. A positive
`MAX_CONCURRENCY` remains an advanced troubleshooting ceiling, not a fixed
worker count. It does not raise MetaFusion's internal maximums.

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
| `/config/cache/meta_db.sqlite3` | Durable media state, retry queue, learned identities and bounded transition history, discovered-library inventory, artwork ownership, per-library full scans, schedules, and job history | Back up with appdata while the container is stopped. Before a schema upgrade, two bounded `pre-v*` backups are retained. Do not treat as disposable. |
| `/config/cache/tmdb_cache.sqlite3` | Compressed successful TMDb responses | Disposable; it is storage-sized and pruned automatically. Corruption is quarantined with a timestamp and causes a clean rebuild. |

Rows are read and updated individually rather than loading and rewriting a
large JSON cache. TMDb cache expiry or pruning cannot remove durable scan or
artwork ownership state. After jobs, both databases run bounded optimization;
WAL files are truncated only after reaching the maintenance threshold.

### Explicit SQLite maintenance

Normal jobs already perform bounded automatic optimization. Use the standalone
maintenance CLI only for diagnosis, a deliberate backup, or an administrator
maintenance window:

```bash
python metafusion.py --sqlite-maintenance check
python metafusion.py --sqlite-maintenance backup --sqlite-target state
python metafusion.py --sqlite-maintenance optimize
python metafusion.py --sqlite-maintenance checkpoint --sqlite-target tmdb
python metafusion.py --sqlite-maintenance vacuum --sqlite-target tmdb
```

`check` opens databases read-only and runs SQLite `quick_check`. `optimize`
updates SQLite planner statistics. `checkpoint` explicitly truncates WAL state.
`vacuum` rewrites the selected database and refuses to start unless conservative
free-space headroom is available. `backup` uses SQLite's online backup API,
verifies the copy, writes mode `0664` under `/config/backups`, and retains the
newest three copies per database. Missing databases are reported and skipped,
which is normal before the first run. Every mutating operation holds the normal
job lock and refuses to overlap a MetaFusion job. Stop the container before
copying live database files outside this command.

To restore durable state, stop MetaFusion, keep the damaged database for
forensics, copy a verified `meta_db-*.sqlite3` backup back to
`/config/cache/meta_db.sqlite3`, retain owner/group write access, and run
`--sqlite-maintenance check --sqlite-target state` before resuming scheduled
jobs. Never replace a live database while the container is running. TMDb cache
restoration is normally unnecessary because that database is disposable and
rebuilds from later API responses.

### Compatibility profiles

`COMPATIBILITY_PROFILE=auto` is recommended. It resolves to `kometa-2.4` in
Kometa mode and `plex-api-v1` in Plex mode. A profile is a declared output
contract, not an emulation layer or a request to modify the connected server.

- `kometa-2.4` checks that the selected mode, output root, and Kometa schema
  validation support MetaFusion's generated YAML/assets contract.
- `plex-api-v1` checks the Plex server identity, selected libraries, and mapped
  artwork paths required by enabled features. Direct Plex metadata can remain
  disabled for an artwork-only deployment.

An explicit profile that conflicts with `RUN_MODE` is a configuration error.
`--compatibility-check` contacts Plex and TMDb, performs the relevant path
checks, writes `/config/reports/compatibility-*.txt`, and exits nonzero when a
required capability is unavailable.

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
/config/reports/asset-audit-YYYYMMDD-HHMMSS.txt
/config/reports/change-plan-YYYYMMDD-HHMMSS.txt
/config/reports/library-asset-audit-YYYYMMDD-HHMMSS.txt
/config/reports/mapping-diagnosis-YYYYMMDD-HHMMSS.txt
/config/reports/identity-inspection-YYYYMMDD-HHMMSS.txt
/config/reports/compatibility-YYYYMMDD-HHMMSS.txt
/config/reports/destination-history-YYYYMMDD-HHMMSS.txt
/config/reports/plex-metadata-YYYYMMDD-HHMMSS.txt
/config/reports/metafusion-support-*.txt
```

Artwork-gap reports identify missing/rejected artwork and identity failures.
Asset-audit reports include the selected candidate's language, dimensions,
vote score, ownership status, existing dimensions, score components, the top
rejected candidates, and the action a real run would consider. They omit
filesystem paths and do not prove that a later download will succeed.
Identity-inspection and destination-history reports contain computed or actual
media paths and must be reviewed before sharing. Destination-history reports
identify old and current artwork paths after a
Plex title/path rename; MetaFusion does not delete the old path. Plex metadata
reports identify fields and outcomes. Reports are bounded.

At the default `LOG_LEVEL=INFO`, MetaFusion logs confirmed mutations such as
Kometa YAML writes, Plex API update batches, and artwork downloads or upgrades.
Routine unchanged checks are available at `DEBUG` to avoid flooding normal
logs. Plex locked-field, conflict, and write-limit totals are warnings; the
corresponding `plex-metadata-*.txt` report retains field-level audit details.

Direct Plex metadata progress is automatic and not configurable. Small
libraries report every 5 items or 10%, medium libraries every 25 items or 5%,
and large libraries every 100 items or 5%, whichever interval is larger. A
30-second minimum gap prevents log flooding, a 60-second heartbeat covers slow
shows, and start/final progress is always logged. TV progress counts top-level
shows while their seasons and episodes remain part of each show operation.

Every completed job also logs one local performance summary: total, Plex
inventory and library-processing time; items per minute; TMDb requests,
cache hits/misses, retries and rate-limit waits; and the five slowest items by
library plus Plex rating key. It intentionally omits media paths and metadata
values. Use it to compare full and incremental runs without adding a metrics
service.

Before normal writes, MetaFusion validates `/config`, Kometa output, and any
configured Plex mapping destinations. `MIN_FREE_SPACE_MB` and an automatic
1%-of-volume floor (bounded from 256 MiB to 2 GiB) are checked at each artwork
destination before a download. MetaFusion first prunes disposable TMDb cache
rows when both databases share the pressured volume. If space remains low,
artwork is deferred to the retry queue while metadata processing continues.
A missing/unmounted destination still fails safely instead of writing into an
unintended container directory. `VALIDATE_MEDIA_MOUNTS=False` disables only the
startup mapping-root check; per-artwork destination checks remain active.

Plex inventories are retrieved through automatic bounded pages. A lightweight
discovery pass establishes global title/edition counts; only one library's Plex
objects are retained during its processing pass. MetaFusion verifies the item
total before and after paging, rejects duplicate rating keys, and requires the
processing pass to match discovery. Any missing, repeated, or changing page
fails the library safely and disables cleanup for that run. Page size is
automatic and has no user setting.

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
