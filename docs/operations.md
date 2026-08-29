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

With `CONFIG_RELOAD=True`, the long-running scheduler watches the selected
YAML profile and mounted secret files. A changed file is reloaded only while
no job is running, then the complete effective configuration and required
paths are validated. Invalid changes are rejected and the last working
configuration and schedule remain active. A valid `RUN_TIMES` change replaces
the in-process schedule. If an already-running scheduler reloads
`RUN_SCHEDULE=False`, it pauses jobs but continues watching so a later valid
YAML change can resume them. Environment
variables cannot change inside an existing container, and Docker-level mounts,
`PUID`, `PGID`, or `TZ` still require recreating the container. Configuration
is never changed during a running job.

## Run one job

With Docker Compose:

```bash
docker compose run --rm -e METAFUSION_RUN=True metafusion
```

From an existing container console:

```bash
metafusion --metafusion_run
```

The container-provided `metafusion` command always passes through the image
entrypoint. If the console was opened as root, it prepares only managed runtime
paths and drops to `PUID:PGID` before starting Python. Do not invoke
`python /app/metafusion.py` directly from an Unraid or Docker exec console;
that deliberately triggers the root-ownership safety guard.

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
| Diagnostics | `--preflight` | None | Contact Plex and TMDb, check selected libraries, mappings, and required storage, print guidance, and exit without retaining a report. |
| Diagnostics | `--release-check` | None | Run the complete read-only preflight and SQLite health checks, write a redacted release-qualification report, and exit nonzero when an automated gate fails. |
| Diagnostics | `--asset-audit` | None | Perform a read-only full artwork selection and ownership/quality audit through TMDb, Fanart.tv, and Plex fallback stages, then write a report. |
| Diagnostics | `--metadata-audit` | None | Perform a read-only full metadata comparison against TMDb and write field-level proposed actions. Artwork and cleanup are disabled. |
| Diagnostics | `--plan` | None | Produce one read-only full-scan plan for metadata, provider-aware artwork, and eligible cleanup reconciliation. The report is the only deliberate output. |
| Diagnostics | `--library-audit` | None | Inventory selected and available Plex libraries and audit enabled artwork, provider attempts, ownership, quality, and rejected candidates in either output mode. |
| Diagnostics | `--mapping-diagnose` | None | Run a standalone comparison of Plex TV inventory with TMDb ordering, overrides, split-series mappings, and episode groups. Requires `--rating-key`; writes guidance only. |
| Diagnostics | `--identity-inspect` | None | Run a standalone explanation of Plex-to-TMDb identity, learned binding/history, warnings, edition, and destinations. Requires `--rating-key`; writes only a report. |
| Diagnostics | `--explain-item` | None | Run a standalone unified item explanation covering identity, scheduled selection, policies, TV mapping, retries, cached artwork source, and destinations. Requires `--rating-key`; writes only a report. |
| Diagnostics | `--compatibility-check` | None | Test connectors, paths, and the configured Kometa/Plex output contract, write a compatibility report, and exit. |
| Diagnostics | `--status` | None | Print current runtime status and recent durable job history as JSON, then exit. |
| Diagnostics | `--problems` | None | Print the persistent open unresolved-work ledger as JSON without contacting Plex or an artwork provider. |
| Diagnostics | `--support-report` | None | Perform a local value-free configuration/build/state inventory, write it under `/config/reports`, and exit without contacting providers. |
| Diagnostics | `--capture-replay` | None | Capture sanitized support data for items selected by `--rating-key`; writes a text manifest and JSON companion without changing metadata, artwork, or state. |
| SQLite reports | `--state-report` | None | Generate human-readable and JSON reports entirely from recorded SQLite state; no provider, Plex, YAML, or artwork access occurs. |
| SQLite reports | `--dashboard-report` | None | Generate a self-contained offline HTML dashboard plus JSON companion from recorded SQLite state. |
| SQLite reports | `--upgrade-canary-report` | None | Generate the configured report format from the latest detailed upgrade-canary result stored in SQLite. It does not rerun the canary or contact providers. |
| Formula 1 artwork | `--formula1-upgrade-artwork` | `current` or `all` | Explicitly evaluate extension-managed Formula 1 artwork against Flickr and apply only a materially better, same-constructor source. Requires Kometa mode and a configured Flickr API key. |
| SQLite reports | `--state-section` | `all`, `database`, `libraries`, `jobs`, `ownership`, `provenance`, `problems`, or `items` | Limit `--state-report`; defaults to `all`. |
| SQLite reports | `--include-state-items` | None | Include item-level rows in `--state-report`; otherwise items appear only when targeted or when the `items` section is selected. |
| SQLite reports | `--cleanup-history-report` | None | Report pending cleanup confirmations and completed/cancelled automated or manual cleanup actions. |
| SQLite reports | `--history-source` | `automated` or `manual` | Filter `--cleanup-history-report`; repeat to select both. |
| SQLite reports | `--run-history` | None | Write retained run duration, throughput, provider activity, library results, and capacity guidance without contacting Plex/providers. |
| SQLite reports | `--schedule-advice` | None | Compare retained median/95th-percentile duration with the shortest configured schedule interval. |
| Cleanup recovery | `--cleanup-quarantine-report` | None | Report active, restored, purged, missing, and protected cleanup quarantine records. |
| Cleanup recovery | `--cleanup-restore` | History ID | Restore one active checksum-proven quarantined artwork file without overwriting an existing destination. |
| Cleanup recovery | `--cleanup-purge` | None | Purge only expired, checksum-matching quarantine files; protected mismatches are retained. |
| Output lifecycle | `--output-action` | `preview`, `remove`, `forget`, or `rebuild` | Inspect or manage only the checksum-proven output of exactly one recorded item. Requires a unique target. |
| Output lifecycle | `--output-type` | `all`, `metadata`, `poster`, `background`, or `season` | Limit `--output-action`; defaults to `all`. |
| Output lifecycle | `--season-number` | Integer | Limit season output management or a season exception to one Plex season, including `0` for Specials. |
| Output lifecycle | `--acknowledge-metadata-loss` | None | Acknowledge that removing/rebuilding one Kometa YAML entry can also remove manual fields within that entry. |
| Exceptions | `--exception-action` | `list`, `add`, or `remove` | Maintain a durable per-item processing exception selected by library/rating key. |
| Exceptions | `--exception-output` | `all`, `metadata`, `plex_metadata`, `poster`, `background`, `season`, or `cleanup` | Select the output lane affected by `--exception-action add/remove`. |
| Identity | `--identity-override-action` | `list`, `set`, or `remove` | Maintain an explicit durable Plex-item to TMDb binding. `set` uses exactly one `--tmdb-id`. |
| Identity | `--identity-review-queue` | None | Generate text/JSON reports of persistent unresolved identity work from SQLite only. |
| Audit trail | `--reason` | Text | Store an operator explanation with an added exception or identity override. |
| Migration | `--library-rebind` | `plan` or `apply` | Safely transfer non-conflicting durable ownership after a library migration; always run `plan` first. |
| Migration | `--from-library` | Library name or UUID | Select the already-recorded source for `--library-rebind`. |
| Migration | `--to-library` | Library name or UUID | Select the already-scanned destination for `--library-rebind`. |
| Recovery | `--recovery-bundle` | None | Create a redacted, verified bundle of durable state, configuration, ownership, and Kometa YAML; artwork/provider caches are excluded. |
| Recovery | `--verify-recovery` | Bundle path | Offline-verify archive paths, hashes, manifest, and SQLite integrity. |
| Configuration | `--config-impact` | Proposed `config.yml` path | Compare current and proposed effective values and explain affected behavior without processing. |
| Plex artwork | `--plex-artwork-verify` | None | Read Plex's currently selected images and compare them with checksum-proven local artwork; never refreshes or changes Plex. |
| Kometa verification | `--kometa-application-audit` | None | In Kometa mode, compare generated YAML fields and managed artwork with live Plex after Kometa has run. It does not invoke Kometa or change Plex. |
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
| SQLite | `--sqlite-target` | `all`, `state`, `tmdb`, or `fanart` | Limit `--sqlite-maintenance`; defaults to every database and is invalid without that command. |
| Plex maintenance | `--plex-metadata-restore` | None | Restore MetaFusion-owned Plex fields for items selected by `--rating-key`. Cannot be combined with `--plex-metadata-unlock`. |
| Plex maintenance | `--plex-metadata-unlock` | None | Remove only MetaFusion-created Plex locks for items selected by `--rating-key`. Cannot be combined with `--plex-metadata-restore`. |

`--healthcheck` belongs to the container entrypoint and is reserved for the
image's Docker health check; it is not a public `metafusion.py` option.

### Explicit Formula 1 artwork upgrade

Formula 1 source bindings remain stable during normal scheduled runs. To
deliberately evaluate existing extension-managed artwork against Flickr, run:

```bash
metafusion --formula1-upgrade-artwork current
metafusion --formula1-upgrade-artwork all
```

`current` checks the active race's episode cards and current show poster and
background. `all` checks episode cards for every detected race round, plus the
current show pair. Both modes keep each round's bound constructor, evaluate the
background independently, require a material decoded-quality or race-specificity
gain, and preserve files whose managed checksum no longer matches. Decisions are
recorded in the private Formula 1 SQLite database and in a paired TXT/JSON report.
Repeated use is safe: an existing Flickr binding or a candidate without a
meaningful gain is recorded and left unchanged.

## Diagnostics

Use the [diagnostics and support reports guide](diagnostics.md) to choose a
local check, connector preflight, full library audit, or item-level explanation.
That guide documents provider contact, deliberate report writes, privacy,
standalone-command rules, and the difference between cached item explanation
and live provider artwork scoring. The table above remains the authoritative
inventory of public flags.

The [lifecycle management guide](lifecycle-management.md) provides complete
workflows for targeted output management, exceptions, identity review,
cleanup history, library rebinding, recovery bundles, SQLite reporting,
configuration comparison, and Plex artwork adoption verification.

Public CLI exit codes are `0` for success, `1` for an operational or connector
failure, and `2` for invalid configuration or command combinations. Docker can
forward public options directly, for example `docker run --rm IMAGE --help` or
`docker run --rm IMAGE --version`.

## Targeted repairs

Target one or more exact Plex libraries and rating keys:

```bash
# Metadata only
metafusion --metafusion_run \
  --library Movies --rating-key 12345 --metadata-only

# Enabled artwork only
metafusion --metafusion_run \
  --library "TV Shows" --rating-key 12345 --asset-only

# Target IDs Plex already exposes through TMDb GUIDs
metafusion --metafusion_run \
  --library Movies --tmdb-id 550,551 --metadata-only

# Process every selected movie library but no show library
metafusion --metafusion_run --media-type movie

# Explain why an item is or is not due without processing it
metafusion --library Movies --rating-key 12345 --explain-selection
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

With `TMDB_CHANGE_RECHECKS=True`, an incremental job also reads TMDb's bounded
movie and TV change feeds from the last successfully committed checkpoint. A
locally known TMDb ID returned by that feed selects its Plex item even when
Plex's update marker is unchanged. Its TMDb detail and season requests refresh
the corresponding cached responses before normal metadata and artwork policy
checks run. IDs that are not present in the selected Plex inventory are ignored.

The first eligible job establishes an authoritative full-scan baseline. A
missing, corrupt, future, or older-than-13-days checkpoint also forces a safe
full scan because TMDb exposes only a bounded change window. If either change
feed is incomplete, normal incremental safeguards continue and the old
checkpoint is retained. Targeted and dry-run commands neither consume nor
advance it. The new checkpoint is committed only when the complete MetaFusion
job succeeds.

See [TMDb change-aware rechecks](runtime-safeguards.md#tmdb-change-aware-rechecks)
for the full selection, refresh, and checkpoint contract.

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

### One-time upgrade canary

`UPGRADE_CANARY=True` qualifies each new published image commit once for the
current Plex server, output mode, and compatibility profile. After connector
and lightweight inventory checks, MetaFusion deterministically exercises up to
two items per non-empty selected library through the existing identity,
mapping, policy, and destination explanation pipeline. It runs before media
output writes and never edits Plex, YAML, or artwork.

The canary stores its detailed checks and samples in `meta_db.sqlite3` without
creating files during startup or normal processing. A failure stops the job
before output processing. Generate the latest stored result on demand with
`metafusion --upgrade-canary-report`. A pass is remembered in SQLite
only after the surrounding job also completes successfully, so a later failure
causes the canary to run again. Development builds, dry runs, and an unchanged
published commit do not rerun it. Disable this advanced safety gate only with
`UPGRADE_CANARY=False`.

See the [one-time upgrade canary](runtime-safeguards.md#one-time-upgrade-canary)
for qualification scope, retry behavior, and reports.

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
not enable cleanup. `metafusion --status` shows pending, running, and
parked queue totals. Retry deadlines are evaluated at job start; MetaFusion
does not wake outside configured schedule times solely for a retry.

To retry a deliberate subset immediately:

```bash
# Retry every queued item in the currently selected libraries
metafusion --retry-failed

# Retry only parked items in one library
metafusion --retry-failed --retry-status parked --library Movies

# Retry one known queued item
metafusion --retry-failed --library Movies --rating-key 12345
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
- TMDb's localized canonical `poster_path`/`backdrop_path` is preferred when it
  passes absolute validation. A changed canonical path for existing managed
  artwork is confirmed on two provider observations; a built-in 24-hour
  follow-up prevents a long library interval from delaying that confirmation.
  Missing artwork installs a valid canonical image immediately. Setting the
  applicable interval to `0` also disables the timed confirmation follow-up.
- Under the default `managed` policy, an unchanged provider image identifier
  receives a byte-level verification re-download after the longer of 90 days
  or three times its configured base, capped at 365 days. Identical bytes are
  discarded; changed bytes continue through normal validation. Noncanonical
  fallback replacements retain the relative quality guard. This is automatic
  and does not add a configuration variable.

Candidate identity, observations, and successful same-source verification
timestamps are saved in SQLite. MetaFusion does not walk the media tree or read
file mtimes to calculate artwork age.

## Log retention

Every scheduled, startup, catch-up, or forced processing job creates a separate
timestamped file under `/config/logs`, for example
`metafusion-2026-08-22_07-35-00_123456.log`. The file is opened after the
single-job lock is acquired and is closed after reports, cache maintenance, the
optional dashboard, and the final run status have completed. Scheduler startup,
idle, reload, and shutdown messages remain in Docker's continuous console log
and do not contaminate an individual job file.

`LOG_BACKUP_COUNT` keeps the newest 14 run groups by default. `LOG_MAX_MB`
remains an emergency size limit for one unusually verbose run; additional parts
use the same filename followed by a timestamp suffix and are retained or removed
with their parent run. Set `LOG_MAX_MB=0` when every run must remain in exactly
one file regardless of size. Dry runs continue to write only to Docker output so
that their no-persistent-write contract is preserved. A legacy
`/config/logs/metafusion.log` from an older release is left untouched and is no
longer appended.

At the first scheduled job after an interval expires, MetaFusion evaluates a
candidate. [Artwork policy](policies.md#artwork-update-policies) and quality
rules determine whether the file may actually be replaced.

Under `managed`, a run can also report `Adopted` artwork. This is a one-time
ownership verification for an existing file that exactly matches the selected
provider bytes; it does not rewrite the destination or alter filesystem ownership
or permissions. The first verification run can therefore take longer and use
more TMDb/network I/O than later runs.

Per-library timing overrides are documented in
[Configuration](configuration.md#per-library-overrides).

## Run history and schedule advice

The newest 500 completed jobs retain value-safe metrics in SQLite: duration,
throughput, library result counts, provider/cache/retry totals, cleanup outcome,
full-scan scope, slow rating keys, and final adaptive-concurrency state. Media
paths, metadata values, response bodies, tokens, and API keys are not retained.

Generate the configured report format without contacting Plex or providers:

```bash
metafusion --run-history
metafusion --schedule-advice
```

The first command includes individual retained jobs. The second is concise and
compares median and 95th-percentile runtime with the shortest configured
schedule interval. It warns about retained failures, schedule-capacity risk,
or a latest successful throughput more than 30% below the retained median.
This is local operational guidance, not an external metrics or notification
service.

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

TMDb, Fanart.tv, and Plex circuit failure counts and unexpired cooldowns are
retained across scheduled jobs. Connector identities are one-way hashes; URLs,
tokens, API keys, response bodies, and metadata values are not stored. An
expired cooldown starts cleanly, while a still-active cooldown prevents a
container restart or later schedule slot from immediately repeating an outage
storm.

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

Real cleanup moves eligible checksum-proven managed artwork to
`/config/quarantine/cleanup` instead of deleting it immediately. The default
14-day retention is controlled by `CLEANUP_QUARANTINE_DAYS`; expired matching
files are purged during later cleanup runs or with `--cleanup-purge`. See
[cleanup quarantine and restoration](lifecycle-management.md#cleanup-quarantine-and-restoration).

Cleanup uses the same outcome-oriented logging style as metadata and artwork.
At the normal INFO level, each affected title receives one consolidated
`[Cleanup] Inventory | title | outcome | reason` record. The final report
separates stale title/season/episode scope, cache and Kometa YAML entries,
managed artwork quarantined, protected artwork preserved, unchanged valid artwork,
and failures. Dry-run state/YAML removals are listed as `Would remove`, while
managed artwork is listed as `Would quarantine`.
Enable DEBUG only when exact cache keys, YAML season/episode entries, asset
paths, or preserved unmanaged files are needed. In Plex mode the report labels
cleanup as state-only by default. The advanced
`PLEX_CLEANUP_MANAGED_ARTWORK=True` opt-in is documented in
[Lifecycle management](lifecycle-management.md); Plex media files remain
untouched in every case.

## Persistent state and I/O

MetaFusion uses separate SQLite databases:

| Path | Purpose | Recovery behavior |
| --- | --- | --- |
| `/config/cache/meta_db.sqlite3` | Durable media state, retry queue, learned identities/history, review queue and overrides, item exceptions, cleanup candidates/history/quarantine, provider cooldowns, library rebinding history, discovered-library inventory, artwork ownership, per-library scans, schedules, and bounded run metrics | Back up with appdata while the container is stopped. Before a schema upgrade, two bounded `pre-v*` backups are retained. Do not treat as disposable. |
| `/config/cache/tmdb_cache.sqlite3` | Compressed successful TMDb responses | Disposable; it is storage-sized and pruned automatically. Corruption is quarantined with a timestamp and causes a clean rebuild. |
| `/config/cache/fanart_cache.sqlite3` | Compressed Fanart.tv artwork responses | Disposable; it shares the bounded provider-cache policy and can be rebuilt automatically. |

Rows are read and updated individually rather than loading and rewriting a
large JSON cache. Provider-cache expiry or pruning cannot remove durable scan or
artwork ownership state. After jobs, active databases run bounded optimization;
WAL files are truncated only after reaching the maintenance threshold.

### Explicit SQLite maintenance

Normal jobs already perform bounded automatic optimization. Use the standalone
maintenance CLI only for diagnosis, a deliberate backup, or an administrator
maintenance window:

```bash
metafusion --sqlite-maintenance check
metafusion --sqlite-maintenance backup --sqlite-target state
metafusion --sqlite-maintenance optimize
metafusion --sqlite-maintenance checkpoint --sqlite-target tmdb
metafusion --sqlite-maintenance vacuum --sqlite-target tmdb
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
Checksum-proven managed artwork removed by automated cleanup is retained under
`/config/quarantine/cleanup` until restored or its retention expires.

Shared reports and logs are:

```text
/config/logs/metafusion-YYYY-MM-DD_HH-MM-SS_microseconds.log
/config/reports/artwork-gaps-YYYYMMDD-HHMMSS.txt
/config/reports/asset-audit-YYYYMMDD-HHMMSS.txt
/config/reports/metadata-audit-YYYYMMDD-HHMMSS.txt
/config/reports/change-plan-YYYYMMDD-HHMMSS.txt
/config/reports/library-asset-audit-YYYYMMDD-HHMMSS.txt
/config/reports/mapping-diagnosis-YYYYMMDD-HHMMSS.txt
/config/reports/identity-inspection-YYYYMMDD-HHMMSS.txt
/config/reports/item-explanation-YYYYMMDD-HHMMSS.txt
/config/reports/compatibility-YYYYMMDD-HHMMSS.txt
/config/reports/destination-history-YYYYMMDD-HHMMSS.txt
/config/reports/unresolved-work-YYYYMMDD-HHMMSS.txt
/config/reports/adoption-audit-YYYYMMDD-HHMMSS.txt
/config/reports/upgrade-canary-YYYYMMDD-HHMMSSffffff.txt
/config/reports/provider-replay-capture-YYYYMMDD-HHMMSSffffff.txt
/config/reports/plex-metadata-YYYYMMDD-HHMMSS.txt
/config/reports/support-report-YYYYMMDD-HHMMSSffffff.txt
/config/reports/release-qualification-YYYYMMDD-HHMMSSffffff.txt
/config/reports/state-report-YYYYMMDD-HHMMSSffffff.txt
/config/reports/metafusion-dashboard-YYYYMMDD-HHMMSSffffff.html
/config/reports/metafusion-dashboard-latest.html
/config/reports/cleanup-history-YYYYMMDD-HHMMSSffffff.txt
/config/reports/cleanup-quarantine-YYYYMMDD-HHMMSSffffff.txt
/config/reports/run-history-YYYYMMDD-HHMMSSffffff.txt
/config/reports/schedule-advice-YYYYMMDD-HHMMSSffffff.txt
/config/reports/identity-review-YYYYMMDD-HHMMSSffffff.txt
/config/reports/output-management-YYYYMMDD-HHMMSSffffff.txt
/config/reports/library-rebinding-YYYYMMDD-HHMMSSffffff.txt
/config/reports/configuration-impact-YYYYMMDD-HHMMSSffffff.txt
/config/reports/plex-artwork-verification-YYYYMMDD-HHMMSSffffff.txt
```

Artwork-gap reports identify missing/rejected artwork and identity failures.
A successful non-dry job always writes one, including when the open count is
zero. It combines current-run observations with open SQLite-ledger records and
pre-ledger missing evidence already recorded in media state. Current,
carried-forward, not-due, and recently resolved counts are logged and retained;
the report also records the last check, next recheck, and recorded destination
state when known. This snapshot does not add provider calls or a physical
artwork scan. It applies to movie/show posters and backgrounds plus individual
season posters in Kometa and Plex modes. Provider decisions and selected source
are included in read-only artwork audits.
Asset-audit reports include the selected candidate's language, dimensions,
raw vote/likes score, supporting count and confidence, provider-score contribution,
TMDb canonical status and selection stage, ownership status, existing
dimensions, score components, the top
rejected candidates, and the action a real run would consider. They omit
filesystem paths and do not prove that a later download will succeed.
Conventional diagnostic reports use `REPORT_FORMAT=both` by default. Select
`text` to retain only human-readable `.txt` files or `json` to retain only
machine-readable `.json` files. Retention treats the selected representation
as one logical report. The offline HTML dashboard and sanitized provider replay
remain paired with JSON because their structured files are part of the command's
core output rather than optional companions.
Automatic HTML-dashboard refresh is disabled by default. Set
`output.dashboard_enabled: true` or `DASHBOARD_ENABLED=True` to refresh it
after successful non-dry runs; `--dashboard-report` always works on demand.

Post-write artwork verification always runs. `ADOPTION_AUDIT=anomalies` writes
an adoption report only for checksum mismatches, missing destinations, or
verification failures; `all` also reports successful verified writes and
adoptions; `off` suppresses only this report. An abnormal verification is also
added to the persistent artwork-gap/unresolved-work ledger regardless of this
setting. Adoption audits remain separate from artwork-gap reports because the
former proves what happened after a write while the latter tracks unresolved
provider, identity, destination, and verification work.

Identity-inspection,
destination-history, unresolved-work, adoption-audit, and item-explanation
reports can contain media titles or computed/actual paths and must be reviewed
before sharing. Destination reconciliation removes an old artwork file only
under `managed` policy when the new destination matches current managed state,
the old file is inside a configured managed root, and its checksum still proves
MetaFusion ownership. An old path that is still the current destination of
another managed cache record is retained for that owner.
Item-level JSON records consistently include nullable `plex_rating_key`,
`tmdb_id`, `imdb_id`, `tvdb_id`, `edition`, `season_number`, and
`identity_source` fields so artwork, metadata, cleanup follow-up, and adoption
results can be correlated without title-only matching.
Modified, unproven, symlinked, and out-of-scope files are preserved. Plex
metadata reports identify fields and outcomes. `REPORT_RETENTION` retains the
newest logical reports for each report type; its default is `10`.

At the default `LOG_LEVEL=INFO`, each changed item uses the same compact
`[Component] Library | Title | Outcome | Source | Target` structure. Metadata
uses `Source: TMDb` with `Target: Kometa YAML` or `Target: Plex`; artwork names
TMDb, Fanart.tv, or Plex and targets Kometa assets or Plex local media.
`Field coverage` describes populated expected fields; it does not decide the
update outcome. A 100% record that genuinely changed remains `INFO` and names
the changed field categories. An unchanged 100% record remains `DEBUG`; every
unchanged record below 100% is promoted to `INFO` with its missing percentage
so incomplete movie and TV records are not hidden during a normal run.

Routine accepted identities, successful mappings, unchanged/preserved checks,
provider requests, cache details, and internal API batches remain at `DEBUG`.
Missing or deferred outcomes are warnings and failed outcomes are errors.
Season-poster warnings name the missing Plex season and summarize the provider
attempts. One final summary combines only the libraries processed by that run.
Metadata and every enabled artwork lane have separate schedule and result
lines. Schedule states are mutually exclusive and always reconcile with the
reported destination count:

| State | Metadata meaning | Artwork meaning |
| --- | --- | --- |
| `Required` | A new Plex title has no prior durable processing state. | The artwork destination has never completed an artwork check, including a newly discovered title or season. |
| `Due` | A pending metadata retry, periodic Plex metadata recheck, TMDb change notification, or deferred retry selected the title. | The configured adaptive artwork-refresh cadence has expired. |
| `Forced` | A current title was selected by a full scan, targeted request, configuration change, Plex item change, or TV child-inventory change. | A current destination was selected by one of those non-cadence triggers. |
| `Not due` | The title did not require metadata work and stayed outside metadata processing. | The destination remained current and outside artwork processing. |

Metadata destinations count titles. Poster and background destinations also
count titles, while season-poster destinations count individual known Plex
seasons. A season-poster schedule also reports `Season inventories unavailable`,
which counts TV titles for which neither the current Plex object nor durable
state provides a season count. Those titles are not falsely reported as having
zero season destinations; their unknown season destinations are excluded from
the numeric destination total until Plex supplies an inventory. `Not selected`
on the incremental inventory line remains an item-level total across all work;
it is not a substitute for the metadata or artwork lane-specific `Not due`
figures.

Metadata result lines identify the target (`Kometa YAML` or `Plex`) and report
created/updated or changed, unchanged, API batches where applicable, and failed
items. Metadata coverage is separate: it reports how many processed records met
the configured field-completeness threshold. Consequently, a metadata record
may be unchanged but below the threshold, or changed while already at 100%
coverage. Item-level logging is action-driven rather than completeness-driven:

- Created and updated metadata remains visible at `INFO`; a simultaneous
  coverage regression raises that outcome to `WARNING`.
- A first observation below 100% and a later coverage improvement are logged at
  `INFO`, even if the metadata output itself is unchanged.
- A decrease from the last successful coverage observation is logged once as a
  `WARNING` with the previous and current percentages.
- Stable unchanged metadata stays at `DEBUG` regardless of whether its coverage
  is 100%, 75%, or another value. TMDb may permanently omit optional fields, so
  incompleteness by itself is not treated as a recurring operational problem.
- Provider failures, rejected identities, and output failures retain their
  existing warning or error levels.

The last successful field-coverage observation is stored in `meta_db.sqlite3`.
Dry runs and failed metadata actions do not replace it. A corrected TMDb
identity starts a new baseline rather than being compared with the old title's
coverage. A configuration fingerprint change also starts a new baseline because
the enabled metadata field surface may have changed. The first run after
installing this behavior has no historical coverage baseline, so each evaluated
incomplete item can appear at `INFO` once; subsequent unchanged observations
move to `DEBUG`. Final library and overall summaries retain evaluated, threshold,
improvement, regression, and first-incomplete counts, so `INFO` remains concise
without hiding coverage state.

A Kometa library summary therefore reads as three separate questions rather
than treating `100%` coverage as proof that a write occurred:

```text
[Summary] Movies | Metadata schedule | Destinations: 2,000 | Required: 12 | Due: 31 | Forced: 7 | Not due: 1,950
[Summary] Movies | Metadata result | Target: Kometa YAML | Created: 8 | Updated: 6 | Unchanged: 34 | Failed: 2
[Summary] Movies | Metadata coverage | Evaluated: 48 | Meets threshold: 42 (87.5%) | Below threshold: 6 (12.5%) | Improved: 2 | Regressed: 0 | First incomplete: 1
```

The schedule line accounts for the complete selected-library inventory. The
result and coverage lines account only for titles evaluated during this run;
their totals can therefore be much smaller than `Destinations`. In Plex mode,
the same schedule is used, while the result line changes its target and reports
field writes and API batches instead of YAML creation/update counts.

At runtime, MetaFusion verifies that `Required + Due + Forced + Not due` equals
`Destinations` for every enabled metadata and artwork lane in every processed
library. A mismatch does not hide or rewrite the reported figures: it emits a
`[Diagnostics] Schedule reconciliation` warning with the library, lane,
destination total, accounted total, and difference. This is a regression guard
for reporting logic rather than a media-processing failure. Unknown season
inventories are reported separately because their destination count cannot be
calculated safely.

Season-poster item outcomes use one compact line per show. Meaningful actions
name up to eight affected seasons—for example `Downloaded: 1 [S03]` or
`Missing: 2 [S10, S11]`—while `Unchanged` and `Not due` remain counts. If every
season for a show is not due, no item-level line is emitted; the schedule
summary still counts all of those destinations. This distinction means `Not
due` is a scheduling decision, not proof that providers were queried or that a
new candidate does not exist.

Artwork result lines report evaluated, downloaded, upgraded, adopted,
unchanged, preserved, missing, deferred, failed, and applicable policy
outcomes. Enabled TV libraries always receive season-poster schedule and result
lines, including zero-evaluated lines. The
persistent artwork-gap summary is separate so known not-due gaps remain visible.
The run summary also reconciles artwork destinations evaluated during that run
as present or absent and separates current installed sources from write sources
for the run. The older standalone per-library summary
blocks are intentionally omitted. Plex locked-field, conflict, and write-limit
totals are warnings; the corresponding `plex-metadata-*.txt` report retains
field-level audit details.

Startup records use human-readable `Label: value` fields and are not padded or
wrapped to a fixed width. Lightweight dividers identify startup, system,
configuration, processing, and final-summary sections without ANSI control
codes. `[Startup]` identifies the build and run, `[System]` reports the runtime
resources, `[Connection]` reports Plex/TMDb validation, and `[Inventory]`
reports available, selected, and skipped libraries. The effective
feature profile is one `[Configuration] Run profile` record at `INFO`; individual
feature toggles and a successfully loaded configuration file remain at `DEBUG`.
One successful connector result is `INFO`, retryable attempts are `WARNING`, and
terminal failures are `ERROR` at the run boundary.

Every final-summary record begins with `[Summary]` and one logical record is
emitted by one logger call, so long entries are not split into continuation
lines. Per-library storage separates posters, backgrounds, season posters, and
Kometa YAML. Plex-mode metadata is explicitly labelled server-managed because
its Plex database use cannot be measured from the container. The scope says
`full inventory` or `processed items`; MetaFusion stats known generated output
files and does not read artwork contents or recursively scan the media tree.
Runtime storage separately reports the durable state database, TMDb cache,
Fanart.tv cache, logs, reports, and cleanup quarantine. Filesystem records show total volume used,
free, capacity, and free percentage; they do not claim that all used bytes
belong to MetaFusion. TMDb and Fanart.tv cache entry statistics use the same
`[Cache] Provider: ...` format at `DEBUG`.

Direct Plex metadata progress is automatic and not configurable. It uses the
`[Metadata] Plex progress` component. Small
libraries report every 5 items or 10%, medium libraries every 25 items or 5%,
and large libraries every 100 items or 5%, whichever interval is larger. A
30-second minimum gap prevents log flooding, a 60-second heartbeat covers slow
shows, and start/final progress is always logged. TV progress counts top-level
shows while their seasons and episodes remain part of each show operation.

The field-level Plex metadata report emits one detailed `[Metadata] Plex report`
record at `DEBUG`. It is intentionally not repeated at `INFO`; the target-aware
metadata result in the final summary is the canonical operator summary. Plex
locked-field, conflict, and write-limit protection remains at `WARNING`, with a
path to the retained diagnostic report.

Every completed job also logs one local performance summary: total, Plex
inventory and library-processing time; items per minute; TMDb requests,
cache hits/misses, retries and rate-limit waits; Fanart.tv activity when the
fallback was used; and the five slowest items by library plus Plex rating key.
Run timing and provider activity use shared `Label: value` fields. Slow-item
observations remain available at `DEBUG` without flooding normal `INFO` logs.
Recovery, provider-circuit, heartbeat, job-history, and disk-pressure warnings
use the same fields while retaining their existing severity. The performance
records intentionally omit media paths and metadata values. Use them to compare
full and incremental runs without adding a metrics service.

Before normal writes, MetaFusion validates `/config`, Kometa output, and any
configured Plex mapping destinations. `MIN_FREE_SPACE_MB` and an automatic
1%-of-volume floor (bounded from 256 MiB to 2 GiB) are checked at each artwork
destination before a download. MetaFusion first prunes disposable provider-cache
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

When opening a GitHub issue, use the
[issue chooser](https://github.com/ray-cys/metafusion/issues/new/choose) for a
general bug, Docker/Unraid, artwork/identity, Kometa output, Plex metadata,
runtime/cleanup, or feature request. Attach only the redacted report and log
section relevant to the selected form; explain when requested evidence is not
available. Never attach `config.yml`, tokens, API keys, Docker inspection
output, or unredacted host paths.
