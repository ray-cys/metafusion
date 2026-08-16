import asyncio
import copy
import json
from datetime import datetime
from helper.config import CACHE_DIR
from helper.io import atomic_write_json, backup_path_for, read_json_with_backup
from helper.logging import log_cache_event

CACHE_FILE = CACHE_DIR / "meta_cache.json"

_cache_data = None
_cache_dirty = False
_cache_source = None
_cache_lock = None
_cache_lock_loop = None


def begin_cache_session():
    global _cache_data, _cache_dirty, _cache_source, _cache_lock, _cache_lock_loop
    _cache_data = None
    _cache_dirty = False
    _cache_source = None
    _cache_lock = None
    _cache_lock_loop = None


def load_cache(force_reload=False):
    global _cache_data, _cache_dirty, _cache_source
    if not force_reload and _cache_data is not None and _cache_source == CACHE_FILE:
        return _cache_data

    if (
        (CACHE_FILE.exists() and CACHE_FILE.stat().st_size > 0)
        or backup_path_for(CACHE_FILE).exists()
    ):
        try:
            cache = read_json_with_backup(CACHE_FILE)
            if not isinstance(cache, dict):
                raise ValueError("Cache root must be a JSON object")
            log_cache_event("cache_loaded", count=len(cache), cache_file=CACHE_FILE)
            _cache_data = cache
            _cache_dirty = False
            _cache_source = CACHE_FILE
            return _cache_data
        except (OSError, json.JSONDecodeError, ValueError) as error:
            log_cache_event("cache_load_failed", cache_file=CACHE_FILE, error=error)
    log_cache_event("cache_empty", cache_file=CACHE_FILE)
    _cache_data = {}
    _cache_dirty = False
    _cache_source = CACHE_FILE
    return _cache_data


def _write_cache(cache):
    cache_to_save = copy.deepcopy(cache)
    for entry in cache_to_save.values():
        if isinstance(entry, dict) and entry.get("media_type") == "tv":
            entry.pop("season_average", None)
            entry.pop("season_number", None)

    atomic_write_json(CACHE_FILE, cache_to_save, backup=True)
    log_cache_event("cache_saved", count=len(cache_to_save), cache_file=CACHE_FILE)


def save_cache(cache):
    global _cache_data, _cache_dirty, _cache_source
    _cache_data = cache
    _cache_source = CACHE_FILE
    _write_cache(_cache_data)
    _cache_dirty = False


def mark_cache_dirty():
    global _cache_dirty
    _cache_dirty = True


def flush_cache():
    global _cache_dirty
    if _cache_dirty and _cache_data is not None:
        _write_cache(_cache_data)
        _cache_dirty = False
        return True
    return False

def get_cache_lock():
    """Return a cache lock bound to the active scheduled job's event loop."""
    global _cache_lock, _cache_lock_loop
    loop = asyncio.get_running_loop()
    if _cache_lock is None or _cache_lock_loop is not loop:
        _cache_lock = asyncio.Lock()
        _cache_lock_loop = loop
    return _cache_lock


async def meta_cache_async(
    cache_key, tmdb_id, title, year, media_type, update_timestamp=True, asset_upgraded=False, 
    poster_upgraded=False, background_upgraded=False, season_upgraded=None,
    poster_checked=False, background_checked=False, season_checked=False,
    legacy_cache_key=None, **kwargs
):
    async with get_cache_lock():
        cache = load_cache()
        if cache_key not in cache and legacy_cache_key and legacy_cache_key in cache:
            entry = cache.pop(legacy_cache_key)
        else:
            entry = cache.get(cache_key, {})
        identity_fields = {
            "tmdb_id": tmdb_id,
            "title": title,
            "year": year,
            "media_type": media_type,
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
        if background_upgraded:
            entry["background_last_upgraded"] = now_iso
        if poster_checked:
            entry["poster_last_checked"] = now_iso
        if background_checked:
            entry["background_last_checked"] = now_iso
        if season_checked:
            entry["season_last_checked"] = now_iso
        season_number = kwargs.pop("season_number", None)
        if season_number is not None:
            seasons = entry.setdefault("seasons", {})
            season_entry = seasons.setdefault(str(season_number), {})
            for k, v in kwargs.items():
                season_entry[k] = v
            if type(season_upgraded) is int and season_upgraded == int(season_number):
                season_entry["season_last_upgraded"] = now_iso
        else:
            for k, v in kwargs.items():
                entry[k] = v
        cache[cache_key] = entry
        log_cache_event("cache_updated", cache_key=cache_key, media_type=media_type, title=title, year=year)
        mark_cache_dirty()
