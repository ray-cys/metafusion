# Runtime safeguards and application verification

MetaFusion uses three independent safeguards to catch upstream changes, qualify
new container builds, and confirm that Kometa applied generated output. The
first two run automatically. The post-Kometa audit is an explicit read-only
command because MetaFusion does not start Kometa and cannot know when a
separately scheduled Kometa job has finished.

## At a glance

| Safeguard | Default or command | When it runs | What it may write |
| --- | --- | --- | --- |
| TMDb change-aware rechecks | `TMDB_CHANGE_RECHECKS=True` | Eligible non-targeted, non-dry-run incremental jobs | Refreshed TMDb cache rows and a successful-job checkpoint in `meta_db.sqlite3` |
| Upgrade canary | `UPGRADE_CANARY=True` | Once per published commit, Plex server, mode, and compatibility profile | Detailed qualification state in `meta_db.sqlite3`; a report only when `--upgrade-canary-report` is requested |
| Post-Kometa application verification | `metafusion --kometa-application-audit` | Manually, after Kometa processes MetaFusion output | A text/JSON report only |

Neither automatic safeguard changes the configured metadata or artwork policy.
They decide when an existing policy must be reevaluated and whether a newly
published build is safe to begin writing output.

## TMDb change-aware rechecks

Plex update timestamps cannot reveal a title, release-date, translation, or
artwork change made only at TMDb. When `TMDB_CHANGE_RECHECKS=True`, MetaFusion
therefore reads TMDb's bounded movie and television change feeds between the
last successfully committed checkpoint and the current job.

Only TMDb IDs already associated with items in the selected Plex inventory are
eligible. Unknown change-feed IDs are ignored. A matching item is selected as
`tmdb_change_detected`; its cached movie/show detail and applicable season
responses are refreshed, then the normal metadata, artwork, lock, ownership,
quality, and output-mode policies decide whether anything changes.

TMDb change windows use calendar dates and can overlap between scheduled runs.
The run log therefore reports selected movie and television Plex-item counts
separately. MetaFusion compares the refreshed result semantically before
writing: provider order changes and duplicate values in set-like Kometa tag
fields do not create a metadata update, while meaningful field changes remain
visible at `INFO`.

The checkpoint rules fail closed:

- The first eligible run establishes an authoritative full-scan baseline.
- A missing, unreadable, future, or more-than-13-days-old checkpoint forces a
  full scan because TMDb cannot provide an unambiguous older change window.
- Every advertised page of both feeds must be read successfully.
- A failed feed or failed MetaFusion job retains the previous checkpoint.
- Targeted and dry-run commands do not consume or advance the checkpoint.
- A successful full scan advances the baseline even when no changed items were
  reported.

The setting is global and requires no per-library configuration. Disable it
only when TMDb-side changes should wait for the next normal full scan:

```env
TMDB_CHANGE_RECHECKS=False
```

## One-time upgrade canary

`UPGRADE_CANARY=True` protects the first real job after a published image
commit changes. The qualification scope combines the build commit, a hashed
Plex-server identity, output mode, and resolved compatibility profile. A
Kometa-mode pass therefore does not silently qualify Plex mode, and qualifying
one Plex server does not qualify another.

After connector and lightweight inventory checks, the canary deterministically
selects up to two records from every non-empty selected library and exercises
the existing identity, provider mapping, policy, edition, and destination
explanation path. At least one sample per non-empty library must succeed. Empty
libraries are valid when connector and compatibility checks pass.

The canary runs before YAML, media artwork, or Plex metadata writes. It never
edits Plex or media output. Every executed pass or failure stores its detailed
checks and deterministic samples in a bounded SQLite history. It does not
create a report during container startup, after an image update, or during a
normal scheduled job. Generate the latest stored result explicitly:

```bash
metafusion --upgrade-canary-report
```

This SQLite-only command does not contact Plex, TMDb, or Fanart.tv and writes
the configured `REPORT_FORMAT` under `/config/reports`. A canary failure stops
the job before output writes and tells the operator to run this command.

A pass marker is committed only after the surrounding MetaFusion job also
succeeds. If later processing fails, the next job repeats the canary. Local
development builds and dry runs do not create a published-build pass marker.

Disable the gate only for diagnosis, then restore it:

```env
UPGRADE_CANARY=False
```

## Post-Kometa application verification

Kometa mode generates YAML and artwork; Kometa later applies them to Plex.
MetaFusion cannot verify that final state during generation because Kometa may
run on a different schedule. After Kometa completes, run:

```bash
metafusion --kometa-application-audit
```

Limit the audit when investigating a particular library or item:

```bash
metafusion --kometa-application-audit --library Movies
metafusion --kometa-application-audit --library Movies --rating-key 12345
```

The command requires `RUN_MODE=kometa` and a working Plex connection. It does
not require TMDb, invoke Kometa, trigger a Plex metadata refresh, update Plex,
or rewrite YAML/assets/state. Its only writes are paired reports:

```text
/config/reports/kometa-application-audit-*.txt
/config/reports/kometa-application-audit-*.json
```

Metadata results are classified as:

| Status | Meaning |
| --- | --- |
| `applied` | Every verifiable generated value is visible in Plex. |
| `partial` | Some generated values are visible and some differ or are absent. |
| `not_applied` | None of the verifiable generated values match. |
| `missing_yaml` | The Plex item has no matching entry in generated YAML. |
| `no_verifiable_fields` | The entry contains no field that can be compared safely through PlexAPI. |
| `unverifiable` | Plex readback failed for the item or one of its children. |
| `not_found` | A requested rating key was not found in the selected libraries. |

For generated tag fields, MetaFusion requires the generated values to be a
subset of Plex's values. Additional tags supplied by Plex or another provider
do not cause a false failure. Artwork verification reuses durable ownership,
checksums, and perceptual hashes to determine whether Plex selected the managed
local image.

Run this command only after the relevant Kometa job. Running it earlier is safe
but will correctly report output that Kometa has not yet applied.

## Reports, retention, and privacy

Explicit reports follow `REPORT_RETENTION` and `REPORT_FORMAT`. Reports use normalized item identity
fields and do not include Plex tokens or provider API keys. Review paths and
titles before attaching a report to a public issue.

Use the [diagnostics guide](diagnostics.md) for all report commands and the
[operations guide](operations.md) for incremental scheduling and checkpoint
behavior.
