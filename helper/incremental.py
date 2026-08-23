import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helper.config import get_image_upgrade_days
from helper.state_db import (
    STATE_DATABASE,
    load_global_full_scan,
    load_scan_states,
    mark_global_full_scan,
)
from helper.state_db import (
    mark_scan_complete as persist_scan_complete,
)
from helper.state_db import (
    mark_scan_started as persist_scan_started,
)


@dataclass(frozen=True)
class PlannedItem:
    """A Plex item paired with the exact operations selected for this run."""

    item: object
    reasons: frozenset[str]
    selection_causes: frozenset[str] = field(default_factory=frozenset)


def utc_now():
    return datetime.now(timezone.utc)


def item_updated_at(item):
    value = getattr(item, "updatedAt", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return None if value is None else str(value)


def child_inventory_fingerprint(item):
    """Fingerprint Plex's show-level season and episode inventory counters."""
    if isinstance(item, dict):
        media_type = item.get("type") or item.get("library_type")
        get_value = item.get
    else:
        media_type = getattr(item, "type", None)

        def get_value(name):
            return getattr(item, name, None)
    if str(media_type or "").lower() not in {"show", "shows", "tv"}:
        return None

    counters = {}
    for source, target in (
        ("childCount", "children"),
        ("seasonCount", "seasons"),
        ("leafCount", "episodes"),
    ):
        value = get_value(source)
        if value is None:
            continue
        try:
            counters[target] = int(value)
        except (TypeError, ValueError):
            counters[target] = str(value)
    if not counters:
        return None
    encoded = json.dumps(counters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def config_fingerprint(config):
    relevant = {
        "library_name": config.get("_library_name"),
        "mode": config.get("settings", {}).get("mode"),
        "metadata": config.get("metadata", {}),
        "kometa": config.get("kometa", {}),
        "assets": config.get("assets", {}),
        "plex_metadata": config.get("plex_metadata", {}),
        "plex_path_mappings": config.get("plex", {}).get("path_mappings", []),
        "image_upgrades": config.get("image_upgrades", {}),
        "tmdb": {
            "language": config.get("tmdb", {}).get("language"),
            "fallback": config.get("tmdb", {}).get("fallback"),
            "region": config.get("tmdb", {}).get("region"),
            "artwork_allow_any_language": config.get("tmdb", {}).get(
                "artwork_allow_any_language"
            ),
            "title_search_fallback": config.get("tmdb", {}).get(
                "title_search_fallback"
            ),
            "episode_group_fallback": config.get("tmdb", {}).get(
                "episode_group_fallback"
            ),
            "split_series_show_policy": config.get("tmdb", {}).get(
                "split_series_show_policy"
            ),
            "split_series_mappings": config.get("tmdb", {}).get(
                "split_series_mappings", {}
            ),
            "episode_overrides": config.get("tmdb", {}).get(
                "episode_overrides", {}
            ),
        },
        "poster_set": config.get("poster_set", {}),
        "season_set": config.get("season_set", {}),
        "background_set": config.get("background_set", {}),
    }
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_state(path=None, scopes=None):
    database = Path(path or STATE_DATABASE)
    document = {"libraries": load_scan_states(scopes or [], path=database)}
    if not scopes:
        document["last_full_scan"] = load_global_full_scan(path=database)
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

    decisions = library_full_scan_decisions(
        config,
        targeted=targeted,
        now=now,
        scopes=scopes,
        path=path,
    )
    return True if not decisions else any(decisions.values())


def library_full_scan_decisions(
    config, targeted=False, now=None, scopes=None, path=None
):
    scopes = list(scopes or [])
    if not scopes:
        return {}
    if not config.get("incremental", {}).get("enabled", True):
        return {_scope_key(scope): True for scope in scopes}
    if targeted:
        return {_scope_key(scope): False for scope in scopes}
    now = utc_now() if now is None else now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    interval = timedelta(
        hours=max(
            1.0,
            float(config.get("incremental", {}).get("full_scan_interval_hours", 168)),
        )
    )
    states = load_state(path=path, scopes=scopes).get("libraries", {})
    decisions = {}
    for scope in scopes:
        key = (
            str(scope.get("server_id") or "unknown"),
            str(scope.get("library_uuid") or scope.get("library_name")),
        )
        library_state = states.get(key)
        if library_state is None:
            decisions[key] = True
            continue
        expected_fingerprint = scope.get("config_fingerprint")
        stored_fingerprint = library_state.get("config_fingerprint")
        if (
            expected_fingerprint
            and stored_fingerprint
            and expected_fingerprint != stored_fingerprint
        ):
            decisions[key] = True
            continue
        decisions[key] = _timestamp_is_due(
            library_state.get("last_full_scan_completed"), interval, now
        )
    return decisions


def _scope_key(scope):
    return (
        str(scope.get("server_id") or "unknown"),
        str(scope.get("library_uuid") or scope.get("library_name")),
    )


def mark_full_scan_complete(dry_run=False, path=None, now=None, scopes=None):
    if dry_run:
        return False
    now = utc_now() if now is None else now
    value = now.isoformat() if isinstance(now, datetime) else str(now)
    if scopes:
        return persist_scan_complete(
            scopes, full_scan=True, path=path or STATE_DATABASE, now=value
        )
    return mark_global_full_scan(value, path=path or STATE_DATABASE)


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


def adaptive_artwork_days(cached, asset_type, configured_days):
    """Return an automatic recheck interval from durable artwork observations."""
    try:
        base_days = float(configured_days)
    except (TypeError, ValueError):
        return configured_days
    if base_days <= 0 or not isinstance(cached, dict):
        return base_days
    missing_checks = max(
        0, int(cached.get(f"{asset_type}_missing_checks") or 0)
    )
    if missing_checks:
        schedule = (1.0, 3.0, 7.0, 14.0, 30.0, 60.0)
        return min(base_days, schedule[min(missing_checks - 1, len(schedule) - 1)])
    unchanged_checks = max(
        0, int(cached.get(f"{asset_type}_unchanged_checks") or 0)
    )
    return min(max(base_days, 180.0), base_days * (2 ** min(3, unchanged_checks)))


def same_source_verification_days(config, media_type, asset_type):
    """Return the bounded interval for re-downloading an unchanged source ID."""
    normalized_type = _media_type(media_type)
    interval_type = (
        "season"
        if asset_type == "season"
        else ("movie" if normalized_type == "movie" else "series")
    )
    base_days = get_image_upgrade_days(config, interval_type)
    if base_days <= 0:
        return 0.0
    # Provider CDNs rarely mutate bytes behind one stable source identifier.
    # Rechecking no sooner than 90 days catches that edge case without turning
    # ordinary incremental runs into full artwork downloads.
    return min(365.0, max(90.0, base_days * 3.0))


def same_source_verification_due(
    cached,
    media_type,
    asset_type,
    config,
    *,
    season_number=None,
    source_path=None,
    now=None,
):
    """Return whether a managed source needs a byte-level periodic recheck."""
    if not isinstance(cached, dict) or not config:
        return False
    interval_days = same_source_verification_days(
        config, media_type, asset_type
    )
    if interval_days <= 0:
        return False
    if asset_type == "season":
        seasons = cached.get("seasons") or {}
        record = (
            seasons.get(str(season_number), {})
            if isinstance(seasons, dict)
            else {}
        )
        verified_at = record.get("season_source_verified_at")
        verified_path = record.get("season_source_verified_path")
        checked_at = (
            verified_at
            if source_path and verified_path == source_path
            else record.get("season_last_upgraded")
        )
    else:
        verified_at = cached.get(f"{asset_type}_source_verified_at")
        verified_path = cached.get(f"{asset_type}_source_verified_path")
        checked_at = (
            verified_at
            if source_path and verified_path == source_path
            else cached.get(f"{asset_type}_last_upgraded")
        )
    return timestamp_due(checked_at, interval_days, now=now)


def image_upgrade_reasons(cached, media_type, config, feature_flags=None, now=None):
    """Return the artwork operations due for an otherwise unchanged item."""
    causes = due_selection_causes(
        cached,
        media_type,
        config,
        feature_flags=feature_flags,
        now=now,
    )
    reasons = set()
    if causes & {"metadata_pending_recheck", "plex_metadata_recheck"}:
        reasons.add("metadata")
    if "poster_refresh_due" in causes:
        reasons.add("poster")
    if "background_refresh_due" in causes:
        reasons.add("background")
    if "season_refresh_due" in causes:
        reasons.add("season")
    return reasons


def due_selection_causes(cached, media_type, config, feature_flags=None, now=None):
    """Return exact time-based causes for selecting an unchanged item."""
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

    causes = set()
    try:
        pending_count = int(cached.get("metadata_pending_count") or 0)
    except (TypeError, ValueError):
        pending_count = 0
    metadata_enabled = feature_flags is None or any(
        flags.get(name, False)
        for name in ("metadata_basic", "metadata_enhanced", "plex_metadata")
    )
    if pending_count > 0 and metadata_enabled:
        recheck_hours = max(
            0.1,
            float(
                config.get("incremental", {}).get(
                    "metadata_pending_recheck_hours", 24.0
                )
            ),
        )
        check_time = utc_now() if now is None else now
        if check_time.tzinfo is None:
            check_time = check_time.replace(tzinfo=timezone.utc)
        if _timestamp_is_due(
            cached.get("metadata_pending_at"),
            timedelta(hours=recheck_hours),
            check_time,
        ):
            causes.add("metadata_pending_recheck")
    if flags.get("plex_metadata", False) and timestamp_due(
        cached.get("plex_metadata_last_checked"),
        config.get("plex_metadata", {}).get("recheck_days", 30),
        now=now,
    ):
        causes.add("plex_metadata_recheck")
    if flags.get("poster", False) and timestamp_due(
        cached.get("poster_last_checked") or cached.get("poster_last_upgraded"),
        adaptive_artwork_days(cached, "poster", days),
        now=now,
    ):
        causes.add("poster_refresh_due")
    if flags.get("background", False) and timestamp_due(
        cached.get("background_last_checked")
        or cached.get("background_last_upgraded"),
        adaptive_artwork_days(cached, "background", days),
        now=now,
    ):
        causes.add("background_refresh_due")
    if (
        normalized_type == "tv"
        and flags.get("season", False)
        and timestamp_due(
            cached.get("season_last_checked"),
            adaptive_artwork_days(
                cached, "season", get_image_upgrade_days(config, "season")
            ),
            now=now,
        )
    ):
        causes.add("season_refresh_due")
    return causes


def _scoped_cache_by_rating_key(
    cache, items, *, server_id=None, library_uuid=None
):
    """Return the relevant durable records indexed by Plex rating key."""
    scoped_cache = (
        cache.entries_for_scope(
            server_id,
            library_uuid,
            rating_keys=[getattr(item, "ratingKey", "") for item in items],
        ).values()
        if server_id is not None
        and library_uuid is not None
        and hasattr(cache, "entries_for_scope")
        else cache.values()
    )
    return {
        str(entry.get("rating_key")): entry
        for entry in scoped_cache
        if isinstance(entry, dict) and entry.get("rating_key") is not None
    }


def _media_type(value):
    normalized = str(value or "").lower()
    if normalized in {"show", "shows"}:
        return "tv"
    if normalized == "movies":
        return "movie"
    return normalized


def _season_destination_records(item, cached):
    """Return state records and whether Plex season inventory is unavailable."""
    cached = cached if isinstance(cached, dict) else {}
    seasons = cached.get("seasons") or {}
    if not isinstance(seasons, dict):
        seasons = {}
    inventory_counts = []
    for field_name in ("seasonCount", "childCount"):
        raw_value = getattr(item, field_name, None)
        if raw_value is None:
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            inventory_counts.append(value)
    inventory_unknown = not inventory_counts and not seasons
    destination_count = max([len(seasons), *inventory_counts], default=0)
    records = [record for record in seasons.values() if isinstance(record, dict)]
    if not records and cached.get("season_last_checked"):
        records = [cached] * destination_count
    if len(records) < destination_count:
        records.extend([None] * (destination_count - len(records)))
    return records, inventory_unknown


def artwork_schedule_summary(
    items,
    cache,
    planned_items,
    config,
    *,
    feature_flags=None,
    rating_keys=None,
    now=None,
    server_id=None,
    library_uuid=None,
):
    """Classify every enabled artwork destination into one schedule state.

    The states are mutually exclusive. ``required`` means the destination has
    never completed an artwork check, ``due`` means its cadence has expired,
    ``forced`` means another run trigger selected a current destination, and
    ``not_due`` means the destination stayed outside processing.
    """
    flags = feature_flags or {}
    target_keys = {
        str(value) for value in (rating_keys or []) if str(value).strip()
    }
    candidates = [
        item
        for item in items
        if not target_keys or str(getattr(item, "ratingKey", "")) in target_keys
    ]
    cache_by_rating_key = _scoped_cache_by_rating_key(
        cache,
        candidates,
        server_id=server_id,
        library_uuid=library_uuid,
    )
    planned_by_rating_key = {
        str(getattr(planned.item, "ratingKey", "")): planned
        for planned in planned_items
    }
    summary = {
        lane: dict.fromkeys(
            ("destinations", "due", "required", "forced", "not_due"), 0
        )
        for lane in ("poster", "background", "season_poster")
    }
    summary["season_poster"]["inventory_unknown"] = 0
    check_time = utc_now() if now is None else now

    def classify(lane, timestamp, days, selected):
        values = summary[lane]
        values["destinations"] += 1
        if not timestamp:
            values["required"] += 1
        elif timestamp_due(timestamp, days, now=check_time):
            values["due"] += 1
        elif selected:
            values["forced"] += 1
        else:
            values["not_due"] += 1

    for item in candidates:
        rating_key = str(getattr(item, "ratingKey", ""))
        cached = cache_by_rating_key.get(rating_key)
        cached_record = cached if isinstance(cached, dict) else {}
        planned = planned_by_rating_key.get(rating_key)
        reasons = planned.reasons if planned is not None else frozenset()
        media_type = _media_type(
            getattr(item, "type", None) or cached_record.get("media_type")
        )
        if media_type not in {"movie", "tv"}:
            continue
        artwork_days = get_image_upgrade_days(
            config, "movie" if media_type == "movie" else "series"
        )
        for lane in ("poster", "background"):
            if not flags.get(lane, False):
                continue
            timestamp = cached_record.get(f"{lane}_last_checked") or cached_record.get(
                f"{lane}_last_upgraded"
            )
            classify(
                lane,
                timestamp,
                adaptive_artwork_days(cached_record, lane, artwork_days),
                lane in reasons,
            )
        if media_type != "tv" or not flags.get("season", False):
            continue
        season_days = adaptive_artwork_days(
            cached_record, "season", get_image_upgrade_days(config, "season")
        )
        season_records, inventory_unknown = _season_destination_records(
            item, cached_record
        )
        if inventory_unknown:
            summary["season_poster"]["inventory_unknown"] += 1
        for season_record in season_records:
            timestamp = None
            if season_record is not None:
                timestamp = (
                    season_record.get("season_last_checked")
                    or season_record.get("season_last_upgraded")
                    or cached_record.get("season_last_checked")
                    or cached_record.get("season_last_upgraded")
                )
            classify(
                "season_poster",
                timestamp,
                season_days,
                "season" in reasons,
            )
    return summary


def metadata_schedule_summary(
    items,
    cache,
    planned_items,
    config,
    *,
    feature_flags=None,
    rating_keys=None,
    now=None,
    server_id=None,
    library_uuid=None,
):
    """Classify each enabled metadata destination into one schedule state."""
    flags = feature_flags or {}
    enabled = any(
        flags.get(name, False)
        for name in ("metadata_basic", "metadata_enhanced", "plex_metadata")
    )
    summary = dict.fromkeys(
        ("destinations", "due", "required", "forced", "not_due"), 0
    )
    if not enabled:
        return summary
    target_keys = {
        str(value) for value in (rating_keys or []) if str(value).strip()
    }
    candidates = [
        item
        for item in items
        if not target_keys or str(getattr(item, "ratingKey", "")) in target_keys
    ]
    cache_by_rating_key = _scoped_cache_by_rating_key(
        cache,
        candidates,
        server_id=server_id,
        library_uuid=library_uuid,
    )
    planned_by_rating_key = {
        str(getattr(planned.item, "ratingKey", "")): planned
        for planned in planned_items
    }
    metadata_due_causes = {
        "metadata_pending_recheck",
        "plex_metadata_recheck",
        "tmdb_change_detected",
        "deferred_retry_due",
    }
    check_time = utc_now() if now is None else now
    for item in candidates:
        rating_key = str(getattr(item, "ratingKey", ""))
        cached = cache_by_rating_key.get(rating_key)
        cached_record = cached if isinstance(cached, dict) else {}
        media_type = _media_type(
            getattr(item, "type", None) or cached_record.get("media_type")
        )
        if media_type not in {"movie", "tv"}:
            continue
        summary["destinations"] += 1
        if not cached_record:
            summary["required"] += 1
            continue
        planned = planned_by_rating_key.get(rating_key)
        reasons = planned.reasons if planned is not None else frozenset()
        causes = set(planned.selection_causes if planned is not None else ())
        causes.update(
            due_selection_causes(
                cached_record,
                media_type,
                config,
                feature_flags=flags,
                now=check_time,
            )
        )
        if "metadata" in reasons and causes & metadata_due_causes:
            summary["due"] += 1
        elif "metadata" in reasons:
            summary["forced"] += 1
        else:
            summary["not_due"] += 1
    return summary


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
    if (
        flags.get("metadata_basic", True)
        or flags.get("metadata_enhanced", False)
        or flags.get("plex_metadata", False)
    ):
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
    server_id=None,
    library_uuid=None,
    retry_rating_keys=None,
    change_rating_keys=None,
):
    """Plan selected items without losing which operations made them eligible."""
    target_keys = {str(value) for value in (rating_keys or []) if str(value).strip()}
    retry_keys = {
        str(value) for value in (retry_rating_keys or []) if str(value).strip()
    }
    changed_keys = {
        str(value) for value in (change_rating_keys or []) if str(value).strip()
    }
    candidates = [
        item
        for item in items
        if not target_keys or str(getattr(item, "ratingKey", "")) in target_keys
    ]
    if full_scan or target_keys:
        selection_cause = "targeted_rating_key" if target_keys else "full_scan"
        return [
            PlannedItem(
                item,
                frozenset(
                    enabled_work_reasons(
                        getattr(item, "type", None), feature_flags=feature_flags
                    )
                ),
                frozenset({selection_cause}),
            )
            for item in candidates
        ]

    cache_by_rating_key = _scoped_cache_by_rating_key(
        cache,
        candidates,
        server_id=server_id,
        library_uuid=library_uuid,
    )
    planned = []
    for item in candidates:
        rating_key = str(getattr(item, "ratingKey", ""))
        updated_at = item_updated_at(item)
        child_fingerprint = child_inventory_fingerprint(item)
        cached = cache_by_rating_key.get(rating_key)
        selection_causes = set()
        if rating_key in changed_keys:
            selection_causes.add("tmdb_change_detected")
        if rating_key in retry_keys:
            selection_causes.add("deferred_retry_due")
        if not cached:
            selection_causes.add("new_rating_key")
        else:
            if updated_at is None:
                selection_causes.add("missing_plex_update_marker")
            elif cached.get("plex_updated_at") != updated_at:
                selection_causes.add("plex_updated_at_changed")
            if cached.get("config_fingerprint") != fingerprint:
                selection_causes.add("configuration_changed")
            if (
                child_fingerprint is not None
                and cached.get("plex_child_fingerprint") != child_fingerprint
            ):
                selection_causes.add(
                    "tv_child_inventory_baseline"
                    if not cached.get("plex_child_fingerprint")
                    else "tv_child_inventory_changed"
                )
        if selection_causes:
            reasons = enabled_work_reasons(
                getattr(item, "type", None), feature_flags=feature_flags
            )
        else:
            selection_causes = due_selection_causes(
                cached,
                getattr(item, "type", None),
                config,
                feature_flags=feature_flags,
                now=now,
            )
            reasons = image_upgrade_reasons(
                cached,
                getattr(item, "type", None),
                config,
                feature_flags=feature_flags,
                now=now,
            )
        if reasons:
            planned.append(
                PlannedItem(
                    item,
                    frozenset(reasons),
                    frozenset(selection_causes),
                )
            )
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
    server_id=None,
    library_uuid=None,
    retry_rating_keys=None,
    change_rating_keys=None,
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
            server_id=server_id,
            library_uuid=library_uuid,
            retry_rating_keys=retry_rating_keys,
            change_rating_keys=change_rating_keys,
        )
    ]
