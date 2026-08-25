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
was used:

```text
F1 2026/Season 01/S01E01 - Australia Grand Prix - Weekend.Warm-Up.mkv
Formula/Season 2018/01 - Australian GP/01x01 - Australian GP - Free Practice 1.mkv
```

Filename season numbers are the race round and episode numbers are the program
slot. The parser validates both against Plex. Ambiguous or duplicate logical
identities are reported and excluded. Season 0 is ignored by default; set
`testing.include: true` to include testing programs.

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
when available and fall back to the race date. The extension ignores unrelated
Kometa metadata files.

Artwork is deterministic. It combines schedule/circuit facts, a translucent
host-country flag and colour gradient, circuit outline, race title, circuit,
locality, weekend date, and a Sprint marker. Public-domain flag assets are bundled
for current and historical Formula 1 host countries, so rendering does not add a
runtime network dependency. An unknown country falls back to the neutral gradient.
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

Schedule and venue facts come from the Jolpica F1 API. Circuit outlines come from
the open `julesr0y/f1-circuits-svg` project (CC BY 4.0). Provider responses are
validated, cached in the extension database, retried, and may use an explicitly
logged stale cache during a temporary outage. Missing schedule identity prevents
that round from being written rather than guessing.

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
