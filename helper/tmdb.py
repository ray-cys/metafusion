import asyncio, json, hashlib, re, unicodedata
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
        CACHE_DIR / "tmdb_cache.sqlite3",
        ttl_hours=cache_config.get("ttl_hours", 24),
        max_entries=cache_config.get("max_entries", 5000),
        max_mb=cache_config.get("max_mb", 0),
        enabled=cache_config.get("enabled", True),
        writable=not config.get("settings", {}).get("dry_run", False),
    )


def flush_tmdb_cache():
    result = tmdb_response_cache.flush()
    stats = tmdb_response_cache.stats()
    log_tmdb_event("tmdb_cache_stats", **stats)
    if stats.get("health") == "degraded":
        log_tmdb_event(
            "tmdb_cache_degraded",
            error=stats.get("last_error") or "persistent cache unavailable",
        )
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
    max_response_bytes=None, include_locale=True, **kwargs,
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
        if language is None and include_locale:
            language = config.get("tmdb", {}).get("language", "en-US")
        if region is None and include_locale:
            region = config.get("tmdb", {}).get("region", "US")
        url = f"https://api.themoviedb.org/3/{endpoint_or_url}"
        query = {"api_key": api_key}
        params = dict(params or {})
        if include_locale and "language" not in params:
            params["language"] = language
        if include_locale and "region" not in params:
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
                        if response.status == 404:
                            # A missing resource is permanent for this identifier;
                            # retrying only delays recovery through other IDs.
                            return None
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
    title=None,
    year=None,
    session=None,
    excluded_ids=None,
):
    """Resolve a missing TMDb ID from IDs exposed by Plex legacy agents."""
    excluded_ids = {str(value) for value in (excluded_ids or set()) if value is not None}
    if tmdb_id and str(tmdb_id) not in excluded_ids:
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
        for match in matches:
            candidate_id = match.get("id")
            if candidate_id is not None and str(candidate_id) not in excluded_ids:
                return str(candidate_id)

    if not config.get("tmdb", {}).get("title_search_fallback", False) or not title:
        return None
    search_type = "movie" if normalized_type in {"movie", "movies"} else "tv"
    params = {"query": str(title), "include_adult": "false"}
    if year:
        params["year" if search_type == "movie" else "first_air_date_year"] = year
    result = await tmdb_api_request(
        config,
        f"search/{search_type}",
        params=params,
        session=session,
    )
    candidates = []
    wanted_title = normalize_title(title)
    wanted_year = _year_value(year)
    for candidate in result.get("results", []) if isinstance(result, dict) else []:
        names = (
            (candidate.get("title"), candidate.get("original_title"))
            if search_type == "movie"
            else (candidate.get("name"), candidate.get("original_name"))
        )
        candidate_year = _year_value(
            candidate.get("release_date")
            if search_type == "movie"
            else candidate.get("first_air_date")
        )
        if wanted_title not in {normalize_title(name) for name in names if name}:
            continue
        if wanted_year and candidate_year and wanted_year != candidate_year:
            continue
        if (
            candidate.get("id") is not None
            and str(candidate["id"]) not in excluded_ids
        ):
            candidates.append(str(candidate["id"]))
    return candidates[0] if len(set(candidates)) == 1 else None


def normalize_title(value):
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _year_value(value):
    match = re.match(r"^(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def tmdb_identity_consistent(media_type, title, year, details):
    """Reject strong TMDb identity conflicts while tolerating translated titles."""
    normalized_type = str(media_type or "").lower()
    date_value = (
        details.get("release_date")
        if normalized_type in {"movie", "movies"}
        else details.get("first_air_date")
    )
    expected_year = _year_value(year)
    actual_year = _year_value(date_value)
    if expected_year and actual_year and abs(expected_year - actual_year) > 1:
        return False, f"year mismatch ({expected_year} vs {actual_year})"
    names = (
        (details.get("title"), details.get("original_title"))
        if normalized_type in {"movie", "movies"}
        else (details.get("name"), details.get("original_name"))
    )
    expected_title = normalize_title(title)
    if expected_title and not any(normalize_title(name) == expected_title for name in names if name):
        return True, "title differs from localized/original TMDb names"
    return True, "matched"


async def tmdb_unfiltered_images(
    config, media_type, tmdb_id, session=None, season_number=None
):
    normalized_type = "tv" if str(media_type).lower() in {"tv", "show"} else "movie"
    endpoint = f"{normalized_type}/{tmdb_id}"
    if season_number is not None:
        endpoint += f"/season/{int(season_number)}"
    return await tmdb_api_request(
        config,
        f"{endpoint}/images",
        params={},
        include_locale=False,
        session=session,
    )


def _inventory_pairs(inventory):
    return {
        (int(season), int(episode))
        for season, episodes in (inventory or {}).items()
        for episode in episodes
    }


async def resolve_episode_group_mapping(
    config,
    tmdb_id,
    plex_inventory,
    episode_ordering=None,
    session=None,
):
    """Return a TMDb episode-group mapping only when one layout is unambiguous."""
    if not config.get("tmdb", {}).get("episode_group_fallback", True):
        return None
    wanted = _inventory_pairs(plex_inventory)
    if not wanted:
        return None
    listing = await tmdb_api_request(
        config, f"tv/{tmdb_id}/episode_groups", session=session
    )
    descriptors = listing.get("results", []) if isinstance(listing, dict) else []
    preferred_types = {
        "tmdb_aired": 1,
        "tvdb_aired": 1,
        "tvdb_absolute": 2,
        "absolute": 2,
        "tvdb_dvd": 3,
        "dvd": 3,
    }
    preferred = preferred_types.get(str(episode_ordering or "").lower())
    if preferred and any(item.get("type") == preferred for item in descriptors):
        descriptors = [item for item in descriptors if item.get("type") == preferred]

    matches = {}
    for descriptor in descriptors[:10]:
        group_id = descriptor.get("id")
        if not group_id:
            continue
        details = await tmdb_api_request(
            config, f"tv/episode_group/{group_id}", session=session
        )
        groups = details.get("groups", []) if isinstance(details, dict) else []
        for season_offset in (0, 1):
            for episode_offset in (0, 1):
                candidate = {}
                season_context = {}
                duplicate = False
                for group_index, group in enumerate(groups):
                    group_order = group.get("order", group_index)
                    try:
                        target_season = int(group_order) + season_offset
                    except (TypeError, ValueError):
                        duplicate = True
                        break
                    season_context[target_season] = {
                        "title": group.get("name") or "",
                        "summary": group.get("description") or "",
                    }
                    for episode_index, episode in enumerate(group.get("episodes", [])):
                        order = episode.get("order", episode_index)
                        try:
                            target_episode = int(order) + episode_offset
                        except (TypeError, ValueError):
                            duplicate = True
                            break
                        key = (target_season, target_episode)
                        if key in candidate:
                            duplicate = True
                            break
                        candidate[key] = episode
                    if duplicate:
                        break
                if duplicate or not wanted <= set(candidate):
                    continue
                selected = {key: candidate[key] for key in wanted}
                signature = tuple(
                    sorted((key, value.get("id")) for key, value in selected.items())
                )
                matches[signature] = {
                    "group_id": group_id,
                    "episodes": selected,
                    "seasons": season_context,
                }
    return next(iter(matches.values())) if len(matches) == 1 else None
