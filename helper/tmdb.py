import asyncio, json, hashlib
from aiolimiter import AsyncLimiter
from helper.logging import log_tmdb_event

tmdb_response_cache = {}
tmdb_limiter = AsyncLimiter(40, 10)

def _redact_query(query):
    sensitive_keys = {"api_key", "access_token", "token"}
    return {
        key: "***" if key.lower() in sensitive_keys else value
        for key, value in query.items()
    }

async def tmdb_api_request(
    config, endpoint_or_url, params=None, retries=3, delay=2, backoff_factor=2, api_key=None,
    language=None, region=None, cache=True, raw=False, session=None, **kwargs,
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
    if session is None:
        log_tmdb_event("tmdb_failed", retries=retries, url=url, query=logged_query)
        return {}

    cache_hash = hashlib.sha256(cache_key.encode()).hexdigest()
    if cache and cache_hash in tmdb_response_cache:
        log_tmdb_event("tmdb_cache_hit", url=url, params=logged_query)
        return tmdb_response_cache[cache_hash]

    for attempt in range(1, retries + 1):
        try:
            log_tmdb_event("tmdb_request", url=url, query=logged_query, attempt=attempt, retries=retries)
            async with tmdb_limiter:
                async with session.get(url, params=query, **kwargs) as response:
                    if response.status == 200:
                        if raw:
                            data = await response.read()
                        else:
                            data = await response.json()
                        if cache:
                            tmdb_response_cache[cache_hash] = data
                        log_tmdb_event("tmdb_success", url=url, attempt=attempt)
                        return data
                    elif response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", delay))
                        log_tmdb_event("tmdb_rate_limited", retry_after=retry_after, query=logged_query)
                        await asyncio.sleep(retry_after)
                    else:
                        body = await response.text()
                        log_tmdb_event("tmdb_non_200", status=response.status, url=url, query=logged_query, body=body[:500])
        except Exception as e:
            log_tmdb_event("tmdb_request_failed", attempt=attempt, url=url, query=logged_query, error=e)
        if attempt < retries:
            sleep_time = delay * (backoff_factor ** (attempt - 1))
            log_tmdb_event("tmdb_retrying", sleep_time=sleep_time, next_attempt=attempt + 1, retries=retries)
            await asyncio.sleep(sleep_time)
    log_tmdb_event("tmdb_failed", retries=retries, url=url, query=logged_query)
    return None
