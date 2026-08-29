# Policy behavior and safety rules

MetaFusion separates artwork files, direct Plex metadata, Kometa tags, and
cleanup into independent controls. Selecting one policy never grants authority
to another subsystem.

| Control | Applies to | Default |
| --- | --- | --- |
| `ASSET_UPDATE_POLICY` | Poster, background, and season-poster files | `managed` |
| `PLEX_METADATA_POLICY` | Supported fields written directly through the Plex API | `fill_missing` |
| `KOMETA_TAG_POLICY` | Supported tag fields in generated Kometa YAML | `append` |
| `RUN_CLEANUP` | Guarded reconciliation of stale generated output/state | `False` |

## Artwork update policies

Artwork source selection is separate from the update policy. MetaFusion tries
TMDb first, then Fanart.tv through its bundled project key, then the artwork
exposed by Plex. If none meets the hard dimension requirements, the highest-scoring
TMDb/Fanart.tv reserve is used as `best available`; otherwise the destination
is preserved and the item is reported. This order is identical in Kometa and
Plex modes. See [Artwork providers](artwork-providers.md) for complete failure,
privacy, and attribution behavior.

The artwork policy applies in both modes:

- Kometa mode destinations are below `/kometa/assets`.
- Plex mode destinations are Plex-compatible local artwork files beside the
  mapped media.
- MetaFusion creates movie/show posters, movie/show backgrounds, and season
  posters. It does not create episode artwork.

### Existing-file behavior

Every policy can create missing artwork. The policy gate is evaluated when a
destination already exists:

| Existing destination | `fill_missing` | `managed` | `overwrite` |
| --- | --- | --- | --- |
| MetaFusion wrote it and it is unchanged | Preserve | Eligible for upgrade | Eligible for upgrade |
| MetaFusion wrote it and it was later edited/replaced | Preserve | Preserve | Eligible for replacement |
| Another program or user created it | Preserve | Preserve | Eligible for replacement |
| Ownership record has no checksum or points to another path | Preserve | Preserve | Eligible for replacement |

#### `fill_missing`

If any file exists at the destination, MetaFusion leaves it untouched. It does
not matter who created the file, whether the content is valid, or whether TMDb
has a better candidate. This is the appropriate policy when artwork is managed
manually or by another application.

Because ownership hashing and replacement downloads are unnecessary for an
existing destination, this policy also produces the least artwork-file I/O.
The title can still be inventoried or queried when another task makes it due.

#### `managed`

This is the recommended default. When MetaFusion creates or replaces artwork,
it records the destination path, SHA-256 checksum, selected provider source,
score, and upgrade timestamp in durable state.

An existing file is eligible only when:

1. MetaFusion has an ownership record for that item and artwork type.
2. The recorded path matches the current destination.
3. The record contains a checksum.
4. The current file's SHA-256 checksum still matches that record.

Changing even one byte makes the file user-modified. MetaFusion then preserves
it rather than claiming ownership of the new content.

When an existing file has no ownership record, or an older record has a path
but no checksum, MetaFusion performs a safe ownership check. It downloads the
currently selected provider image to a temporary file and compares SHA-256 values:

- An exact byte match is adopted into durable ownership state.
- The destination file is not replaced, opened for writing, renamed, chmodded,
  or chowned, so its inode, timestamps, owner, and permissions stay unchanged.
- A different image, symbolic link, download failure, or checksum failure is
  preserved without an ownership claim.

The first managed run after this correction can perform extra provider downloads
for unverified files. Later scheduled runs use the recorded check/ownership
state. Artwork that was already orphaned before a live Plex title could verify
it cannot be auto-adopted and remains a manual cleanup decision.

#### `overwrite`

This removes the ownership requirement, allowing a manual or third-party file
to be considered for replacement. Artwork overwrite does not require
`PLEX_METADATA_ALLOW_OVERWRITE`; that acknowledgement applies only to direct
Plex metadata.

`overwrite` means eligible for replacement, not unconditional rewriting. It
does not bypass source, quality, path, or collision checks.

### Canonical selection, quality, and refresh behavior

MetaFusion first looks for the canonical `poster_path` or `backdrop_path`
selected by TMDb's localized detail response. Season posters use the season
detail `poster_path`. When that canonical candidate meets the configured
minimum dimensions and later passes download decoding, format, aspect, blank
image, and declared-dimension validation, it is preferred without attempting
to reproduce TMDb's internal ordering from vote fields. This applies to movie,
show, background, and season artwork in both Kometa and Plex modes.

The deterministic 0-100 score is used only when TMDb has no usable canonical
candidate and MetaFusion must rank candidates within a fallback provider stage:

| Component | Weight | Meaning |
| --- | ---: | --- |
| Resolution | 45 | Image area relative to the configured preferred dimensions, capped at full credit. |
| Provider score | 35 | TMDb image vote average or the Fanart.tv likes proxy, capped at 10. Vote/like count is retained only as supporting confidence and a final tie-breaker. Plex candidates have no pre-download score. |
| Aspect ratio | 10 | Closeness to 2:3 for posters/season posters or 16:9 for backgrounds. |
| Language | 10 | Preferred language gets 10, configured fallback 7, untagged 4, and another language 0. |
| Cached content quality | up to 8 | A bounded sharpness bonus from a previously validated download; the final score remains capped at 100. |

Fallback provider contribution and configured preferred, relaxed, and upgrade
thresholds use the raw 0-10 provider score. Candidate ordering uses count only
after normalized quality and raw average, making it a final tie-breaker for
otherwise equivalent fallback candidates.
Fanart.tv likes remain the primary score within the Fanart.tv stage; their count
is the equivalent supporting tie-break signal. The two
providers remain separate fallback stages rather than a global popularity
contest. The highest score wins within the current provider stage; raw provider
score, supporting count, pixel area, and source path provide stable tie breakers.
A cached blank-image result makes
the candidate ineligible, while a perceptual hash identifies visually duplicate
candidates in selection explanations. A perceptual hash is not treated as a
quality measurement. Scoring does not allow a lower-priority language to jump
ahead of an available preferred-language tier or bypass the normal provider and
minimum-dimension stages. A deliberately selected `best available` fallback can
remain below the configured minimum, but its downloaded dimensions, format,
aspect ratio and provider declaration are still validated before installation.
The chosen candidate and component scores appear in explicit asset, library,
and change-plan audits. Content signals are used only after that provider/source
has been downloaded and validated on an earlier attempt or run; MetaFusion does
not download every candidate merely to score it.

After a policy permits an existing destination to be considered, the artwork
upgrade engine follows these rules:

- Missing artwork installs a valid canonical image immediately.
- A different canonical TMDb source for an existing managed destination is
  recorded as pending. It must be returned by two consecutive provider checks
  before replacement. A built-in 24-hour follow-up makes confirmation
  independent of a longer artwork interval. If the canonical path changes
  again or is no longer usable, confirmation resets. An interval of `0`
  continues to disable this timed follow-up.
- Once confirmed, a valid canonical replacement may be applied even when its
  vote average, vote count, resolution, or combined score is lower than the
  previous image. It must still meet absolute configured minimums and every
  download/content safety check.
- An unchanged canonical source is preserved without rescoring. Its bounded
  byte verification can still detect the exceptional case where a provider
  changes content behind the same identifier.
- The same recorded provider source normally uses the managed-file shortcut. A
  bounded byte-level verification re-download occurs automatically after the
  longer of 90 days or three times the configured artwork interval, capped at
  365 days. This detects providers silently changing bytes behind an unchanged
  source identifier without downloading every managed image on normal runs.
- Byte-identical downloaded artwork is skipped.
- For noncanonical fallback artwork, before the timed refresh age a replacement
  must improve the normalized quality score by at least one point, preserve
  overall quality while gaining at least 10% pixel area, or provide a better
  provider rating
  without reducing dimensions or aspect suitability.
- Crossing a configured vote threshold identifies the reason for an approved
  replacement; it does not bypass the quality guard. One larger dimension alone
  and a lower within-threshold score are no longer sufficient.
- Once stale, a noncanonical candidate must be no worse in normalized score,
  provider rating, width, height, aspect suitability, and
  image validation. Otherwise the existing artwork is preserved.
- TMDb vote count, Fanart.tv likes/count, provider score, language, dimensions,
  content signals, and the resulting comparison are retained for subsequent
  managed-artwork decisions.
- A rejected or failed candidate leaves the existing file intact.

If TMDb has no usable canonical candidate, the established provider order and
fallback scoring remain TMDb candidate pool, Fanart.tv, Plex, and best
available. Manual or externally modified artwork remains protected by
`managed`; canonical authority and the quality guard do not weaken ownership
enforcement.

Artwork age and candidate observations are saved in SQLite, not derived from
the file's mtime. `MOVIE_IMAGE_UPGRADE_DAYS`, `SERIES_IMAGE_UPGRADE_DAYS`, and
`SEASON_IMAGE_UPGRADE_DAYS` are adaptive base intervals. Missing candidates
retry sooner; repeatedly unchanged candidates back off to 180 days; a longer
explicit base remains respected. A changed candidate resets the backoff. A
pending canonical change receives its fixed follow-up check before the normal
adaptive interval.
Setting an interval to `0` disables
timed rechecks and same-source verification for that type. Changed items and
full scans can still install missing or objectively better artwork. Successful
same-source verification timestamps are stored per poster, background, and
individual season in SQLite; no additional setting or environment variable is
required.

### Canonical ownership and collisions

Destination protection applies under every artwork policy, including
`overwrite`.

- Different Plex identities cannot concurrently claim one output path.
- Editions stored in a shared physical directory can share artwork only when
  they resolve to the same TMDb title and exact selected provider source image.
- Verified shared artwork is downloaded once and its checksum is recorded for
  each qualifying identity.
- Conflicting TMDb identities or different selected images are rejected and
  reported rather than silently choosing a winner.

Use different physical folders if editions require different local artwork.

### Read-only asset audit

Run `metafusion --asset-audit` before changing artwork policy or
after a large path/library migration. The command forces a full artwork-only
selection while dry-run protection remains active. It reports:

- missing local destinations that a real run would download;
- managed, modified, unmanaged, adoptable, and shared ownership states;
- existing dimensions and candidates with higher dimensions;
- canonical destination collisions; and
- missing artwork, TMDb failures, and rejected identities.

The audit does not download candidates solely to compare exact bytes, so
`would_verify_for_adoption` remains a prediction until a real managed run. It
does not modify artwork, YAML, ownership, incremental state, or SQLite caches.
The report under `/config/reports` is the only intentional persistent write.

## Direct Plex metadata policies

These policies have no effect unless:

```text
RUN_MODE=plex
PLEX_METADATA_UPDATES=True
```

They change selected fields in Plex's live database through the Plex API. They
do not generate Kometa YAML and do not control local artwork.

### Supported scope

| Plex item | Basic fields and tags | Enhanced additions |
| --- | --- | --- |
| Movie | Original title, release date, content rating, studio, tagline, summary, countries, genres | Directors, writers, producers |
| Show | Original title, first-air date, content rating, network/studio, tagline, summary, countries, genres | None |
| Season | Missing title and summary | None |
| Episode | Missing title, summary, and air date | Directors and writers |

`RUN_BASIC` controls the basic fields. `RUN_ENHANCED` requires basic processing
and enables only the listed crew additions. `PLEX_METADATA_FIELDS` can further
limit the supported fields.

MetaFusion intentionally leaves external matching IDs, cast and character
roles, audience/critic ratings, labels, collections, playback data, extras,
recommendations, and provider-specific artwork choices to Plex's provider or
the user.

### `fill_missing`

- Fills empty supported scalar fields.
- Appends missing supported tags such as genres or countries.
- Does not replace a non-empty scalar value.
- Does not remove tags.
- Does not cross an existing Plex field lock.

If Plex and TMDb already contain the same information, no write or new lock is
performed.

### `managed`

Managed starts with `fill_missing` behavior. It can later update or remove only
values that remain equal to MetaFusion's recorded last write. If the current
Plex value differs, MetaFusion records a manual conflict and leaves it alone.

This enables ongoing updates without treating all Plex/provider content as
MetaFusion-owned.

### `overwrite`

Overwrite makes selected supported fields match TMDb and can replace existing
values or remove values/tags not present in the desired set. It requires:

```text
PLEX_METADATA_POLICY=overwrite
PLEX_METADATA_ALLOW_OVERWRITE=True
```

Limit initial use with `PLEX_METADATA_FIELDS`, a low
`PLEX_METADATA_MAX_WRITES_PER_RUN`, and a targeted dry run. This policy is the
most likely to replace provider or manually curated metadata.

### Locks

- `PLEX_METADATA_LOCK_WRITES=True` locks scalar fields MetaFusion writes.
- `PLEX_METADATA_LOCK_MERGED_TAGS=True` locks the entire merged tag field,
  including tags originally supplied by Plex or the user.
- `fill_missing` and `managed` skip pre-existing locks unless the ownership
  ledger proves MetaFusion created that lock.
- An unchanged value causes no API write and no new lock.
- Disabling metadata updates later does not unlock or restore earlier writes.

MetaFusion does not request a Plex metadata refresh after writing. A refresh
could immediately let the online provider replace unlocked fields.

### Safe initial rollout

Use an owner/admin Plex token and back up the Plex database. Start with one or
two representative items:

```text
RUN_MODE=plex
PLEX_METADATA_UPDATES=True
PLEX_METADATA_POLICY=fill_missing
PLEX_METADATA_MAX_WRITES_PER_RUN=25
DRY_RUN=True
```

Review `/config/reports/plex-metadata-*.txt`, then disable dry-run and increase
the cap gradually. Reports contain field names and outcomes, not metadata
summaries, tokens, or API keys.

Targeted recovery commands require at least one rating key and refuse a
library-wide operation:

```bash
# Preview restoring only values still equal to MetaFusion's last write
metafusion --plex-metadata-restore \
  --library Movies --rating-key 12345 --dry_run

# Restore recorded prior values and lock states
metafusion --plex-metadata-restore \
  --library Movies --rating-key 12345

# Keep values but remove only locks recorded as MetaFusion-created
metafusion --plex-metadata-unlock \
  --library Movies --rating-key 12345
```

If the current value differs from MetaFusion's ownership record, recovery
treats it as a manual change and preserves it.

## Kometa tag policy

`KOMETA_TAG_POLICY` applies only to supported tag fields in generated Kometa
YAML:

- `append` retains Plex/user values and adds missing supported TMDb tags.
- `sync` makes supported generated tag fields match TMDb.

It does not control scalar YAML fields, direct Plex metadata, or artwork.
Unknown/manual YAML fields are preserved. When TMDb has no replacement value,
MetaFusion retains an existing non-empty generated value rather than erasing
it due to a temporary source gap.

## Cleanup and deletion safety

Cleanup is opt-in and defaults to disabled. Before enabling it:

1. Confirm every `PLEX_LIBRARIES` name exactly matches Plex.
2. Back up existing Kometa metadata, assets, and `/config` state.
3. Run a complete reconciliation with `DRY_RUN=True`.
4. Inspect every proposed removal before using `DRY_RUN=False`.

When `PLEX_LIBRARIES=auto`, MetaFusion records the discovered library UUIDs. If
a previously discovered library is absent from a later inventory, it is
reported and excluded from cleanup rather than interpreted as deletion. Exact
library overrides remain available when an operator intentionally wants a
smaller scope.

For a one-time complete cleanup preview:

```text
INCREMENTAL=False
RUN_CLEANUP=True
DRY_RUN=True
```

Restore normal incremental settings after the test.

Cleanup starts only after every configured library of the relevant media type
completes successfully. It is skipped when there is a missing library,
incomplete season/episode inventory, item failure, invalid YAML, write failure,
incremental-only run, or targeted run.

An absence first becomes a durable pending candidate. By default, deletion
requires two distinct authoritative full scans and a 48-hour grace period
(`CLEANUP_CONFIRMATION_SCANS=2`, `CLEANUP_GRACE_HOURS=48`). Repeated checks in
one job count once. If the item returns before eligibility, the candidate is
cancelled. Both cancellation and completed changes are retained in SQLite
cleanup history with Plex/TMDb/IMDb/TVDB identities where available.
Checksum-proven managed artwork is moved to a 14-day recoverable quarantine,
not permanently deleted at cleanup time. The copy is verified before the
active destination is removed.

### Kometa mode

- Removes generated movie/show entries that no longer exist in Plex.
- Removes stale generated season and episode entries after a complete inventory.
- Retains Season 0/Specials while they remain in Plex.
- Quarantines artwork only when its path and checksum still match MetaFusion's
  ownership record.
- Evaluates a shared canonical artwork destination once across all recorded
  edition owners and accepts a checksum match from any canonical owner.
- Preserves modified, unmanaged, symbolic-link, legacy-without-checksum, and
  unverifiable artwork.
- Does not clean an artwork type whose generation feature is disabled.

### Plex mode

Cleanup removes stale MetaFusion state records only. It does not delete local
artwork, Kometa YAML/assets, or any Plex media file.

`PLEX_CLEANUP_MANAGED_ARTWORK=True` is a separate, advanced opt-in. It can
quarantine only an exact local artwork destination recorded for an eligible stale
item when the current checksum still proves MetaFusion ownership. Modified,
unmanaged, checksum-less, or symbolic-link files remain protected. Video and
audio files are never candidates.

The final cleanup summary explicitly labels this as state-only and reports
cache records separately from Kometa YAML and artwork outcomes. A failed
cleanup retains confirmed pre-failure counts in the final report instead of
silently losing the cleanup summary.

Use `--cleanup-quarantine-report` and `--cleanup-restore HISTORY_ID` during the
retention window. See [Lifecycle management](lifecycle-management.md#cleanup-quarantine-and-restoration)
for restoration and expiry rules.

The cleanup checksum rule is independent of `ASSET_UPDATE_POLICY`. Even when
artwork updates use `overwrite`, cleanup cannot delete an unverified manual
file.

## TMDb identity and episode policies

MetaFusion prefers usable Plex external IDs and validates the returned TMDb
title/type/year before accepting the identity. Independent Plex IMDb/TVDB IDs
are compared with the selected TMDb record: a conflict triggers recovery to a
matching TMDb record or safe rejection. A year embedded at the end of a
Plex title can resolve a conflicting Plex year when the embedded year matches
the TMDb release year and the result came from a Plex external ID. Title-search
results do not receive this exception, and ordinary unexplained year mismatches
remain rejected. Trusted external-ID aliases are recorded at `INFO`; ambiguous
or rejected identities remain `WARNING` or `ERROR`.

High-confidence Plex-to-TMDb resolutions are learned in durable state. The
binding key uses the Plex server, library UUID, rating key, media type, and
provider-ID fingerprint, so harmless localized title changes do not repeat
recovery work. A Plex provider GUID change invalidates the binding and forces
normal validation again; ambiguous searches are never learned.

`TMDB_TITLE_SEARCH_FALLBACK=True` permits exact normalized title/year search
only when Plex exposes no usable external ID. Ambiguous results are rejected.

`TMDB_EPISODE_GROUP_FALLBACK=True` can use alternate TMDb ordering only when
one episode group uniquely maps the complete Plex inventory. Unresolved
episodes preserve existing generated YAML rather than deleting it.

MetaFusion also contains narrow provider-compatibility mappings for anthologies
that TheTVDB/Plex stores as one multi-season show but TMDb stores as separate
series. The original Plex season number remains in the output while metadata
and season artwork come from the corresponding TMDb series. Current mappings
cover `The Haunting` (TheTVDB 345246) and `Monster` (TheTVDB 389492).
Additional verified mappings can be supplied with
`TMDB_SPLIT_SERIES_MAPPINGS`. A mapping is an explicit exception to the normal
TVDB consensus rule. The default `preserve` show policy leaves existing
top-level show metadata and artwork untouched while mapped season metadata,
episode metadata, and season artwork continue to update. `primary` opts into
using the primary TMDb series for top-level show data.

When Plex lists a future episode beyond TMDb's latest episode for that season,
MetaFusion records an `INFO` message and preserves existing metadata. It does
not fail the show, and schedules a focused metadata recheck after
`METADATA_PENDING_RECHECK_HOURS`. A gap inside the available TMDb
episode range is still treated as an unresolved ordering warning.

Known stable numbering exceptions can use `TMDB_EPISODE_OVERRIDES`. The
override maps one Plex `SxxExx` position to one TMDb `SxxExx` source and never
renumbers Plex or generated Kometa output. Automatic episode-group resolution
remains preferred; explicit overrides apply when configured and disable that
automatic fallback for the affected show.

For multiple same-title/year movie copies, unique Plex edition names are the
safe identity. `ALLOW_AMBIGUOUS_EDITIONS=True` disables that fail-safe and can
associate output with the wrong copy; it is not recommended.
