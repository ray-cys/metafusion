import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from helper.incremental import (
    config_fingerprint,
    image_upgrade_reasons,
    image_upgrade_due,
    item_updated_at,
    load_state,
    mark_full_scan_complete,
    mark_library_scan_complete,
    plan_items,
    select_items,
    should_run_full_scan,
)
from helper.state_db import MediaStateStore


def incremental_config():
    return {
        "settings": {"mode": "kometa"},
        "metadata": {"run_basic": True, "run_enhanced": True},
        "assets": {"run_poster": True},
        "image_upgrades": {
            "default_days": 30,
            "movie_days": None,
            "series_days": None,
            "season_days": None,
        },
        "tmdb": {"language": "en", "fallback": ["fr"], "region": "US"},
        "poster_set": {},
        "season_set": {},
        "background_set": {},
        "incremental": {"enabled": True, "full_scan_interval_hours": 24},
    }


def test_incremental_selection_skips_only_matching_successful_fingerprints():
    updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = [
        SimpleNamespace(ratingKey="1", updatedAt=updated),
        SimpleNamespace(ratingKey="2", updatedAt=updated + timedelta(hours=1)),
        SimpleNamespace(ratingKey="3", updatedAt=None),
    ]
    fingerprint = config_fingerprint(incremental_config())
    cache = {
        "one": {
            "rating_key": "1",
            "plex_updated_at": updated.isoformat(),
            "config_fingerprint": fingerprint,
        },
        "two": {
            "rating_key": "2",
            "plex_updated_at": "old",
            "config_fingerprint": fingerprint,
        },
    }

    selected = select_items(items, cache, fingerprint)

    assert [item.ratingKey for item in selected] == ["2", "3"]
    assert select_items(items, cache, fingerprint, rating_keys=["1"])[0].ratingKey == "1"
    assert len(select_items(items, cache, fingerprint, full_scan=True)) == 3


def test_configuration_changes_invalidate_incremental_entries():
    first = incremental_config()
    second = incremental_config()
    second["assets"]["run_background"] = True

    assert config_fingerprint(first) != config_fingerprint(second)


def test_full_scan_schedule_and_state_are_persistent(tmp_path):
    state_path = tmp_path / "metafusion.sqlite3"
    config = incremental_config()
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)

    assert should_run_full_scan(config, state={}, now=now) is True
    assert mark_full_scan_complete(path=state_path, now=now) is True
    state = load_state(state_path)
    assert should_run_full_scan(config, state=state, now=now + timedelta(hours=23)) is False
    assert should_run_full_scan(config, state=state, now=now + timedelta(hours=25)) is True
    assert should_run_full_scan(config, targeted=True, state={}, now=now) is False

    config["incremental"]["enabled"] = False
    assert should_run_full_scan(config, targeted=True, state=state, now=now) is True


def test_dry_run_does_not_persist_incremental_state(tmp_path):
    state_path = tmp_path / "metafusion.sqlite3"

    assert mark_full_scan_complete(dry_run=True, path=state_path) is False
    assert not state_path.exists()
    assert item_updated_at(SimpleNamespace(updatedAt=123)) == "123"


def test_per_library_scan_state_and_fingerprint_control_full_scans(tmp_path):
    state_path = tmp_path / "metafusion.sqlite3"
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    scopes = [
        {
            "server_id": "server",
            "library_uuid": "movies",
            "library_name": "Movies",
            "config_fingerprint": "first",
            "item_count": 10,
        }
    ]
    mark_full_scan_complete(path=state_path, now=now, scopes=scopes)
    state = load_state(path=state_path, scopes=scopes)

    assert state["libraries"][("server", "movies")][
        "last_full_scan_completed"
    ] == now.isoformat()
    assert should_run_full_scan(
        incremental_config(),
        scopes=scopes,
        now=now + timedelta(hours=23),
        path=state_path,
    ) is False

    changed = [{**scopes[0], "config_fingerprint": "second"}]
    assert should_run_full_scan(
        incremental_config(),
        scopes=changed,
        now=now + timedelta(hours=23),
        path=state_path,
    ) is True


def test_legacy_full_scan_timestamp_remains_valid_until_due(tmp_path):
    database = tmp_path / "metafusion.sqlite3"
    legacy = tmp_path / "incremental_state.json"
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    legacy.write_text(
        json.dumps({"last_full_scan": now.isoformat()}), encoding="utf-8"
    )
    store = MediaStateStore(
        path=database,
        legacy_meta_cache=tmp_path / "meta_cache.json",
        legacy_incremental_state=legacy,
    )
    store.close()
    scopes = [
        {
            "server_id": "server",
            "library_uuid": "movies",
            "library_name": "Movies",
            "config_fingerprint": "fingerprint",
        }
    ]

    mark_library_scan_complete(
        scopes,
        full_scan=False,
        path=database,
        now=now + timedelta(hours=1),
    )

    assert should_run_full_scan(
        incremental_config(),
        scopes=scopes,
        path=database,
        now=now + timedelta(hours=23),
    ) is False


def test_per_type_artwork_intervals_select_only_due_unchanged_items():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    config = incremental_config()
    config["assets"] = {
        "run_poster": True,
        "run_background": True,
        "run_season": True,
    }
    config["image_upgrades"].update(
        {"movie_days": 30, "series_days": 15, "season_days": 7}
    )
    fingerprint = config_fingerprint(config)
    items = [
        SimpleNamespace(ratingKey="movie", updatedAt=updated, type="movie"),
        SimpleNamespace(ratingKey="show", updatedAt=updated, type="show"),
    ]
    cache = {
        "movie": {
            "rating_key": "movie",
            "plex_updated_at": updated.isoformat(),
            "config_fingerprint": fingerprint,
            "poster_last_checked": (now - timedelta(days=29)).isoformat(),
            "background_last_checked": (now - timedelta(days=29)).isoformat(),
        },
        "show": {
            "rating_key": "show",
            "plex_updated_at": updated.isoformat(),
            "config_fingerprint": fingerprint,
            "poster_last_checked": (now - timedelta(days=14)).isoformat(),
            "background_last_checked": (now - timedelta(days=14)).isoformat(),
            "season_last_checked": (now - timedelta(days=8)).isoformat(),
        },
    }

    selected = select_items(
        items,
        cache,
        fingerprint,
        config=config,
        feature_flags={"poster": True, "background": True, "season": True},
        now=now,
    )

    assert [item.ratingKey for item in selected] == ["show"]


def test_artwork_schedule_respects_disabled_features_and_zero_intervals():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    config = incremental_config()
    config["image_upgrades"].update(
        {"movie_days": 0, "series_days": 0, "season_days": 0}
    )
    missing_timestamps = {"media_type": "tv"}

    assert image_upgrade_due(
        missing_timestamps,
        "show",
        config,
        feature_flags={"poster": True, "background": True, "season": True},
        now=now,
    ) is False
    assert image_upgrade_due(
        missing_timestamps,
        "show",
        incremental_config(),
        feature_flags={"poster": False, "background": False, "season": False},
        now=now,
    ) is False


def test_artwork_schedule_uses_legacy_upgrade_timestamp_during_migration():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    config = incremental_config()
    cached = {
        "media_type": "movie",
        "poster_last_upgraded": (now - timedelta(days=29)).isoformat(),
    }

    assert image_upgrade_due(
        cached,
        "movie",
        config,
        feature_flags={"poster": True, "background": False, "season": False},
        now=now,
    ) is False
    cached["poster_last_upgraded"] = (now - timedelta(days=31)).isoformat()
    assert image_upgrade_due(
        cached,
        "movie",
        config,
        feature_flags={"poster": True, "background": False, "season": False},
        now=now,
    ) is True


def test_planner_preserves_independent_artwork_reasons():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    config = incremental_config()
    config["assets"] = {
        "run_poster": True,
        "run_background": True,
        "run_season": True,
    }
    config["image_upgrades"].update({"series_days": 30, "season_days": 15})
    fingerprint = config_fingerprint(config)
    item = SimpleNamespace(
        ratingKey="show", updatedAt=updated, type="show", title="Show"
    )
    cached = {
        "rating_key": "show",
        "plex_updated_at": updated.isoformat(),
        "config_fingerprint": fingerprint,
        "poster_last_checked": (now - timedelta(days=10)).isoformat(),
        "background_last_checked": (now - timedelta(days=10)).isoformat(),
        "season_last_checked": (now - timedelta(days=16)).isoformat(),
    }

    assert image_upgrade_reasons(
        cached,
        "show",
        config,
        feature_flags={"poster": True, "background": True, "season": True},
        now=now,
    ) == {"season"}
    planned = plan_items(
        [item],
        {"show": cached},
        fingerprint,
        config=config,
        feature_flags={
            "metadata_basic": True,
            "poster": True,
            "background": True,
            "season": True,
        },
        now=now,
    )
    assert planned[0].reasons == frozenset({"season"})
