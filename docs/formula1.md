# Formula 1 extension

The Formula 1 extension is an opt-in, Kometa-only workflow for a dedicated Plex
TV library. It is intentionally isolated from normal movie and TV processing:
it has its own configuration, SQLite database, reports, logs, metadata files,
and managed artwork tree.

This feature is deliberately not linked from the main README. Enable it only if
your Formula 1 library follows one of the supported layouts.

## Enable and first run

Set `RUN_MODE=kometa` and `FORMULA1_ENABLED=True`. The first enabled run creates
`/config/formula1/formula1_template.yml`. Copy that file to
`/config/formula1/formula1.yml` only when you need to override its defaults.
Nothing under `/config/formula1` is generated while the extension is disabled
or while Plex mode is selected.

The default dedicated Plex library name is `Formula 1`. Change `library.name`
in the private configuration when required. Do not include ordinary television
shows in this library.

## Supported layouts

`library.naming_profile: auto` recognizes both layouts and records which parser
was used. The default `auto` profile lets both conventions coexist while a
library is being renamed.

### Current MetaFusion layout

Use one show folder per championship year, one Plex season per race round, and
one episode per programme:

```text
F1 2026/
├── Season 01/
│   ├── S01E01 - Australia Grand Prix - Weekend.Warm-Up.mkv
│   ├── S01E02 - Australia Grand Prix - FP1.mkv
│   ├── S01E09 - Australia Grand Prix - Pre-Qualifying.Show.mkv
│   ├── S01E10 - Australia Grand Prix - Qualifying.mkv
│   ├── S01E12 - Australia Grand Prix - Pre-Race.Show.mkv
│   ├── S01E13 - Australia Grand Prix - Race.mkv
│   └── S01E14 - Australia Grand Prix - Post-Race.Show.mkv
└── Season 02/
    └── S02E01 - China Grand Prix - Weekend.Warm-Up.mkv
```

### Kometa Formula 1 layout

The Kometa-style convention places each race in a round folder. The filename
still supplies the round and programme slot used by Plex and MetaFusion:

```text
Formula/
└── Season 2018/
    ├── 01 - Australian GP/
    │   ├── 01x01 - Australian GP - Free Practice 1.mkv
    │   ├── 01x04 - Australian GP - Pre-Qualifying Buildup.mkv
    │   ├── 01x05 - Australian GP - Qualifying Session.mkv
    │   ├── 01x07 - Australian GP - Pre-Race Buildup.mkv
    │   ├── 01x08 - Australian GP - Race Session.mkv
    │   ├── 01x09 - Australian GP - Post-Race Analysis.mkv
    │   └── 01x10 - Australian GP - Highlights.mkv
    └── 02 - Bahrain GP/
        └── 02x01 - Bahrain GP - Free Practice 1.mkv
```

In both layouts, the first number is the race round and the second is the
programme slot. They must match the season and episode numbers reported by Plex.
Episode slots do not need to be consecutive, but each round/slot combination
must be unique.

Recognized programme labels include `Weekend Warm-Up`, `FP1` through `FP3`,
`Free Practice 1` through `Free Practice 3`, `Sprint Qualifying`,
`Pre-Sprint Show`, `Sprint`, `Post-Sprint Show`, `Pre-Qualifying Show`,
`Qualifying`, `Post-Qualifying Show`, `Pre-Race Show`, `Race`, `Post-Race Show`,
and `Highlights`. The equivalent `Buildup`, `Session`, and `Analysis` names from
the Kometa layout are also accepted. Dots, underscores, and backslashes in the
programme portion are treated as spaces.

The event portion may use `Grand Prix` or `GP`; common aliases such as
`Australia`/`Australian`, `Britain`/`British`, and
`Netherlands`/`Dutch` are normalized before schedule matching. Ambiguous,
unsupported, or duplicate logical identities are reported and excluded rather
than guessed. Season 0 is ignored by default; set `testing.include: true` to
include testing programmes.

## Outputs and ownership

- Metadata: `/kometa/metadata/formula1_<year>.yml`
- Artwork: `/kometa/assets/formula1/rounds/<year>/round-<nn>/poster.png`
- State: `/config/formula1/cache/formula1.sqlite3`
- Run logs: `/config/formula1/logs/formula1-<run-id>.log`
- Issue reports: `/config/formula1/reports/formula1-issues-<run-id>.json`

Generated YAML uses Kometa Formula 1 controls (`f1_season`, `round_prefix`, and
`shorten_gp`) plus show, round, and episode edits. Existing non-MetaFusion YAML
fields are preserved. The top-level YAML name exactly matches the Plex show, as
required by Kometa's Formula 1 guide. Session dates use provider schedule fields
when available and fall back to the race date. Circuit length, scheduled race
distance, and scheduled lap count are taken from the matching official event
page and cross-validated before use. MetaFusion omits unavailable facts instead
of writing a misleading `to be confirmed` value; the missing fact is recorded in
the Formula 1 issue report. After the official page passes venue-identity checks,
its canonical circuit name and precise meeting locality are used in round and
episode summaries. Jolpica remains authoritative for race name, round order,
country, session dates, and race dates. A country-level official location never
replaces a more precise Jolpica locality. The extension ignores unrelated Kometa
metadata files.

Artwork is deterministic. It combines schedule/circuit facts, a translucent
host-country flag and colour gradient, circuit outline, race title, circuit,
locality, weekend date, and a Sprint marker. Public-domain flag assets are bundled
for current and historical Formula 1 host countries, so rendering does not add a
runtime network dependency. The complete flag is proportionally fitted across the
portrait canvas and feather-blended into the country gradient. This preserves
circles, emblems, and source scale without stretching or oversized centre-crops.
An unknown country falls back to the neutral gradient.
Existing artwork without an extension ownership record is adopted without
replacement. A managed file modified after adoption is treated as manual artwork
and preserved. Renderer revisions regenerate only unchanged extension-managed
posters; adopted and manually edited posters remain protected.

## Branding

Place user-supplied files in `/config/formula1/branding`:

- `logo.png`
- `font-regular.ttf`
- `font-bold.ttf`

If files are absent, MetaFusion uses a generic text mark and an installed fallback
font. MetaFusion does not download, bundle, or claim rights to Formula 1 logos or
commercial fonts. You are responsible for permission to use supplied branding.

## Data and network behavior

Calendar, round, venue, and session dates come from the Jolpica F1 API. Circuit
length, scheduled lap count, and scheduled race distance come from the matching
Formula1.com event page because those fields are not present in Jolpica's season
schedule response. MetaFusion discovers the official event links for each season,
validates plausible ranges and the length/lap/distance relationship, and caches
only the parsed facts in the isolated Formula 1 database. This is a website adapter,
not an official public API, so a markup change may temporarily produce a reported
fact gap; cached valid facts remain available as an explicitly logged stale fallback.

The yearly Jolpica schedule is authoritative for additions, removals, and round
order. A newly added event is matched from its current-year Formula1.com calendar
identity instead of a fixed 2026 fact table. A removed event is simply absent from
the new season; it does not erase historical seasons. Only rounds actually present
in the Plex inventory request official fact pages, which keeps normal runs small.

Circuit outlines come from the open `julesr0y/f1-circuits-svg` project (CC BY 4.0).
Known circuit identities use stable mappings. For a new circuit, MetaFusion reads
and caches the provider's current manifest, accepts only a unique high-confidence
identity match, and remembers that binding. If the outline project has not yet
published the new circuit, the poster still renders using the neutral outline
fallback and the condition is logged rather than guessed.

Provider responses are validated, cached in the extension database, retried, and
may use an explicitly logged stale cache during a temporary outage. Missing
schedule identity prevents that round from being written rather than guessing.

The circuit outline attribution is maintained here as required by its licence:
“F1 circuits SVG”, ROY Jules, CC BY 4.0,
<https://github.com/julesr0y/f1-circuits-svg>.

Bundled national flag renders come from `hampusborgos/country-flags`, which
documents the flags as public domain:
<https://github.com/hampusborgos/country-flags>.

For local development only, the renderer was verified with Saira Condensed from
Omnibus-Type under the SIL Open Font License. No test font or Formula 1 logo is
included in the Docker image.

## Cleanup and diagnostics

Cleanup is disabled by default. When enabled, a missing Plex episode must remain
absent for both `confirmation_scans` and `grace_hours`. Only extension-owned
state and byte-identical managed artwork are eligible. Formula 1 assets are never
passed to core MetaFusion cleanup.

`logging.console` accepts `off`, `summary`, or `full`. The default prints one
Formula 1 summary through the main logger while retaining item details in the
separate Formula 1 run log. Dry-run mode creates no template, database, artwork,
metadata, log, or report files.
