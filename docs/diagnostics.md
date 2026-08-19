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

The full artwork audits can take about as long as normal artwork evaluation.
They exercise the configured source order rather than merely checking whether
an API endpoint responds.

## Local checks and support

### Configuration diagnosis

```bash
python metafusion.py --doctor
```

`--doctor` and `--check-config` are aliases. They validate the effective
configuration and identify whether each value came from a built-in default,
`config.yml`, a secret file, or an environment variable. They do not contact
Plex, TMDb, or Fanart.tv and do not create a report.

### Runtime status

```bash
python metafusion.py --status
```

`--status` reads the ephemeral scheduler heartbeat and query-only durable job
and retry summaries, then prints JSON. It does not start a job or write a
report. A missing heartbeat normally means the long-running container has not
started successfully or `STATUS_FILE` points somewhere else.

### Support attachment

```bash
python metafusion.py --support-report
```

The support report records the image version and commit, platform details,
configuration binding names, validation status, and state/cache health. It does
not contact providers and intentionally omits configuration values, tokens,
API keys, metadata values, and library contents. Attach it with only the
relevant redacted log section and problem-specific report.

## Connector and release checks

### Preflight

```bash
python metafusion.py --preflight
```

Preflight verifies Plex and TMDb authentication, selected library names,
required output storage, and Plex-mode path visibility. With
`PLEX_LIBRARIES=auto`, it prints discovered movie/show libraries. It samples a
small number of Plex media paths and can suggest an unambiguous
`PLEX_PATH_MAPPINGS` translation. Suggestions do not create Docker mounts,
directories, probe files, or change ownership.

### Release qualification

```bash
python metafusion.py --release-check
```

Release qualification adds architecture, storage, durable SQLite state, and
provider-cache health checks to the connector preflight. It writes a redacted
release-qualification report and exits nonzero when an automated gate fails.
Use it before promoting a tested image; it is not a replacement for a real
library soak test.

### Compatibility contract

```bash
python metafusion.py --compatibility-check
```

This command validates connector, path, and configured-mode requirements for
the effective `COMPATIBILITY_PROFILE`. In Kometa mode it checks the generated
YAML/assets contract. In Plex mode it checks the Plex API and mapped local
artwork requirements enabled by the configuration. The report explains each
capability and the command exits nonzero when a required capability is absent.

## Full library audits

### Artwork audit

```bash
python metafusion.py --asset-audit
```

The asset audit performs a full read-only artwork selection pass. It evaluates
TMDb first, Fanart.tv when TMDb does not provide an acceptable candidate, Plex
artwork next, and the best available TMDb/Fanart.tv reserve last. The report
records attempted provider stages, selected source and image ID when available,
language, dimensions, quality score, ownership, collision protection, rejected
candidates, and the action a normal run would consider.

It does not prove that a later image download will succeed, and it does not
write artwork, YAML, caches, ownership, or incremental state. The report omits
host paths.

### Metadata audit

```bash
python metafusion.py --metadata-audit
```

The metadata audit uses a full dry-run comparison against TMDb. Kometa mode
compares generated fields with existing YAML. Plex mode compares supported
TMDb candidates with current Plex fields and reports locks, policy exclusions,
conflicts, missing source values, differences, unchanged fields, and proposed
actions. Artwork and cleanup are disabled. Metadata values are omitted from the
report, and no metadata, cache, ownership, or incremental state is written.

### Combined change plan

```bash
python metafusion.py --plan
```

The change plan applies the same identity, schema, metadata policy, artwork
selection, ownership, and cleanup gates as a normal full scan while forcing
read-only behavior. When cleanup is enabled and inventory is complete, it
separates stale cache/YAML scope, managed artwork eligible for removal,
protected artwork, unchanged valid output, and failures. Targeted plans disable
cleanup because a partial library or item scope cannot prove an orphan.

### Cross-mode library and artwork audit

```bash
python metafusion.py --library-audit
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
python metafusion.py --mapping-diagnose --rating-key 12345
```

Mapping diagnosis compares the complete Plex season/episode inventory with
TMDb standard ordering, `TMDB_EPISODE_OVERRIDES`, split-series mappings, and
available TMDb episode groups. When one complete one-step offset is uniquely
provable, it includes a proposed configuration snippet for manual review. It
never applies the proposal or changes a learned identity. An unresolved result
is a successful diagnostic outcome.

### Identity and binding inspection

```bash
python metafusion.py --identity-inspect --rating-key 12345
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
python metafusion.py --explain-item --rating-key 12345
```

This is the normal starting point for one-item investigation. It combines
identity and binding evidence with the scheduled-run decision, last full scan,
configuration and Plex update markers, TV child inventory, retry status,
effective library overrides, metadata policy, artwork policy and recheck
cadence, TV mapping outcome, cached artwork provider, and computed destinations.

The command does not perform a live multi-provider artwork candidate audit.
Use `--library-audit` when candidate scores, rejected alternatives, or provider
fallback stages are the question.

## Reports, retention, and privacy

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

`REPORT_RETENTION` keeps the newest files independently for each report type;
the default is `10`. Routine processing can additionally produce artwork-gap,
destination-history, and Plex-metadata reports. See
[generated output and reports](operations.md#generated-output-and-reports).

Reports redact connector secrets, but identity, mapping, destination-history,
and item-explanation reports can contain media titles and computed or actual
paths. Review every report before sharing it. Never publish `config.yml`,
Docker inspection output, tokens, API keys, or unredacted private host paths.
Follow the [support and version policy](../SUPPORT.md) when opening an issue.

## Command rules and exit results

- Choose only one of `--asset-audit`, `--metadata-audit`, `--plan`, or
  `--library-audit` per invocation.
- `--mapping-diagnose`, `--identity-inspect`, and `--explain-item` require
  `--rating-key` and run as standalone diagnostics.
- Target filters can reduce relevant audit scope, but a targeted plan cannot
  qualify cleanup.
- Exit code `0` means the command completed, including a diagnostic that found
  reviewable differences. Exit code `1` means an operational, connector, or
  qualification failure. Exit code `2` means invalid configuration or an
  unsupported command combination.
- Docker forwards public arguments, for example
  `docker run --rm IMAGE --support-report`.
