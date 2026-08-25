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
- Rotating show artwork:
  `/kometa/assets/formula1/shows/<year>/round-<nn>-<team>-<source>/poster.png`
  and `background.png`
- State: `/config/formula1/cache/formula1.sqlite3`
- Run logs: `/config/formula1/logs/formula1-<run-id>.log`
- Issue reports: `/config/formula1/reports/formula1-issues-<run-id>.json`
- Show-artwork attribution:
  `/config/formula1/reports/formula1-show-artwork-attribution.txt` and `.json`

Generated YAML uses Kometa Formula 1 controls (`f1_season`, `round_prefix`, and
`shorten_gp`) plus show, round, and episode edits. Existing non-MetaFusion YAML
fields are preserved. The top-level YAML name exactly matches the Plex show, as
required by Kometa's Formula 1 guide. Session dates use provider schedule fields
when available and fall back to the race date. Circuit length, scheduled race
distance, and scheduled lap count are taken from the matching official event
page and cross-validated before use. MetaFusion omits unavailable facts instead
of writing a misleading `to be confirmed` value; the missing fact is recorded in
the Formula 1 issue report. The same validated page supplies the venue's first
Grand Prix year and its `What's the circuit like?` section. MetaFusion maps the
official description to a controlled set of factual characteristics—such as
street, fast, technical, flowing, heavy-braking, chicanes, or hairpin—and writes
a concise attributed profile instead of copying Formula1.com's editorial text.
Unavailable history or characteristics are omitted and reported rather than
invented. After the official page passes venue-identity checks, its canonical
circuit name and precise meeting locality are used in round and episode
summaries. Jolpica remains authoritative for race name, round order, country,
session dates, and race dates. A country-level official location never replaces
a more precise Jolpica locality. Metadata-only profile changes do not regenerate
otherwise unchanged artwork. The extension ignores unrelated Kometa metadata
files.

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

### Race-triggered show poster and background rotation

`show_artwork.enabled: true` creates a paired show poster and 16:9 background
using the same licensed current-season car photograph. This is separate from the
round/season posters described above. The portrait uses a stable black, charcoal,
white, and red technical frame, with the complete landscape photograph fitted in
a horizontal band so the car is not stretched or cut off. The 16:9 background
uses the same photograph with the car kept toward the right and a dark Plex-safe
title area on the left. Both designs use the supplied logo and fonts.

Rotation is not time-based. `show_artwork.trigger: plex_new_race` means a new
pair is selected only when MetaFusion successfully parses at least one episode
from a race-round season that is newer than the last applied round. Merely adding
an event to the public calendar does not rotate anything. Container restarts and
repeated scans of the same Plex inventory also do not rotate anything. For
example, adding the first valid episode under Plex Season 05 changes the current
show pair to Round 05; subsequent episodes added to Season 05 leave that pair
unchanged. The next rotation happens when a valid Season 06 episode appears.

The pair is written to a versioned round/team/source directory before the Kometa
YAML is updated. This prevents a half-written poster/background pair from becoming
active. The generated show entry uses `file_poster` and `file_background`; Kometa
applies both to Plex on its next normal run. MetaFusion does not call Plex directly
from this Kometa-only extension.
Because those two YAML fields are the application mechanism, disabling
`metadata.enabled` also prevents new show-artwork rotations. Disabling only
`show_artwork.enabled` stops future rotation while leaving the last valid YAML
references and artwork intact.

The team roster is discovered for the detected championship year rather than
being hard-coded. The next constructor is chosen deterministically after the
previous one, so new, renamed, or departing constructors can be handled without
an annual code edit. The current race round is retained in the design while the
car layer advances through the available constructors. Rotation state is stored
in the isolated Formula 1 SQLite database and is based on Plex inventory, never
Docker uptime.

If a current-season photograph is not safely available, MetaFusion keeps the
previous valid pair and retries after the provider cache expires. It will not use
an older car, a different team, an ambiguous result, or a photograph with an
incompatible licence merely to force a rotation. This means the workflow requires
no annual input, but a newly revealed car may temporarily retain the previous
pair until a reusable photograph becomes available.

The source output is managed. If either file in the active pair no longer matches
its recorded checksum, both automatic repair and future rotation pause so a
manual edit cannot be silently displaced. If a managed file is merely missing,
MetaFusion recreates the matched pair from its recorded licensed source.

## Branding

Place user-supplied files in `/config/formula1/branding`:

- `logo.png`
- `font-regular.ttf`
- `font-bold.ttf`

If files are absent, MetaFusion uses a generic text mark and an installed fallback
font. MetaFusion does not download, bundle, or claim rights to Formula 1 logos or
commercial fonts. You are responsible for permission to use supplied branding.
The same three branding files are used for both race-round posters and rotating
show-level poster/background designs; no second logo or font configuration is
required.

## Data and network behavior

Calendar, round, venue, and session dates come from the Jolpica F1 API. Circuit
length, scheduled lap count, and scheduled race distance come from the matching
Formula1.com event page because those fields are not present in Jolpica's season
schedule response. Its circuit-history sections also supply the first Grand Prix
year and the source characteristics used by the concise circuit profile.
MetaFusion discovers the official event links for each season, validates page
identity, plausible ranges, and the length/lap/distance relationship, and caches
only the parsed facts and generated profile in the isolated Formula 1 database.
It does not persist or reproduce the full editorial paragraphs. This is a website
adapter, not an official public API, so a markup change may temporarily produce a
reported fact or profile gap; cached valid data remains available as an explicitly
logged stale fallback.

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

Current-season car photographs come from the Wikimedia Commons API without an
API key. MetaFusion discovers the live constructor roster from Jolpica, searches
each constructor dynamically, and requires the season year plus an unambiguous
team identity in the Commons file title. It rejects historic cars, models,
sculptures, safety cars, portraits, undersized or portrait images, ambiguous
teams, non-image responses, corrupt or blank files, and incompatible licences.
Accepted licences are Public Domain, CC0, and attribution-only CC BY variants;
ShareAlike, NonCommercial, and NoDerivatives media are not used by the automatic
renderer. Downloaded pixels are decoded and checked for dimensions, aspect ratio,
visual content, and sharpness before being cached under `/config/formula1/cache`.

For every retained generated pair, the persistent TXT and JSON attribution reports
record the Commons page, source title, author, licence, licence URL, team, season,
trigger round, and generated destinations. Old versioned pairs remain covered by
the report. MetaFusion uses the original photograph as a deterministic compositing
layer; it does not regenerate, repaint, or distort the car with AI.

Wikimedia's reuse and API guidance is available at
<https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia>
and <https://commons.wikimedia.org/wiki/Commons:API>.

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
separate Formula 1 run log. The summary distinguishes resolved and missing
circuit facts from resolved and missing circuit profiles. Dry-run mode creates
no template, database, artwork, metadata, log, or report files.
