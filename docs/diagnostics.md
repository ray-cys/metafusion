# Diagnostics and support reports

MetaFusion diagnostics are read-only with respect to Plex metadata, Kometa
YAML, artwork, ownership, incremental markers, retry state, and cleanup state.
Commands that retain a report write only that report under `/config/reports`.
Use the [complete command-line reference](operations.md#command-line-reference)
for every public flag and supported value.

## Choose the right command

| Need | Command | External access | Retained output |
| --- | --- | --- | --- |
| Validate configuration and show each value's source | `--doctor` or `--check-config` | None | None; prints locally |
| Read scheduler heartbeat, recent jobs, and retry totals | `--status` | None | None; prints JSON |
| List persistent unresolved artwork/identity work | `--problems` | None | None; prints JSON |
| Prepare a value-free attachment for a support request | `--support-report` | None | `support-report-*.txt` |
| Check connections, selected libraries, mappings, and required storage | `--preflight` | Plex and TMDb | None; prints guidance |
| Qualify a build and its local state before release | `--release-check` | Plex and TMDb | `release-qualification-*.txt` |
| Validate the configured Kometa or Plex contract | `--compatibility-check` | Plex and TMDb | `compatibility-*.txt` |
| Audit artwork choice, ownership, quality, and fallback outcome | `--asset-audit` | Plex, TMDb, and Fanart.tv as needed | `asset-audit-*.txt` |
| Compare generated or direct Plex metadata with TMDb | `--metadata-audit` | Plex and TMDb | `metadata-audit-*.txt` |
| Preview metadata, artwork, and eligible cleanup together | `--plan` | Plex, TMDb, and Fanart.tv as needed | `change-plan-*.txt` |
| Inventory selected libraries and explain artwork health in both modes | `--library-audit` | Plex, TMDb, and Fanart.tv as needed | `library-asset-audit-*.txt` |
| Diagnose TV season/episode numbering | `--mapping-diagnose --rating-key KEY` | Plex and TMDb | `mapping-diagnosis-*.txt` |
| Explain how one Plex item is bound to TMDb | `--identity-inspect --rating-key KEY` | Plex and TMDb | `identity-inspection-*.txt` |
| Explain identity, scheduling, policy, mapping, retry, and destinations together | `--explain-item --rating-key KEY` | Plex and TMDb | `item-explanation-*.txt` |
| Capture sanitized item evidence for a reproducible support case | `--capture-replay --rating-key KEY` | Plex and TMDb | `provider-replay-capture-*.txt` |
| Inspect durable state without opening raw SQLite | `--state-report` | None | `state-report-*.txt` and `.json` |
| Review the latest automatically stored upgrade qualification | `--upgrade-canary-report` | None | `upgrade-canary-*.txt`, `.json`, or both according to `REPORT_FORMAT` |
| Browse run, library, problem, cleanup, provider, and provenance state offline | `--dashboard-report` | None | `metafusion-dashboard-*.html` and `.json` |
| Review pending and completed cleanup actions | `--cleanup-history-report` | None | `cleanup-history-*.txt` and `.json` |
| Review unresolved identity work | `--identity-review-queue` | None | `identity-review-*.txt` and `.json` |
| Verify whether Plex selected managed local artwork | `--plex-artwork-verify` | Plex only | `plex-artwork-verification-*.txt` and `.json` |
| Verify generated Kometa output after Kometa applies it | `--kometa-application-audit` | Plex only | `kometa-application-audit-*.txt` and `.json` |
| Compare a proposed configuration with the current effective one | `--config-impact /path/config.yml` | None | `configuration-impact-*.txt` and `.json` |

The full artwork audits can take about as long as normal artwork evaluation.
They exercise the configured source order rather than merely checking whether
an API endpoint responds.

SQLite-only reports describe recorded evidence, not current Plex/provider or
filesystem truth. Plex artwork verification is the live read-only check for
adoption. Detailed command examples and report limitations are in
[Lifecycle management](lifecycle-management.md).

### Offline HTML dashboard

```bash
metafusion --dashboard-report
```

The command always generates both a retained timestamped dashboard and
`/config/reports/metafusion-dashboard-latest.html`. Automatic refresh after a
successful non-dry run is disabled by default. Opt in with either:

```yaml
output:
  dashboard_enabled: true
```

or `DASHBOARD_ENABLED=True`. Disabling automatic refresh does not delete an
existing dashboard. Open either file directly in a browser; it contains its
own styling, tables, filtering, section
navigation, and print support and makes no network request. The dashboard is
built only from bounded SQLite evidence and covers recent jobs, library scan
state, unresolved work, retries, identity review, cleanup, provider health,
database health, and value-free field-level metadata provenance. A same-name
JSON companion is available for automation. It does not embed artwork,
provider response bodies, metadata values, credentials, or configuration.

The provenance view records which source supplied or retained each field, the
target (`kometa_yaml` or `plex_api`), policy, decision, one-way value
fingerprint, and the time that provenance state last changed. Repeated
identical decisions do not rewrite the row. Use `--state-report
--state-section provenance` for the equivalent text/JSON view or
`--explain-item --rating-key KEY` to combine recorded provenance with live
identity and policy diagnosis.

## Local checks and support

### Configuration diagnosis

```bash
metafusion --doctor
```

`--doctor` and `--check-config` are aliases. They validate the effective
configuration and identify whether each value came from a built-in default,
`config.yml`, a secret file, or an environment variable. They do not contact
Plex, TMDb, or Fanart.tv and do not create a report.

### Runtime status

```bash
metafusion --status
```

`--status` reads the ephemeral scheduler heartbeat and query-only durable job
and retry summaries, then prints JSON. It does not start a job or write a
report. A missing heartbeat normally means the long-running container has not
started successfully or `STATUS_FILE` points somewhere else.

### Support attachment

```bash
metafusion --support-report
```

The support report records the image version and commit, platform details,
configuration binding names, validation status, and state/cache health. It does
not contact providers and intentionally omits configuration values, tokens,
API keys, metadata values, and library contents. Attach it with only the
relevant redacted log section and problem-specific report.

## Connector and release checks

### Preflight

```bash
metafusion --preflight
```

Preflight verifies Plex and TMDb authentication, selected library names,
required output storage, and Plex-mode path visibility. With
`PLEX_LIBRARIES=auto`, it prints discovered movie/show libraries. It samples a
small number of Plex media paths and can suggest an unambiguous
`PLEX_PATH_MAPPINGS` translation. Suggestions do not create Docker mounts,
directories, probe files, or change ownership.

### Release qualification

```bash
metafusion --release-check
```

Release qualification adds architecture, storage, durable SQLite state, and
provider-cache health checks to the connector preflight. It writes a redacted
release-qualification report and exits nonzero when an automated gate fails.
Use it before promoting a tested image; it is not a replacement for a real
library soak test.

### Compatibility contract

```bash
metafusion --compatibility-check
```

This command validates connector, path, and configured-mode requirements for
the effective `COMPATIBILITY_PROFILE`. In Kometa mode it checks the generated
YAML/assets contract. In Plex mode it checks the Plex API and mapped local
artwork requirements enabled by the configuration. The report explains each
capability and the command exits nonzero when a required capability is absent.

## Full library audits

### Artwork audit

```bash
metafusion --asset-audit
```

The asset audit performs a full read-only artwork selection pass. It evaluates
TMDb first, Fanart.tv when TMDb does not provide an acceptable candidate, Plex
artwork next, and the best available TMDb/Fanart.tv reserve. When the computed
destination is missing and those standard stages are empty, it also evaluates
the automatic missing-only relaxed-language stage. Existing artwork is never
eligible for that final stage. The report
records attempted provider stages, selected source and image ID when available,
language, dimensions, quality score, ownership, collision protection, rejected
candidates, and the action a normal run would consider.

It does not prove that a later image download will succeed, and it does not
write artwork, YAML, caches, ownership, or incremental state. The report omits
host paths.

### Metadata audit

```bash
metafusion --metadata-audit
```

The metadata audit uses a full dry-run comparison against TMDb. Kometa mode
compares generated fields with existing YAML. Plex mode compares supported
TMDb candidates with current Plex fields and reports locks, policy exclusions,
conflicts, missing source values, differences, unchanged fields, and proposed
actions. Artwork and cleanup are disabled. Metadata values are omitted from the
report, and no metadata, cache, ownership, or incremental state is written.

### Combined change plan

```bash
metafusion --plan
```

The change plan applies the same identity, schema, metadata policy, artwork
selection, ownership, and cleanup gates as a normal full scan while forcing
read-only behavior. When cleanup is enabled and inventory is complete, it
separates stale cache/YAML scope, managed artwork eligible for removal,
protected artwork, unchanged valid output, and failures. Targeted plans disable
cleanup because a partial library or item scope cannot prove an orphan.

### Cross-mode library and artwork audit

```bash
metafusion --library-audit
```

The library audit works in Kometa and Plex modes. It lists available and
selected libraries, item counts, ownership outcomes, provider attempts,
candidate dimensions, normalized quality scores, and highest-scoring rejected
candidates. It explains whether language order, hard dimensions, vote
threshold, aspect ratio, downgrade protection, or a deterministic tie-break
produced the decision.

Use this command instead of `--explain-item` when live provider candidate
scoring and fallback-stage evidence are required.

## Item-level diagnosis

The following commands require at least one `--rating-key`. The option can be
repeated or contain comma-separated keys. Each command must run as a standalone
diagnostic; do not combine them with scheduling, processing, another audit, or
Plex metadata maintenance.

### TV mapping diagnosis

```bash
metafusion --mapping-diagnose --rating-key 12345
```

Mapping diagnosis compares the complete Plex season/episode inventory with
TMDb standard ordering, `TMDB_EPISODE_OVERRIDES`, split-series mappings, and
available TMDb episode groups. When one complete one-step offset is uniquely
provable, it includes a proposed configuration snippet for manual review. It
never applies the proposal or changes a learned identity. An unresolved result
is a successful diagnostic outcome.

### Identity and binding inspection

```bash
metafusion --identity-inspect --rating-key 12345
```

Identity inspection reports the Plex rating key, media type, GUIDs and external
IDs, localized/original titles, year, edition, selected TMDb ID, resolution
source, confidence, warnings or rejection reasons, active learned binding, and
the newest 50 binding-history events. It also computes the Kometa YAML or Plex
API target and poster, background, and season destinations.

Provider response caching is bypassed and SQLite is queried read-only. Binding
history begins when the history feature is installed; it cannot reconstruct
transitions from older versions.

### Unified item explanation

```bash
metafusion --explain-item --rating-key 12345
```

This is the normal starting point for one-item investigation. It combines
identity and binding evidence with the scheduled-run decision, last full scan,
configuration and Plex update markers, TV child inventory, retry status,
effective library overrides, metadata policy, artwork policy and recheck
cadence, TV mapping outcome, cached artwork provider, and computed destinations.

The command does not perform a live multi-provider artwork candidate audit.
Use `--library-audit` when candidate scores, rejected alternatives, or provider
fallback stages are the question.

### Sanitized replay capture

```bash
metafusion --capture-replay --rating-key 12345
```

Replay capture retains the same read-only identity, scheduling, policy,
mapping, retry, and destination evidence as the item explanation in a
sanitized JSON bundle. Connector secrets, Plex rating keys, private provider
hosts, and local paths are removed or replaced deterministically. Titles and
other matching evidence remain useful for reproducing the problem, so review
the JSON before attaching it to a public issue.

## Reports, retention, and privacy

### Post-Kometa application verification

The complete status, safety, retention, and scheduling contract is in [runtime
safeguards and application verification](runtime-safeguards.md#post-kometa-application-verification).

Run Kometa first, then use:

```bash
metafusion --kometa-application-audit
metafusion --kometa-application-audit --library Movies --rating-key 12345
```

This Kometa-mode command reads MetaFusion's generated YAML, durable managed
artwork ownership, and the current Plex values. For generated tag fields, the
expected values must be present but extra Plex/provider values are allowed. It
reports applied, partial, not-applied, missing-YAML, and unverifiable metadata,
then reuses checksum and perceptual verification to report whether Plex has
selected managed artwork. It never invokes Kometa, triggers a Plex refresh, or
changes metadata/artwork. Run it only after Kometa has processed the generated
files; an earlier run naturally reports output as not yet applied.

Without `--rating-key`, it verifies the selected libraries. With one or more
rating keys it reports only those items plus explicit not-found entries. The
text form is concise; the JSON form retains field mismatches and normalized
TMDb/IMDb/TVDb/Plex identity fields. Choose either or both with `REPORT_FORMAT`.

### Retained report inventory

| Report | Created by |
| --- | --- |
| `/config/reports/asset-audit-*.txt` | `--asset-audit` |
| `/config/reports/metadata-audit-*.txt` | `--metadata-audit` |
| `/config/reports/change-plan-*.txt` | `--plan` |
| `/config/reports/library-asset-audit-*.txt` | `--library-audit` |
| `/config/reports/mapping-diagnosis-*.txt` | `--mapping-diagnose` |
| `/config/reports/identity-inspection-*.txt` | `--identity-inspect` |
| `/config/reports/item-explanation-*.txt` | `--explain-item` |
| `/config/reports/compatibility-*.txt` | `--compatibility-check` |
| `/config/reports/support-report-*.txt` | `--support-report` |
| `/config/reports/release-qualification-*.txt` | `--release-check` |
| `/config/reports/provider-replay-capture-*.txt` | `--capture-replay` |
| `/config/reports/kometa-application-audit-*.txt` | `--kometa-application-audit` |
| `/config/reports/upgrade-canary-*.txt` | `--upgrade-canary-report`; the automatic check stores details in SQLite without creating files |
| `/config/reports/run-history-*.txt` | `--run-history` |
| `/config/reports/schedule-advice-*.txt` | `--schedule-advice` |
| `/config/reports/cleanup-quarantine-*.txt` | `--cleanup-quarantine-report`, restore, or purge |

`REPORT_FORMAT` controls conventional diagnostic output: `text`, `json`, or
`both` (the default). `REPORT_RETENTION` keeps the newest logical reports
independently for each report type; the default is `10`. The HTML dashboard
always keeps its JSON data companion, and sanitized replay capture always keeps
its required JSON payload. Routine processing additionally produces
artwork-gap, destination-history, unresolved-work, post-application adoption,
and Plex-metadata reports. The artwork-gap report is an always-present run
snapshot, including an explicit zero-open result. It separates current
observations, carried-forward open work, and recently resolved history; its
structured JSON form also records destination state and recheck timing. This applies
to movie posters and backgrounds as well as show posters, backgrounds, and
individual season posters in both output modes. See
[generated output and reports](operations.md#generated-output-and-reports).

Every item-level JSON record uses the same nullable identity fields:
`plex_rating_key`, `tmdb_id`, `imdb_id`, `tvdb_id`, `edition`,
`season_number`, and `identity_source`. This applies to artwork gaps,
unresolved work, asset and metadata audits, change plans, adoption audits,
Plex metadata results, mapping/identity diagnostics, item explanations, and
sanitized replay captures. A field is `null` when Plex or the selected provider
did not expose it safely. Replay captures replace the Plex rating key with a
deterministic share-safe identifier.

Reports redact connector secrets, but identity, mapping, destination-history,
and item-explanation reports can contain media titles and computed or actual
paths. Review every report before sharing it. Never publish `config.yml`,
Docker inspection output, tokens, API keys, or unredacted private host paths.
Follow the [support and version policy](../SUPPORT.md) when opening an issue.

## Command rules and exit results

- Choose only one of `--asset-audit`, `--metadata-audit`, `--plan`, or
  `--library-audit` per invocation.
- `--mapping-diagnose`, `--identity-inspect`, `--explain-item`, and
  `--capture-replay` require
  `--rating-key` and run as standalone diagnostics.
- Target filters can reduce relevant audit scope, but a targeted plan cannot
  qualify cleanup.
- Exit code `0` means the command completed, including a diagnostic that found
  reviewable differences. Exit code `1` means an operational, connector, or
  qualification failure. Exit code `2` means invalid configuration or an
  unsupported command combination.
- Docker forwards public arguments, for example
  `docker run --rm IMAGE --support-report`.
