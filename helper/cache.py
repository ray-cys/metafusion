import asyncio
import copy
from contextvars import ContextVar
from datetime import datetime

from helper.asset_registry import normalize_destination
from helper.logging import log_cache_event
from helper.state_db import STATE_DATABASE, MediaStateStore

_cache_store = None
_cache_lock = None
_cache_lock_loop = None
_cache_writable = True
_cache_scope = ContextVar("metafusion_cache_scope", default=None)


def begin_cache_session(
    writable=True,
    database_path=None,
):
    global _cache_store, _cache_lock, _cache_lock_loop, _cache_writable
    if _cache_store is not None:
        _cache_store.close()
    _cache_writable = bool(writable)
    _cache_store = MediaStateStore(
        path=database_path or STATE_DATABASE,
        writable=_cache_writable,
    )
    _cache_lock = None
    _cache_lock_loop = None
    log_cache_event(
        "cache_loaded", count=len(_cache_store), cache_file=_cache_store.path
    )
    return _cache_store


def close_cache_session():
    """Flush and release the current durable-state connection."""
    global _cache_store, _cache_lock, _cache_lock_loop
    if _cache_store is not None:
        _cache_store.close()
    _cache_store = None
    _cache_lock = None
    _cache_lock_loop = None


def load_cache(force_reload=False):
    global _cache_store
    if force_reload or _cache_store is None:
        begin_cache_session(writable=_cache_writable)
    return _cache_store


def save_cache(cache):
    store = load_cache()
    store.replace_all(dict(cache))
    log_cache_event("cache_saved", count=len(store), cache_file=store.path)


def mark_cache_dirty():
    """Compatibility hook; assignments to the state mapping track changes directly."""
    return None


def flush_cache():
    store = load_cache()
    changed = store.flush()
    if changed:
        log_cache_event("cache_saved", count=len(store), cache_file=store.path)
    return changed


def set_cache_scope(server_id=None, library_uuid=None, library_name=None):
    return _cache_scope.set(
        {
            "server_id": None if server_id is None else str(server_id),
            "library_uuid": None if library_uuid is None else str(library_uuid),
            "library_name": library_name,
        }
    )


def reset_cache_scope(token):
    _cache_scope.reset(token)


def get_cache_lock():
    """Return a cache lock bound to the active scheduled job's event loop."""
    global _cache_lock, _cache_lock_loop
    loop = asyncio.get_running_loop()
    if _cache_lock is None or _cache_lock_loop is not loop:
        _cache_lock = asyncio.Lock()
        _cache_lock_loop = loop
    return _cache_lock


def _record_destination_change(
    entry,
    asset_type,
    previous,
    current,
    now_iso,
    *,
    season_number=None,
    previous_checksum=None,
):
    if not previous or not current:
        return
    old_path = normalize_destination(previous)
    new_path = normalize_destination(current)
    if not old_path or old_path == new_path:
        return
    history = entry.get("destination_history")
    if not isinstance(history, list):
        history = []
        entry["destination_history"] = history
    duplicate = any(
        event.get("asset_type") == asset_type
        and event.get("season_number") == season_number
        and event.get("previous_destination") == old_path
        and event.get("new_destination") == new_path
        for event in history[-20:]
        if isinstance(event, dict)
    )
    if duplicate:
        return
    history.append(
        {
            "asset_type": asset_type,
            "season_number": season_number,
            "previous_destination": old_path,
            "new_destination": new_path,
            "previous_checksum": previous_checksum,
            "detected_at": now_iso,
            "reported_at": None,
        }
    )
    del history[:-100]


def _record_artwork_observation(entry, asset_type, candidate, missing_count=0):
    """Track unchanged/missing observations used by adaptive refresh timing."""
    marker_key = f"{asset_type}_candidate_fingerprint"
    unchanged_key = f"{asset_type}_unchanged_checks"
    missing_key = f"{asset_type}_missing_checks"
    marker = "" if candidate is None else str(candidate)
    previous = entry.get(marker_key)
    if previous is None or str(previous) != marker:
        unchanged = 0
    else:
        unchanged = int(entry.get(unchanged_key) or 0) + 1
    missing = max(0, int(missing_count or 0))
    if missing:
        entry[missing_key] = int(entry.get(missing_key) or 0) + 1
        unchanged = 0
    else:
        entry[missing_key] = 0
    entry[marker_key] = marker
    entry[unchanged_key] = min(20, unchanged)


async def meta_cache_async(
    cache_key,
    tmdb_id,
    title,
    year,
    media_type,
    update_timestamp=True,
    asset_upgraded=False,
    poster_upgraded=False,
    background_upgraded=False,
    season_upgraded=None,
    poster_checked=False,
    background_checked=False,
    season_checked=False,
    plex_metadata_checked=False,
    metadata_pending_count=None,
    **kwargs,
):
    async with get_cache_lock():
        cache = load_cache()
        entry = copy.deepcopy(cache.get(cache_key, {}))
        previous_tmdb_id = entry.get("tmdb_id")
        if (
            previous_tmdb_id is not None
            and tmdb_id is not None
            and str(previous_tmdb_id) != str(tmdb_id)
        ):
            for asset_type in ("poster", "background", "season"):
                for suffix in (
                    "candidate_fingerprint",
                    "unchanged_checks",
                    "missing_checks",
                    "last_checked",
                ):
                    entry.pop(f"{asset_type}_{suffix}", None)
        identity_fields = {
            "tmdb_id": tmdb_id,
            "title": title,
            "year": year,
            "media_type": media_type,
            **(_cache_scope.get() or {}),
        }
        for field, value in identity_fields.items():
            if value is not None:
                entry[field] = value
        now_iso = datetime.now().astimezone().isoformat()
        if update_timestamp:
            entry["last_updated"] = now_iso
        if asset_upgraded:
            entry["asset_last_upgraded"] = now_iso
        if poster_upgraded:
            entry["poster_last_upgraded"] = now_iso
            entry["poster_unchanged_checks"] = 0
        if background_upgraded:
            entry["background_last_upgraded"] = now_iso
            entry["background_unchanged_checks"] = 0
        if poster_checked:
            entry["poster_last_checked"] = now_iso
            _record_artwork_observation(
                entry,
                "poster",
                kwargs.get(
                    "poster_candidate_source_path",
                    kwargs.get("poster_source_path"),
                ),
                missing_count=1
                if not kwargs.get(
                    "poster_candidate_source_path",
                    kwargs.get("poster_source_path"),
                )
                else 0,
            )
        if background_checked:
            entry["background_last_checked"] = now_iso
            _record_artwork_observation(
                entry,
                "background",
                kwargs.get(
                    "background_candidate_source_path",
                    kwargs.get("background_source_path"),
                ),
                missing_count=1
                if not kwargs.get(
                    "background_candidate_source_path",
                    kwargs.get("background_source_path"),
                )
                else 0,
            )
        if season_checked:
            entry["season_last_checked"] = now_iso
            _record_artwork_observation(
                entry,
                "season",
                kwargs.get("season_candidate_fingerprint"),
                missing_count=kwargs.get("season_missing_count", 0),
            )
        if plex_metadata_checked:
            entry["plex_metadata_last_checked"] = now_iso
        if metadata_pending_count is not None:
            pending_count = max(0, int(metadata_pending_count))
            entry["metadata_pending_count"] = pending_count
            entry["metadata_pending_at"] = now_iso if pending_count else ""
        season_number = kwargs.pop("season_number", None)
        if season_number is not None:
            seasons = entry.setdefault("seasons", {})
            season_entry = seasons.setdefault(str(season_number), {})
            if "season_path" in kwargs:
                _record_destination_change(
                    entry,
                    "season",
                    season_entry.get("season_path"),
                    kwargs.get("season_path"),
                    now_iso,
                    season_number=int(season_number),
                    previous_checksum=season_entry.get("season_checksum"),
                )
            for key, value in kwargs.items():
                season_entry[key] = value
            if type(season_upgraded) is int and season_upgraded == int(season_number):
                season_entry["season_last_upgraded"] = now_iso
                entry["season_unchanged_checks"] = 0
        else:
            for asset_type, path_field, checksum_field in (
                ("poster", "poster_path", "poster_checksum"),
                ("background", "background_path", "background_checksum"),
            ):
                if path_field in kwargs:
                    _record_destination_change(
                        entry,
                        asset_type,
                        entry.get(path_field),
                        kwargs.get(path_field),
                        now_iso,
                        previous_checksum=entry.get(checksum_field),
                    )
            for key, value in kwargs.items():
                entry[key] = value
        cache[cache_key] = entry
        log_cache_event(
            "cache_updated",
            cache_key=cache_key,
            media_type=media_type,
            title=title,
            year=year,
        )
