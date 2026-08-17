import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helper.config import get_image_upgrade_days
from helper.io import read_json_with_backup
from helper.state_db import (
    LEGACY_INCREMENTAL_STATE,
    STATE_DATABASE,
    load_scan_states,
    mark_scan_complete as persist_scan_complete,
    mark_scan_started as persist_scan_started,
    set_legacy_full_scan,
)


@dataclass(frozen=True)
class PlannedItem:
    """A Plex item paired with the exact operations selected for this run."""

    item: object
    reasons: frozenset[str]


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
        "library_name": config.get("_library_name"),
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


def load_state(path=None, scopes=None):
    database = Path(path or STATE_DATABASE)
    states, legacy = load_scan_states(scopes or [], path=database)
    if legacy is None and database == Path(STATE_DATABASE):
        document = read_json_with_backup(LEGACY_INCREMENTAL_STATE, default={})
        if isinstance(document, dict):
            legacy = document.get("last_full_scan")
    document = {"libraries": states}
    if legacy:
        document["last_full_scan"] = legacy
    return document


def _timestamp_is_due(value, interval, now):
    if not value:
        return True
    try:
        last_full = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if last_full.tzinfo is None:
            last_full = last_full.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return now - last_full >= interval


def should_run_full_scan(
    config, targeted=False, state=None, now=None, scopes=None, path=None
):
    if not config.get("incremental", {}).get("enabled", True):
        return True
    if targeted:
        return False
    now = utc_now() if now is None else now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    interval = timedelta(
        hours=max(
            1.0,
            float(config.get("incremental", {}).get("full_scan_interval_hours", 168)),
        )
    )
    if state is not None:
        return _timestamp_is_due(state.get("last_full_scan"), interval, now)

    scopes = list(scopes or [])
    document = load_state(path=path, scopes=scopes)
    legacy = document.get("last_full_scan")
    if not scopes:
        return _timestamp_is_due(legacy, interval, now)

    states = document.get("libraries", {})
    for scope in scopes:
        key = (
            str(scope.get("server_id") or "unknown"),
            str(scope.get("library_uuid") or scope.get("library_name")),
        )
        library_state = states.get(key)
        if library_state is None:
            if _timestamp_is_due(legacy, interval, now):
                return True
            continue
        expected_fingerprint = scope.get("config_fingerprint")
        stored_fingerprint = library_state.get("config_fingerprint")
        if (
            expected_fingerprint
            and stored_fingerprint
            and expected_fingerprint != stored_fingerprint
        ):
            return True
        if _timestamp_is_due(
            library_state.get("last_full_scan_completed") or legacy, interval, now
        ):
            return True
    return False


def mark_full_scan_complete(dry_run=False, path=None, now=None, scopes=None):
    if dry_run:
        return False
    now = utc_now() if now is None else now
    value = now.isoformat() if isinstance(now, datetime) else str(now)
    if scopes:
        return persist_scan_complete(
            scopes, full_scan=True, path=path or STATE_DATABASE, now=value
        )
    return set_legacy_full_scan(value, path=path or STATE_DATABASE)


def mark_library_scan_started(scopes, full_scan, dry_run=False, path=None, now=None):
    if dry_run:
        return False
    value = utc_now() if now is None else now
    if isinstance(value, datetime):
        value = value.isoformat()
    return persist_scan_started(
        scopes, full_scan=full_scan, path=path or STATE_DATABASE, now=value
    )


def mark_library_scan_complete(scopes, full_scan, dry_run=False, path=None, now=None):
    if dry_run:
        return False
    value = utc_now() if now is None else now
    if isinstance(value, datetime):
        value = value.isoformat()
    return persist_scan_complete(
        scopes, full_scan=full_scan, path=path or STATE_DATABASE, now=value
    )


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


def image_upgrade_reasons(cached, media_type, config, feature_flags=None, now=None):
    """Return the artwork operations due for an otherwise unchanged item."""
    if not isinstance(cached, dict) or not config:
        return set()
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
        return set()

    reasons = set()
    if flags.get("poster", False) and timestamp_due(
        cached.get("poster_last_checked") or cached.get("poster_last_upgraded"),
        days,
        now=now,
    ):
        reasons.add("poster")
    if flags.get("background", False) and timestamp_due(
        cached.get("background_last_checked")
        or cached.get("background_last_upgraded"),
        days,
        now=now,
    ):
        reasons.add("background")
    if normalized_type == "tv" and flags.get("season", False):
        if timestamp_due(
            cached.get("season_last_checked"),
            get_image_upgrade_days(config, "season"),
            now=now,
        ):
            reasons.add("season")
    return reasons


def image_upgrade_due(cached, media_type, config, feature_flags=None, now=None):
    """Return whether enabled artwork makes an unchanged item eligible."""
    return bool(
        image_upgrade_reasons(
            cached,
            media_type,
            config,
            feature_flags=feature_flags,
            now=now,
        )
    )


def enabled_work_reasons(media_type, feature_flags=None):
    flags = feature_flags or {}
    normalized_type = str(media_type or "").lower()
    if normalized_type in {"show", "shows"}:
        normalized_type = "tv"
    if normalized_type == "movies":
        normalized_type = "movie"

    reasons = set()
    if flags.get("metadata_basic", True) or flags.get("metadata_enhanced", False):
        reasons.add("metadata")
    if flags.get("poster", False):
        reasons.add("poster")
    if flags.get("background", False):
        reasons.add("background")
    if normalized_type == "tv" and flags.get("season", False):
        reasons.add("season")
    return reasons or {"identity"}


def plan_items(
    items,
    cache,
    fingerprint,
    full_scan=False,
    rating_keys=None,
    config=None,
    feature_flags=None,
    now=None,
):
    """Plan selected items without losing which operations made them eligible."""
    target_keys = {str(value) for value in (rating_keys or []) if str(value).strip()}
    candidates = [
        item
        for item in items
        if not target_keys or str(getattr(item, "ratingKey", "")) in target_keys
    ]
    if full_scan or target_keys:
        return [
            PlannedItem(
                item,
                frozenset(
                    enabled_work_reasons(
                        getattr(item, "type", None), feature_flags=feature_flags
                    )
                ),
            )
            for item in candidates
        ]

    cache_by_rating_key = {
        str(entry.get("rating_key")): entry
        for entry in cache.values()
        if isinstance(entry, dict) and entry.get("rating_key") is not None
    }
    planned = []
    for item in candidates:
        rating_key = str(getattr(item, "ratingKey", ""))
        updated_at = item_updated_at(item)
        cached = cache_by_rating_key.get(rating_key)
        changed = (
            not cached
            or updated_at is None
            or cached.get("plex_updated_at") != updated_at
            or cached.get("config_fingerprint") != fingerprint
        )
        if changed:
            reasons = enabled_work_reasons(
                getattr(item, "type", None), feature_flags=feature_flags
            )
        else:
            reasons = image_upgrade_reasons(
                cached,
                getattr(item, "type", None),
                config,
                feature_flags=feature_flags,
                now=now,
            )
        if reasons:
            planned.append(PlannedItem(item, frozenset(reasons)))
    return planned


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
    return [
        planned.item
        for planned in plan_items(
            items,
            cache,
            fingerprint,
            full_scan=full_scan,
            rating_keys=rating_keys,
            config=config,
            feature_flags=feature_flags,
            now=now,
        )
    ]
