import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from helper.incremental import (
    artwork_schedule_summary,
    child_inventory_fingerprint,
    config_fingerprint,
    image_upgrade_due,
    image_upgrade_reasons,
    item_updated_at,
    library_full_scan_decisions,
    load_state,
    mark_full_scan_complete,
    plan_items,
    select_items,
    should_run_full_scan,
)


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


def test_tv_child_inventory_change_selects_show_without_updated_at_change():
    updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fingerprint = config_fingerprint(incremental_config())
    original = SimpleNamespace(
        ratingKey="show",
        updatedAt=updated,
        type="show",
        childCount=2,
        seasonCount=2,
        leafCount=20,
    )
    cached = {
        "show": {
            "rating_key": "show",
            "plex_updated_at": updated.isoformat(),
            "plex_child_fingerprint": child_inventory_fingerprint(original),
            "config_fingerprint": fingerprint,
        }
    }
    new_episode = SimpleNamespace(**{**vars(original), "leafCount": 21})
    new_season = SimpleNamespace(
        **{**vars(original), "childCount": 3, "seasonCount": 3}
    )

    assert select_items([original], cached, fingerprint) == []
    assert select_items([new_episode], cached, fingerprint) == [new_episode]
    assert select_items([new_season], cached, fingerprint) == [new_season]

    planned = plan_items([new_episode], cached, fingerprint)
    assert planned[0].selection_causes == frozenset(
        {"tv_child_inventory_changed"}
    )


def test_tv_child_inventory_fingerprint_is_lightweight_and_backward_safe():
    updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fingerprint = config_fingerprint(incremental_config())
    show = SimpleNamespace(
        ratingKey="show",
        updatedAt=updated,
        type="show",
        childCount="2",
        seasonCount=2,
        leafCount=20,
        viewedLeafCount=0,
    )
    watched = SimpleNamespace(**{**vars(show), "viewedLeafCount": 20})
    legacy_cache = {
        "show": {
            "rating_key": "show",
            "plex_updated_at": updated.isoformat(),
            "config_fingerprint": fingerprint,
        }
    }

    assert child_inventory_fingerprint(show) == child_inventory_fingerprint(watched)
    assert child_inventory_fingerprint(SimpleNamespace(type="movie", leafCount=20)) is None
    assert select_items([show], legacy_cache, fingerprint) == [show]
    assert plan_items([show], legacy_cache, fingerprint)[0].selection_causes == (
        frozenset({"tv_child_inventory_baseline"})
    )

    unavailable = SimpleNamespace(
        ratingKey="show", updatedAt=updated, type="show"
    )
    assert select_items([unavailable], legacy_cache, fingerprint) == []


def test_selection_causes_distinguish_triggers_from_selected_work():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    config = incremental_config()
    config["assets"].update(run_poster=True, run_background=False)
    config["image_upgrades"]["movie_days"] = 30
    fingerprint = config_fingerprint(config)
    item = SimpleNamespace(ratingKey="movie", updatedAt=updated, type="movie")
    cache = {
        "movie": {
            "rating_key": "movie",
            "plex_updated_at": updated.isoformat(),
            "config_fingerprint": fingerprint,
            "poster_last_checked": (now - timedelta(days=31)).isoformat(),
        }
    }

    due = plan_items(
        [item], cache, fingerprint, config=config, now=now,
        feature_flags={"metadata_basic": True, "poster": True},
    )[0]
    targeted = plan_items(
        [item], cache, fingerprint, rating_keys=["movie"], config=config,
        feature_flags={"metadata_basic": True, "poster": True},
    )[0]
    full = plan_items(
        [item], cache, fingerprint, full_scan=True, config=config,
        feature_flags={"metadata_basic": True, "poster": True},
    )[0]

    assert due.selection_causes == frozenset({"poster_refresh_due"})
    assert due.reasons == frozenset({"poster"})
    assert targeted.selection_causes == frozenset({"targeted_rating_key"})
    assert full.selection_causes == frozenset({"full_scan"})


def test_configuration_changes_invalidate_incremental_entries():
    first = incremental_config()
    second = incremental_config()
    second["assets"]["run_background"] = True

    assert config_fingerprint(first) != config_fingerprint(second)


def test_full_scan_schedule_and_state_are_persistent(tmp_path):
    state_path = tmp_path / "meta_db.sqlite3"
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
    state_path = tmp_path / "meta_db.sqlite3"

    assert mark_full_scan_complete(dry_run=True, path=state_path) is False
    assert not state_path.exists()
    assert item_updated_at(SimpleNamespace(updatedAt=123)) == "123"


def test_per_library_scan_state_and_fingerprint_control_full_scans(tmp_path):
    state_path = tmp_path / "meta_db.sqlite3"
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


def test_full_scan_decisions_are_independent_per_library(tmp_path):
    state_path = tmp_path / "meta_db.sqlite3"
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    scopes = [
        {
            "server_id": "server",
            "library_uuid": "movies",
            "library_name": "Movies",
            "config_fingerprint": "same",
        },
        {
            "server_id": "server",
            "library_uuid": "tv",
            "library_name": "TV",
            "config_fingerprint": "same",
        },
    ]
    mark_full_scan_complete(
        path=state_path, now=now, scopes=[scopes[0]]
    )

    decisions = library_full_scan_decisions(
        incremental_config(),
        scopes=scopes,
        path=state_path,
        now=now + timedelta(hours=1),
    )

    assert decisions == {
        ("server", "movies"): False,
        ("server", "tv"): True,
    }


def test_legacy_full_scan_json_is_ignored(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    legacy = tmp_path / "incremental_state.json"
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    legacy.write_text(
        json.dumps({"last_full_scan": now.isoformat()}), encoding="utf-8"
    )
    scopes = [
        {
            "server_id": "server",
            "library_uuid": "movies",
            "library_name": "Movies",
            "config_fingerprint": "fingerprint",
        }
    ]

    assert should_run_full_scan(
        incremental_config(),
        scopes=scopes,
        path=database,
        now=now + timedelta(hours=23),
    ) is True


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
        feature_flags={
            "metadata_basic": True,
            "poster": False,
            "background": False,
            "season": False,
        },
        now=now,
    ) is False


def test_pending_episode_metadata_uses_short_recheck_queue():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    config = incremental_config()
    config["incremental"]["metadata_pending_recheck_hours"] = 6
    cached = {
        "media_type": "tv",
        "metadata_pending_count": 2,
        "metadata_pending_at": (now - timedelta(hours=7)).isoformat(),
    }

    assert image_upgrade_reasons(
        cached,
        "show",
        config,
        feature_flags={
            "metadata_basic": True,
            "poster": False,
            "background": False,
            "season": False,
        },
        now=now,
    ) == {"metadata"}

    cached["metadata_pending_at"] = (now - timedelta(hours=5)).isoformat()
    assert image_upgrade_reasons(
        cached,
        "show",
        config,
        feature_flags={
            "metadata_basic": True,
            "poster": False,
            "background": False,
            "season": False,
        },
        now=now,
    ) == set()


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


def test_artwork_schedule_summary_counts_due_required_forced_and_not_due():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    config = incremental_config()
    config["assets"] = {
        "run_poster": True,
        "run_background": False,
        "run_season": True,
    }
    config["image_upgrades"].update(
        {"movie_days": 30, "series_days": 30, "season_days": 15}
    )
    fingerprint = config_fingerprint(config)
    current = SimpleNamespace(
        ratingKey="current", updatedAt=updated, type="movie"
    )
    due = SimpleNamespace(ratingKey="due", updatedAt=updated, type="movie")
    new = SimpleNamespace(ratingKey="new", updatedAt=updated, type="movie")
    show = SimpleNamespace(
        ratingKey="show",
        updatedAt=updated,
        type="show",
        childCount=3,
        seasonCount=3,
        leafCount=30,
    )
    items = [current, due, new, show]
    cache = {
        "current": {
            "rating_key": "current",
            "media_type": "movie",
            "plex_updated_at": updated.isoformat(),
            "config_fingerprint": fingerprint,
            "poster_last_checked": (now - timedelta(days=10)).isoformat(),
        },
        "due": {
            "rating_key": "due",
            "media_type": "movie",
            "plex_updated_at": updated.isoformat(),
            "config_fingerprint": fingerprint,
            "poster_last_checked": (now - timedelta(days=31)).isoformat(),
        },
        "show": {
            "rating_key": "show",
            "media_type": "tv",
            "plex_updated_at": updated.isoformat(),
            "plex_child_fingerprint": child_inventory_fingerprint(show),
            "config_fingerprint": fingerprint,
            "poster_last_checked": (now - timedelta(days=10)).isoformat(),
            "season_last_checked": (now - timedelta(days=10)).isoformat(),
            "seasons": {"1": {}, "2": {}},
        },
    }
    flags = {
        "metadata_basic": True,
        "poster": True,
        "background": False,
        "season": True,
    }
    planned = plan_items(
        items,
        cache,
        fingerprint,
        config=config,
        feature_flags=flags,
        now=now,
    )

    schedule = artwork_schedule_summary(
        items,
        cache,
        planned,
        config,
        feature_flags=flags,
        now=now,
    )

    assert schedule["poster"] == {
        "destinations": 4,
        "due": 1,
        "required": 1,
        "forced": 0,
        "not_due": 2,
    }
    assert schedule["background"] == {
        "destinations": 0,
        "due": 0,
        "required": 0,
        "forced": 0,
        "not_due": 0,
    }
    assert schedule["season_poster"] == {
        "destinations": 3,
        "due": 0,
        "required": 1,
        "forced": 0,
        "not_due": 2,
    }

    full_plan = plan_items(
        items,
        cache,
        fingerprint,
        full_scan=True,
        config=config,
        feature_flags=flags,
        now=now,
    )
    forced_schedule = artwork_schedule_summary(
        items,
        cache,
        full_plan,
        config,
        feature_flags=flags,
        now=now,
    )
    assert forced_schedule["poster"] == {
        "destinations": 4,
        "due": 1,
        "required": 1,
        "forced": 2,
        "not_due": 0,
    }
    assert forced_schedule["season_poster"] == {
        "destinations": 3,
        "due": 0,
        "required": 1,
        "forced": 2,
        "not_due": 0,
    }


def test_artwork_schedule_summary_handles_legacy_seasons_and_plural_movies():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    checked = (now - timedelta(days=1)).isoformat()
    config = incremental_config()
    config["assets"] = {"run_poster": True, "run_season": True}
    movie = SimpleNamespace(ratingKey="movie", type="movies")
    show = SimpleNamespace(
        ratingKey="show",
        type="show",
        seasonCount="invalid",
        childCount=2,
    )
    cache = {
        "movie": {
            "rating_key": "movie",
            "media_type": "movie",
            "poster_last_checked": checked,
        },
        "show": {
            "rating_key": "show",
            "media_type": "tv",
            "poster_last_checked": checked,
            "season_last_checked": checked,
            "seasons": ["legacy-invalid-shape"],
        },
    }

    schedule = artwork_schedule_summary(
        [movie, show],
        cache,
        [],
        config,
        feature_flags={"poster": True, "season": True},
        now=now,
    )

    assert schedule["poster"]["destinations"] == 2
    assert schedule["poster"]["not_due"] == 2
    assert schedule["season_poster"] == {
        "destinations": 2,
        "due": 0,
        "required": 0,
        "forced": 0,
        "not_due": 2,
    }
