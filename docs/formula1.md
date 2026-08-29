# Formula 1 extension

The Formula 1 extension is an opt-in, Kometa-only workflow for a dedicated Plex
TV library. It is intentionally isolated from normal movie and TV processing:
it has its own configuration, SQLite database, reports, logs, metadata files,
and managed artwork tree.

It exists for users who store each championship as one Plex show, each race as
one season, and each broadcast programme as one episode. It is not a general
sports downloader, recording scheduler, Formula 1 news client, or replacement
for Plex, Kometa, SABnzbd, an NZB indexer, or a Usenet service.

## What it does

When explicitly enabled, the extension:

- discovers the dedicated Formula 1 Plex library and championship year;
- validates every episode filename against the provider schedule;
- creates one Kometa metadata file per championship year;
- writes show, race-season, and episode metadata with provider-validated dates,
  circuit facts, race distance, lap count, and concise circuit context;
- creates deterministic race-season posters and per-episode 16:9 cards;
- rotates the show poster through licensed current-season team-car photographs;
- rotates a cinematic, full-bleed show background through licensed photographs
  matched to the current race, circuit, and expected day/night environment;
- records identity, source, ownership, attribution, cleanup, and application
  verification state in its private SQLite database and reports; and
- automatically accommodates later championship years, schedule changes,
  constructor changes, new session date fields, and newly published circuits
  when its providers expose safe identities.

The extension never edits Plex directly. It writes Kometa YAML and referenced
artwork; a later normal Kometa run applies those files to Plex. It does not create
media, download NZBs, fetch episodes, create a Plex library, or create calendar-only
races that are not present in Plex.

## Operating flow

1. A completed race programme is named in one of the supported forms and placed
   in the dedicated media tree.
2. Plex scans the file and exposes it under the championship show, race-round
   season, and programme episode.
3. MetaFusion reads that Plex inventory, validates year/round/event/session
   identity, and obtains only the provider data required by detected media.
4. MetaFusion atomically creates or reconciles its Kometa YAML, artwork, state,
   logs, attribution, issues, and verification expectation.
5. Kometa reads `formula1_<year>.yml` and applies the selected metadata and local
   artwork to Plex on Kometa's next run.
6. A later MetaFusion run performs the configured read-only application check.

Downloads and Plex/Kometa scheduling remain external so failures in those systems
cannot silently broaden this extension's filesystem or deletion authority.

## Enable and first run

Set `RUN_MODE=kometa` and `FORMULA1_ENABLED=True`. The first enabled run creates
`/config/formula1/formula1_template.yml`. Copy that file to
`/config/formula1/formula1.yml` only when you need to override its defaults.
Nothing under `/config/formula1` is generated while the extension is disabled
or while Plex mode is selected.

The default dedicated Plex library name is `Formula 1`. Change `library.name`
in the private configuration when required. Do not include ordinary television
shows in this library.

Only two container-level values are needed: `RUN_MODE=kometa` selects the core
output mode and `FORMULA1_ENABLED=True` opts into the extension. Everything else
belongs in `/config/formula1/formula1.yml`. The packaged
`formula1_template.yml` is refreshed without overwriting the active file. The
active file may contain only the values you want to override because its mappings
are merged over the packaged defaults. It is read again at the start of every
scheduled or forced MetaFusion run; changing it does not require recreating the
container, although an already-running scan keeps the settings it started with.

## Configuration reference

The generated template is the authoritative copyable configuration. Keep provider
URLs, the managed policies, and `plex_new_race` unchanged unless this document
explicitly says otherwise.

| Setting | Default | Purpose and constraints |
| --- | --- | --- |
| `library.name` | `Formula 1` | Exact dedicated Plex library name. |
| `library.naming_profile` | `auto` | `auto` accepts both filename conventions; `current` or `kometa` restricts parsing to one convention. |
| `sessions.aliases` | `{}` | Optional mappings for broadcaster-specific programme labels. Each alias requires a display `title` and internal `kind`. |
| `sessions.date_fields` | supplied mapping | Maps programme kinds to date-bearing Jolpica schedule fields. Existing defaults are retained when extra fields are added. |
| `metadata.enabled` | `true` | Creates and reconciles Kometa YAML. Disabling it also prevents new show-artwork references from being activated. |
| `metadata.original_title` | `Formula Internationale` | Show-level original title written to Kometa. |
| `metadata.originally_available` | `1950-05-13` | Show-level franchise availability date; race and episode dates remain schedule-derived. |
| `metadata.content_rating` | `PG-13` | Show-level rating written to Kometa. |
| `metadata.studio` | `F1TV` | Show-level studio value. |
| `metadata.tagline` | `We race as one.` | Show-level tagline. |
| `metadata.genre` | `[Sport]` | Show-level Kometa genre list. |
| `metadata.round_prefix` | `true` | Passes Kometa's Formula 1 round-prefix control. |
| `metadata.shorten_gp` | `false` | Passes Kometa's Formula 1 Grand Prix shortening control. |
| `artwork.enabled` | `true` | Creates race-season posters and their YAML references. |
| `artwork.width`, `artwork.height` | `1000`, `1500` | Race poster dimensions; must remain exactly 2:3 and within the validated limits. |
| `artwork.policy` | `managed` | Only supported policy. Extension-owned byte-identical output may be updated; manual or unknown files are preserved. |
| `artwork.asset_reference_root` | `config/assets/formula1/rounds` | Kometa-visible reference root corresponding to the mounted `/kometa/assets/formula1/rounds` output. |
| `artwork.logo`, `font_regular`, `font_bold` | paths under `branding/` | Optional user-supplied branding resolved below `/config/formula1`; paths cannot escape that directory. |
| `show_artwork.enabled` | `true` | Enables rotating show poster/background pairs and detected episode cards. |
| `show_artwork.trigger` | `plex_new_race` | Only supported trigger. Rotation occurs when a newer valid race-round season first appears in Plex, not on elapsed time. |
| `show_artwork.policy` | `managed` | Only supported ownership policy. A manual change pauses automatic pair replacement. |
| `show_artwork.poster_width`, `poster_height` | `1000`, `1500` | Show poster dimensions; must remain exactly 2:3. |
| `show_artwork.background_width`, `background_height` | `3840`, `2160` | Clean show-background output dimensions; the default is true 4K UHD and must remain exactly 16:9. |
| `show_artwork.episode_width`, `episode_height` | `1920`, `1080` | Episode-card dimensions, kept at Full HD independently of the show background. |
| `show_artwork.minimum_source_width`, `minimum_source_height` | `1600`, `900` | Minimum decoded Flickr or Commons photograph dimensions for portrait and episode artwork. |
| `show_artwork.minimum_background_source_width`, `minimum_background_source_height` | `3840`, `2160` | Preferred decoded photograph dimensions for a genuine 4K show-background source. |
| `show_artwork.fallback_background_source_width`, `fallback_background_source_height` | `1600`, `900` | Last acceptable source floor when no 4K photograph survives the same subject, environment, colour, licence, decoding, and sharpness checks. The final background is still rendered at the configured 3840×2160 output size. |
| `show_artwork.retention_pairs_per_season` | `30` | Maximum retained versioned managed show pairs per championship. |
| `show_artwork.source_cache_retention_days` | `120` | Age after which inactive downloaded source photographs may be pruned and later redownloaded. |
| `show_artwork.asset_reference_root` | `config/assets/formula1/shows` | Kometa-visible reference root corresponding to the mounted show-artwork output. |
| `verification.enabled` | `true` | Enables delayed, read-only comparison of expected Kometa results with Plex. |
| `verification.delay_hours` | `1` | Minimum wait after an output-changing run before checking Plex. Set `0` only for controlled testing. |
| `verification.retention` | `20` | Number of application-verification reports retained. |
| `verification.perceptual_distance` | `8` | Maximum perceptual-hash distance accepted for Plex artwork transcodes. |
| `providers.*_url` | packaged HTTPS URLs | Jolpica, Formula1.com, circuit SVG/manifest, and Commons endpoints. These are adapter contracts, not normal tuning controls. |
| `providers.cache_hours` | `24` | Validity of schedule, facts, profile, and circuit-provider cache entries. |
| `providers.commons_cache_hours` | `24` | Validity of team-car and race-car/background Commons discovery results. |
| `providers.flickr_api_key` | empty | Optional Flickr public API key. `FORMULA1_FLICKR_API_KEY` overrides this value. The secret and OAuth token are not needed. |
| `providers.flickr_url` | official Flickr REST URL | Pinned provider contract; not a normal tuning control. |
| `providers.flickr_cache_hours` | `24` | Flickr search and live licence-registry cache validity. |
| `providers.retries` | `3` | Bounded provider attempts, from 1 through 5. |
| `cleanup.enabled` | `false` | Enables reconciliation only for extension-owned state and byte-identical managed artwork. Review a dry run first. |
| `cleanup.confirmation_scans` | `2` | Consecutive missing inventories required before cleanup eligibility. |
| `cleanup.grace_hours` | `48` | Minimum elapsed absence required in addition to confirmation scans. |
| `logging.level` | `INFO` | Detail-log threshold: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `logging.console` | `summary` | `off`, `summary`, or `full` Formula 1 output in the main console. The separate run log remains available. |
| `logging.retention` | `20` | Number of private Formula 1 run logs retained. |

Relative branding and reference paths in the template are intentional. The
extension validates output roots and does not accept arbitrary filesystem escape
paths. Core Plex URL/token, Kometa path, scheduling, dry-run, and runtime settings
remain in the normal MetaFusion configuration rather than this private file.

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
│   ├── S01E14 - Australia Grand Prix - Post-Race.Show.mkv
│   └── S01E15 - Australia Grand Prix - Post-Race.Press.Conference.mkv
└── Season 02/
    └── S02E01 - China Grand Prix - Weekend.Warm-Up.mkv
```

### Kometa Formula 1 layout

The adapted Kometa-style convention keeps the same Plex show and season folders
as the current layout while using Kometa's `round x programme` filename form:

```text
F1 2018/
├── Season 01/
│   ├── 01x01 - Australian GP - Free Practice 1.mkv
│   ├── 01x04 - Australian GP - Pre-Qualifying Buildup.mkv
│   ├── 01x05 - Australian GP - Qualifying Session.mkv
│   ├── 01x07 - Australian GP - Pre-Race Buildup.mkv
│   ├── 01x08 - Australian GP - Race Session.mkv
│   ├── 01x09 - Australian GP - Post-Race Analysis.mkv
│   ├── 01x10 - Australian GP - Highlights.mkv
│   └── 01x11 - Australian GP - Post-Race Press Conference.mkv
└── Season 02/
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
`Post-Race Press Conference`, and `Highlights`. The equivalent `Buildup`,
`Session`, and `Analysis` names from the Kometa layout are also accepted. Dots,
underscores, hyphens, and backslashes in the programme portion are treated as
spaces. A detected post-race press conference is dated to race day, receives its
own metadata entry and managed episode card, and does not appear as an unknown
programme.

The event portion may use `Grand Prix` or `GP`; common aliases such as
`Australia`/`Australian`, `Britain`/`British`, and
`Netherlands`/`Dutch` are normalized before schedule matching. Ambiguous,
unsupported, or duplicate logical identities are reported and excluded rather
than guessed. Season 0 is outside the race-weekend scope and is always ignored;
the extension has no pre-season testing configuration or processing path.

The parsed event name is checked against the authoritative event assigned to
that round in the current-year schedule. A file labelled for a different Grand
Prix is quarantined before metadata, artwork, or cleanup reconciliation; it is
never silently attached to the scheduled race. An unfamiliar programme label
remains usable as a title, but is explicitly reported for review because
MetaFusion cannot select a programme-specific session date for it.

Exactly one Plex show may represent a championship year. If two shows resolve
to the same year, the run stops during preflight before any YAML, artwork,
binding, or cleanup change. This prevents two Plex identities from competing
for `formula1_<year>.yml`.

Championship discovery is not tied to 2026. A plausible four-digit year is read
from the Plex show title, with the Plex year as a fallback. Provider existence,
not a rolling `current year + N` limit, decides whether that season is ready. A
new `Formula 1 (2027)` or later show is handled after Plex exposes its first valid
episode. MetaFusion does not create empty Plex shows or calendar-only rounds.

If Plex exposes a future championship before its schedule is published, that
year is marked **schedule pending**. Existing YAML, artwork, bindings,
verification work, and cleanup authority for that year are preserved; other
championship years continue normally. The pending year retries automatically on
every later scheduled run.

The top-level YAML mapping is permanently named `F1 <year>`, even after Kometa
changes the Plex display title. MetaFusion emits a `match.title` list containing
both `F1 <year>` and `Formula 1 (<year>)`, plus any currently discovered Plex
alias, then applies `title: Formula 1 (<year>)`. This lets the first Kometa run
match the scanner title and every later run match the renamed title without a
one-run gap. Older title-keyed Formula 1 entries are consolidated into the
stable mapping while manual fields are preserved.

## Optional SABnzbd intake automation

An external Bash post-processing script can automate the intake side before Plex
and MetaFusion run. This is optional and deliberately remains outside MetaFusion:
the extension neither searches for NZBs nor controls SABnzbd. SABnzbd supports a
Scripts Folder, executable Unix scripts, per-category completed folders, and
per-category post-processing scripts; consult its official
[folder documentation](https://sabnzbd.org/wiki/configuration/5.1/folders),
[configuration overview](https://sabnzbd.org/wiki/configuration/5.1/configure),
and [post-processing documentation](https://sabnzbd.org/wiki/scripts/post-processing-scripts)
for the version you run.

A private post-processing script may accept a successfully completed `f1` or
`formula1` category job, validate exactly one media file, render one selected
naming profile, move it atomically into the Plex tree, and then request or await
a Plex scan. It should generate only one of these forms in steady state:

```text
# current profile
F1 2027/Season 03/S03E10 - Japan Grand Prix - Qualifying.mkv

# adapted Kometa profile
F1 2027/Season 03/03x10 - Japanese GP - Qualifying Session.mkv
```

`library.naming_profile: auto` reads both forms during a deliberate migration.
For steady-state use, one output profile is easier to audit. Keep the top-level
`F1 <year>` name for newly scanned media; let generated `match.title` aliases
handle Kometa's later display-title change.

Treat intake as privileged filesystem automation: validate SAB arguments,
completion status, category, source path, year, event, programme, and media count;
use a lock and idempotent destination checks; never recursively delete an
unresolved path or library root; and keep a credential-free audit log. Do not
assume `ffprobe` exists in the SABnzbd image. MetaFusion supplies no feeds, NZB
files, indexer configuration, credentials, or download sources; users remain
responsible for lawful access and source terms.

## Outputs and ownership

- Metadata: `/kometa/metadata/formula1_<year>.yml`
- Artwork: `/kometa/assets/formula1/rounds/<year>/round-<nn>/poster.png`
- Episode artwork:
  `/kometa/assets/formula1/rounds/<year>/round-<nn>/episodes/episode-<nn>.png`
- Rotating show artwork:
  `/kometa/assets/formula1/shows/<year>/round-<nn>-<team>-<source>/poster.png`
  and `background.png`
- State: `/config/formula1/cache/formula1.sqlite3`
- Run logs: `/config/formula1/logs/formula1-<run-id>.log`
- Issue reports: `/config/formula1/reports/formula1-issues-<run-id>.json`
- Show-artwork attribution:
  `/config/formula1/reports/formula1-show-artwork-attribution.txt` and `.json`

The corresponding directory layout is:

```text
/config/
└── formula1/
    ├── formula1_template.yml        # generated reference; do not edit
    ├── formula1.yml                 # optional active overrides; user-created
    ├── branding/
    │   ├── logo.png                 # optional, user supplied
    │   ├── font-regular.ttf         # optional, user supplied
    │   └── font-bold.ttf            # optional, user supplied
    ├── cache/
    │   ├── formula1.sqlite3         # isolated state, cache, bindings and history
    │   └── show-artwork/            # validated Flickr/Commons source cache
    ├── logs/
    │   └── formula1-<run-id>.log
    └── reports/
        ├── formula1-issues-<run-id>.json
        ├── formula1-application-verification-<run-id>.json
        ├── formula1-show-artwork-attribution.txt
        └── formula1-show-artwork-attribution.json

/kometa/
├── metadata/
│   └── formula1_<year>.yml
└── assets/
    └── formula1/
        ├── rounds/<year>/round-<nn>/
        │   ├── poster.png
        │   └── episodes/episode-<nn>.png
        └── shows/<year>/round-<nn>-<team>-<source>/
            ├── poster.png
            └── background.png
```

`/config/formula1` is private runtime state and should be included in normal
appdata backups. `/kometa/metadata` and `/kometa/assets` are generated Kometa
inputs. Do not point the extension at a media directory or allow another cleanup
tool to treat these generated directories as source media.

## Generated metadata example

The following abbreviated example shows the ordering and identity contract. The
actual dates, circuit facts, summaries, paths, episodes, and artwork sources are
calculated from the detected Plex inventory and validated provider responses.

```yaml
metadata:
  F1 2027:
    match:
      title:
        - F1 2027
        - Formula 1 (2027)
    title: Formula 1 (2027)
    sort_title: Formula 1 (2027)
    original_title: Formula Internationale
    originally_available: '1950-05-13'
    content_rating: PG-13
    studio: F1TV
    tagline: We race as one.
    summary: The 2027 FIA Formula One World Championship.
    genre:
      - Sport
    file_poster: /config/assets/formula1/shows/2027/round-03-team-source/poster.png
    file_background: /config/assets/formula1/shows/2027/round-03-team-source/background.png
    f1_season: 2027
    round_prefix: true
    shorten_gp: false
    seasons:
      3:
        title: Japanese Grand Prix
        summary: >-
          Round 3 of the 2027 Formula 1 season at Suzuka International Racing
          Course in Suzuka, Japan. The circuit measures 5.807 km; the scheduled
          race runs for 53 laps and covers 307.471 km. The venue first hosted a
          Formula 1 Grand Prix in 1987. Formula1.com circuit profile: fast,
          flowing, technical.
        file_poster: /config/assets/formula1/rounds/2027/round-03/poster.png
        episodes:
          10:
            title: Qualifying Session
            originally_available: '2027-04-03'
            summary: >-
              Qualifying Session from the Japanese Grand Prix at Suzuka
              International Racing Course, Suzuka, Japan. Round 3 of the 2027
              Formula 1 season at Suzuka International Racing Course in Suzuka,
              Japan. The circuit measures 5.807 km; the scheduled race runs for
              53 laps and covers 307.471 km. The venue first hosted a Formula 1
              Grand Prix in 1987. Formula1.com circuit profile: fast, flowing,
              technical.
            file_poster: /config/assets/formula1/rounds/2027/round-03/episodes/episode-10.png
```

`match` always remains first, show fields remain above `seasons`, and season
fields remain above `episodes`. The stable mapping key remains `F1 <year>` even
after Kometa changes the Plex display title to `Formula 1 (<year>)`.

## Generated artwork examples

These examples were produced by the actual deterministic renderers using generic
branding and synthetic source-photo placeholders. Runtime output uses your
supplied logo/fonts and separately validated, licensed Flickr or Wikimedia Commons team-car
or race-background photographs.

| Race-season poster | Rotating show poster |
| --- | --- |
| ![Example Formula 1 race-season poster](images/formula1/round-poster.png) | ![Example rotating Formula 1 show poster](images/formula1/show-poster.png) |

Episode cards use the race weekend's persistent team-car binding and an adaptive
cinematic layout. MetaFusion detects the car's visual side, places the simplified
session copy opposite it, protects the car's exposure, and applies only a soft
local readability gradient. The session is the primary title; the round, Grand
Prix, and date remain secondary. The circuit trace is an aspect-preserving,
luminance-aware watermark anchored to the adaptive outer edge below the F1 logo:
left when the copy uses the left side and right when it uses the right. Its light
and dark strokes adapt to the underlying photograph so it remains subtle but
legible at television viewing distance.
Technical borders and the long circuit/location sentence are deliberately omitted
so the card remains clear in Plex's smaller episode and Continue Watching tiles:

![Example Formula 1 episode card](images/formula1/episode-card.png)

The show background is a clean, full-bleed 16:9 race photograph with restrained
grading, a subtle left readability gradient, and a light vignette:

![Example Formula 1 show background](images/formula1/show-background.png)

Generated YAML uses Kometa's Formula 1 controls plus show, race-season, and
episode edits. The stable key and title aliases keep matching reliable before
and after Kometa changes the Plex display title. MetaFusion preserves compatible
manual fields, does not generate `visible_library`, and keeps `match` first,
show fields above `seasons`, and season fields above `episodes`.

Known programme dates come from `sessions.date_fields`. Add a broadcaster label
without changing code:

```yaml
sessions:
  aliases:
    Hyper Practice:
      title: Hyper Practice
      kind: practice4
  date_fields:
    practice4: PracticeFour
```

Jolpica remains authoritative for the season schedule and dates. A matching,
identity-validated Formula1.com event page supplies circuit length, scheduled
lap count and distance, first Grand Prix year, and concise circuit traits.
Unavailable values are omitted and reported instead of being written as `to be
confirmed`. Metadata-only profile changes do not regenerate unchanged artwork.

Race-season artwork is deterministic and combines validated schedule/circuit
facts, a proportionally rendered host-country flag, circuit outline, event and
venue text, weekend dates, and Sprint state. The public-domain flag catalogue is
bundled; unknown countries use the neutral canvas without a flag. Text is fitted
to safe areas rather than allowed to overflow.

Artwork fingerprints include the supplied logo and font bytes, so replacing a
branding file refreshes only checksum-matching managed output. Unknown, adopted,
or manually modified files remain protected.

Every Plex-detected episode receives a unique 16:9 card after Kometa applies its
`file_poster` reference. One persistent constructor and validated team-car source
is stored per round, so every programme from that weekend remains visually
consistent and newly added episodes inherit the same binding. Different rounds
advance deterministically through the current constructor roster.

The renderer preserves source geometry, controls highlights and background
exposure, adapts text placement to the car position, and keeps the circuit
watermark legible at television distance. Scheduled runs create missing cards
but do not opportunistically repaint stable historical bindings. Use the
one-time upgrade command below when a deliberate re-evaluation is wanted.

### Race-triggered show poster and background rotation

`show_artwork.enabled: true` creates a paired show poster and 16:9 background
from two independently validated, licensed sources. The portrait and episode
cards rotate current-season Formula 1 team cars. The show background independently
requires a colour photograph with visible Formula 1 action from practice,
qualifying, Sprint, or the race. It prefers the exact event, then recent and
historical action at that exact circuit. Night lighting, street-circuit context,
barriers, grandstands, wet-track reflections, and multiple-car scenes improve
ranking. MetaFusion recognizes car identity from Formula 1 event and chassis
categories as well as literal `F1 car` wording, so Commons files named only for a
team, driver, or chassis remain discoverable. Empty or aerial track views,
venue-only scenes, static cars, black-and-white or effectively monochrome photos,
safety and medical cars, testing, launch or display vehicles, models, and unrelated
event/series photographs are rejected. There is no atmosphere-only fallback. If
no exact-event or exact-circuit race-action source survives validation, MetaFusion
uses the already selected current-season team-car photograph as the explicit last
resort. A decoded, colour, safely licensed 3840×2160 landscape source is preferred.
If one is unavailable, MetaFusion accepts the previous 1600×900 source floor
without relaxing subject, environment, licence, colour, decoding, blank-image, or
sharpness validation. The output remains a clean 3840×2160 background, while
attribution records `4k-source` or `fallback-resolution-source` and the
`current_season_team_car_fallback` match tier makes degraded circuit specificity
visible. If even that source is unsuitable, existing safe artwork is preserved and
the unresolved round is reported for a later retry.
This is separate from the round/season posters described above and also enables
the episode cards described above.

The show poster uses a restrained broadcast layout with a subject-aware,
exposure-controlled team-car photograph, season/round header, event and circuit
text, floating host-country flag, and minimal accent bars. It never stretches or
mirrors the source. The separate 4K background is a full-bleed race-action image
with only gentle grading, highlight control, a subtle readability gradient, and
vignette; it contains no logo, title, border, or other graphic overlay.

Race environment is derived without a hard-coded circuit list. MetaFusion uses
the live schedule's race UTC time and circuit coordinates to estimate whether the
race is day, twilight, or night, then combines that with the official race/circuit,
locality/country, and validated circuit-profile traits such as street, harbour,
urban, desert, or floodlit. It also verifies the downloaded image's luminance so
a bright daytime image cannot satisfy a Singapore-like night race merely because
its description says “night”.

On the poster only, the circuit remains as text while a small rounded-rectangle
host-country flag replaces repeated locality/country wording. An unknown flag
falls back to country text. All rendered designs use the same supplied branding.

Rotation is not time-based. `show_artwork.trigger: plex_new_race` means a new
pair is selected only when MetaFusion successfully parses at least one episode
from a race-round season that is newer than the last applied round. Merely adding
an event to the public calendar does not rotate anything. Container restarts and
repeated scans of the same Plex inventory also do not rotate anything. For
example, adding the first valid episode under Plex Season 05 changes the current
show pair to Round 05; subsequent episodes added to Season 05 leave that pair
unchanged. The next rotation happens when a valid Season 06 episode appears.

The pair is written atomically to a versioned round/team/source directory before
YAML references change. Kometa later applies `file_poster` and `file_background`;
MetaFusion never edits Plex directly. Disabling `metadata.enabled` prevents new
references, while disabling only `show_artwork.enabled` leaves the last valid
references intact.

Poster and background maintenance are independent. A checksum-matching missing
or outdated poster can be rebuilt without replacing the background, and a
degraded background can be upgraded without repainting the poster. Manual changes
remain protected and are reported.

The constructor roster is discovered for the detected year. Show-poster and
episode bindings are stored in Formula 1 SQLite state and driven by Plex inventory,
not container uptime. New, renamed, or departing teams therefore do not require
an annual configuration edit.

If no eligible photograph exists, MetaFusion preserves the previous valid pair
and retries later rather than forcing an unrelated or unsafe image. Historical
race-action remains eligible only with exact-circuit evidence. Renderer or
branding changes may refresh checksum-matching active output without advancing
constructor rotation. Retention removes only inactive, byte-identical managed
versions and caches; protected output remains untouched.

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

Branding is checked at startup. Missing files produce clear fallback warnings.
A supplied but unreadable logo, invalid font, extremely small logo, or unsafe
oversized logo stops the run before output generation instead of failing
part-way through rendering.

The circuit outline attribution is maintained here as required by its licence:
“F1 circuits SVG”, ROY Jules, CC BY 4.0,
<https://github.com/julesr0y/f1-circuits-svg>.

Bundled national flag renders come from `hampusborgos/country-flags`, which
documents the flags as public domain:
<https://github.com/hampusborgos/country-flags>.

No test font or Formula 1 logo is included in the Docker image.

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

With a public Flickr key, team-car photographs use Flickr and then Wikimedia
Commons. Show backgrounds independently use race-aware Flickr and then race-aware
Commons discovery. Search terms discover candidates but never prove identity;
accepted results must independently establish the season, Formula 1 subject,
constructor or exact event/circuit, compatible licence, and author.

For backgrounds only, Flickr may also establish event context from several
independent weaker signals when a photograph omits the full event or circuit
name. The photo must still identify Formula 1 race-car action and must have a
provider capture date in the target season and within seven days of the scheduled
race. It must additionally have either a city-or-better geotag within 32 km of
the circuit or an explicit locality match. This is stored as
`composite_event_action_race_car` with its contributing evidence. Geolocation or
capture time alone is never sufficient, and this policy does not apply to show-
poster or episode constructor identity.

Background discovery prioritizes:

1. exact-event/circuit, current-season Formula 1 race action;
2. recent exact-circuit Formula 1 race action; then
3. historical exact-circuit Formula 1 race action, with newer files ranked higher;
   then
4. the validated 4K current-season team-car source selected for the show poster.

Historical candidates require exact-circuit evidence. Every downloaded file is
decoded and checked for dimensions, aspect ratio, non-blank visual content,
colour, sharpness, expected race environment, and perceptual duplication. Static
or display cars, safety/medical cars, models, portraits, empty tracks, unrelated
series, monochrome media, corrupt files, and incompatible licences are rejected.

Flickr-to-Commons fallback is end to end: Flickr search results that later fail
downloaded-image validation do not stop Commons evaluation. If both strict pools
fail, the current team-car source is explicitly marked as degraded and retried
after provider-cache expiry. A successful retry rewrites only the managed
background. The one-time command below can force fresh Flickr evaluation.

Race identity matching is schedule-derived and explainable. It considers the
official race name, country and demonym, locality, circuit name, and circuit ID,
requires an exact or high-confidence result, and stores accepted year/round
bindings in the private SQLite database. A changed scheduled identity invalidates
an older learned binding. This handles identities such as `Turkey`, `Türkiye`,
and `Turkish Grand Prix` while still rejecting a filename assigned to the wrong
round.

For every retained generated show pair and persistent episode-round source, the
TXT and JSON attribution reports record the asset scope, provider, source page,
title, author, licence, licence URL, vehicle/team, season, and trigger round. The
show poster's team-car source and show background's race-matched source are separate
records. Generated show destinations are also recorded. Old versioned pairs and
the sources behind historical episode cards remain covered by the report.
MetaFusion uses each original photograph as a deterministic compositing layer; it
does not regenerate, repaint, or distort the vehicle with AI.

Wikimedia's reuse and API guidance is available at
<https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia>
and <https://commons.wikimedia.org/wiki/Commons:API>.

## Optional Flickr setup and provider order

Create a non-commercial Flickr API key in Flickr's App Garden, then set either:

```yaml
providers:
  flickr_api_key: your-public-api-key
```

or the container variable `FORMULA1_FLICKR_API_KEY`. The environment variable has
priority. Do not configure or publish the API secret, an OAuth token, or another
person's project key. With no key, Flickr is disabled and existing Commons-only
behavior remains unchanged.

MetaFusion applies the provider in this order:

1. show background: strict Flickr event/circuit action, then strict Commons
   event/circuit action, then the selected licensed team-car photograph;
2. show poster: strict current-season Flickr team car, then strict Commons team
   car;
3. episode cards: the persistent validated team-car source bound to that round,
   which may be Flickr or Commons.

The Flickr licence registry is resolved dynamically instead of relying on
hard-coded numeric IDs. Automatic rendering accepts Public Domain, CC0, No Known
Copyright Restrictions, and attribution-only CC BY media. ShareAlike,
NonCommercial, NoDerivatives, All Rights Reserved, missing-author, unsafe-host,
and unknown-licence results are rejected. Each accepted Flickr page, author, and
licence is written to the normal attribution reports. MetaFusion uses Flickr's
API and static image hosts; it does not scrape search-result pages.

Flickr API and attribution guidance:
<https://www.flickr.com/services/api/>,
<https://www.flickr.com/services/api/flickr.photos.search.html>, and
<https://www.flickr.com/services/developer/attributions/>.
This product uses the Flickr API but is not endorsed or certified by SmugMug,
Inc.

## One-time artwork upgrade command

Adding a Flickr key does not silently repaint stable Commons bindings. Run an
explicit one-shot upgrade when you intentionally want to evaluate existing
extension-managed output against Flickr.

From the MetaFusion container console:

```bash
# Latest detected race's episode cards and the active show poster/background
metafusion --formula1-upgrade-artwork current

# Every detected race round's episode cards and the current show pair
metafusion --formula1-upgrade-artwork all
```

From the Docker host, replace `<container-name>` with the actual name:

```bash
docker exec -it <container-name> metafusion --formula1-upgrade-artwork current
```

Preview the same selection without writing artwork, YAML, SQLite state, logs, or
reports:

```bash
metafusion --dry_run --formula1-upgrade-artwork current
```

The command requires `RUN_MODE=kometa`, `FORMULA1_ENABLED=True`, and a configured
public Flickr API key. It enables a single MetaFusion run, disables scheduling
and core cleanup for that invocation, and refreshes Flickr background discovery
instead of waiting for normal cache expiry.

Team-car replacement retains the constructor already assigned to the round; it
does not change a Ferrari binding into McLaren merely because another photograph
scores higher. The background is evaluated independently for better event/circuit
specificity. A candidate must provide a meaningful decoded quality improvement,
a non-duplicate team-car image, or a stronger race-specific background without
an unacceptable quality regression.

Only missing or byte-identical extension-managed files are eligible. Manual,
modified, and unknown files are preserved. `current` checks the latest detected
round's episode cards and active show pair. `all` checks episode cards for every
detected round plus the active show pair; it does not repaint retained historical
show-pair versions.

A non-dry-run command records upgraded, already-Flickr, no-better-candidate,
unchanged, and preserved-manual decisions in `formula1.sqlite3` and writes:

```text
/config/formula1/reports/formula1-artwork-upgrade-<run-id>.txt
/config/formula1/reports/formula1-artwork-upgrade-<run-id>.json
```

The renderer and branding remain unchanged; only eligible source photography is
reconsidered. Repeating the command is idempotent when no materially better
candidate exists. Normal scheduled runs may automatically retry a background
marked as the degraded team-car fallback, but deliberate re-evaluation of stable
show-poster and episode-card bindings still requires this command.

## Cleanup and diagnostics

Cleanup is disabled by default. When enabled, a missing Plex episode must remain
absent for both `confirmation_scans` and `grace_hours`. Only extension-owned
state and byte-identical managed artwork are eligible. Formula 1 assets are never
passed to core MetaFusion cleanup.

The same confirmation and grace decision controls YAML reconciliation. A
temporarily missing episode or race stays authoritative in generated YAML until
its cleanup candidate becomes eligible; its state and owned round poster are
retained for the same period. After eligibility, YAML, binding state, and an
otherwise-unused byte-identical managed poster are reconciled together. If
cleanup is disabled, inventory absence alone never prunes generated records.

With `verification.enabled: true`, output-changing runs place an expectation in
the private SQLite database. After `verification.delay_hours` (default one
hour), a later run reads Plex without changing it and compares generated show,
season, and episode fields plus selected show, season, and episode artwork. Perceptually
equivalent Plex transcodes are accepted within
`verification.perceptual_distance`. Results are written to
`/config/formula1/reports/formula1-application-verification-<run-id>.json` and
retained according to `verification.retention`. A partial result means Kometa
has not applied every expected value or Plex currently selects different
artwork; it does not trigger a Plex edit or refresh.

`logging.console` accepts `off`, `summary`, or `full`. The default prints one
Formula 1 summary through the main logger while retaining item details in the
separate Formula 1 run log. The summary distinguishes resolved and missing
circuit facts from resolved and missing circuit profiles. Dry-run mode creates
no template, database, artwork, metadata, log, or report files.

## Recommended first run and soak test

1. Back up `/config`, the Kometa metadata directory, and existing Formula 1
   assets. Keep `cleanup.enabled: false`.
2. Confirm Plex already exposes one dedicated library, one championship show,
   race-round seasons, and correctly numbered episodes. Resolve duplicates and
   Season 0 content before enabling the extension.
3. Set `RUN_MODE=kometa` and `FORMULA1_ENABLED=True`, start one normal run to
   create the private template, then copy it to `formula1.yml` only if defaults
   need adjustment. Alternatively, copy the packaged template from this
   repository before the first run.
4. Add optional branding, run again, and confirm the branding validation lines,
   detected naming profile, championship year, schedule identity, fact/profile
   counts, and artwork actions in the separate Formula 1 log.
5. Inspect `formula1_<year>.yml`, generated artwork, the issues report when one is
   present, and both attribution reports before running Kometa.
6. Run Kometa and verify the Plex show, race seasons, and several programme types.
   Keep at least one practice, qualifying, Sprint when applicable, race, and
   analysis episode in the sample.
7. Allow a later MetaFusion run after `verification.delay_hours` and review the
   application-verification report. A Plex thumbnail cache may lag behind the
   underlying selected artwork; verify the item directly before treating a Home
   screen thumbnail as an application failure.
8. Repeat across a newly added episode in the same round and the first episode of
   a later round. The same round must retain its team-car source; the newer round
   may rotate the show pair and receives its own persistent episode binding.
9. Test one temporary provider outage or Plex disconnect. Existing YAML, managed
   artwork, and state should be preserved, with a reported retry rather than a
   destructive partial update.
10. Enable cleanup only after multiple successful scans and a reviewed dry run.

## Troubleshooting boundaries

- **Library not detected:** verify the exact `library.name`, Kometa mode, explicit
  extension opt-in, Plex connection, and that the dedicated library contains a
  year-bearing show title.
- **No metadata file:** confirm `metadata.enabled`, a supported non-Season-0
  episode, a valid filename, and a schedule match for its championship year and
  round. Calendar entries alone never create YAML.
- **Schedule pending:** the year was detected but the provider has not published
  a valid schedule. Existing output is preserved and later runs retry.
- **Race or episode rejected:** compare the filename's event, round, programme
  number, and programme label with the supported convention and Plex identity.
  The extension will not silently attach a conflicting event to a round.
- **Artwork preserved instead of changed:** inspect the detailed action and
  attribution report. The output may be manual/modified, the current pair may be
  newer, or no safely licensed current-season source may yet exist.
- **Kometa no longer finds the renamed show:** keep the stable `F1 <year>` mapping
  and generated `match.title` aliases. Do not manually replace the mapping key
  with only the current Plex display title.
- **Missing circuit facts or profile:** inspect the issue report for provider page
  identity or markup failure. Unavailable values are intentionally omitted rather
  than generated as `to be confirmed`.
- **Plex still shows an older thumbnail:** first run Kometa, then inspect the Plex
  item itself and the delayed verification report. Plex Home/Continue Watching
  thumbnails can remain cached after the selected item artwork changes.
- **SABnzbd job never appears:** troubleshoot the external NZB/RSS/category/script,
  completed path, permissions, and Plex scan first. MetaFusion starts only after
  Plex exposes the media and cannot repair an upstream missing download.

When reporting an extension problem, include the MetaFusion commit or image tag,
redacted Formula 1 log excerpt, naming profile, one representative media filename,
Plex show/season/episode identity, and the relevant redacted JSON report. Never
share Plex tokens, private RSS URLs, NZB contents, indexer credentials, or an
unredacted private configuration.
