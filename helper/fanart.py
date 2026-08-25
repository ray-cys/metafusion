"""Reliable, cached Fanart.tv artwork fallback provider."""

import asyncio
import hashlib
import json
import weakref
from typing import Any

from aiolimiter import AsyncLimiter

from helper.concurrency import CircuitOpenError, runtime_slot
from helper.config import CACHE_DIR
from helper.logging import log_fanart_event, redact_secrets
from helper.performance import tracker_for
from helper.provider_credentials import fanart_project_api_key
from helper.tmdb_cache import PersistentTTLCache

BASE_URL = "https://webservice.fanart.tv/v3.2"
_NEGATIVE_STATUS_KEY = "__metafusion_negative_http_status__"
_CACHE_MISS = object()
fanart_response_cache = PersistentTTLCache()
_fanart_limiter = None
_fanart_limiter_loop = None
_inflight_requests: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Task[Any]]
] = weakref.WeakKeyDictionary()
_authorization_disabled: weakref.WeakSet[asyncio.AbstractEventLoop] = weakref.WeakSet()
_missing_key_logged: weakref.WeakSet[asyncio.AbstractEventLoop] = weakref.WeakSet()


def begin_fanart_cache(config):
    cache_config = config.get("tmdb_cache", {})
    configured = bool(fanart_project_api_key())
    fanart_response_cache.configure(
        CACHE_DIR / "fanart_cache.sqlite3",
        ttl_hours=cache_config.get("ttl_hours", 24),
        max_entries=cache_config.get("max_entries", 0),
        max_mb=cache_config.get("max_mb", 0),
        enabled=configured and cache_config.get("enabled", True),
        writable=not config.get("settings", {}).get("dry_run", False),
    )


def flush_fanart_cache():
    if not fanart_response_cache.enabled:
        return False
    result = fanart_response_cache.flush()
    stats = fanart_response_cache.stats()
    log_fanart_event("fanart_cache_stats", **stats)
    if stats.get("health") == "degraded":
        log_fanart_event(
            "fanart_cache_degraded",
            error=stats.get("last_error") or "persistent cache unavailable",
        )
    return result


def get_fanart_limiter():
    global _fanart_limiter, _fanart_limiter_loop
    loop = asyncio.get_running_loop()
    if _fanart_limiter is None or _fanart_limiter_loop is not loop:
        _fanart_limiter = AsyncLimiter(20, 10)
        _fanart_limiter_loop = loop
    return _fanart_limiter


async def _read_limited(response, max_bytes):
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ValueError("response exceeds configured size limit")
        except ValueError as error:
            if "exceeds" in str(error):
                raise
    chunks = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("response exceeds configured size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(value):
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


async def fanart_api_request(
    config,
    resource_type,
    resource_id,
    *,
    session=None,
    retries=3,
    delay=2,
    cache=True,
    _coalesced_owner=False,
):
    """Return one value-safe Fanart.tv response, or ``None`` on absence/outage."""
    key = fanart_project_api_key()
    loop = asyncio.get_running_loop()
    if not key:
        if loop not in _missing_key_logged:
            _missing_key_logged.add(loop)
            log_fanart_event("fanart_disabled")
        return None
    if loop in _authorization_disabled or session is None:
        return None

    resource_type = "tv" if str(resource_type).lower() == "tv" else "movies"
    resource_id = str(resource_id or "").strip()
    if not resource_id.isdigit():
        return None
    url = f"{BASE_URL}/{resource_type}/{resource_id}"
    cache_identity = hashlib.sha256(
        f"v3.2:{resource_type}:{resource_id}".encode("utf-8")
    ).hexdigest()
    performance = tracker_for(config)
    cached = (
        fanart_response_cache.get(cache_identity, _CACHE_MISS)
        if cache
        else _CACHE_MISS
    )
    if cached is not _CACHE_MISS:
        if performance:
            performance.increment("fanart_cache_hits")
        if isinstance(cached, dict) and cached.get(_NEGATIVE_STATUS_KEY) == 404:
            log_fanart_event(
                "fanart_negative_cache_hit",
                resource_type=resource_type,
                resource_id=resource_id,
            )
            return None
        log_fanart_event(
            "fanart_cache_hit",
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return cached

    if not _coalesced_owner:
        inflight = _inflight_requests.setdefault(loop, {})
        existing = inflight.get(cache_identity)
        if existing is not None:
            if performance:
                performance.increment("fanart_coalesced_waits")
            log_fanart_event(
                "fanart_request_coalesced",
                resource_type=resource_type,
                resource_id=resource_id,
            )
            return await asyncio.shield(existing)
        task = asyncio.create_task(
            fanart_api_request(
                config,
                resource_type,
                resource_id,
                session=session,
                retries=retries,
                delay=delay,
                cache=cache,
                _coalesced_owner=True,
            )
        )
        inflight[cache_identity] = task

        def clear_inflight(completed):
            current = _inflight_requests.get(loop)
            if current is not None and current.get(cache_identity) is completed:
                current.pop(cache_identity, None)

        task.add_done_callback(clear_inflight)
        return await asyncio.shield(task)

    if cache and performance:
        performance.increment("fanart_cache_misses")
    max_bytes = max(
        1024 * 1024,
        int(config.get("runtime", {}).get("max_image_mb", 25)) * 1024 * 1024,
    )
    headers = {"api-key": key, "Accept": "application/json"}
    for attempt in range(1, max(1, int(retries)) + 1):
        rate_limit_waited = False
        try:
            log_fanart_event(
                "fanart_request",
                resource_type=resource_type,
                resource_id=resource_id,
                attempt=attempt,
                retries=retries,
            )
            if performance:
                performance.increment("fanart_requests")
            async with get_fanart_limiter():
                async with runtime_slot(config, "fanart") as concurrency:
                    async with session.get(
                        url, headers=headers, allow_redirects=False
                    ) as response:
                        if response.status == 200:
                            content_type = str(
                                response.headers.get("Content-Type") or ""
                            ).lower()
                            if content_type and "json" not in content_type:
                                log_fanart_event(
                                    "fanart_invalid_response",
                                    resource_type=resource_type,
                                    resource_id=resource_id,
                                    error=f"unexpected content type {content_type}",
                                )
                                return None
                            try:
                                payload = json.loads(
                                    (await _read_limited(response, max_bytes)).decode(
                                        "utf-8"
                                    )
                                )
                            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                                log_fanart_event(
                                    "fanart_invalid_response",
                                    resource_type=resource_type,
                                    resource_id=resource_id,
                                    error=redact_secrets(error, key),
                                )
                                return None
                            except ValueError as error:
                                event = (
                                    "fanart_response_too_large"
                                    if "size limit" in str(error)
                                    else "fanart_invalid_response"
                                )
                                values = {
                                    "resource_type": resource_type,
                                    "resource_id": resource_id,
                                }
                                if event == "fanart_invalid_response":
                                    values["error"] = redact_secrets(error, key)
                                log_fanart_event(event, **values)
                                return None
                                return None
                            if not isinstance(payload, dict):
                                log_fanart_event(
                                    "fanart_invalid_response",
                                    resource_type=resource_type,
                                    resource_id=resource_id,
                                    error="response root is not an object",
                                )
                                return None
                            if cache:
                                fanart_response_cache[cache_identity] = payload
                            log_fanart_event(
                                "fanart_success",
                                resource_type=resource_type,
                                resource_id=resource_id,
                            )
                            return payload
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
                                fanart_response_cache.set(
                                    cache_identity,
                                    {_NEGATIVE_STATUS_KEY: 404},
                                    ttl_seconds=negative_hours * 3600,
                                )
                            log_fanart_event(
                                "fanart_not_found",
                                resource_type=resource_type,
                                resource_id=resource_id,
                            )
                            return None
                        if response.status in {401, 403}:
                            concurrency.failure(
                                "authorization_error", immediate_open=True, cooldown=300
                            )
                            _authorization_disabled.add(loop)
                            log_fanart_event("fanart_authorization_failed")
                            return None
                        if response.status == 429:
                            try:
                                retry_after = min(
                                    60,
                                    max(1, int(response.headers.get("Retry-After", delay))),
                                )
                            except (TypeError, ValueError):
                                retry_after = max(1, int(delay))
                            concurrency.failure("rate_limit", cooldown=retry_after)
                            if performance:
                                performance.increment("fanart_rate_limits")
                            log_fanart_event(
                                "fanart_rate_limited", retry_after=retry_after
                            )
                            rate_limit_waited = True
                        elif response.status >= 500:
                            concurrency.failure("server_error")
                        else:
                            log_fanart_event(
                                "fanart_request_failed",
                                resource_type=resource_type,
                                resource_id=resource_id,
                                attempt=attempt,
                                retries=retries,
                                error=f"HTTP {response.status}",
                            )
                            return None
            if rate_limit_waited and attempt < retries:
                await asyncio.sleep(retry_after)
                continue
        except CircuitOpenError as error:
            if performance:
                performance.increment("fanart_circuit_rejections")
            log_fanart_event(
                "fanart_circuit_open", retry_after=error.retry_after
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_fanart_event(
                "fanart_request_failed",
                resource_type=resource_type,
                resource_id=resource_id,
                attempt=attempt,
                retries=retries,
                error=redact_secrets(error, key),
            )
        if attempt < retries and not rate_limit_waited:
            sleep_time = delay * (2 ** (attempt - 1))
            log_fanart_event(
                "fanart_retrying",
                sleep_time=sleep_time,
                next_attempt=attempt + 1,
                retries=retries,
            )
            await asyncio.sleep(sleep_time)
    log_fanart_event(
        "fanart_failed",
        retries=retries,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    return None


def _normalize_candidate(record, *, asset_type):
    if not isinstance(record, dict) or not str(record.get("url") or "").startswith(
        "https://"
    ):
        return None
    language: str | None = str(record.get("lang") or "").strip().lower()
    if language in {"", "00", "null"}:
        language = None
    likes = _safe_float(record.get("likes"))
    return {
        "file_path": str(record["url"]),
        "provider": "fanart",
        "provider_label": "Fanart.tv",
        "provider_image_id": str(record.get("id") or ""),
        "iso_639_1": language,
        "width": _safe_int(record.get("width")),
        "height": _safe_int(record.get("height")),
        "vote_average": min(10.0, likes),
        "vote_count": max(0, int(likes)),
        "provider_likes": likes,
        "asset_type": asset_type,
        "added": record.get("added"),
    }


async def fanart_artwork_candidates(
    config,
    media_type,
    *,
    tmdb_id=None,
    tvdb_id=None,
    asset_type="poster",
    season_number=None,
    session=None,
):
    """Return Fanart.tv candidates normalized to MetaFusion's artwork contract."""
    is_tv = str(media_type or "").lower() in {"tv", "show", "shows"}
    resource_id = tvdb_id if is_tv else tmdb_id
    if not resource_id:
        return []
    response = await fanart_api_request(
        config,
        "tv" if is_tv else "movies",
        resource_id,
        session=session,
    )
    if not isinstance(response, dict):
        return []
    if is_tv:
        key = {
            "poster": "tvposter",
            "background": "showbackground",
            "season": "seasonposter",
        }.get(asset_type)
    else:
        key = {
            "poster": "movieposter",
            "background": "moviebackground",
        }.get(asset_type)
    if not key:
        return []
    records = response.get(key, [])
    if not isinstance(records, list):
        return []
    normalized = []
    for record in records:
        if asset_type == "season" and str(record.get("season")) != str(
            int(season_number or 0)
        ):
            continue
        candidate = _normalize_candidate(record, asset_type=asset_type)
        if candidate is not None:
            normalized.append(candidate)
    return normalized
