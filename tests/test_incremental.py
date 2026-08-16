from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from helper.incremental import (
    config_fingerprint,
    item_updated_at,
    load_state,
    mark_full_scan_complete,
    select_items,
    should_run_full_scan,
)


def incremental_config():
    return {
        "settings": {"mode": "kometa"},
        "metadata": {"run_basic": True, "run_enhanced": True},
        "assets": {"run_poster": True},
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
    state_path = tmp_path / "incremental.json"
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
    state_path = tmp_path / "incremental.json"

    assert mark_full_scan_complete(dry_run=True, path=state_path) is False
    assert not state_path.exists()
    assert item_updated_at(SimpleNamespace(updatedAt=123)) == "123"
