import asyncio, json, hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from aiolimiter import AsyncLimiter
from helper.config import CACHE_DIR
from helper.logging import log_tmdb_event, redact_secrets
from helper.tmdb_cache import tmdb_response_cache

_tmdb_limiter = None
_tmdb_limiter_loop = None
_CACHE_MISS = object()


def get_tmdb_limiter():
    """Return a limiter bound to the current job's event loop."""
    global _tmdb_limiter, _tmdb_limiter_loop
    loop = asyncio.get_running_loop()
    if _tmdb_limiter is None or _tmdb_limiter_loop is not loop:
        _tmdb_limiter = AsyncLimiter(40, 10)
        _tmdb_limiter_loop = loop
    return _tmdb_limiter


def begin_tmdb_cache(config):
    cache_config = config.get("tmdb_cache", {})
    tmdb_response_cache.configure(
        CACHE_DIR / "tmdb_response_cache.sqlite3",
        ttl_hours=cache_config.get("ttl_hours", 24),
        max_entries=cache_config.get("max_entries", 5000),
        max_mb=cache_config.get("max_mb", 0),
        enabled=cache_config.get("enabled", True),
        writable=not config.get("settings", {}).get("dry_run", False),
    )


def flush_tmdb_cache():
    result = tmdb_response_cache.flush()
    log_tmdb_event("tmdb_cache_stats", **tmdb_response_cache.stats())
    return result


def artwork_language_codes(config):
    """Return TMDb image languages without changing the metadata language."""
    tmdb_config = config.get("tmdb", {})
    fallback = tmdb_config.get("fallback", [])
    if isinstance(fallback, str):
        fallback = [fallback]
    candidates = [tmdb_config.get("language", "en-US"), *(fallback or []), "null"]
    languages = []
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value.lower() != "null":
            value = value.split("-", 1)[0].lower()
        else:
            value = "null"
        if value and value not in languages:
            languages.append(value)
    return ",".join(languages)


class ResponseTooLargeError(RuntimeError):
    pass


async def _read_limited(response, max_bytes):
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ResponseTooLargeError(
                    f"response Content-Length exceeds {max_bytes} bytes"
                )
        except ValueError:
            pass

    chunks = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)

def _redact_query(query):
    sensitive_keys = {"api_key", "access_token", "token"}
    return {
        key: "***" if key.lower() in sensitive_keys else value
        for key, value in query.items()
    }


def _redact_url(url):
    parts = urlsplit(url)
    if not parts.query:
        return url
    redacted_query = [
        (key, "***" if key.lower() in {"api_key", "access_token", "token"} else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(redacted_query), parts.fragment)
    )

async def tmdb_api_request(
    config, endpoint_or_url, params=None, retries=3, delay=2, backoff_factor=2, api_key=None,
    language=None, region=None, cache=True, raw=False, session=None,
    max_response_bytes=None, **kwargs,
):
    if endpoint_or_url.startswith("http"):
        url = endpoint_or_url
        query = dict(params or {})
        cache_key = f"{url}:{json.dumps(query, sort_keys=True)}"
    else:
        if api_key is None:
            api_key = config.get("tmdb", {}).get("api_key")
            if not api_key:
                tmdb_config = config.get("tmdb", {})
                log_tmdb_event(
                    "tmdb_no_api_key",
                    tmdb_config={
                        "language": tmdb_config.get("language"),
                        "region": tmdb_config.get("region"),
                    },
                )
        if language is None:
            language = config.get("tmdb", {}).get("language", "en-US")
        if region is None:
            region = config.get("tmdb", {}).get("region", "US")
        url = f"https://api.themoviedb.org/3/{endpoint_or_url}"
        query = {"api_key": api_key}
        params = dict(params or {})
        if "language" not in params:
            params["language"] = language
        if "region" not in params:
            params["region"] = region
        query.update(params)
        cache_key = f"{url}:{json.dumps(query, sort_keys=True)}"

    logged_query = _redact_query(query)
    logged_url = _redact_url(url)
    sensitive_values = [
        value
        for key, value in query.items()
        if key.lower() in {"api_key", "access_token", "token"} and value
    ]
    if max_response_bytes is None:
        max_image_mb = config.get("runtime", {}).get("max_image_mb", 25)
        max_response_bytes = max(1, int(max_image_mb)) * 1024 * 1024
    if session is None:
        log_tmdb_event("tmdb_failed", retries=retries, url=logged_url, query=logged_query)
        return {}

    cache_hash = hashlib.sha256(cache_key.encode()).hexdigest()
    cached_response = (
        tmdb_response_cache.get(cache_hash, _CACHE_MISS) if cache else _CACHE_MISS
    )
    if cached_response is not _CACHE_MISS:
        log_tmdb_event("tmdb_cache_hit", url=logged_url, params=logged_query)
        return cached_response

    for attempt in range(1, retries + 1):
        rate_limit_waited = False
        try:
            log_tmdb_event("tmdb_request", url=logged_url, query=logged_query, attempt=attempt, retries=retries)
            async with get_tmdb_limiter():
                async with session.get(url, params=query, **kwargs) as response:
                    if response.status == 200:
                        if raw:
                            data = await _read_limited(response, max_response_bytes)
                        else:
                            data = await response.json()
                        if cache:
                            tmdb_response_cache[cache_hash] = data
                        log_tmdb_event("tmdb_success", url=logged_url, attempt=attempt)
                        return data
                    elif response.status == 429:
                        retry_after = min(
                            60,
                            max(0, int(response.headers.get("Retry-After", delay))),
                        )
                        log_tmdb_event("tmdb_rate_limited", retry_after=retry_after, query=logged_query)
                        await asyncio.sleep(retry_after)
                        rate_limit_waited = True
                    else:
                        body = redact_secrets(await response.text(), *sensitive_values)
                        log_tmdb_event("tmdb_non_200", status=response.status, url=logged_url, query=logged_query, body=body[:500])
        except ResponseTooLargeError as e:
            log_tmdb_event(
                "tmdb_response_too_large",
                url=logged_url,
                query=logged_query,
                error=e,
            )
            return None
        except Exception as e:
            log_tmdb_event(
                "tmdb_request_failed",
                attempt=attempt,
                url=logged_url,
                query=logged_query,
                error=redact_secrets(e, *sensitive_values),
            )
        if attempt < retries and not rate_limit_waited:
            sleep_time = delay * (backoff_factor ** (attempt - 1))
            log_tmdb_event("tmdb_retrying", sleep_time=sleep_time, next_attempt=attempt + 1, retries=retries)
            await asyncio.sleep(sleep_time)
    log_tmdb_event("tmdb_failed", retries=retries, url=logged_url, query=logged_query)
    return None


async def resolve_tmdb_id(
    config,
    media_type,
    tmdb_id=None,
    imdb_id=None,
    tvdb_id=None,
    session=None,
):
    """Resolve a missing TMDb ID from IDs exposed by Plex legacy agents."""
    if tmdb_id:
        return str(tmdb_id)
    normalized_type = str(media_type or "").lower()
    candidates = []
    if imdb_id:
        candidates.append((str(imdb_id), "imdb_id"))
    if normalized_type in {"tv", "show", "shows"} and tvdb_id:
        candidates.append((str(tvdb_id), "tvdb_id"))
    result_key = "movie_results" if normalized_type in {"movie", "movies"} else "tv_results"
    for external_id, source in candidates:
        result = await tmdb_api_request(
            config,
            f"find/{external_id}",
            params={"external_source": source},
            session=session,
        )
        matches = result.get(result_key, []) if isinstance(result, dict) else []
        if matches and matches[0].get("id") is not None:
            return str(matches[0]["id"])
    return None
