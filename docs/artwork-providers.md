# Artwork providers and fallback behavior

MetaFusion uses one deterministic source order for movie posters, show posters,
season posters, and backgrounds in both Kometa and Plex modes:

1. TMDb candidates in the configured artwork languages, followed by TMDb's
   unfiltered images when `ARTWORK_ALLOW_ANY_LANGUAGE=True`.
2. Fanart.tv using MetaFusion's bundled project integration key.
3. The current artwork exposed by the configured Plex server.
4. The highest-scoring TMDb or Fanart.tv candidate below the preferred minimum
   dimensions, labelled `best available`.
5. If the destination file is missing and every standard stage is empty,
   automatically reconsider the remaining TMDb and Fanart.tv candidates with
   the artwork-language preference relaxed. This is labelled `automatic
   missing-only relaxation`.
6. Preserve an existing destination and add a report entry, or report missing
   artwork when no candidate exists.

Provider priority is evaluated before quality scoring. A lower-priority source
does not replace an acceptable higher-priority candidate simply because its
score is higher. `ASSET_UPDATE_POLICY` still decides whether the selected
candidate may replace an existing file.

The automatic relaxed stage is deliberately narrower than `overwrite`. It is
eligible only when the computed destination does not exist. It can create a
missing poster, season poster, or background, but it cannot replace manual or
otherwise existing artwork—even when `ASSET_UPDATE_POLICY=overwrite`. A second
write-time guard protects against a file appearing after selection. It needs no
inbox, override file, or additional configuration.

## Fanart.tv integration and attribution

Fanart.tv supplies artwork only. It never changes TMDb identity
matching or metadata fields. MetaFusion bundles its application project key,
so users do not configure a Fanart.tv project key, environment variable, or
secret file. The fallback is available automatically in official images and
source builds. A missing or rejected bundled credential disables only
Fanart.tv for that run; processing continues to Plex and preservation.

The bundled project key identifies MetaFusion to Fanart.tv. It is not a user
account credential and does not grant access to a user's Fanart.tv account.
MetaFusion does not currently accept personal Fanart.tv client keys.

Use of Fanart.tv is subject to the [Fanart.tv terms and
conditions](https://fanart.tv/terms-and-conditions/) and [API
documentation](https://api.fanart.tv/). Project-tier access can receive newly
approved artwork later than personal-key access. MetaFusion is not affiliated
with or endorsed by Fanart.tv, TMDb, or Plex. Artwork remains subject to its
respective owner and provider terms.

## Reliability and safety

Fanart.tv has its own bounded concurrency lane, rate limiter, circuit breaker,
positive and short negative SQLite caching, response-size limit, JSON
validation, and retry handling. HTTP 429 respects `Retry-After`; 401/403
disables Fanart.tv for the rest of the run; 404 is cached briefly; transient
5xx/network failures use bounded retries. A provider failure never deletes an
existing destination.

Provider URLs are restricted before download. Fanart.tv downloads require
HTTPS and a Fanart.tv host. Plex artwork must use the exact configured Plex
scheme and server, with the Plex token sent as a header rather than in the URL.
Redirects are rejected.

For TV seasons, MetaFusion uses the season thumbnail already exposed on Plex
episode records as its fast path. If a present season has no such thumbnail,
it reads Plex season objects once and caches their explicit thumbnails.
Season artwork is evaluated only for seasons present in the authoritative Plex
inventory; a future or unrelated TMDb season does not create a false missing
warning. Specials use Season 0 when Season 0 exists in Plex. Split-series
mappings retain the Plex destination season while querying TMDb and Fanart.tv
with the configured source season.

Fallback advances because a provider has no candidate, its candidate does not
meet the configured hard dimensions, or a selected download/decoded image is
unusable. Download-time failover is strictly missing-only: after bounded
retries, MetaFusion can continue through later providers only while the final
destination is still absent. An outage never replaces an existing/manual file
or changes the provider for an upgrade already installed at that destination.

Every successful response is decoded before it is installed. MetaFusion
rejects unsupported formats, implausible aspect ratios, provider dimensions
that differ materially from the downloaded image, and images detected as
effectively blank. The validated width, height, format, content checksum,
sharpness signal, blank result, and perceptual hash are cached in durable
SQLite by provider/source. Later candidate scoring can use the cached
sharpness signal, reject known blank content, and explain visually duplicate
candidates without downloading them again. Provider order and ownership
policy remain stronger than the content score.

## Logs and diagnostics

Changed item logs identify `Source=TMDb`, `Source=Fanart.tv`, or `Source=Plex`
and distinguish the Kometa-assets or Plex-local-media target. Preserved output
uses `Source=Existing`; a missing outcome uses `Source=None`. Season warnings
name missing season numbers and report whether TMDb and Fanart.tv had no
candidates and whether Plex exposed an explicit season thumbnail. The final
summary separates writes, adoption, unchanged, not-due, preserved, missing,
deferred, and failed outcomes and counts successful writes/adoptions by
provider. It also counts successful `Automatic relaxation` writes and reports
missing-only transport recovery separately as `Download failover`. Detailed
request/cache activity is available at `LOG_LEVEL=DEBUG`;
authorization, rate limiting, malformed responses, and provider exhaustion are
warnings.

`--asset-audit`, `--library-audit`, and `--plan` reports show the selected
provider, provider image ID when available, selection reason, candidate count,
and provider stages attempted, including the missing-only relaxed stage when it
is eligible. `--explain-item` reports the saved provider and
destinations but does not perform live candidate scoring. When every stage
fails, `/config/reports/artwork-gaps-*.txt` retains the bounded
missing/preserved entry. Reports never include API keys.
