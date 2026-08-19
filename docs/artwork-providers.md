# Artwork providers and fallback behavior

MetaFusion uses one deterministic source order for movie posters, show posters,
season posters, and backgrounds in both Kometa and Plex modes:

1. TMDb candidates in the configured artwork languages, followed by TMDb's
   unfiltered images when `ARTWORK_ALLOW_ANY_LANGUAGE=True`.
2. Fanart.tv when `FANART_PROJECT_API_KEY` or its secret file is configured.
3. The current artwork exposed by the configured Plex server.
4. The highest-scoring TMDb or Fanart.tv candidate below the preferred minimum
   dimensions, labelled `best available`.
5. Preserve an existing destination and add a report entry, or report missing
   artwork when no destination exists.

Provider priority is evaluated before quality scoring. A lower-priority source
does not replace an acceptable higher-priority candidate simply because its
score is higher. `ASSET_UPDATE_POLICY` still decides whether the selected
candidate may replace an existing file.

## Fanart.tv configuration and attribution

Fanart.tv is optional and supplies artwork only. It never changes TMDb identity
matching or metadata fields. Configure a project key as a secret:

```text
FANART_PROJECT_API_KEY=your-project-key
```

For Docker secrets, mount a one-line file and set:

```text
FANART_PROJECT_API_KEY_FILE=/run/secrets/fanart_project_key
```

The direct environment value wins when both are set. Never commit or post the
key in screenshots, logs, support reports, or issues. MetaFusion redacts it
from normal exception and provider logging, but container administrators can
still inspect environment values.

Use of Fanart.tv is subject to the [Fanart.tv terms and
conditions](https://fanart.tv/terms-and-conditions/) and [API
documentation](https://api.fanart.tv/). Project keys can receive newly approved
artwork later than personal API access. MetaFusion is not affiliated with or
endorsed by Fanart.tv, TMDb, or Plex. Artwork remains subject to its respective
owner and provider terms.

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

Fallback advances because a provider has no candidate or its candidate does
not meet the configured hard dimensions. It does not automatically switch an
entire run to another provider after a selected image suffers a transient
download outage: the selected source is retried, the destination is preserved,
and the failure is reported. This avoids widespread source churn during an
upstream outage.

## Logs and diagnostics

Changed item logs identify `Source=TMDb`, `Source=Fanart.tv`, or `Source=Plex`.
The final summary counts successful artwork writes by provider and includes
only libraries processed in the run. Detailed request/cache activity is
available at `LOG_LEVEL=DEBUG`; authorization, rate limiting, malformed
responses, and provider exhaustion are warnings.

`--asset-audit`, `--library-audit`, `--plan`, and item explanation reports show
the selected provider, provider image ID when available, selection reason,
candidate count, and the provider stages attempted. When every stage fails,
`/config/reports/artwork-gaps-*.txt` retains the bounded missing/preserved
entry. Reports never include API keys.
