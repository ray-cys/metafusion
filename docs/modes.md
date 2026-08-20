# Kometa and Plex operation modes

`RUN_MODE` selects where MetaFusion writes its output. Both modes inventory the
selected Plex libraries, resolve identities through Plex/TMDb data, use the
same guarded artwork provider chain, and never modify video or audio files.

| Goal | Required setting | Output | How Plex changes |
| --- | --- | --- | --- |
| Generate files for Kometa | `RUN_MODE=kometa` | YAML under `/kometa/metadata` and artwork under `/kometa/assets` | Kometa applies the generated files during a later Kometa run. MetaFusion never edits Plex metadata directly. |
| Use Plex local artwork only | `RUN_MODE=plex` and `PLEX_METADATA_UPDATES=False` | Artwork beside mapped media files; no Kometa YAML | Plex discovers local artwork. MetaFusion does not edit metadata fields. |
| Use local artwork and cautious API enrichment | `RUN_MODE=plex` and `PLEX_METADATA_UPDATES=True` | Local artwork plus selected direct Plex API updates; no Kometa YAML | MetaFusion applies only supported fields allowed by the selected metadata policy. |

Artwork and metadata controls are independent. Plex mode can run with every
artwork option disabled and direct metadata enabled, or with metadata updates
disabled and artwork enabled. MetaFusion does not generate episode artwork in
either mode.

## Kometa mode

```text
Plex inventory + TMDb -> MetaFusion -> Kometa YAML/assets -> Kometa -> Plex
```

- `RUN_BASIC=True` generates supported core movie, show, season, and episode
  metadata.
- `RUN_ENHANCED=True` adds Kometa-supported director, writer, and producer
  fields. Cast and character roles remain with Plex's online provider.
- `RUN_POSTER`, `RUN_SEASON`, and `RUN_BACKGROUND` control generated artwork.
- Deleting either generated metadata file causes the next eligible run to
  rebuild it when `RUN_BASIC=True`.
- Unknown/manual YAML fields and known-good output are preserved when a
  partial provider request fails.

Kometa mode writes:

```text
/kometa/metadata/movie_metadata.yml
/kometa/metadata/tv_metadata.yml
/kometa/metadata/.metafusion-backups/*.bak
/kometa/assets/movie/...
/kometa/assets/tv/...
```

Kometa must be configured to read those metadata and asset directories and run
after MetaFusion. MetaFusion validates its generated YAML against the supported
[Kometa metadata contract](https://kometa.wiki/en/latest/files/metadata/), but
it does not start Kometa. `PLEX_METADATA_UPDATES=True` is invalid in Kometa
mode.

After Kometa runs, `python metafusion.py --kometa-application-audit` can compare
the generated fields and managed artwork with live Plex. This is a read-only
application audit, not a Kometa launcher or Plex refresh. See
[Diagnostics](diagnostics.md#post-kometa-application-verification).

## Plex mode

```text
Plex inventory + TMDb -> MetaFusion -> local artwork and/or Plex API -> Plex
```

- MetaFusion never creates or updates Kometa YAML.
- Local artwork is written beside mapped media with Plex-compatible names.
- `PLEX_PATH_MAPPINGS` translates paths returned by Plex to container paths;
  matching writable Docker/Unraid mappings are still required.
- Direct Plex metadata enrichment remains disabled unless
  `PLEX_METADATA_UPDATES=True`.
- Successful API edits appear immediately. Local artwork appears when Plex
  discovers it according to the library's local-assets and scan behavior.
- MetaFusion does not trigger a metadata refresh because a refresh could
  replace unlocked values supplied by Plex's online provider.

Direct enrichment supports only the documented field allowlist and leaves
external IDs, cast/roles, ratings, labels, collections, playback data, extras,
recommendations, and provider-specific artwork choices to Plex or the user.
Start with `PLEX_METADATA_POLICY=fill_missing`, a low write cap, and a targeted
dry run. See [direct Plex metadata policies](policies.md#direct-plex-metadata-policies).
Plex documents its supported filenames in
[Local Media Assets – TV Shows](https://support.plex.tv/articles/200220717-local-media-assets-tv-shows/).

## Artwork behavior in both modes

MetaFusion selects movie/show posters, movie/show backgrounds, and season
posters through TMDb, Fanart.tv, Plex, and best-available preservation rules.
The selected mode changes the destination, not the source order or quality
policy:

- Kometa destinations are below `/kometa/assets`.
- Plex destinations are local artwork files beside mapped media.

`ASSET_UPDATE_POLICY` decides whether an existing destination may be replaced.
The [policy guide](policies.md#artwork-update-policies) explains
`fill_missing`, `managed`, and `overwrite`; the
[provider guide](artwork-providers.md) explains fallback and scoring.

## Cleanup and switching modes

Cleanup is mode-aware and disabled by default:

- Kometa mode can reconcile stale generated YAML and checksum-verified owned
  artwork after a complete successful inventory.
- Plex mode removes stale MetaFusion state only. It never deletes local
  artwork, Kometa output, or media files.

Switching to Plex mode therefore does not remove existing Kometa files, but it
stops creating or updating them. Switching back to Kometa mode resumes the
Kometa workflow on the next eligible run. Review the
[cleanup safety checklist](policies.md#cleanup-and-deletion-safety) before
enabling `RUN_CLEANUP`.

## Path requirements

| Mode | Required writable paths |
| --- | --- |
| Kometa | `/config` and the configured `/kometa` output mapping |
| Plex artwork | `/config` and mapped media destinations translated from Plex paths |
| Plex metadata only with all artwork disabled | `/config`; writable media mappings are not required |

Use the platform-specific [Docker Compose](docker-compose.md) or
[Unraid](unraid.md) guide for path examples and ownership requirements. Run
`python metafusion.py --preflight` before the first real job.
