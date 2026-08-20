import asyncio
import copy
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from helper.concurrency import AdaptiveLane
from helper.config import DEFAULT_CONFIG, validate_config
from helper.identity import plex_identity_fingerprint
from helper.incremental import adaptive_artwork_days, plan_items
from helper.plex import connect_plex_library
from helper.plex_metadata import apply_plex_metadata, begin_plex_metadata_run
from helper.plex_paths import advise_path_mappings
from helper.runtime import DiskPressureError
from helper.state_db import (
    SCHEMA_VERSION,
    _connect,
    classify_item_failure,
    clear_item_retries,
    clear_item_retry,
    load_due_item_retries,
    load_identity_binding,
    mark_item_started,
    mark_items_started,
    missing_library_inventory,
    reconcile_library_inventory,
    record_item_failure,
    retry_queue_summary,
    save_identity_binding,
)
from helper.tmdb_cache import PersistentTTLCache
from modules import builder

UTC = timezone.utc


def test_state_schema_upgrade_creates_bounded_sqlite_backup(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA user_version = 3")

    connection = _connect(database, writable=True)
    connection.close()

    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "item_retry_queue",
        "plex_library_inventory",
        "identity_bindings",
        "identity_binding_history",
    } <= tables
    assert len(list(tmp_path.glob("meta_db.sqlite3.pre-v3-*.bak"))) == 1


def test_retry_queue_recovers_interrupted_items_and_bounds_failures(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    now = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)
    result = record_item_failure(
        "server",
        "library",
        "10",
        TimeoutError("provider timeout"),
        library_name="Movies",
        plex_updated_at="one",
        path=database,
        now=now,
    )
    assert result["status"] == "pending"
    assert load_due_item_retries(
        "server", "library", path=database, now=now + timedelta(minutes=14)
    ) == {}
    assert "10" in load_due_item_retries(
        "server", "library", path=database, now=now + timedelta(minutes=16)
    )

    mark_item_started(
        "server",
        "library",
        "10",
        plex_updated_at="one",
        path=database,
        now=now + timedelta(minutes=16),
    )
    assert "10" in load_due_item_retries(
        "server", "library", path=database, now=now
    )
    assert clear_item_retry("server", "library", "10", path=database) == 1
    assert retry_queue_summary(path=database) == {}

    parked = record_item_failure(
        "server",
        "library",
        "11",
        "identity mismatch requires review",
        failure_class="permanent",
        path=database,
        now=now,
    )
    assert parked["status"] == "parked"
    assert load_due_item_retries(
        "server", "library", path=database, now=now + timedelta(days=30)
    ) == {}


def test_failure_classification_parks_deterministic_errors_only():
    assert classify_item_failure("invalid path mapping") == "permanent"
    assert classify_item_failure(TimeoutError("provider timed out")) == "transient"
    assert classify_item_failure(RuntimeError("unexpected response")) == "transient"


def test_changed_plex_marker_resets_a_parked_retry(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    now = datetime(2026, 8, 18, tzinfo=UTC)
    for attempt in range(5):
        result = record_item_failure(
            "server",
            "library",
            "12",
            "temporary provider failure",
            plex_updated_at="old",
            path=database,
            now=now + timedelta(hours=attempt),
        )
    assert result["status"] == "parked"

    mark_item_started(
        "server",
        "library",
        "12",
        plex_updated_at="new",
        path=database,
        now=now + timedelta(days=1),
    )
    reset = record_item_failure(
        "server",
        "library",
        "12",
        "temporary provider failure",
        plex_updated_at="new",
        path=database,
        now=now + timedelta(days=1),
    )
    assert reset["attempts"] == 1
    assert reset["status"] == "pending"


def test_retry_markers_and_successes_are_batched_per_library(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    assert mark_items_started(
        "server",
        "library",
        [
            {"rating_key": "one", "library_name": "Movies"},
            {"rating_key": "two", "library_name": "Movies"},
        ],
        path=database,
    )
    assert set(
        load_due_item_retries("server", "library", path=database)
    ) == {"one", "two"}
    assert clear_item_retries(
        "server", "library", ["one", "two"], path=database
    ) == 2
    assert retry_queue_summary(path=database) == {}


def test_incremental_selection_prioritizes_due_retry():
    item = SimpleNamespace(type="movie", ratingKey="1", updatedAt="same")
    cache = {
        "movie:plex:1": {
            "rating_key": "1",
            "plex_updated_at": "same",
            "config_fingerprint": "same",
        }
    }
    planned = plan_items(
        [item],
        cache,
        "same",
        config={"assets": {}},
        feature_flags={"metadata_basic": True},
        retry_rating_keys={"1"},
    )
    assert len(planned) == 1
    assert planned[0].selection_causes == frozenset({"deferred_retry_due"})


def test_adaptive_artwork_intervals_back_off_and_retry_missing_soon():
    assert adaptive_artwork_days({"poster_missing_checks": 1}, "poster", 30) == 1
    assert adaptive_artwork_days({"poster_missing_checks": 3}, "poster", 30) == 7
    assert adaptive_artwork_days({"poster_unchanged_checks": 2}, "poster", 30) == 120
    assert adaptive_artwork_days({"poster_unchanged_checks": 9}, "poster", 30) == 180
    assert adaptive_artwork_days({"poster_unchanged_checks": 9}, "poster", 0) == 0


def test_auto_library_discovery_uses_supported_sections_only():
    sections = [
        SimpleNamespace(title="Films", TYPE="movie", uuid="one"),
        SimpleNamespace(title="Series", TYPE="show", uuid="two"),
        SimpleNamespace(title="Music", TYPE="artist", uuid="three"),
    ]
    plex = SimpleNamespace(
        library=SimpleNamespace(sections=lambda: sections),
    )
    config = {"plex_libraries": ["auto"], "runtime": {}}

    selected, names, available = connect_plex_library(config, plex=plex)

    assert [section.title for section in selected] == ["Films", "Series"]
    assert names == ["Films", "Series"]
    assert len(available) == 3
    assert config["_library_discovery_auto"] is True


def test_auto_library_discovery_fails_when_no_supported_section_exists():
    sections = [SimpleNamespace(title="Music", TYPE="artist", uuid="music")]
    plex = SimpleNamespace(library=SimpleNamespace(sections=lambda: sections))

    try:
        connect_plex_library(
            {"plex_libraries": ["auto"], "runtime": {}},
            plex=plex,
        )
    except RuntimeError as error:
        assert "No supported Plex movie or show libraries" in str(error)
    else:
        raise AssertionError("auto discovery silently accepted no supported libraries")


def test_explicit_unsupported_library_fails_instead_of_silently_skipping():
    sections = [SimpleNamespace(title="Music", TYPE="artist", uuid="music")]
    plex = SimpleNamespace(library=SimpleNamespace(sections=lambda: sections))

    try:
        connect_plex_library(
            {"plex_libraries": ["Music"], "runtime": {}},
            plex=plex,
        )
    except RuntimeError as error:
        assert "unsupported types" in str(error)
    else:
        raise AssertionError("unsupported explicit library was silently accepted")


def test_library_inventory_preserves_temporarily_missing_library(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    libraries = [
        {"title": "Films", "type": "movie", "uuid": "one"},
        {"title": "Series", "type": "show", "uuid": "two"},
    ]
    assert reconcile_library_inventory(
        "server", libraries, path=database, now="2026-08-18T00:00:00+00:00"
    ) == []
    missing = reconcile_library_inventory(
        "server", libraries[:1], path=database, now="2026-08-19T00:00:00+00:00"
    )
    assert [entry["library_name"] for entry in missing] == ["Series"]
    with closing(sqlite3.connect(database)) as connection, connection:
        row = connection.execute(
            "SELECT active, missing_since FROM plex_library_inventory "
            "WHERE library_uuid = 'two'"
        ).fetchone()
    assert row[0] == 0
    assert row[1]


def test_library_inventory_dry_run_comparison_does_not_write(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    libraries = [
        {"title": "Films", "type": "movie", "uuid": "one"},
        {"title": "Series", "type": "show", "uuid": "two"},
    ]
    reconcile_library_inventory("server", libraries, path=database)

    missing = missing_library_inventory(
        "server", libraries[:1], path=database
    )

    assert [entry["library_name"] for entry in missing] == ["Series"]
    with closing(sqlite3.connect(database)) as connection, connection:
        row = connection.execute(
            "SELECT active, missing_since FROM plex_library_inventory "
            "WHERE library_uuid = 'two'"
        ).fetchone()
    assert row == (1, None)


def test_high_confidence_identity_binding_survives_title_but_not_guid_change(
    tmp_path,
):
    database = tmp_path / "meta_db.sqlite3"
    meta = {
        "library_type": "movie",
        "tmdb_id": "100",
        "imdb_id": "tt100",
        "title": "Localized title",
    }
    fingerprint = plex_identity_fingerprint(meta)
    assert save_identity_binding(
        "server",
        "library",
        "1",
        "movie",
        "100",
        fingerprint,
        path=database,
    )
    renamed = {**meta, "title": "Renamed title"}
    assert plex_identity_fingerprint(renamed) == fingerprint
    assert load_identity_binding(
        "server", "library", "1", fingerprint, path=database
    )["tmdb_id"] == "100"
    changed = plex_identity_fingerprint({**renamed, "imdb_id": "tt200"})
    assert load_identity_binding(
        "server", "library", "1", changed, path=database
    ) is None


def test_path_advisor_infers_one_visible_suffix_mapping(tmp_path):
    media_root = tmp_path / "media"
    target = media_root / "Movies" / "Example" / "movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")

    advice = advise_path_mappings(
        ["/plex/Movies/Example/movie.mkv"],
        mount_roots=[media_root],
    )

    assert advice["suggestions"] == [f"/plex=>{media_root}"]
    assert advice["records"][0]["status"] == "suggested"


def test_path_advisor_distinguishes_visible_and_unresolved_paths(tmp_path):
    visible = tmp_path / "visible.mkv"
    visible.write_bytes(b"media")

    advice = advise_path_mappings(
        [str(visible), "/plex/not-mounted/movie.mkv"],
        mount_roots=[],
    )

    statuses = {
        record["reported"]: record["status"] for record in advice["records"]
    }
    assert statuses[str(visible)] == "resolved"
    assert statuses["/plex/not-mounted/movie.mkv"] == "unresolved"
    assert advice["suggestions"] == []


def test_plex_lane_reduces_after_a_slow_success():
    async def exercise():
        lane = AdaptiveLane("plex", initial=3, ceiling=4)
        lease = await lane.acquire()
        await lane.release(lease, 6.0)
        return lane.snapshot()

    snapshot = asyncio.run(exercise())
    assert snapshot["final_limit"] == 2
    assert snapshot["slow_responses"] == 1


def test_plex_write_limit_deferral_is_returned_to_retry_planner(monkeypatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="plex", dry_run=False)
    config["plex_metadata"].update(enabled=True, max_writes_per_run=1)
    begin_plex_metadata_run(config)
    monkeypatch.setattr(
        "helper.plex_metadata._apply_candidate",
        lambda *_args: {"writes": 0, "failures": 0, "deferred": 2},
    )

    result = asyncio.run(
        apply_plex_metadata(
            SimpleNamespace(title="Example"),
            {"root": {"fields": {"summary": "value"}}},
            config,
            {"title": "Example"},
        )
    )
    assert result == {"writes": 0, "failures": 0, "deferred": 2}


def test_tmdb_cache_automatic_limits_and_maintenance(tmp_path):
    path = tmp_path / "tmdb_cache.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path, max_entries=0, max_mb=0)
    cache["movie/1"] = {"id": 1}
    assert cache.flush() is True

    stats = cache.stats()
    maintenance = cache.maintain(wal_threshold_mb=1)
    assert stats["automatic_limits"] is True
    assert stats["max_entries"] >= 5000
    assert stats["max_mib"] >= 64
    assert maintenance["optimized"] is True
    assert cache._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_disk_pressure_defers_artwork_without_changing_policy(monkeypatch, tmp_path):
    error = DiskPressureError(tmp_path, 1, 2, "disk pressure")
    monkeypatch.setattr(builder, "asset_temp_path", lambda *_args: (_ for _ in ()).throw(error))
    config = {"assets": {"update_policy": "managed"}}

    assert builder._asset_temp_path_or_defer(config, {}) is None
    assert config["assets"]["update_policy"] == "managed"
    assert config["_deferred_artwork"] == 1


def test_automatic_defaults_keep_destructive_choices_disabled():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["plex"].update(url="http://plex:32400", token="token")
    config["tmdb"]["api_key"] = "key"

    assert validate_config(config) == []
    assert config["plex_libraries"] == ["auto"]
    assert config["cleanup"]["run_cleanup"] is False
    assert config["plex_metadata"]["enabled"] is False
    assert config["assets"]["update_policy"] == "managed"
