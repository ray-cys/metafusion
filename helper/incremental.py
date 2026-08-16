import hashlib
import json
from datetime import datetime, timedelta, timezone

from helper.config import CACHE_DIR, get_image_upgrade_days
from helper.io import atomic_write_json


STATE_FILE = CACHE_DIR / "incremental_state.json"


def utc_now():
    return datetime.now(timezone.utc)


def item_updated_at(item):
    value = getattr(item, "updatedAt", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return None if value is None else str(value)


def config_fingerprint(config):
    relevant = {
        "mode": config.get("settings", {}).get("mode"),
        "metadata": config.get("metadata", {}),
        "assets": config.get("assets", {}),
        "image_upgrades": config.get("image_upgrades", {}),
        "tmdb": {
            "language": config.get("tmdb", {}).get("language"),
            "fallback": config.get("tmdb", {}).get("fallback"),
            "region": config.get("tmdb", {}).get("region"),
        },
        "poster_set": config.get("poster_set", {}),
        "season_set": config.get("season_set", {}),
        "background_set": config.get("background_set", {}),
    }
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_state(path=None):
    path = STATE_FILE if path is None else path
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return document if isinstance(document, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def should_run_full_scan(config, targeted=False, state=None, now=None):
    if not config.get("incremental", {}).get("enabled", True):
        return True
    if targeted:
        return False
    state = load_state() if state is None else state
    last_value = state.get("last_full_scan")
    if not last_value:
        return True
    try:
        last_full = datetime.fromisoformat(str(last_value).replace("Z", "+00:00"))
        if last_full.tzinfo is None:
            last_full = last_full.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    now = utc_now() if now is None else now
    interval = timedelta(
        hours=max(
            1.0,
            float(config.get("incremental", {}).get("full_scan_interval_hours", 168)),
        )
    )
    return now - last_full >= interval


def mark_full_scan_complete(dry_run=False, path=None, now=None):
    if dry_run:
        return False
    path = STATE_FILE if path is None else path
    now = utc_now() if now is None else now
    atomic_write_json(path, {"last_full_scan": now.isoformat()})
    return True


def timestamp_due(value, days, now=None):
    """Return whether an optional ISO timestamp has reached an interval."""
    try:
        interval_days = float(days)
    except (TypeError, ValueError):
        return True
    if interval_days <= 0:
        return False
    if not value:
        return True
    try:
        last_value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if last_value.tzinfo is None:
        last_value = last_value.astimezone()
    now = utc_now() if now is None else now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - last_value >= timedelta(days=interval_days)


def image_upgrade_due(cached, media_type, config, feature_flags=None, now=None):
    """Return whether enabled artwork makes an unchanged item eligible."""
    if not isinstance(cached, dict) or not config:
        return False
    assets = config.get("assets", {})
    flags = feature_flags or {
        "poster": assets.get("run_poster", True),
        "season": assets.get("run_season", True),
        "background": assets.get("run_background", False),
    }
    normalized_type = str(media_type or cached.get("media_type") or "").lower()
    if normalized_type in {"show", "shows"}:
        normalized_type = "tv"
    if normalized_type == "movies":
        normalized_type = "movie"

    if normalized_type == "movie":
        days = get_image_upgrade_days(config, "movie")
    elif normalized_type == "tv":
        days = get_image_upgrade_days(config, "series")
    else:
        return False

    if flags.get("poster", False) and timestamp_due(
        cached.get("poster_last_checked") or cached.get("poster_last_upgraded"),
        days,
        now=now,
    ):
        return True
    if flags.get("background", False) and timestamp_due(
        cached.get("background_last_checked")
        or cached.get("background_last_upgraded"),
        days,
        now=now,
    ):
        return True
    if normalized_type == "tv" and flags.get("season", False):
        return timestamp_due(
            cached.get("season_last_checked"),
            get_image_upgrade_days(config, "season"),
            now=now,
        )
    return False


def select_items(
    items,
    cache,
    fingerprint,
    full_scan=False,
    rating_keys=None,
    config=None,
    feature_flags=None,
    now=None,
):
    target_keys = {str(value) for value in (rating_keys or []) if str(value).strip()}
    candidates = [
        item
        for item in items
        if not target_keys or str(getattr(item, "ratingKey", "")) in target_keys
    ]
    if full_scan or target_keys:
        return candidates

    cache_by_rating_key = {
        str(entry.get("rating_key")): entry
        for entry in cache.values()
        if isinstance(entry, dict) and entry.get("rating_key") is not None
    }
    changed = []
    for item in candidates:
        rating_key = str(getattr(item, "ratingKey", ""))
        updated_at = item_updated_at(item)
        cached = cache_by_rating_key.get(rating_key)
        if (
            not cached
            or updated_at is None
            or cached.get("plex_updated_at") != updated_at
            or cached.get("config_fingerprint") != fingerprint
            or image_upgrade_due(
                cached,
                getattr(item, "type", None),
                config,
                feature_flags=feature_flags,
                now=now,
            )
        ):
            changed.append(item)
    return changed
