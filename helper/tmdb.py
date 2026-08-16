import asyncio, json, hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from aiolimiter import AsyncLimiter
from helper.config import CACHE_DIR
from helper.logging import log_tmdb_event, redact_secrets
from helper.tmdb_cache import tmdb_response_cache

tmdb_limiter = AsyncLimiter(40, 10)


def begin_tmdb_cache(config):
    cache_config = config.get("tmdb_cache", {})
    tmdb_response_cache.configure(
        CACHE_DIR / "tmdb_response_cache.json",
        ttl_hours=cache_config.get("ttl_hours", 24),
        max_entries=cache_config.get("max_entries", 5000),
        enabled=cache_config.get("enabled", True),
        writable=not config.get("settings", {}).get("dry_run", False),
    )


def flush_tmdb_cache():
    return tmdb_response_cache.flush()


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
            language = config.get("tmdb", {}).get("language", "en")
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
    if cache and cache_hash in tmdb_response_cache:
        log_tmdb_event("tmdb_cache_hit", url=logged_url, params=logged_query)
        return tmdb_response_cache[cache_hash]

    for attempt in range(1, retries + 1):
        rate_limit_waited = False
        try:
            log_tmdb_event("tmdb_request", url=logged_url, query=logged_query, attempt=attempt, retries=retries)
            async with tmdb_limiter:
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
