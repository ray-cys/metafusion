import asyncio
import hashlib
import json
import re
import unicodedata
import weakref
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiolimiter import AsyncLimiter

from helper.concurrency import CircuitOpenError, runtime_slot
from helper.config import CACHE_DIR
from helper.logging import log_tmdb_event, redact_secrets
from helper.performance import tracker_for
from helper.tmdb_cache import tmdb_response_cache

_tmdb_limiter = None
_tmdb_limiter_loop = None
_CACHE_MISS = object()
_NEGATIVE_STATUS_KEY = "__metafusion_negative_http_status__"
_inflight_requests = weakref.WeakKeyDictionary()


def _change_refresh_requested(config, endpoint_or_url):
    """Identify detail endpoints selected by the current TMDb change window."""
    if str(endpoint_or_url).startswith("http"):
        return False
    parts = str(endpoint_or_url).strip("/").split("/")
    if len(parts) < 2 or parts[0] not in {"movie", "tv"}:
        return False
    configured = config.get("_tmdb_refresh_ids", {})
    return str(parts[1]) in {
        str(value) for value in configured.get(parts[0], []) if value is not None
    }


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
        max_entries=cache_config.get("max_entries", 0),
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
    max_response_bytes=None, include_locale=True, refresh_cache=None,
    _coalesced_owner=False, **kwargs,
):
    performance = tracker_for(config)
    if refresh_cache is None:
        refresh_cache = _change_refresh_requested(config, endpoint_or_url)
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
        tmdb_response_cache.get(cache_hash, _CACHE_MISS)
        if cache and not refresh_cache
        else _CACHE_MISS
    )
    if cached_response is not _CACHE_MISS:
        if performance:
            performance.increment("tmdb_cache_hits")
        if (
            isinstance(cached_response, dict)
            and cached_response.get(_NEGATIVE_STATUS_KEY) == 404
        ):
            log_tmdb_event(
                "tmdb_negative_cache_hit", url=logged_url, params=logged_query
            )
            return None
        log_tmdb_event("tmdb_cache_hit", url=logged_url, params=logged_query)
        return cached_response
    option_hash = hashlib.sha256(
        json.dumps(kwargs, sort_keys=True, default=str).encode()
    ).hexdigest()
    request_identity = (
        f"{cache_hash}:{int(bool(cache))}:{int(bool(refresh_cache))}:"
        f"{int(bool(raw))}:{max_response_bytes}:{option_hash}"
    )
    if not _coalesced_owner:
        loop = asyncio.get_running_loop()
        inflight = _inflight_requests.setdefault(loop, {})
        existing = inflight.get(request_identity)
        if existing is not None:
            if performance:
                performance.increment("tmdb_coalesced_waits")
            log_tmdb_event("tmdb_request_coalesced", url=logged_url)
            return await asyncio.shield(existing)
        task = asyncio.create_task(
            tmdb_api_request(
                config,
                endpoint_or_url,
                params=dict(params or {}),
                retries=retries,
                delay=delay,
                backoff_factor=backoff_factor,
                api_key=api_key,
                language=language,
                region=region,
                cache=cache,
                raw=raw,
                session=session,
                max_response_bytes=max_response_bytes,
                include_locale=include_locale,
                refresh_cache=refresh_cache,
                _coalesced_owner=True,
                **kwargs,
            )
        )
        inflight[request_identity] = task

        def clear_inflight(completed):
            current = _inflight_requests.get(loop)
            if current is not None and current.get(request_identity) is completed:
                current.pop(request_identity, None)

        task.add_done_callback(clear_inflight)
        return await asyncio.shield(task)
    if cache and performance:
        performance.increment("tmdb_cache_misses")

    for attempt in range(1, retries + 1):
        rate_limit_waited = False
        retry_after = 0
        try:
            log_tmdb_event("tmdb_request", url=logged_url, query=logged_query, attempt=attempt, retries=retries)
            if performance:
                performance.increment("tmdb_requests")
            async with get_tmdb_limiter():
                async with runtime_slot(config, "tmdb") as concurrency:
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
                        if response.status == 429:
                            retry_after = min(
                                60,
                                max(0, int(response.headers.get("Retry-After", delay))),
                            )
                            concurrency.failure(
                                "rate_limit",
                                cooldown=retry_after,
                            )
                            log_tmdb_event("tmdb_rate_limited", retry_after=retry_after, query=logged_query)
                            if performance:
                                performance.increment("tmdb_rate_limits")
                                performance.increment(
                                    "tmdb_rate_limit_wait_seconds", retry_after
                                )
                            rate_limit_waited = True
                        else:
                            if response.status >= 500:
                                concurrency.failure("server_error")
                            elif response.status in {401, 403}:
                                concurrency.failure("authorization_error")
                            body = redact_secrets(await response.text(), *sensitive_values)
                            log_tmdb_event("tmdb_non_200", status=response.status, url=logged_url, query=logged_query, body=body[:500])
                            if response.status == 404:
                                if cache:
                                    negative_hours = max(
                                        0.1,
                                        float(
                                            config.get("tmdb_cache", {}).get(
                                                "negative_ttl_hours", 12.0
                                            )
                                        ),
                                    )
                                    if "/season/" in str(endpoint_or_url):
                                        negative_hours = min(
                                            negative_hours,
                                            max(
                                                0.1,
                                                float(
                                                    config.get("incremental", {}).get(
                                                        "metadata_pending_recheck_hours",
                                                        24.0,
                                                    )
                                                ),
                                            ),
                                        )
                                    tmdb_response_cache.set(
                                        cache_hash,
                                        {_NEGATIVE_STATUS_KEY: 404},
                                        ttl_seconds=negative_hours * 3600,
                                    )
                                    log_tmdb_event(
                                        "tmdb_negative_cached",
                                        url=logged_url,
                                        ttl_hours=negative_hours,
                                    )
                                # Missing resources are retried after a short negative
                                # TTL instead of on every item or caching them forever.
                                return None
            if rate_limit_waited and attempt < retries:
                if performance:
                    performance.increment("tmdb_retries")
                await asyncio.sleep(retry_after)
        except CircuitOpenError as e:
            if performance:
                performance.increment("tmdb_circuit_rejections")
            log_tmdb_event(
                "tmdb_circuit_open",
                retry_after=e.retry_after,
            )
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
            if performance:
                performance.increment("tmdb_retries")
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
    cache=True,
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
            cache=cache,
        )
        matches = result.get(result_key, []) if isinstance(result, dict) else []
        for match in matches:
            candidate_id = match.get("id")
            if candidate_id is not None and str(candidate_id) not in excluded_ids:
                return str(candidate_id)

    if not config.get("tmdb", {}).get("title_search_fallback", False) or not title:
        return None
    search_type = "movie" if normalized_type in {"movie", "movies"} else "tv"
    plex_year = _year_value(year)
    base_title, title_year = _title_year_hint(title)
    title_year_resolves_conflict = bool(
        title_year and plex_year and abs(title_year - plex_year) > 1
    )
    search_title = base_title if title_year_resolves_conflict else str(title)
    search_year = title_year if title_year_resolves_conflict else plex_year
    params = {"query": search_title, "include_adult": "false"}
    if search_year:
        params["year" if search_type == "movie" else "first_air_date_year"] = (
            search_year
        )
    result = await tmdb_api_request(
        config,
        f"search/{search_type}",
        params=params,
        session=session,
        cache=cache,
    )
    candidates = []
    wanted_title = normalize_title(search_title)
    wanted_year = search_year
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


def _title_year_hint(value):
    """Return a title without a terminal disambiguation year and that year."""
    title = str(value or "").strip()
    match = re.search(r"\s+\(((?:18|19|20|21)\d{2})\)\s*$", title)
    if not match:
        return title, None
    base_title = title[: match.start()].strip()
    return base_title, int(match.group(1))


def _year_value(value):
    match = re.match(r"^(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def tmdb_identity_consistent(
    media_type, title, year, details, *, trusted_external_id=False
):
    """Reject strong TMDb identity conflicts while tolerating translated titles."""
    normalized_type = str(media_type or "").lower()
    date_value = (
        details.get("release_date")
        if normalized_type in {"movie", "movies"}
        else details.get("first_air_date")
    )
    expected_year = _year_value(year)
    actual_year = _year_value(date_value)
    names = (
        (details.get("title"), details.get("original_title"))
        if normalized_type in {"movie", "movies"}
        else (details.get("name"), details.get("original_name"))
    )
    base_title, title_year = _title_year_hint(title)
    available_titles = {normalize_title(name) for name in names if name}
    expected_title = normalize_title(title)
    base_title_matches = bool(
        title_year
        and normalize_title(base_title)
        and normalize_title(base_title) in available_titles
    )
    exact_title_matches = bool(
        expected_title and expected_title in available_titles
    )

    if title_year and actual_year:
        if abs(title_year - actual_year) > 1:
            return False, f"title year mismatch ({title_year} vs {actual_year})"
        if (
            expected_year
            and abs(expected_year - actual_year) > 1
            and (base_title_matches or exact_title_matches or trusted_external_id)
        ):
            return (
                True,
                (
                    "trusted external ID matched "
                    if trusted_external_id
                    and not (base_title_matches or exact_title_matches)
                    else "matched "
                )
                + f"title year {title_year}; ignored conflicting Plex year "
                + f"{expected_year}",
            )

    if expected_year and actual_year and abs(expected_year - actual_year) > 1:
        return False, f"year mismatch ({expected_year} vs {actual_year})"
    if expected_title and not (exact_title_matches or base_title_matches):
        if trusted_external_id:
            return True, "trusted external ID; title is a Plex/TMDb alias"
        return True, "title differs from localized/original TMDb names"
    return True, "matched"


def tmdb_external_id_consensus(
    media_type,
    details,
    *,
    imdb_id=None,
    tvdb_id=None,
    allow_tvdb_mismatch=False,
):
    """Compare independent Plex provider IDs with the selected TMDb record.

    Returns ``(accepted, trusted, reason)``. A matching independent ID makes
    aliases trustworthy; a conflicting ID rejects the record unless an
    explicit split-series mapping permits the expected TVDB disagreement.
    """
    external_ids = details.get("external_ids") or {}
    comparisons = []

    if imdb_id and external_ids.get("imdb_id"):
        comparisons.append(
            (
                "IMDb",
                str(imdb_id).strip().casefold(),
                str(external_ids.get("imdb_id")).strip().casefold(),
                False,
            )
        )
    if (
        str(media_type or "").lower() in {"tv", "show", "shows"}
        and tvdb_id
        and external_ids.get("tvdb_id")
    ):
        comparisons.append(
            (
                "TVDB",
                str(tvdb_id).strip(),
                str(external_ids.get("tvdb_id")).strip(),
                bool(allow_tvdb_mismatch),
            )
        )

    conflicts = [
        f"{provider} {expected} vs {actual}"
        for provider, expected, actual, permitted in comparisons
        if expected != actual and not permitted
    ]
    if conflicts:
        return False, False, "external ID conflict (" + "; ".join(conflicts) + ")"

    matches = [
        provider
        for provider, expected, actual, _permitted in comparisons
        if expected == actual
    ]
    if matches:
        return True, True, "matched " + ", ".join(matches)
    if allow_tvdb_mismatch and any(
        provider == "TVDB" and expected != actual
        for provider, expected, actual, _permitted in comparisons
    ):
        return True, True, "accepted configured split-series TVDB mapping"
    return True, False, "no independent external ID was available for consensus"


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
    cache=True,
):
    """Return a TMDb episode-group mapping only when one layout is unambiguous."""
    if not config.get("tmdb", {}).get("episode_group_fallback", True):
        return None
    wanted = _inventory_pairs(plex_inventory)
    if not wanted:
        return None
    listing = await tmdb_api_request(
        config, f"tv/{tmdb_id}/episode_groups", session=session, cache=cache
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
            config, f"tv/episode_group/{group_id}", session=session, cache=cache
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
