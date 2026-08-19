# Lifecycle management and recovery

These tools handle exceptional operator work without weakening MetaFusion's
normal processing safeguards. Commands either read SQLite/live Plex without
changing them, or require an exact target and hold the normal single-job lock.
Every retained text report has a same-name JSON companion under
`/config/reports`.

## Safe targeted output management

Select exactly one recorded item with `--library` and `--rating-key`. Preview
first:

```bash
python metafusion.py --output-action preview \
  --library Movies --rating-key 12345 --output-type all
```

`remove` deletes only a regular, non-symlink artwork file inside a configured
managed root whose current SHA-256 checksum still equals the ownership record.
`forget` keeps the artwork file but removes MetaFusion's ownership claim, so a
later managed run treats it as user-owned. `rebuild` performs the same guarded
removal and immediately runs targeted full processing for the selected output.
No action targets video or audio media.

Kometa metadata is different: an entry can contain both generated and manually
maintained fields, so `remove` or `rebuild` requires
`--acknowledge-metadata-loss`. `forget` cannot safely split ownership inside a
YAML entry and is refused. Plex mode never owns Kometa YAML.

```bash
python metafusion.py --output-action remove --output-type poster \
  --library Movies --rating-key 12345
python metafusion.py --output-action rebuild --output-type season \
  --season-number 2 --library "TV Shows" --rating-key 67890
```

Successful manual removals or forgets are labelled `manual` in cleanup history
with relevant Plex/provider IDs, destination, checksum, action, and reason.

## Cleanup confirmation, grace, and history

Cleanup still requires a successful authoritative full inventory. A missing
item is recorded as pending, not immediately deleted. Defaults require two
separate full scans and 48 hours from first detection. A returning item cancels
the candidate:

```text
CLEANUP_CONFIRMATION_SCANS=2
CLEANUP_GRACE_HOURS=48
PLEX_CLEANUP_MANAGED_ARTWORK=False
```

Kometa cleanup can remove eligible generated YAML/state and checksum-proven
artwork. Plex cleanup remains state-only unless the final setting is explicitly
enabled; even then, only exact checksum-proven local artwork is eligible. It
never removes Plex database records or media files.

Generate a human and JSON audit trail without contacting Plex or disk outputs:

```bash
python metafusion.py --cleanup-history-report
python metafusion.py --cleanup-history-report --history-source automated
python metafusion.py --cleanup-history-report --history-source manual \
  --library Movies --rating-key 12345
```

History says whether MetaFusion acted automatically during full-scan cleanup
or an operator acted through targeted output management. Pending candidates
show their confirmation count and eligibility time.

## Persistent item exceptions

Exceptions skip one output lane for one durable Plex identity across future
runs. They do not delete current output or change global configuration.

```bash
python metafusion.py --exception-action add --exception-output poster \
  --library Movies --rating-key 12345 --reason "Keep commissioned poster"
python metafusion.py --exception-action add --exception-output season \
  --season-number 0 --library "TV Shows" --rating-key 67890
python metafusion.py --exception-action add --exception-output cleanup \
  --library Movies --rating-key 12345 --reason "Retain during migration"
python metafusion.py --exception-action list --library Movies
python metafusion.py --exception-action remove --exception-output poster \
  --library Movies --rating-key 12345
```

Scopes are `metadata`, `plex_metadata`, `poster`, `background`, `season`,
`cleanup`, or `all`. A season scope requires `--season-number`; use `0` for
Specials.

## Identity overrides and review queue

Normal automatic identity validation remains the default. Rejected or missing
identities are persisted in a review queue and can be reported without provider
access:

```bash
python metafusion.py --identity-review-queue
```

After independently confirming the correct TMDb media type and ID, bind one
recorded Plex item explicitly:

```bash
python metafusion.py --identity-override-action set --tmdb-id 550 \
  --library Movies --rating-key 12345 --reason "Verified against TMDb page"
python metafusion.py --identity-override-action list --library Movies
python metafusion.py --identity-override-action remove \
  --library Movies --rating-key 12345
```

An override is authoritative for that Plex server/library UUID/rating key and
prevents title/year aliases or stale learned bindings from selecting another
TMDb ID. The selected TMDb endpoint must still exist and match the media type.
Removing the override returns the item to normal validation.

## Library migration and rebinding

Use rebinding after Plex creates new library/rating-key identities for the same
media. Disable cleanup, scan the destination library once, and create a plan:

```bash
python metafusion.py --library-rebind plan \
  --from-library "Old Movies" --to-library "Movies"
python metafusion.py --library-rebind apply \
  --from-library "Old Movies" --to-library "Movies"
```

Matching requires one unique media type, TMDb ID, and edition on each side.
Apply transfers only non-conflicting artwork ownership, destination history,
exceptions, identity overrides, and unresolved-review references. It does not
transfer direct Plex metadata ownership or the old Plex GUID fingerprint; the
new Plex identity must establish those normally. Ambiguous and unmatched rows
remain unchanged and appear in the report.

## Disaster-recovery bundle

Create a consistent online SQLite backup plus redacted configuration, template,
Kometa YAML, ownership manifest, hashes, build data, and a manifest:

```bash
python metafusion.py --recovery-bundle
python metafusion.py --verify-recovery \
  /config/backups/metafusion-recovery-YYYYMMDD-HHMMSSffffff.tar.gz
```

The bundle excludes artwork bytes and disposable TMDb/Fanart.tv caches.
Verification checks archive paths and links, every file hash, the manifest, and
SQLite `quick_check`. Restore is deliberately manual: stop the container,
preserve the current files, extract and inspect the verified bundle, restore
`meta_db.sqlite3` and any required Kometa YAML with correct ownership, then run
SQLite maintenance `check` before restarting.

## Human-readable SQLite state report

The report opens SQLite query-only and does not initialize or migrate it, touch
timestamps, contact Plex/providers, or inspect artwork/YAML:

```bash
python metafusion.py --state-report
python metafusion.py --state-report --state-section problems
python metafusion.py --state-report --include-state-items --library Movies
python metafusion.py --state-report --rating-key 12345
```

It covers database health and size, table counts, library/full-scan state,
recent jobs, ownership, retries, exceptions, overrides, identity reviews,
cleanup candidates/history, and rebinding history. Recorded state is historical
evidence; use live diagnostics before concluding Plex or a file currently
matches it.

## Plex artwork adoption verification

This command contacts Plex but is read-only. It compares Plex's live selected
image with the checksum-proven local managed image using an exact content hash
or a narrow perceptual threshold:

```bash
python metafusion.py --plex-artwork-verify --library Movies
python metafusion.py --plex-artwork-verify \
  --library "TV Shows" --rating-key 67890
```

Statuses distinguish selected, not selected, local missing or modified,
unmanaged, unavailable Plex image, and unverifiable content. It does not ask
Plex to refresh, upload, select, or lock artwork.

## Configuration impact comparison

Compare the current effective configuration with a proposed YAML file before
changing the deployment:

```bash
python metafusion.py --config-impact /config/proposed-config.yml
```

The proposed file is evaluated over MetaFusion defaults without importing the
current process environment, so differences are visible instead of masked by
current environment variables. Secrets are redacted. The report classifies
destination, cleanup, and replacement changes as high risk, estimates whether
saved items may be selected again, and directs cleanup-scope changes to a live
`--plan`. It never applies the proposed file.
